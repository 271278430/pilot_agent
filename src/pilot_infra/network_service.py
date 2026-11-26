"""Reviewable multi-node TCP tasks with bounded effects and explicit restoration."""

import hashlib
import ipaddress
import json
import time
import uuid
from pathlib import Path

from pydantic import Field, model_validator

from . import network_worker
from .asset_service import AssetService
from .contracts import Contract
from .node_authority import require_operation
from .registered_workbench import RegisteredWorkbench
from .resource_registry import ResourceRegistry
from .store import InfraStore


class NetworkPeer(Contract):
    server_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    address: str
    port: int = Field(default=23181, ge=1024, le=65535)

    @model_validator(mode="after")
    def local_address(self):
        ip = ipaddress.ip_address(self.address)
        if ip.version != 4 or ip.is_unspecified or ip.is_multicast or ip.is_loopback:
            raise ValueError("跨节点检查需使用节点已有IPv4接口地址")
        return self


class NetworkPlan(Contract):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    title: str = Field(min_length=2, max_length=100)
    peers: list[NetworkPeer] = Field(min_length=2, max_length=4)
    payload_bytes: int = Field(default=1024**2, ge=1024, le=8 * 1024**2)
    rounds: int = Field(default=2, ge=1, le=8)
    duration_seconds: int = Field(default=30, ge=10, le=120)

    @model_validator(mode="after")
    def distinct_nodes(self):
        if len({p.server_id for p in self.peers}) != len(self.peers):
            raise ValueError("跨节点任务不能重复同一服务器")
        return self


class NetworkRecommend(Contract):
    server_ids: list[str] = Field(min_length=2, max_length=4)


class NetworkTarget(Contract):
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class RegisteredNetwork(RegisteredWorkbench):
    worker_file = network_worker.__file__
    subdir, prefix = "network-tasks", "pilot-network-"

    def admit(self, edit=False):
        server = ResourceRegistry(self.project).get(self.server["id"])
        if server["revision"] != self.server["revision"]:
            raise ValueError("网络任务节点配置或权限已变化，需重新审阅")
        require_operation(server, "network")
        if self.scope["port"] not in server["policy"].get("network_ports", []):
            raise ValueError("网络任务端口超出节点允许范围")
        return []


