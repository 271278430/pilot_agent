"""Node asset registration, persistent preparation and compatibility projection."""

import json
import time
import uuid
from pathlib import Path

from .fleet_contracts import AssetAction, AssetSave, ExecutorInspect, ExecutorUpdate, StackSave
from .fleet_transport import FleetTransport
from .resource_registry import ResourceRegistry
from .stack_catalog import select_stacks
from .store import digest


class AssetService:
    def __init__(self, root, transport=None):
        self.root = Path(root).resolve()
        self.registry = ResourceRegistry(root)
        self.transport = transport or FleetTransport(root)
        with self.registry.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS fleet_assets(id TEXT PRIMARY KEY,revision INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS fleet_stacks(id TEXT PRIMARY KEY,revision INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS fleet_executors(server_id TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS fleet_operations(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,asset_id TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS fleet_reviews(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,root_session TEXT,data TEXT);""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fleet_proposals(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,root_session TEXT,data TEXT)"
            )

    def executor(self, server_id, cleanup=False):
        with self.registry.db() as db:
            row = db.execute(
                "SELECT data FROM fleet_executors WHERE server_id=?", (server_id,)
            ).fetchone()
        if not row:
            raise ValueError("该服务器尚未配置持久执行器")
        value = json.loads(row[0])
        server = self.registry.get(server_id)
        if not cleanup and (
            value["server_revision"] != server["revision"]
            or not value["enabled"]
            or not server["enabled"]
        ):
            raise ValueError("执行器配置已失效或停用，请重新核验")
        return value

    def executors(self):
        with self.registry.db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM fleet_executors")]

    def operation_rows(self):
        with self.registry.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM fleet_operations ORDER BY rowid DESC")
            ]

    def dependent_node_tasks(self, server_id):
        from .store import InfraStore

        found = []
        with InfraStore(self.root).db() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("build_tasks", "network_tasks", "pd_tasks"):
                if table not in names:
                    continue
                for (data,) in db.execute("SELECT data FROM " + table):
                    task = json.loads(data)
                    if task.get("status") in {"draft", "restored"} or task.get("restored") is True:
                        continue
                    if task.get("server_id") == server_id or server_id in task.get(
                        "server_ids", []
                    ):
                        found.append(task)
        return found

    def inspect_executor(self, args):
        request = ExecutorInspect.model_validate(args)
        server = self.registry.get(request.server_id)
        if server["revision"] != request.expected_revision:
            raise ValueError("服务器配置版本冲突，请重新读取")
        if not server["enabled"]:
            raise ValueError("服务器已停用")
        scope = {
            "namespace": digest(str(self.root))[:12],
            "owner_namespace": digest(str(self.root)),
            "resource_owner_namespace": __import__("hashlib")
            .sha256(str(self.root).encode())
            .hexdigest(),
            "root": request.root,
            "asset_root": request.asset_root
            or (request.root + "/assets" if request.root else None),
        }
        value = self.transport.inspect_storage(server, scope)
        value.update(server_revision=server["revision"], received_at=time.time())
        self.registry.node_check_result(server["id"], server["revision"], value)
        return value

    def docker_inventory(self, args):
        from .docker_inventory import program

        server = self.registry.get(args["server_id"])
        if not server["enabled"]:
            raise ValueError("服务器已停用")
        executor = next((e for e in self.executors() if e["server_id"] == server["id"]), None)
        reply = self.transport.exchange(
            server,
            ["python3", "-B", "-c", program()],
            {
                "root": executor["root"] if executor else None,
                "owner_namespace": digest(str(self.root)),
            },
        )
        if not reply["ok"]:
            raise ValueError(reply.get("error", "节点Docker访问不可用，请检查登记的Docker访问方式"))
        result = reply["value"]
        return {**result, "server_id": server["id"]}

    def node_readiness(self):
        from .fleet_transport import worker_current
        from .node_authority import allowed_operations

        assets, executors = self.rows("fleet_assets"), self.executors()
        result = []
        for server in self.registry.catalog():
            check = server.get("last_node_check")
            current_check = check and check.get("server_revision") == server["revision"]
            executor = next((e for e in executors if e["server_id"] == server["id"]), None)
            active = bool(
                executor
                and executor.get("enabled")
                and executor["server_revision"] == server["revision"]
            )
            ready = [a for a in assets if a["server_id"] == server["id"] and a["status"] == "ready"]
            reasons = []
            if not server["enabled"]:
                reasons.append("服务器已停用")
            if not current_check or not check.get("ready"):
                reasons.append("节点目录与Docker能力需核验")
            if not active:
                reasons.append("持久执行器未启用或配置已失效")
            elif server["connection"]["transport"] != "local" and not worker_current(executor):
                reasons.append("节点执行器版本需更新；空闲后重新核验并准备")
            for kind, label in [("model", "模型"), ("image", "镜像")]:
                if not any(a["kind"] == kind for a in ready):
                    reasons.append("目标节点缺少已核验" + label)
            if server["policy"]["mode"] != "review":
                reasons.append("当前仅允许观察")
            else:
                allowed = allowed_operations(server["policy"])
                if "inference" not in allowed:
                    reasons.append("尚未允许推理部署和测试")
                if "containers" not in allowed:
                    reasons.append("尚未允许本任务容器")
            if not server["policy"]["approvable_gpu_ids"]:
                reasons.append("尚未指定可审批设备")
            result.append(
                {
                    "server_id": server["id"],
                    "executor_registered": active,
                    "node_check_current": bool(current_check),
                    "ready_assets": len(ready),
                    "deployment_registered": not reasons,
                    "reasons": reasons,
                }
            )
        return result

    def configure_executor(self, args):
        request = ExecutorUpdate.model_validate(args)
        server = self.registry.get(request.server_id)
        if not request.enabled:
            with self.registry.db() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT data FROM fleet_executors WHERE server_id=?", (server["id"],)
                ).fetchone()
                if request.expected_revision != server["revision"]:
                    raise ValueError("服务器配置版本冲突")
                if not previous:
                    raise ValueError("首次配置请启用并核验节点执行器")
                value = json.loads(previous[0])
                if request.root != value["root"]:
                    raise ValueError("停用时保留原执行根；迁移目录需要重新启用核验")
                if request.asset_root and request.asset_root != value.get(
                    "asset_root", value["root"] + "/assets"
                ):
                    raise ValueError("停用时保留原资源目录")
                value.update(enabled=False, configuring=False)
                db.execute(
                    "UPDATE fleet_executors SET data=? WHERE server_id=?",
                    (json.dumps(value), server["id"]),
                )
            return value
        from .store import InfraStore

        active_tasks = [
            t
            for t in InfraStore(self.root).list()
            if t["plan"]["machine_id"] == server["id"]
            and (t["state"].get("active_job") or t["state"].get("service_candidate"))
        ]
        active_assets = [
            o
            for o in self.operation_rows()
            if o["server_id"] == server["id"] and o["status"] in {"submitted", "running", "unknown"}
        ]
        if active_tasks or active_assets or self.dependent_node_tasks(server["id"]):
            raise ValueError("节点有未结算任务或资产操作，先核对并停止自有操作再配置执行器")
        if request.expected_revision != server["revision"]:
            raise ValueError("服务器配置版本冲突")
        if not (server.get("last_connection_test") or {}).get("ready"):
            raise ValueError("先核验当前服务器接入")
        storage = self.inspect_executor(request.model_dump(exclude={"enabled"}))
        if not storage["ready"]:
            raise ValueError("节点准备条件未满足：" + "; ".join(storage["reasons"]))
        observation = self.transport.transport.observe(server)
        value = {
            "server_id": server["id"],
            "cuda_runtime": storage.get("cuda_runtime"),
            "server_revision": server["revision"],
            "root": request.root,
            "asset_root": request.asset_root or request.root + "/assets",
            "machine_identity": observation["identity"]["machine_fingerprint"],
            "owner_namespace": digest(str(self.root)),
            "resource_owner_namespace": __import__("hashlib")
            .sha256(str(self.root).encode())
            .hexdigest(),
            "enabled": request.enabled,
            "platform": observation["accelerator_platform"],
            "server_snapshot": server,
            "checked_at": time.time(),
            "namespace": digest(str(self.root))[:12],
            "storage_devices": {k: storage["paths"][k]["device"] for k in ("root", "asset_root")},
            "node_identity": storage.get("node_identity"),
        }
        # Reserve configuration before touching the node; a concurrent prepare
        # must see a disabled/configuring executor and cannot submit to old roots.
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if any(
                json.loads(r[0])["server_id"] == server["id"]
                and json.loads(r[0])["status"] in {"submitted", "running", "unknown"}
                for r in db.execute("SELECT data FROM fleet_operations")
            ):
                raise ValueError("节点已有未结算资产操作")
            pending = {**value, "enabled": False, "configuring": True}
            db.execute(
                "INSERT OR REPLACE INTO fleet_executors VALUES(?,?)",
                (server["id"], json.dumps(pending)),
            )
        if request.enabled:
            value["probe"] = self.transport.bootstrap(server, value)
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(
                db.execute(
                    "SELECT data FROM fleet_executors WHERE server_id=?", (server["id"],)
                ).fetchone()[0]
            )
            if (
                current != pending
                or self.registry.get(server["id"])["revision"] != server["revision"]
            ):
                raise ValueError("执行器或接入在核验中发生变化，请重新核验")
            db.execute(
                "UPDATE fleet_executors SET data=? WHERE server_id=?",
                (json.dumps(value), server["id"]),
            )
        return value

    def rows(self, table):
        if table not in {"fleet_assets", "fleet_stacks"}:
            raise ValueError("Invalid registry table")
        with self.registry.db() as db:
            return [
                {**json.loads(r["data"]), "revision": r["revision"]}
                for r in db.execute("SELECT * FROM " + table + " ORDER BY id")
            ]

    def asset(self, asset_id):
        value = next((a for a in self.rows("fleet_assets") if a["asset_id"] == asset_id), None)
        if not value:
            raise ValueError("资产尚未登记")
        return value

    def save(self, args, proposal_id=None):
        request = AssetSave.model_validate(args)
        self.require_not_in_use(request.asset_id)
        legacy = self.root / ".local/infra-assets.json"
        if legacy.is_file():
            values = json.loads(legacy.read_text())
            if request.asset_id in {
                a["id"] for key in ["models", "images"] for a in values.get(key, [])
            }:
                raise ValueError("资产ID与现有资产冲突，请使用新的节点资产ID")
        server = self.registry.get(request.server_id)
        self.executor(server["id"])
        if (
            request.source.kind != "existing"
            and request.max_bytes > server["policy"]["max_disk_gib"] * 1024**3
        ):
            raise ValueError("资产大小上限超过服务器磁盘授权")
        if request.stack_id and request.stack_id not in {
            s["recipe"]["id"] for s in self.rows("fleet_stacks")
        }:
            raise ValueError("先登记该兼容栈配方")
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM fleet_assets WHERE id=?", (request.asset_id,)
            ).fetchone()
            if request.expected_revision != (old["revision"] if old else 0):
                raise ValueError("资产版本冲突，请刷新")
            if old and json.loads(old["data"]).get("operation_id"):
                raise ValueError("先结算原资产准备操作")
            value = {
                **request.model_dump(),
                "status": "registered",
                "prepared": None,
                "operation_id": None,
            }
            db.execute(
                "INSERT OR REPLACE INTO fleet_assets VALUES(?,?,?)",
                (request.asset_id, request.expected_revision + 1, json.dumps(value)),
            )
            if proposal_id:
                self.finish_proposal(
                    db, proposal_id, {**value, "revision": request.expected_revision + 1}
                )
        return self.asset(request.asset_id)

    def require_not_in_use(self, asset_id):
        from .store import InfraStore

        for task in InfraStore(self.root).list():
            if asset_id in {task["plan"]["model_asset"], task["plan"]["image_asset"]} and (
                task["state"].get("active_job")
                or task["state"].get("service_candidate")
                or task["state"]["status"] == "running"
            ):
                raise ValueError("资产被活动任务使用，请先停止任务的自有服务")

    def save_stack(self, args, proposal_id=None):
        request = StackSave.model_validate(args)
        for a in self.rows("fleet_assets"):
            if a.get("stack_id") == request.recipe.id:
                self.require_not_in_use(a["asset_id"])
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT revision FROM fleet_stacks WHERE id=?", (request.recipe.id,)
            ).fetchone()
            if request.expected_revision != (old[0] if old else 0):
                raise ValueError("栈配方版本冲突")
            value = {"recipe": request.recipe.model_dump(), "runtime_receipts": []}
            db.execute(
                "INSERT OR REPLACE INTO fleet_stacks VALUES(?,?,?)",
                (request.recipe.id, request.expected_revision + 1, json.dumps(value)),
            )
            if proposal_id:
                self.finish_proposal(db, proposal_id, value)
        return value

    def operation(self, operation_id):
        with self.registry.db() as db:
            row = db.execute(
                "SELECT data FROM fleet_operations WHERE id=?", (operation_id,)
            ).fetchone()
        if not row:
            raise ValueError("未知资产操作")
        return json.loads(row[0])

    def prepare(self, args, root_session=""):
        request = AssetAction.model_validate(args)
        asset = self.asset(request.asset_id)
        self.require_not_in_use(request.asset_id)
        if asset["revision"] != request.expected_revision:
            raise ValueError("资产版本已变化")
        executor = self.executor(asset["server_id"])
        server = self.registry.get(asset["server_id"])
        from .node_authority import require_operation

        require_operation(server, "assets")
        if asset["source"]["kind"] != "existing":
            require_operation(server, "downloads")
        with self.registry.db() as db:
            existing = db.execute(
                "SELECT data FROM fleet_operations WHERE request_id=?", (request.request_id,)
            ).fetchone()
        if existing:
            value = json.loads(existing[0])
            if value["request_digest"] != digest(request.model_dump()):
                raise ValueError("操作ID冲突")
            return value
        if asset["operation_id"]:
            raise ValueError("先核对原准备操作，禁止重放")
        # Connection/identity preflight has no external effect. Do it before
        # claiming a persistent mutation so an unreachable node is retryable.
        probe = self.transport.call(server, executor, "probe", {})
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT revision,data FROM fleet_assets WHERE id=?", (request.asset_id,)
            ).fetchone()
            if latest["revision"] != request.expected_revision or json.loads(latest["data"]).get(
                "operation_id"
            ):
                raise ValueError("资产在预检期间变化或原操作未结算")
            if self.registry.get(server["id"])["revision"] != server["revision"]:
                raise ValueError("权限或连接在预检期间变化")
            latest_executor = db.execute(
                "SELECT data FROM fleet_executors WHERE server_id=?", (server["id"],)
            ).fetchone()
            if not latest_executor or json.loads(latest_executor[0]) != executor:
                raise ValueError("执行器在预检期间变化，请重新核验")
            old = db.execute(
                "SELECT data FROM fleet_operations WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if old:
                value = json.loads(old[0])
                if value["request_digest"] != digest(request.model_dump()):
                    raise ValueError("操作ID冲突")
                return value
            if asset["operation_id"]:
                raise ValueError("先核对原准备操作，禁止重放")
            op = uuid.uuid4().hex
            value = {
                "id": op,
                "asset_id": asset["asset_id"],
                "server_id": asset["server_id"],
                "request_digest": digest(request.model_dump()),
                "status": "submitted",
                "revision": asset["revision"],
                "root_session": root_session,
                "notified": False,
                "executor_snapshot": executor,
            }
            db.execute(
                "INSERT INTO fleet_operations VALUES(?,?,?,?)",
                (op, request.request_id, asset["asset_id"], json.dumps(value)),
            )
            asset.update(operation_id=op, status="preparing")
            asset.pop("revision")
            db.execute(
                "UPDATE fleet_assets SET data=? WHERE id=?", (json.dumps(asset), asset["asset_id"])
            )
        payload = {
            **asset,
            "action": "asset_prepare",
            "asset_root": executor.get("asset_root", executor["root"] + "/assets"),
            "machine_identity": executor["machine_identity"],
            "boot_id": probe["boot_id"],
            "deadline": time.time() + server["policy"]["max_duration_seconds"],
        }
        try:
            self.transport.call(
                server, executor, "submit", {"operation_id": op, "request": payload}
            )
        except Exception:
            value["status"] = "unknown"
            self.update_operation(value)
            raise ValueError("提交响应丢失；核对原资产操作，不能重新提交") from None
        return value

    def request_prepare(self, args, root_session):
        request = AssetAction.model_validate(args)
        asset = self.asset(request.asset_id)
        if asset["revision"] != request.expected_revision or asset["operation_id"]:
            raise ValueError("资产版本变化或原操作未结算")
        server = self.registry.get(asset["server_id"])
        executor = self.executor(asset["server_id"])
        scope = {
            "asset": asset,
            "server_revision": server["revision"],
            "executor_identity": executor["machine_identity"],
            "executor_digest": digest(executor),
            "input": request.model_dump(),
        }
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT root_session,data FROM fleet_reviews WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if old:
                review = json.loads(old[1])
                if old[0] != root_session or review["digest"] != digest(scope):
                    raise ValueError("资产审批请求冲突")
                return review
            review = {
                "id": uuid.uuid4().hex,
                "root_session": root_session,
                "digest": digest(scope),
                "scope": scope,
                "decision": None,
                "expires_at": time.time() + 600,
            }
            db.execute(
                "INSERT INTO fleet_reviews VALUES(?,?,?,?)",
                (review["id"], request.request_id, root_session, json.dumps(review)),
            )
        return review

    def decide_prepare(self, args, root_session):
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT root_session,data FROM fleet_reviews WHERE id=?", (args["review_id"],)
            ).fetchone()
            if not old or old[0] != root_session:
                raise ValueError("资产审批不属于此根会话")
            review = json.loads(old[1])
            if review["digest"] != args["expected_digest"] or review["decision"]:
                raise ValueError("审批版本已变化或已结算")
            if args["decision"] not in {"allowed-once", "rejected", "cancelled", "unavailable"}:
                raise ValueError("无效审批决定")
            if time.time() >= review["expires_at"]:
                raise ValueError("资产审批已过期")
            current = self.asset(review["scope"]["asset"]["asset_id"])
            if (
                current != review["scope"]["asset"]
                or self.registry.get(current["server_id"])["revision"]
                != review["scope"]["server_revision"]
                or digest(self.executor(current["server_id"])) != review["scope"]["executor_digest"]
            ):
                raise ValueError("源、资产或服务器版本变化，审批失效")
            review["decision"] = args["decision"]
            db.execute(
                "UPDATE fleet_reviews SET data=? WHERE id=?", (json.dumps(review), review["id"])
            )
        if review["decision"] == "allowed-once":
            operation = self.prepare(review["scope"]["input"], root_session)
            review.update(operation=operation, job_id=operation["id"])
            with self.registry.db() as db:
                db.execute(
                    "UPDATE fleet_reviews SET data=? WHERE id=?", (json.dumps(review), review["id"])
                )
        return review

    def monitor(self, root_session):
        result = []
        for operation in self.operation_rows():
            if operation.get("root_session") != root_session or operation.get("notified"):
                continue
            if operation["status"] in {"submitted", "running", "unknown"}:
                try:
                    operation = self.refresh(operation["id"])
                except (ValueError, OSError):
                    continue
            if operation["status"] in {"completed", "failed", "cancelled"}:
                result.append(operation)
        return result

    def notified(self, operation_id, root_session):
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM fleet_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown asset operation")
            operation = json.loads(row[0])
            if operation.get("root_session") != root_session:
                raise ValueError("Asset notification belongs to another root")
            operation["notified"] = True
            db.execute(
                "UPDATE fleet_operations SET data=? WHERE id=?",
                (json.dumps(operation), operation_id),
            )
        return {"notified": True}

    def update_operation(self, value):
        with self.registry.db() as db:
            db.execute(
                "UPDATE fleet_operations SET data=? WHERE id=?", (json.dumps(value), value["id"])
            )

    def refresh(self, operation_id, cancel=False, reconcile=False):
        value = self.operation(operation_id)
        if value["status"] in {"completed", "failed", "cancelled"}:
            return value
        asset = self.asset(value["asset_id"])
        executor = value.get("executor_snapshot") or self.executor(value["server_id"], cleanup=True)
        row = self.transport.call(
            executor["server_snapshot"],
            executor,
            "reconcile" if reconcile else "cancel" if cancel else "status",
            {"operation_id": operation_id},
        )
        value.update(
            status=row["status"],
            node={
                k: row[k]
                for k in [
                    "result",
                    "error",
                    "error_type",
                    "progress",
                    "pid",
                    "start_ticks",
                    "boot_id",
                ]
                if k in row
            },
        )
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = json.loads(
                db.execute(
                    "SELECT data FROM fleet_operations WHERE id=?", (operation_id,)
                ).fetchone()[0]
            )
            if latest["status"] in {"completed", "failed", "cancelled"}:
                return latest
            value["notified"] = latest.get("notified", False)
            db.execute(
                "UPDATE fleet_operations SET data=? WHERE id=?", (json.dumps(value), operation_id)
            )
        if row["status"] in {"completed", "failed", "cancelled"}:
            with self.registry.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT revision,data FROM fleet_assets WHERE id=?", (asset["asset_id"],)
                ).fetchone()
                actual = json.loads(current["data"])
                if (
                    current["revision"] == value["revision"]
                    and actual.get("operation_id") == operation_id
                ):
                    actual.update(
                        status="ready" if row["status"] == "completed" else row["status"],
                        prepared=row.get("result"),
                        operation_id=None,
                    )
                    db.execute(
                        "UPDATE fleet_assets SET data=? WHERE id=?",
                        (json.dumps(actual), asset["asset_id"]),
                    )
        return value

    def catalog(self):
        from pilot_core.resource_paths import layout

        with self.registry.db() as db:
            operations = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT data FROM fleet_operations ORDER BY rowid DESC LIMIT 50"
                )
            ]
        with self.registry.db() as db:
            proposals = [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM fleet_proposals ORDER BY rowid DESC LIMIT 50")
            ]
        registered = self.rows("fleet_assets")
        groups = {}
        for asset in registered:
            prepared = asset.get("prepared") or {}
            source = asset["source"]
            identity = (
                {"kind": "model", "repository": source["location"], "revision": source["revision"]}
                if asset["kind"] == "model" and source["kind"] == "huggingface"
                else {
                    "kind": asset["kind"],
                    "digest": prepared.get("digest")
                    or prepared.get("manifest", {}).get("digest")
                    or source["location"],
                }
            )
            key = digest(identity)
            group = groups.setdefault(
                key, {"identity": identity, "label": asset["label"], "locations": []}
            )
            group["locations"].append(
                {
                    "asset_id": asset["asset_id"],
                    "server_id": asset["server_id"],
                    "status": asset["status"],
                    "path": prepared.get("path"),
                    "manifest_digest": prepared.get("manifest", {}).get("digest"),
                }
            )
        for group in groups.values():
            manifests = {v["manifest_digest"] for v in group["locations"] if v["manifest_digest"]}
            group["identity_consistent"] = len(manifests) <= 1 if manifests else None
        return {
            "assets": registered,
            "logical_assets": list(groups.values()),
            "stacks": self.rows("fleet_stacks"),
            "executors": self.executors(),
            "operations": operations,
            "proposals": proposals,
            "resource_paths": layout(self.root),
            "host_executor_supported": __import__("sys").platform == "linux",
            "node_readiness": self.node_readiness(),
        }

    def propose(self, kind, args, root_session):
        value = (
            AssetSave.model_validate(args).model_dump()
            if kind == "asset"
            else StackSave.model_validate(args).model_dump()
        )
        request_id = value.get("request_id") or digest(
            {"root": root_session, "kind": kind, "value": value}
        )
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT data FROM fleet_proposals WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                proposal = json.loads(old[0])
                if proposal["root_session"] != root_session or proposal["payload"] != value:
                    raise ValueError("登记提案请求冲突")
                return proposal
            proposal = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "root_session": root_session,
                "payload": value,
                "digest": digest(value),
                "status": "pending",
            }
            db.execute(
                "INSERT INTO fleet_proposals VALUES(?,?,?,?)",
                (proposal["id"], request_id, root_session, json.dumps(proposal)),
            )
        return proposal

    def register_proposal(self, args):
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT data FROM fleet_proposals WHERE id=?", (args["proposal_id"],)
            ).fetchone()
            if not old:
                raise ValueError("未知登记提案")
            proposal = json.loads(old[0])
            if proposal["digest"] != args["expected_digest"]:
                raise ValueError("提案版本变化或已经结算")
            if proposal["status"] == "registered" and args["decision"] == "register":
                return proposal
            if proposal["status"] not in {"pending", "registering"}:
                raise ValueError("提案已经结算")
            if proposal["status"] == "registering" and args["decision"] != "register":
                raise ValueError("登记正在结算，先核对原决定")
            if args["decision"] not in {"register", "reject"}:
                raise ValueError("无效登记决定")
            proposal["status"] = "registering" if args["decision"] == "register" else "rejected"
            db.execute(
                "UPDATE fleet_proposals SET data=? WHERE id=?",
                (json.dumps(proposal), proposal["id"]),
            )
        if args["decision"] == "register":
            try:
                result = (
                    self.save(proposal["payload"], proposal_id=proposal["id"])
                    if proposal["kind"] == "asset"
                    else self.save_stack(proposal["payload"], proposal_id=proposal["id"])
                )
                proposal.update(status="registered", result=result)
            except Exception:
                with self.registry.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = json.loads(
                        db.execute(
                            "SELECT data FROM fleet_proposals WHERE id=?", (proposal["id"],)
                        ).fetchone()[0]
                    )
                    if current["status"] == "registered":
                        return current
                    proposal["status"] = "pending"
                    db.execute(
                        "UPDATE fleet_proposals SET data=? WHERE id=?",
                        (json.dumps(proposal), proposal["id"]),
                    )
                raise
            with self.registry.db() as db:
                db.execute(
                    "UPDATE fleet_proposals SET data=? WHERE id=?",
                    (json.dumps(proposal), proposal["id"]),
                )
        return proposal

    @staticmethod
    def finish_proposal(db, proposal_id, result):
        proposal = json.loads(
            db.execute("SELECT data FROM fleet_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        )
        if proposal["status"] != "registering":
            raise ValueError("登记提案认领状态变化")
        proposal.update(status="registered", result=result)
        db.execute(
            "UPDATE fleet_proposals SET data=? WHERE id=?", (json.dumps(proposal), proposal_id)
        )

    def record_runtime(self, task, candidate, result, evidence_sha256):
        """Only independent quality/identity acceptance generates this receipt."""
        accepted = result.get("identity", {})
        if (
            not result.get("passed")
            or not accepted.get("passed")
            or accepted.get("runtime") != "remote-docker"
        ):
            return
        image = next(
            (i for i in self.projected()[1] if i["id"] == task["plan"]["image_asset"]), None
        )
        model = next(
            (m for m in self.projected()[0] if m["id"] == task["plan"]["model_asset"]), None
        )
        if (
            not image
            or not model
            or accepted.get("image") != image["digest"]
            or accepted.get("model_manifest") != model["manifest_digest"]
            or accepted.get("config_digest") != candidate["digest"]
        ):
            raise ValueError("栈验收证据与冻结资源不一致")
        config = model["manifest"]["config"]
        receipt = {
            "task_id": task["id"],
            "job_id": result["job_id"],
            "observed_at": result["observed_at"],
            "model_type": config.get("model_type"),
            "quantization": (config.get("quantization_config") or {}).get("quant_method", "none"),
            "image_digest": image["digest"],
            "platform": image["platform"],
            "architecture": image["stack_recipe"]["architecture"],
            "model_manifest": model["manifest_digest"],
            "evidence_sha256": evidence_sha256,
            "protocol": result.get("protocol"),
            "scope": "quality_and_identity",
        }
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM fleet_stacks WHERE id=?", (image["stack_recipe"]["id"],)
            ).fetchone()
            value = json.loads(row[0])
            if value["recipe"] != image["stack_recipe"]:
                raise ValueError("栈配方在验收期间变化")
            value["runtime_receipts"] = [
                r for r in value.get("runtime_receipts", []) if r["job_id"] != receipt["job_id"]
            ] + [receipt]
            db.execute(
                "UPDATE fleet_stacks SET data=? WHERE id=?",
                (json.dumps(value), image["stack_recipe"]["id"]),
            )

    def cleanup_executor(self, args):
        server_id = args["server_id"]
        if self.dependent_node_tasks(server_id):
            raise ValueError("节点有软件/网络/PD工作区未清理，先结算并归档任务")
        executor = self.executor(server_id, cleanup=True)
        from .store import InfraStore

        if any(
            t["plan"]["machine_id"] == server_id
            and (t["state"].get("active_job") or t["state"].get("service_candidate"))
            for t in InfraStore(self.root).list()
        ):
            raise ValueError("先结算任务并停止自有服务")
        snapshot = self.transport.call(executor["server_snapshot"], executor, "export", {})
        archive = (
            self.root / ".local/fleet-archives" / (server_id + "-" + uuid.uuid4().hex + ".json")
        )
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        from repopilot.storage import atomic_json

        atomic_json(archive, snapshot)
        archive.chmod(0o600)
        result = self.transport.call(
            executor["server_snapshot"],
            executor,
            "cleanup_executor",
            {"owner_namespace": executor["owner_namespace"]},
        )
        if not result.get("restored"):
            raise ValueError("节点清理未确认")
        with self.registry.db() as db:
            db.execute("BEGIN IMMEDIATE")
            executor["enabled"] = False
            db.execute(
                "UPDATE fleet_executors SET data=? WHERE server_id=?",
                (json.dumps(executor), server_id),
            )
            for row in db.execute("SELECT id,data FROM fleet_assets").fetchall():
                asset = json.loads(row["data"])
                if asset["server_id"] == server_id:
                    asset.update(status="registered", prepared=None, operation_id=None)
                    db.execute(
                        "UPDATE fleet_assets SET data=? WHERE id=?", (json.dumps(asset), row["id"])
                    )
        return {
            **result,
            "archive_sha256": digest(snapshot),
            "archive": str(archive.relative_to(self.root)),
        }

    def resolve_image(self, args):
        server = self.registry.get(args["server_id"])
        executor = self.executor(server["id"])
        return self.transport.call(
            server, executor, "resolve_image", {"reference": args["reference"]}
        )

    def resolve_model(self, args):
        from .fleet_contracts import ModelMetadata
        from .fleet_worker import resolve_model

        request = ModelMetadata.model_validate(args)
        if not request.server_id:
            return resolve_model(request.endpoint, request.repository, request.revision)
        server = self.registry.get(request.server_id)
        executor = self.executor(server["id"])
        code = """import hashlib,json,runpy,sys
from pathlib import Path
v=json.load(sys.stdin);p=Path(v['worker'])
assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==v['worker_sha256']
ns=runpy.run_path(str(p))
result=ns['resolve_model'](v['endpoint'],v['repository'],v['revision'])
print(json.dumps({'ok':True,'value':result}))
"""
        return self.transport.exchange(
            server,
            ["python3", "-B", "-c", code],
            {
                "worker": executor["root"] + "/worker.py",
                "worker_sha256": executor["probe"]["worker_sha256"],
                "endpoint": request.endpoint,
                "repository": request.repository,
                "revision": request.revision,
            },
            timeout=80,
        )["value"]

    def match(self, server_id, asset_id, capabilities=()):
        server = self.registry.get(server_id)
        asset = self.asset(asset_id)
        if asset["server_id"] != server_id or asset["kind"] != "model":
            raise ValueError("请选择目标服务器上的模型登记")
        observation = self.transport.transport.observe(server)
        if asset.get("prepared"):
            config = asset["prepared"]["manifest"]["config"]
        elif asset["source"]["kind"] == "huggingface":
            source = asset["source"]
            config = self.resolve_model(
                {
                    "server_id": server_id,
                    "endpoint": source["endpoint"],
                    "repository": source["location"],
                    "revision": source["revision"],
                }
            )["config"]
        else:
            raise ValueError("先核验现有目录的模型配置")
        return {
            "observation": observation,
            "candidates": select_stacks(
                self.rows("fleet_stacks"),
                observation,
                config,
                capabilities,
            ),
        }

    def projected(self):
        models = []
        images = []
        stacks = {s["recipe"]["id"]: s["recipe"] for s in self.rows("fleet_stacks")}
        for a in self.rows("fleet_assets"):
            p = a.get("prepared")
            if a["status"] != "ready" or not p:
                continue
            if a["kind"] == "model":
                models.append(
                    {
                        "id": a["asset_id"],
                        "label": a["label"],
                        "server_id": a["server_id"],
                        "path": p["path"],
                        "manifest_digest": p["manifest"]["digest"],
                        "manifest": p["manifest"],
                        "fleet_asset_revision": a["revision"],
                    }
                )
            elif a["stack_id"] in stacks:
                recipe = stacks[a["stack_id"]]
                environment = p.get("environment", {})
                if not environment.get("modules", {}).get(recipe["engine"]):
                    continue
                if any(
                    environment.get("packages", {}).get(c["package"]) != c["version"]
                    for c in recipe.get("runtime_versions", [])
                ):
                    continue
                images.append(
                    {
                        "id": a["asset_id"],
                        "label": a["label"],
                        "server_id": a["server_id"],
                        "digest": p["digest"],
                        "backend": "docker",
                        "platform": recipe["platform"],
                        "engine": recipe["engine"],
                        "stack_recipe": recipe,
                        "environment": p.get("environment", {}),
                        "fleet_asset_revision": a["revision"],
                    }
                )
        return models, images
