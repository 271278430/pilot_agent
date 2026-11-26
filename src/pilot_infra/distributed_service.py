"""Reviewable two-node topology and evidence-based admission checks."""

import ipaddress
import json
import re
import time
import uuid
from pathlib import Path

from .asset_service import AssetService
from .distributed_contracts import DistributedPlan
from .distributed_recipes import render_commands
from .fleet_transport import worker_current
from .node_authority import require_operation
from .resource_assessment import check_capacity, check_gpu_occupancy
from .resource_registry import ResourceRegistry
from .store import digest


class DistributedService:
    def __init__(self, infra, clock=time.time, adapter=None):
        self.infra, self.root, self.store = infra, Path(infra.root), infra.store
        self.registry = ResourceRegistry(self.root)
        self.assets = AssetService(self.root)
        self.clock = clock
        self.adapter = adapter
        with self.store.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pd_tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,data TEXT)"
            )

    def preflight(self, args):
        plan = DistributedPlan.model_validate(args)
        projected = self.infra.assets.read()
        nodes, issues = [], []
        for node in plan.nodes:
            report = {"server_id": node.server_id, "role": node.role, "issues": []}
            problems = report["issues"]
            try:
                server = self.registry.get(node.server_id)
                executor = self.assets.executor(node.server_id)
                if not re.fullmatch(r"[a-f0-9]{64}", executor.get("node_identity", "")):
                    problems.append("节点硬件身份未知，不能绑定跨机任务")
                if not worker_current(executor):
                    problems.append("持久执行器版本已变化，需空闲时重新准备")
                for operation in ("network", "inference", "containers"):
                    try:
                        require_operation(server, operation)
                    except ValueError as exc:
                        problems.append(str(exc))
                if not set(node.ports.values()).issubset(server["policy"].get("network_ports", [])):
                    problems.append("所需端口未全部包含在节点授权范围")
                limits = node.limits.model_dump()
                mini_plan = {
                    "gpu_ids": node.gpu_ids,
                    "engine_source_profile": None,
                    "serving": {"context_length": plan.context_length, "mem_fraction_static": 0.18},
                    "workload": {"concurrency": [plan.max_concurrency]},
                }
                try:
                    self.infra.resources.within_policy(server, mini_plan, limits)
                except ValueError as exc:
                    problems.append(str(exc))
                check = server.get("last_node_check") or {}
                if (check.get("ready") is not True or check.get("server_revision") != server["revision"]
                        or not 0 <= self.clock() - check.get("received_at", 0) <= 600
                        or check.get("node_identity") != executor.get("node_identity")):
                    problems.append("节点身份或目录检查过期，请重新核验")
                if node.address not in {item.get("address") for item in check.get("network_interfaces", [])}:
                    problems.append("指定地址不在节点已核验接口中")
                # Older policies implicitly allow the RFC 1918 ranges. Other
                # routed addresses require an explicit per-node owner policy.
                # This is only admission, not proof of network isolation.
                permitted = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                try:
                    permitted += tuple(server["policy"].get("distributed_networks", []))
                    ip = ipaddress.ip_address(node.address)
                    if not any(ip in ipaddress.ip_network(cidr) for cidr in permitted):
                        problems.append("节点地址不在所有者允许的跨机网段中")
                except (TypeError, ValueError):
                    problems.append("节点跨机网段配置无效")
                if plan.mode == "sglang_pd":
                    names = {item.get("interface") for item in check.get("network_interfaces", [])
                             if item.get("address") == node.address}
                    if (check.get("rdma_inventory_known") is not True or not any(
                        item.get("device") == node.rdma_device and item.get("state") == "4: ACTIVE"
                        and names.intersection(item.get("interfaces") or [])
                        for item in check.get("rdma_hcas", [])
                    )):
                        problems.append("RDMA设备不可用或与所选网络接口不匹配，请重新核验")
                observation = self.infra.resources.latest(node.server_id)
                if (observation.get("schema_version") != 1 or observation.get("scope") != "whole-machine"
                        or observation.get("identity", {}).get("machine_fingerprint") != executor.get("machine_identity")
                        or observation.get("accelerator_platform") != executor.get("platform")):
                    problems.append("整机观测的节点身份或设备平台不匹配")
                try:
                    self.infra.resources.fresh(server, observation)
                    # Distributed execution is exclusive even if the general node
                    # policy permits sharing; never start beside another workload.
                    check_gpu_occupancy(observation, node.gpu_ids)
                except ValueError as exc:
                    problems.append(str(exc))
                model = next((m for m in projected["models"] if m["id"] == node.model_asset
                              and m.get("server_id") == node.server_id), None)
                image = next((i for i in projected["images"] if i["id"] == node.image_asset
                              and i.get("server_id") == node.server_id), None)
                if not model:
                    problems.append("目标节点没有已核验的指定模型")
                if not image:
                    problems.append("目标节点没有已核验的指定镜像")
                if model:
                    if not re.fullmatch(r"[a-f0-9]{64}", model.get("manifest_digest", "")):
                        problems.append("模型清单摘要缺失或格式无效")
                    try:
                        check_capacity(observation, mini_plan, limits, model, executor["root"])
                    except ValueError as exc:
                        problems.append(str(exc))
                if image:
                    required_engine = "vllm" if plan.mode == "vllm_ray" else "sglang"
                    required_capability = "pd" if plan.mode == "sglang_pd" else "tensor_parallel"
                    if image.get("engine") != required_engine or required_capability not in image.get("stack_recipe", {}).get("capabilities", []):
                        problems.append("引擎或兼容栈未声明所需能力")
                    if image.get("platform") != executor.get("platform"):
                        problems.append("镜像平台与节点设备不匹配")
                    if image.get("backend") != "docker" or not re.fullmatch(
                        r"sha256:[a-f0-9]{64}", image.get("digest", "")
                    ):
                        problems.append("节点镜像必须绑定已核验的固定Docker摘要")
                    if model:
                        config = model.get("manifest", {}).get("config", {})
                        recipe = image.get("stack_recipe", {})
                        if config.get("model_type") not in recipe.get("model_types", []):
                            problems.append("模型类型不在镜像兼容栈范围")
                        quantization = (config.get("quantization_config") or {}).get("quant_method", "none")
                        if quantization not in recipe.get("quantizations", ["none"]):
                            problems.append("模型量化方式不在镜像兼容栈范围")
                    if plan.mode != "sglang_pd" and not image.get("environment", {}).get("modules", {}).get("ray"):
                        problems.append("镜像尚未核验Ray模块，请重新核验资产")
                    if plan.mode == "sglang_ray" and not image.get("environment", {}).get("modules", {}).get("sglang_ray"):
                        problems.append("镜像尚未核验SGLang的Ray执行器")
                    if plan.mode == "sglang_pd" and not image.get("environment", {}).get("modules", {}).get("mooncake"):
                        problems.append("镜像尚未核验Mooncake传输模块")
                    if plan.mode == "sglang_pd" and node.role == "prefill" and not image.get("environment", {}).get("modules", {}).get("sglang_router"):
                        problems.append("镜像尚未核验SGLang PD路由器，请重新核验资产")
                report.update({
                    "server_revision": server["revision"],
                    "policy_revision": server["policy_revision"],
                    "node_identity": executor.get("node_identity"),
                    "observation_id": observation["id"],
                    "observation_digest": observation["digest"],
                    "model_digest": model.get("manifest_digest") if model else None,
                    "model_path": model.get("path") if model else None,
                    "model_revision": model.get("fleet_asset_revision") if model else None,
                    "image_digest": image.get("digest") if image else None,
                    "image_revision": image.get("fleet_asset_revision") if image else None,
                    "stack_digest": digest(image.get("stack_recipe", {})) if image else None,
                    "environment_digest": digest(image.get("environment", {})) if image else None,
                    "executor_digest": digest(executor),
                    "platform": image.get("platform") if image else None,
                })
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(str(exc)[:300] or type(exc).__name__)
            report["issues"] = list(dict.fromkeys(problems))
            issues.extend(f"{node.server_id}: {problem}" for problem in report["issues"])
            nodes.append(report)
        for key, label in (("node_identity", "硬件身份"), ("model_digest", "模型/分词器"),
                           ("image_digest", "镜像"), ("environment_digest", "运行环境"),
                           ("platform", "设备平台")):
            values = [node.get(key) for node in nodes]
            if all(values):
                if (key == "node_identity" and len(set(values)) != 2) or (key != "node_identity" and len(set(values)) != 1):
                    issues.append(f"两节点{label}不符合固定拓扑要求")
        limitations = ["只检查已登记的观测、权限与资产；不证明实际KV传输或Ray集群可用。",
                       "当前仅校验已列出的固定端口；动态KV/通信端口及隔离仍需执行器核验。"]
        if plan.mode != "sglang_pd" and any(node.get("platform") == "musa" for node in nodes):
            limitations.append("Ray声明GPU数量不等于S5000设备映射或调度已验证。")
        return {"plan": plan.model_dump(), "nodes": nodes, "issues": issues,
                "ready_for_review": not issues, "ready_for_execution": False,
                "execution_blockers": ["远端跨机执行器与恢复尚未接入", "动态通信端口隔离及真实跨机验收尚未完成"],
                "limitations": limitations}

    def create(self, args):
        plan = DistributedPlan.model_validate(args).model_dump()
        request_digest = digest(plan)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM pd_tasks WHERE request_id=?", (plan["request_id"],)).fetchone()
            if row:
                task = json.loads(row[0])
                if task["plan_digest"] != request_digest:
                    raise ValueError("任务请求ID已用于不同拓扑")
                return task
            task = {"id": uuid.uuid4().hex, "plan": plan, "plan_digest": request_digest,
                    "server_ids": [n["server_id"] for n in plan["nodes"]], "status": "draft",
                    "restored": True, "created_at": self.clock()}
            db.execute("INSERT INTO pd_tasks VALUES(?,?,?)",
                       (task["id"], plan["request_id"], json.dumps(task)))
        return task

    def get(self, task_id):
        with self.store.db() as db:
            row = db.execute("SELECT data FROM pd_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("分布式任务不存在")
        return json.loads(row[0])

    def list(self):
        with self.store.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM pd_tasks ORDER BY rowid DESC")]

    def review(self, task_id):
        task = self.get(task_id)
        if task["status"] != "draft":
            raise ValueError("只能审阅尚未启动的拓扑")
        checked = self.preflight(task["plan"])
        if not checked["ready_for_review"]:
            return {"task": task, "preflight": checked, "review_digest": None}
        scope = {"plan_digest": task["plan_digest"], "nodes": checked["nodes"],
                 "commands_digest": digest(render_commands(task["plan"]))}
        review_digest = digest(scope)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT data FROM pd_tasks WHERE id=?", (task_id,)).fetchone()[0])
            if current["status"] != "draft" or current["plan_digest"] != task["plan_digest"]:
                raise ValueError("任务状态已变化，请重新审阅")
            current["review"] = {"digest": review_digest, "scope": scope, "created_at": self.clock()}
            db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(current), task_id))
        return {"task": current, "preflight": checked, "review_digest": review_digest}

    @staticmethod
    def resources(task):
        return sorted({
            *(f"{node['server_id']}:gpu:{gpu}" for node in task["plan"]["nodes"] for gpu in node["gpu_ids"]),
            *(f"{node['server_id']}:port:{port}" for node in task["plan"]["nodes"] for port in node["ports"].values()),
        })

    @staticmethod
    def launch_order(task):
        nodes = {node["role"]: node for node in task["plan"]["nodes"]}
        roles = ("prefill", "decode") if task["plan"]["mode"] == "sglang_pd" else ("head", "worker")
        return [nodes[role] for role in roles]

    @staticmethod
    def verified_evidence(task, evidence):
        if not isinstance(evidence, dict) or evidence.get("passed") is not True:
            return False
        http = evidence.get("http") or {}
        request_id = http.get("request_id")
        if (http.get("status") != 200 or http.get("model") != task["plan"]["served_model_name"]
                or not isinstance(http.get("output"), str) or not http["output"].strip()
                or not isinstance(request_id, str) or not request_id):
            return False
        if task["plan"]["mode"] == "sglang_pd":
            transfer = evidence.get("kv_transfer") or {}
            nodes = {n["role"]: n["server_id"] for n in task["plan"]["nodes"]}
            return bool(
                transfer.get("backend") == "mooncake"
                and transfer.get("prefill_server") == nodes["prefill"]
                and transfer.get("decode_server") == nodes["decode"]
                and transfer.get("request_id") == request_id
                and type(transfer.get("bytes")) is int and transfer["bytes"] > 0
                and transfer.get("source_evidence") and transfer.get("destination_evidence")
            )
        ray = evidence.get("ray") or {}
        reviewed = {node["node_identity"] for node in task["review"]["scope"]["nodes"]}
        return bool(
            ray.get("node_identities") and set(ray["node_identities"]) == reviewed
            and set(ray.get("gpu_placement_servers") or []) == set(task["server_ids"])
            and ray.get("placement_evidence")
        )

    def start(self, task_id, expected_digest):
        if self.adapter is None:
            raise ValueError("跨机执行器尚未接入；只能保存和预检，不会启动节点")
        if not callable(getattr(self.adapter, "fence_and_stop", None)):
            raise ValueError("执行器未提供原运行隔离与清理能力")
        task = self.get(task_id)
        review = task.get("review") or {}
        if task["status"] != "draft" or review.get("digest") != expected_digest:
            raise ValueError("任务不是待执行状态或审阅摘要已变化")
        checked = self.preflight(task["plan"])
        current_scope = {"plan_digest": task["plan_digest"], "nodes": checked["nodes"],
                         "commands_digest": digest(render_commands(task["plan"]))}
        if not checked["ready_for_review"] or digest(current_scope) != expected_digest:
            raise ValueError("节点观测、授权或资产变化；请重新审阅")
        # Adapter readiness must be read-only and precede any resource lock.
        self.adapter.preflight(task, checked)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT data FROM pd_tasks WHERE id=?", (task_id,)).fetchone()[0])
            if current["status"] != "draft" or current.get("review", {}).get("digest") != expected_digest:
                raise ValueError("任务已启动或审阅摘要已变化")
            for key in self.resources(current):
                holder = db.execute("SELECT task_id FROM locks WHERE resource=?", (key,)).fetchone()
                if holder:
                    raise ValueError("设备或端口已由其他Pilot任务预留")
                db.execute("INSERT INTO locks VALUES(?,?)", (key, task_id))
            current.update(status="starting", restored=False, started_nodes=[], run_id=uuid.uuid4().hex,
                           deadline=self.clock() + current["plan"]["duration_seconds"])
            db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(current), task_id))
        try:
            # Record each intended effect before submission. Lost replies never
            # cause a new operation to be submitted on retry.
            for node in self.launch_order(current):
                if self.clock() >= current["deadline"]:
                    raise TimeoutError("分布式任务已到期")
                current["started_nodes"].append(node["server_id"])
                self._save(current, expected="starting")
                self.adapter.launch(current, node)
                if self.clock() >= current["deadline"]:
                    raise TimeoutError("分布式任务已到期")
            evidence = self.adapter.verify(current)
            if self.clock() >= current["deadline"]:
                raise TimeoutError("分布式任务验收时已到期")
            if not self.verified_evidence(current, evidence):
                raise ValueError("分布式服务独立验收未通过")
            current.update(status="running", evidence=evidence)
            self._save(current, expected="starting")
            return current
        except Exception as exc:
            current.update(error_type=type(exc).__name__, error=str(exc)[:500])
            try:
                return self._stop(current, failed=True)
            except ValueError:
                # Recovery may have fenced this run while the coordinator was
                # waiting on a node. Never overwrite its newer cleanup result.
                latest = self.get(task_id)
                if latest.get("run_id") == current.get("run_id") and latest["status"] in {
                    "stopping", "stopped", "failed", "unknown"
                }:
                    return latest
                raise

    def _save(self, task, expected):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = json.loads(db.execute("SELECT data FROM pd_tasks WHERE id=?", (task["id"],)).fetchone()[0])
            if previous["status"] != expected or previous.get("run_id") != task.get("run_id"):
                raise ValueError("任务操作已变化，不覆盖当前状态")
            db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))

    def _stop(self, task, failed=False, recovering=False):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT data FROM pd_tasks WHERE id=?", (task["id"],)).fetchone()[0])
            allowed = ({"starting", "stopping", "unknown"} if recovering else
                       {"starting", "unknown"} if failed else {"running", "unknown"})
            if current["status"] not in allowed or current.get("run_id") != task.get("run_id"):
                raise ValueError("任务已有其他操作，不能并发清理")
            if recovering and current["status"] == "starting":
                failed = True
            # Preserve the launch error recorded by the starting caller.
            if failed:
                current.update(error=task.get("error") or current.get("error"),
                               error_type=task.get("error_type") or current.get("error_type"))
            task = current
            task.update(status="stopping", stop_id=uuid.uuid4().hex, stop_failed=failed)
            db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))
        errors = []
        # Fence every planned node, including nodes for which the coordinator
        # crashed before persisting an intent. The adapter must durably reject
        # late submissions for this run, then wait for and remove owned effects.
        for node in reversed(self.launch_order(task)):
            server_id = node["server_id"]
            try:
                result = self.adapter.fence_and_stop(task, node)
                if (not isinstance(result, dict) or result.get("fenced") is not True
                        or result.get("restored") is not True or result.get("run_id") != task["run_id"]
                        or result.get("server_id") != server_id):
                    raise ValueError("节点运行隔离或清理未确认")
            except Exception as exc:
                errors.append({"server_id": server_id, "error_type": type(exc).__name__})
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(db.execute("SELECT data FROM pd_tasks WHERE id=?", (task["id"],)).fetchone()[0])
            if current["status"] != "stopping" or current.get("stop_id") != task["stop_id"]:
                raise ValueError("清理操作已变化，保留设备和端口锁")
            if errors:
                task.update(status="unknown", restored=False, cleanup_errors=errors)
            else:
                task.update(status="failed" if failed else "stopped", restored=True, cleanup_errors=[])
                db.execute("DELETE FROM locks WHERE task_id=?", (task["id"],))
            db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(task), task["id"]))
        return task

    def stop(self, task_id):
        if self.adapter is None:
            raise ValueError("跨机执行器不可用，不能推断节点已清理")
        task = self.get(task_id)
        if task["status"] in {"stopped", "failed"} and task.get("restored"):
            return task
        if task["status"] not in {"running", "unknown"}:
            raise ValueError("任务没有可清理的已启动操作")
        return self._stop(task, failed=bool(task.get("error") or task.get("stop_failed")))

    def recover(self, task_id):
        """Fence a stranded run; never replay its launches or infer cleanup.

        The adapter must implement durable run fencing and idempotent cleanup
        for *both* nodes. No production adapter is registered yet.
        """
        if self.adapter is None or not callable(getattr(self.adapter, "fence_and_stop", None)):
            raise ValueError("跨机执行器不能隔离并恢复原运行")
        task = self.get(task_id)
        if task["status"] in {"stopped", "failed"} and task.get("restored"):
            return task
        if task["status"] not in {"starting", "stopping", "unknown"}:
            raise ValueError("任务没有需要恢复的未结算操作")
        return self._stop(task, failed=bool(task.get("error") or task.get("stop_failed")), recovering=True)
