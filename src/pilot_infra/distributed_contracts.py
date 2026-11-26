"""Explicit, bounded two-node inference topology; saving it does not run it."""

import ipaddress
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .resource_contracts import ResourceLimits

Port = Annotated[int, Field(strict=True, ge=1024, le=65535)]
Device = Annotated[int, Field(strict=True, ge=0, le=127)]


class DistributedNode(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    role: Literal["prefill", "decode", "head", "worker"]
    address: str
    gpu_ids: list[Device] = Field(min_length=1, max_length=8)
    model_asset: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    image_asset: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    rdma_device: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    ports: dict[str, Port] = Field(min_length=1, max_length=16)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @model_validator(mode="after")
    def bounded_scope(self):
        ip = ipaddress.ip_address(self.address)
        if (ip.version != 4 or ip.is_loopback or ip.is_multicast or ip.is_unspecified
                or ip.is_link_local or ip.is_reserved):
            raise ValueError("跨机节点须使用可核验的IPv4单播接口")
        if len(set(self.gpu_ids)) != len(self.gpu_ids):
            raise ValueError("节点GPU不可重复")
        if len(set(self.ports.values())) != len(self.ports):
            raise ValueError("同一节点的端口不可重复")
        return self


class DistributedPlan(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    title: str = Field(min_length=2, max_length=100)
    mode: Literal["sglang_pd", "sglang_ray", "vllm_ray"]
    nodes: list[DistributedNode] = Field(min_length=2, max_length=2)
    served_model_name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    context_length: int = Field(default=4096, ge=512, le=32768)
    max_new_tokens: int = Field(default=32, ge=8, le=256)
    duration_seconds: int = Field(default=1800, ge=120, le=14400)
    max_concurrency: int = Field(default=2, ge=1, le=8)
    # These adapters intentionally support one worker per role and rank.
    # Multi-rank routing needs its own explicit protocol and validation.

    @model_validator(mode="after")
    def topology(self):
        roles = {n.role for n in self.nodes}
        required_roles = {"prefill", "decode"} if self.mode == "sglang_pd" else {"head", "worker"}
        if roles != required_roles:
            raise ValueError("必须为两个不同角色各选择一个节点")
        if len({n.server_id for n in self.nodes}) != 2 or len({n.address for n in self.nodes}) != 2:
            raise ValueError("两个角色须使用不同服务器和节点地址")
        if self.context_length < self.max_new_tokens + 16:
            raise ValueError("输出长度超过上下文")
        ray_ports = {"node_manager", "object_manager", "agent_http", "agent_grpc", "runtime_env",
                     "metrics", "worker_1", "worker_2", "worker_3", "worker_4"}
        ports = {
            "prefill": {"api", "bootstrap", "router"},
            "decode": {"api"},
            "head": ray_ports | {"gcs", "api", "nccl", "dashboard", "ray_client"},
            "worker": ray_ports,
        }
        for node in self.nodes:
            if set(node.ports) != ports[node.role]:
                raise ValueError("节点端口不符合对应配方")
            if node.limits.duration_seconds < self.duration_seconds:
                raise ValueError("节点租约不能短于任务期限")
            if self.mode == "sglang_pd" and not node.rdma_device:
                raise ValueError("PD Mooncake方案需要明确每节点的RDMA设备")
        if len({len(n.gpu_ids) for n in self.nodes}) != 1:
            raise ValueError("初版跨机配方要求两节点使用相同数量的GPU")
        return self


class DistributedTarget(Contract):
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
