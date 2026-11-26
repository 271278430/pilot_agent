"""Transactional business state; full conversations remain in official Sessions."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from repopilot.storage import atomic_json, now

from .contracts import InfraPlan


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class InfraStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "runs/pilot-infra"
        self.base.mkdir(parents=True, exist_ok=True)
        if self.base.is_symlink() or not self.base.resolve().is_relative_to(self.root):
            raise ValueError("Infra store escapes project")
        self.database = self.base / "infra.sqlite3"
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, request_id TEXT UNIQUE, plan TEXT, state TEXT, revision INTEGER, created TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, data TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS bindings(session_id TEXT PRIMARY KEY, task_id TEXT, kind TEXT);
            CREATE TABLE IF NOT EXISTS controls(task_id TEXT, request_id TEXT, payload TEXT, PRIMARY KEY(task_id, request_id));
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, task_id TEXT, action TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS locks(resource TEXT PRIMARY KEY, task_id TEXT);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def directory(self, task_id):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise ValueError("Invalid task identity")
        path = self.base / task_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.base):
            raise ValueError("Task directory escape")
        return path

    def path(self, task_id, relative):
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts or re.match(r"^[A-Za-z]:", str(relative)):
            raise ValueError("Task path escape")
        base = self.directory(task_id)
        path = base / part
        for ancestor in [path, *path.parents]:
            if ancestor == self.base:
                break
            if ancestor.is_symlink():
                raise ValueError("Task symlink denied")
        if not path.resolve().is_relative_to(base):
            raise ValueError("Task path escape")
        return path

    def get(self, task_id):
        self.directory(task_id)
        with self.db() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown Infra task")
        return {
            "id": row["id"],
            "plan": json.loads(row["plan"]),
            "state": json.loads(row["state"]),
            "revision": row["revision"],
            "created": row["created"],
        }

    def list(self):
        with self.db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tasks ORDER BY created DESC")]
        return [self.get(value) for value in ids]

    def create(self, request):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            task_id = self.create_record(c, request)
        task = self.get(task_id)
        self.sync(task)
        return task

    def create_record(self, c, request):
        """Create under the caller's transaction, including resource activation."""
        payload = request.plan.model_dump()
        previous = c.execute(
            "SELECT id, plan FROM tasks WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if previous:
            if json.loads(previous["plan"]) != payload:
                raise ValueError("Request ID reused with different plan")
            task_id = previous["id"]
        else:
            task_id = uuid.uuid4().hex
            self.directory(task_id).mkdir(parents=True)
            state = {
                "status": "draft",
                "phase": "plan",
                "candidates": [],
                "experiments": [],
                "incidents": [],
                "hypotheses": [],
                "strategy": {"mode": "single", "reason": "默认单主控"},
                "strategy_history": [],
                "outcome": {},
                "repairs": 0,
                "active_job": None,
                "session_id": None,
                "budget_revision": 1,
                "max_tokens": payload["max_tokens"],
            }
            c.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                (task_id, request.request_id, json.dumps(payload), json.dumps(state), 1, now()),
            )
        return task_id

    def sync(self, task):
        # A caller may hold an older task snapshot. Serialize with database
        # writers and publish the current state, not that snapshot's budget.
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if not row:
                raise ValueError("Unknown Infra task")
            state = json.loads(row[0])
            atomic_json(
                self.path(task["id"], "budget-policy.json"),
                {"max_tokens": state["max_tokens"], "revision": state["budget_revision"]},
            )

    def update(self, task_id, **values):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown Infra task")
            state = {**json.loads(row[0]), **values}
            c.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(state), task_id))
        task = self.get(task_id)
        self.sync(task)
        return task

    def revise(self, task_id, request):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("Unknown task")
            payload = request.model_dump()
            old = c.execute(
                "SELECT payload FROM controls WHERE task_id=? AND request_id=?",
                (task_id, request.request_id),
            ).fetchone()
            if old:
                if json.loads(old[0]) != payload:
                    raise ValueError("Control ID reused with different payload")
            else:
                state = json.loads(row["state"])
                if row["revision"] != request.expected_revision:
                    raise ValueError("Plan revision conflict; reload current task")
                if state["status"] != "draft":
                    raise ValueError("Only an unstarted draft can be revised")
                c.execute(
                    "UPDATE tasks SET plan=?, revision=revision+1 WHERE id=?",
                    (request.plan.model_dump_json(), task_id),
                )
                state.update(
                    max_tokens=request.plan.max_tokens, budget_revision=state["budget_revision"] + 1
                )
                c.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(state), task_id))
                c.execute(
                    "INSERT INTO controls VALUES(?,?,?)",
                    (task_id, request.request_id, json.dumps(payload)),
                )
        task = self.get(task_id)
        self.sync(task)
        return task

    def bind(self, session_id, task_id, kind="execution"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", session_id):
            raise ValueError("Invalid official Session identity")
        self.get(task_id)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT task_id FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if old and old[0] != task_id:
                raise ValueError("Session already bound to another Infra task")
            row = c.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            state = json.loads(row[0])
            other = c.execute(
                "SELECT session_id FROM bindings WHERE task_id=? AND session_id<>?",
                (task_id, session_id),
            ).fetchone()
            if other or state.get("session_id") not in {None, session_id}:
                raise ValueError(
                    "Infra task already has a root Session; open that task or use a new request ID"
                )
            c.execute("INSERT OR IGNORE INTO bindings VALUES(?,?,?)", (session_id, task_id, kind))
            state["session_id"] = session_id
            c.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(state), task_id))

    def binding(self, session_id):
        with self.db() as c:
            row = c.execute(
                "SELECT task_id FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
        return self.get(row[0]) if row else None

    def event(self, task_id, kind, data):
        with self.db() as c:
            c.execute(
                "INSERT INTO events(task_id,kind,data,created) VALUES(?,?,?,?)",
                (task_id, kind, json.dumps(data, ensure_ascii=False), now()),
            )

    def events(self, task_id):
        with self.db() as c:
            return [
                {**dict(row), "data": json.loads(row["data"])}
                for row in c.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,)
                )
            ]

    def acquire(self, task):
        resources = [f"{task['plan']['machine_id']}:gpu:{gpu}" for gpu in task["plan"]["gpu_ids"]]
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            for resource in resources:
                row = c.execute(
                    "SELECT task_id FROM locks WHERE resource=?", (resource,)
                ).fetchone()
                if row and row[0] != task["id"]:
                    raise ValueError("GPU resource is owned by another Pilot task")
            for resource in resources:
                c.execute("INSERT OR IGNORE INTO locks VALUES(?,?)", (resource, task["id"]))

    def release(self, task_id):
        with self.db() as c:
            c.execute("DELETE FROM locks WHERE task_id=?", (task_id,))

    def claim(self, task_id, action, data):
        operation_id = uuid.uuid4().hex
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            pending = c.execute(
                "SELECT id FROM operations WHERE task_id=? AND status IN ('submitted','running','unknown')",
                (task_id,),
            ).fetchone()
            if pending:
                raise ValueError("Inspect the existing operation before another mutation")
            c.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?)",
                (operation_id, task_id, action, "submitted", json.dumps(data)),
            )
        return operation_id

    def operations(self, task_id):
        with self.db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM operations WHERE task_id=? ORDER BY rowid", (task_id,)
                )
            ]
        return [self.operation(i) for i in ids]

    def start_operation(self, operation_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,data FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not row or row["status"] != "submitted":
                raise ValueError("Worker cannot replay an already claimed operation")
            data = {**json.loads(row["data"]), "started_epoch": time.time()}
            db.execute(
                "UPDATE operations SET status=?,data=? WHERE id=?",
                ("running", json.dumps(data), operation_id),
            )
        return self.operation(operation_id)

    def operation(self, operation_id, status=None, **values):
        with self.db() as c:
            row = c.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise ValueError("Unknown operation")
            data = {**json.loads(row["data"]), **values}
            if status:
                c.execute(
                    "UPDATE operations SET status=?,data=? WHERE id=?",
                    (status, json.dumps(data), operation_id),
                )
        return {
            "id": operation_id,
            "task_id": row["task_id"],
            "action": row["action"],
            "status": status or row["status"],
            "data": data,
        }


class AssetRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / ".local/infra-assets.json"
        self.authority_validator = None

    def read(self):
        from .resource_registry import ResourceRegistry

        value = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.is_file()
            else {"machines": [], "models": [], "images": []}
        )
        value["machines"] = ResourceRegistry(self.root).asset_machines(value["machines"])
        from .engine_source import catalog

        value["engine_source_profiles"] = catalog(self.root)
        from .asset_service import AssetService

        models, images = AssetService(self.root).projected()
        value["models"] += models
        value["images"] += images
        def on_node(asset, machine):
            return (
                asset.get("server_id") == machine["id"]
                if asset.get("server_id")
                else machine.get("transport", "local") == "local"
            )

        value["ready"] = any(
            m.get("executor_available")
            and m.get("enabled", True)
            and m.get("permission_mode", "review") == "review"
            and m.get("worker_version_current", True)
            and {"inference", "containers"}.issubset(
                m.get("permitted_operations", ["inference", "containers"])
            )
            and (m.get("allowed_gpu_ids") or m.get("approvable_gpu_ids"))
            and any(on_node(a, m) for a in value["models"])
            and any(
                on_node(a, m)
                and a.get("backend", "docker") == "docker"
                and a.get("engine", "sglang") in {"sglang", "vllm"}
                for a in value["images"]
            )
            for m in value["machines"]
        )
        return value

    def validate(self, plan, cleanup=False):
        plan = InfraPlan.model_validate(plan)
        data = self.read()
        machine = next((m for m in data["machines"] if m["id"] == plan.machine_id), None)
        model = next(
            (
                m
                for m in data["models"]
                if m["id"] == plan.model_asset
                and m.get("server_id", plan.machine_id) == plan.machine_id
            ),
            None,
        )
        image = next(
            (
                m
                for m in data["images"]
                if m["id"] == plan.image_asset
                and m.get("server_id", plan.machine_id) == plan.machine_id
            ),
            None,
        )
        if not machine or not model or not image:
            raise ValueError("Machine, model and image must be registered before execution")
        if model.get("server_id") != image.get("server_id"):
            raise ValueError("模型与镜像必须位于同一目标节点")
        if not cleanup and image.get("backend", "docker") != "docker":
            raise ValueError("Model deployment requires Docker; host-process runtimes are retired")
        if not cleanup and image.get("engine", "sglang") not in {"sglang", "vllm"}:
            raise ValueError("Engine has no admitted container adapter")
        if not cleanup:
            from .container_runtime import admit_backend

            admit_backend(image, plan.serving.attention_backend)
        if cleanup:
            pass
        elif plan.authorization_id:
            if self.authority_validator is None:
                raise ValueError("Reviewed task authority is unavailable")
            self.authority_validator(plan)
        elif not set(plan.gpu_ids).issubset(machine["allowed_gpu_ids"]):
            raise ValueError("GPU is outside the machine execution quota")
        if (not cleanup and not machine.get("executor_available", True)) or (
            not cleanup and not machine.get("enabled", True)
        ):
            raise ValueError("This server has no admitted inference executor")
        if (not model.get("server_id") and not Path(model["path"]).is_dir()) or not model.get(
            "manifest_digest"
        ):
            raise ValueError("Registered model manifest is unavailable")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image["digest"]):
            raise ValueError("Image must be bound to an immutable Docker image digest")
        if plan.engine_source_profile and not cleanup:
            if image.get("server_id"):
                raise ValueError("远端推理任务尚不支持引擎源码制品加载；使用节点固定镜像或独立源码工作台")
            from .engine_source import profile

            recipe = profile(self.root, plan.engine_source_profile)
            if (
                image.get("backend", "docker") != "docker"
                or image.get("engine", "sglang") != "sglang"
                or recipe["image_id"] != image["id"]
            ):
                raise ValueError("Engine source recipe differs from the selected runtime")
        if image.get("server_id") and not cleanup:
            from .asset_service import AssetService

            fleet = AssetService(self.root)
            executor = fleet.executor(plan.machine_id)
            if image.get("platform") != executor["platform"]:
                raise ValueError("镜像栈与目标设备平台不匹配")
            if model.get("server_id") != plan.machine_id:
                raise ValueError("目标节点必须有已核验的模型资产")
            if (
                model.get("manifest", {}).get("config", {}).get("model_type")
                not in image["stack_recipe"]["model_types"]
            ):
                raise ValueError("模型类型不在兼容配方的支持证据范围")
            config = model["manifest"]["config"]
            if (config.get("quantization_config") or {}).get("quant_method", "none") not in image[
                "stack_recipe"
            ].get("quantizations", ["none"]):
                raise ValueError("量化方式不在兼容栈支持范围")
            environment = image.get("environment", {})
            if not environment.get("modules", {}).get(image["engine"]):
                raise ValueError("节点镜像未核验对应引擎依赖")
            for constraint in image["stack_recipe"].get("runtime_versions", []):
                if (
                    environment.get("packages", {}).get(constraint["package"])
                    != constraint["version"]
                ):
                    raise ValueError("镜像依赖版本不符合配方：" + constraint["package"])
        return machine, model, image