class NetworkService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.registry, self.assets, self.store = (
            ResourceRegistry(root),
            AssetService(root),
            InfraStore(root),
        )
        with self.store.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS network_tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,data TEXT)"
            )

    def list(self):
        with self.store.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM network_tasks ORDER BY rowid DESC")
            ]

    def get(self, task_id):
        return next((t for t in self.list() if t["id"] == task_id), None) or self.missing()

    @staticmethod
    def missing():
        raise ValueError("网络任务不存在")

    def update(self, task):
        with self.store.db() as db:
            db.execute("UPDATE network_tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))
        return task

    def recommend(self, server_ids):
        if not 2 <= len(server_ids) <= 4 or len(set(server_ids)) != len(server_ids):
            raise ValueError("请指定2至4个不同节点")
        checks = [
            self.assets.inspect_executor(
                {"server_id": key, "expected_revision": self.registry.get(key)["revision"]}
            )
            for key in server_ids
        ]
        candidates = []
        for first in checks[0].get("network_interfaces", []):
            subnet = ipaddress.ip_network(
                first["address"] + "/" + str(first["prefixlen"]), strict=False
            )
            chosen = [first]
            for check in checks[1:]:
                found = next(
                    (
                        a
                        for a in check.get("network_interfaces", [])
                        if ipaddress.ip_address(a["address"]) in subnet
                    ),
                    None,
                )
                if not found:
                    break
                chosen.append(found)
            if len(chosen) == len(checks):
                candidates.append(chosen)
        candidates.sort(key=lambda group: -min(a.get("speed_mbps") or 0 for a in group))
        if not candidates:
            return {"available": False, "reason": "未发现共同接口子网，请核对已有路由与节点地址"}
        return {
            "available": True,
            "peers": [
                {"server_id": key, "address": a["address"], "port": 23181}
                for key, a in zip(server_ids, candidates[0], strict=True)
            ],
            "note": "仅建议既有接口；端口权限与真实TCP链路须执行时核验",
        }

    def create(self, args, session_id=None):
        request = NetworkPlan.model_validate(args)
        for existing in self.list():
            if existing["plan"]["request_id"] == request.request_id:
                if (
                    existing["plan"] != request.model_dump()
                    or existing.get("session_id") != session_id
                ):
                    raise ValueError("网络任务请求ID冲突")
                return existing
        if session_id:
            from .build_service import BuildService

            if (
                self.store.binding(session_id)
                or BuildService(self.root).bound(session_id)
                or self.bound(session_id)
            ):
                raise ValueError("当前会话已有业务任务，请使用新会话")
        scopes = []
        for peer in request.peers:
            server = self.registry.get(peer.server_id)
            executor = self.assets.executor(peer.server_id)
            check = server.get("last_node_check") or {}
            if peer.address not in {a["address"] for a in check.get("network_interfaces", [])}:
                raise ValueError("网络地址不是该节点已核验接口")
            scopes.append(
                {
                    "server": server,
                    "executor": executor,
                    "port": peer.port,
                    "gpu_ids": [],
                    "worker_sha256": hashlib.sha256(
                        Path(network_worker.__file__).read_bytes()
                    ).hexdigest(),
                }
            )
        if len({s["executor"].get("node_identity") for s in scopes}) != len(scopes):
            raise ValueError("网络任务包含重复硬件节点")
        task = {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "plan": request.model_dump(),
            "status": "draft",
            "scopes": scopes,
            "server_ids": [p.server_id for p in request.peers],
            "results": [],
            "restored": False,
        }
        with self.store.db() as db:
            db.execute(
                "INSERT INTO network_tasks VALUES(?,?,?)",
                (task["id"], request.request_id, json.dumps(task)),
            )
        return task

    def peer(self, task, index):
        return RegisteredNetwork(
            self.root,
            {"timeout": task["plan"]["duration_seconds"], "node_scope": task["scopes"][index]},
            task["id"],
        )

    def run(self, task_id):
        task = self.get(task_id)
        if task["status"] != "draft":
            raise ValueError("原网络任务已开始，先核对或清理；不得重放")
        plan = task["plan"]
        for index, _peer in enumerate(plan["peers"]):
            self.peer(task, index).admit()
            if (
                plan["duration_seconds"]
                > task["scopes"][index]["server"]["policy"]["max_duration_seconds"]
            ):
                raise ValueError("网络实验时限超过节点限额")
        task.update(
            status="starting",
            nonce=uuid.uuid4().hex,
            started_nodes=[],
            deadline=time.time() + plan["duration_seconds"],
        )
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(
                db.execute("SELECT data FROM network_tasks WHERE id=?", (task_id,)).fetchone()[0]
            )
            if current["status"] != "draft":
                raise ValueError("网络任务已由其他请求启动，不能重复执行")
            for scope in task["scopes"]:
                key = scope["server"]["id"] + ":port:" + str(scope["port"])
                if db.execute("SELECT 1 FROM locks WHERE resource=?", (key,)).fetchone():
                    raise ValueError("该网络端口已由其他Pilot任务预留")
                db.execute("INSERT INTO locks VALUES(?,?)", (key, task_id))
            db.execute("UPDATE network_tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
        try:
            for index, peer in enumerate(plan["peers"]):
                remote = self.peer(task, index)
                task["started_nodes"].append(index)
                self.update(task)
                remote.bootstrap()
                remote.call(
                    "start",
                    {
                        "address": peer["address"],
                        "port": peer["port"],
                        "nonce": task["nonce"],
                        "bytes": plan["payload_bytes"],
                        "connections": (len(plan["peers"]) - 1) * plan["rounds"],
                        "deadline": task["deadline"],
                        "node_identity": task["scopes"][index]["executor"]["node_identity"],
                    },
                )
                until = time.monotonic() + 8
                while time.monotonic() < until:
                    status = remote.call("inspect", {})
                    if status.get("ready"):
                        break
                    if status["status"] == "failed":
                        raise ValueError("节点监听失败或端口不可用")
                    time.sleep(0.1)
                else:
                    raise TimeoutError("节点监听未就绪")
            task["status"] = "running"
            self.update(task)
            for source, peer in enumerate(plan["peers"]):
                for target, destination in enumerate(plan["peers"]):
                    if source == target:
                        continue
                    result = self.peer(task, source).call(
                        "send",
                        {
                            "source": peer["address"],
                            "target": destination["address"],
                            "port": destination["port"],
                            "nonce": task["nonce"],
                            "bytes": plan["payload_bytes"],
                            "rounds": plan["rounds"],
                            "peer_identity": task["scopes"][target]["executor"]["node_identity"],
                            "deadline": task["deadline"],
                        },
                    )
                    task["results"].append(
                        {"source": peer["server_id"], "target": destination["server_id"], **result}
                    )
                    self.update(task)
            task.update(status="completed", passed=True)
        except Exception as error:
            task.update(
                status="failed",
                passed=False,
                error_type=type(error).__name__,
                error=str(error)[:500],
            )
        finally:
            errors = []
            for index in task["started_nodes"]:
                try:
                    self.peer(task, index).call("cleanup", {})
                except Exception as error:
                    errors.append(type(error).__name__)
            task["restored"] = not errors
            if not errors:
                self.store.release(task_id)
            if errors:
                task.update(status="unknown", cleanup_errors=errors)
            self.update(task)
        return task

    def cleanup(self, task_id):
        task = self.get(task_id)
        if task["status"] in {"starting", "running"} and time.time() < task.get("deadline", 0) + 5:
            raise ValueError(
                "网络任务仍在有效执行租约内；结束后自动恢复，若主控中断可在租约到期后核对清理"
            )
        for index in task.get("started_nodes", []):
            self.peer(task, index).call("cleanup", {})
        task.update(status="restored", restored=True)
        self.store.release(task_id)
        return self.update(task)

    def bound(self, session_id):
        return next((t for t in self.list() if t.get("session_id") == session_id), None)

    def tool(self, name, session_id, role, args):
        if name == "network_recommend":
            return self.recommend(NetworkRecommend.model_validate(args).server_ids)
        if name == "network_plan" and role == "infra-lead":
            task = self.create(args, session_id)
            return {
                "network_task": {k: v for k, v in task.items() if k != "scopes"},
                "requires_start": True,
            }
        request = NetworkTarget.model_validate(args)
        task = self.get(request.task_id)
        if task.get("session_id") != session_id:
            raise ValueError("网络任务属于其他会话")
        if name == "network_status":
            return {k: v for k, v in task.items() if k not in {"scopes", "nonce"}}
        if name == "network_cleanup" and role == "infra-lead":
            return self.cleanup(task["id"])
        raise ValueError("角色不允许该网络操作")
