"""Server identity, owner authority and reviewable machine decisions."""

import ipaddress
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, InfraPlan


class Endpoint(Contract):
    host: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,253}$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    auth: Literal["password", "private_key", "agent"] = "password"
    credential_ref: str | None = Field(default=None, pattern=r"^secret-[a-f0-9]{32}$")
    fingerprint: str | None = Field(default=None, pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")


class Connection(Contract):
    transport: Literal["local", "ssh", "ssh_chain", "existing_tunnel", "manual"] = "ssh"
    target: Endpoint | None = None
    jumps: list[Endpoint] = Field(default_factory=list, max_length=4)
    origin: Literal["pilot_host"] = "pilot_host"
    prerequisite: str = Field(default="", max_length=1600)

    @model_validator(mode="after")
    def complete(self):
        if self.transport in {"ssh", "ssh_chain", "existing_tunnel"} and not self.target:
            raise ValueError("SSH needs a structured target endpoint")
        if self.jumps and self.transport != "ssh_chain":
            raise ValueError("Jump endpoints require ssh_chain")
        if self.transport in {"local", "manual"} and (self.target or self.jumps):
            raise ValueError("Local/manual access does not execute an SSH endpoint")
        return self


class DockerAccess(Contract):
    mode: Literal["direct", "sudo_nopasswd", "sudo_password"] = "direct"
    credential_ref: str | None = Field(default=None, pattern=r"^secret-[a-f0-9]{32}$")

    @model_validator(mode="after")
    def credential(self):
        if (self.mode == "sudo_password") != bool(self.credential_ref):
            raise ValueError("sudo密码模式需选择专用或已保存的密码凭据；其他模式不接收密码")
        return self


class ServerSave(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_revision: int = Field(default=0, ge=0)
    label: str = Field(min_length=2, max_length=100)
    connection: Connection
    enabled: bool = False
    docker: DockerAccess | None = None


class Authority(Contract):
    mode: Literal["observe", "review"] = "observe"
    approvable_gpu_ids: list[Annotated[int, Field(strict=True, ge=0, le=127)]] = Field(
        default_factory=list, max_length=32
    )
    max_cpu_cores: int = Field(default=8, ge=1, le=256)
    max_memory_gib: float = Field(default=20, ge=0.5, le=4096)
    max_disk_gib: float = Field(default=10, ge=0.1, le=100000)
    max_duration_seconds: int = Field(default=3600, ge=60, le=86400)
    allow_source_edit: bool = True
    operations: list[Literal["assets", "downloads", "containers", "software", "network", "inference", "profiling"]] | None = Field(default=None, max_length=7)
    network_ports: list[Annotated[int, Field(strict=True, ge=1024, le=65535)]] = Field(default_factory=list, max_length=16)
    distributed_networks: list[Annotated[str, Field(max_length=18)]] = Field(default_factory=list, max_length=8)
    gpu_sharing: Literal["exclusive", "shared"] = "exclusive"

    @model_validator(mode="after")
    def devices(self):
        if self.operations is not None and len(set(self.operations)) != len(self.operations):
            raise ValueError("业务操作权限不能重复")
        if len(set(self.network_ports)) != len(self.network_ports):
            raise ValueError("网络测试端口不能重复")
        networks = []
        for cidr in self.distributed_networks:
            try:
                network = ipaddress.ip_network(cidr, strict=True)
            except ValueError as exc:
                raise ValueError("跨机网络须为规范的IPv4 CIDR") from exc
            if network.version != 4 or network.prefixlen < 8:
                raise ValueError("跨机网络须为前缀长度至少为8的IPv4 CIDR")
            networks.append(str(network))
        if len(set(networks)) != len(networks):
            raise ValueError("跨机网络不能重复")
        self.distributed_networks = networks
        if len(set(self.approvable_gpu_ids)) != len(self.approvable_gpu_ids) or any(
            type(n) is not int or not 0 <= n <= 127 for n in self.approvable_gpu_ids
        ):
            raise ValueError("Device IDs must be unique non-negative integers")
        return self


class AuthorityUpdate(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_revision: int = Field(ge=1)
    policy: Authority


class ObserveRequest(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AssessRequest(ObserveRequest):
    model_asset: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    context_length: int = Field(default=4096, ge=512, le=32768)
    concurrency: int = Field(default=2, ge=1, le=32)


class ResourceLimits(Contract):
    cpu_cores: int = Field(default=4, ge=1, le=256)
    memory_gib: float = Field(default=16, ge=0.5, le=4096)
    disk_gib: float = Field(default=5, ge=0.1, le=100000)
    duration_seconds: int = Field(default=2400, ge=120, le=14400)


class ResourceProposal(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    plan: InfraPlan
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    reason: str = Field(min_length=5, max_length=1600)

    @model_validator(mode="after")
    def reviewed_scope(self):
        if self.plan.kind != "deploy_verify" or self.plan.authorization_id:
            raise ValueError("Resource review creates a new deployment, never imports a grant")
        if self.plan.deadline_seconds > self.limits.duration_seconds:
            raise ValueError("Task duration exceeds requested resource lease")
        return self


class ProposalTarget(Contract):
    proposal_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResourceDecision(ProposalTarget):
    decision: Literal["allowed-once", "rejected", "cancelled", "unavailable"]


class RevokeGrant(Contract):
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SecretCreate(Contract):
    label: str = Field(min_length=1, max_length=100)
    kind: Literal["password", "private_key"]
    value: str = Field(min_length=1, max_length=32000)


class TrustHost(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_revision: int = Field(ge=1)
    hop: int = Field(ge=0, le=4)
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
