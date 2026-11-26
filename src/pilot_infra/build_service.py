"""Durable reviewed source/environment workbench, with pinned SSH executors."""

import json
import re
import shlex
import uuid
from pathlib import Path, PurePosixPath

import paramiko

from repopilot.storage import now

from . import build_worker
from .build_contracts import (
    BuildEdit,
    BuildPatch,
    BuildPlan,
    BuildRead,
    BuildRun,
    BuildTarget,
    RecipeSave,
)
from .build_validation import with_validation
from .registered_workbench import RegisteredWorkbench
from .store import InfraStore, digest


class BuildService:
    def __init__(self, root, remote_factory=None):
        self.root = Path(root).resolve()
        self.store = InfraStore(root)
        self.remote_factory = remote_factory or RegisteredWorkbench
        with self.store.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS build_tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,session_id TEXT UNIQUE,data TEXT);
            CREATE TABLE IF NOT EXISTS build_operations(id TEXT PRIMARY KEY,task_id TEXT,request_id TEXT,payload TEXT,result TEXT,UNIQUE(task_id,request_id));
            CREATE TABLE IF NOT EXISTS software_recipes(id TEXT PRIMARY KEY,revision INTEGER,data TEXT);
            """)

    def profiles(self):
        path = self.root / ".local/workbench-profiles.json"
        old = json.loads(path.read_text(encoding="utf-8"))["profiles"] if path.is_file() else []
        with self.store.db() as db:
            rows = [
                {**json.loads(r[2]), "revision": r[1]}
                for r in db.execute("SELECT * FROM software_recipes")
            ]
        keys = {r["id"] for r in rows}
        return [r for r in old if r["id"] not in keys] + rows

    def save_profile(self, args):
        from .resource_registry import ResourceRegistry

        request = RecipeSave.model_validate(args)
        ResourceRegistry(self.root).get(request.recipe.server_id)
        value = request.recipe.model_dump()
        if value.get("template"):
            from .software_templates import material

            builtin = material(value["template"], value["python"])
            value.update(kind=builtin["kind"], platform=builtin["platform"], steps=builtin["steps"])
        self.materials(value)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM software_recipes WHERE id=?", (value["id"],)
            ).fetchone()
            if request.expected_revision != (row[0] if row else 0):
                raise ValueError("软件配方版本冲突")
            db.execute(
                "INSERT OR REPLACE INTO software_recipes VALUES(?,?,?)",
                (value["id"], request.expected_revision + 1, json.dumps(value)),
            )
        return {**value, "revision": request.expected_revision + 1}

    def recommend(self, args):
        import math

        from .asset_service import AssetService
        from .build_contracts import SoftwareRecommend
        from .fleet_transport import FleetTransport
        from .node_authority import require_operation
        from .resource_assessment import gpu_occupancy_known
        from .resource_registry import ResourceRegistry

        profile = self.profile(SoftwareRecommend.model_validate(args).profile_id)
        server = ResourceRegistry(self.root).get(profile["server_id"])
        reasons = []
        try:
            AssetService(self.root).executor(server["id"])
        except ValueError as error:
            reasons.append(str(error))
        try:
            require_operation(server, "software")
            require_operation(server, "containers")
            if profile.get("kind") == "profile":
                require_operation(server, "profiling")
            if profile.get("kind") == "pd":
                require_operation(server, "inference")
        except ValueError as error:
            reasons.append(str(error))
        observation = FleetTransport(self.root).transport.observe(server)
        needed = (
            2
            if profile.get("kind") == "pd"
            else 1
            if profile.get("platform", "cpu") != "cpu"
            else 0
        )
        occupancy_known = gpu_occupancy_known(observation)
        free = [
            g
            for g in observation["gpus"]
            if occupancy_known and g["index"] in server["policy"]["approvable_gpu_ids"]
            and type(g.get("free_mib")) in {int, float}
            and math.isfinite(g["free_mib"])
            and g["free_mib"] >= profile.get("minimum_gpu_mib", 2048)
            and not any(p["gpu_uuid"] == g["uuid"] for p in observation["gpu_processes"])
        ]
        if needed and profile.get("platform") != observation.get("accelerator_platform"):
            reasons.append("软件配方平台与节点硬件不符")
        if needed and not occupancy_known:
            reasons.append("GPU进程占用未知，请检查设备查询并重新观测")
        if len(free) < needed:
            reasons.append("没有足够的已授权、无业务占用设备；保留现有服务，等待资源窗口")
        ids = [g["index"] for g in free[:needed]]
        return {
            "available": not reasons,
            "server_id": server["id"],
            "profile_id": profile["id"],
            "gpu_ids": ids,
            "reasons": reasons,
            "observed_at": observation.get("received_at"),
            "limits": {
                "cpu_cores": 2 if profile.get("kind") == "pd" else 1,
                "memory_gib": 4 if profile.get("kind") == "pd" else 2,
                "duration_seconds": profile["timeout"],
            },
            "scope": "same_node_1P1D_debug"
            if profile.get("kind") == "pd"
            else profile.get("kind", "build"),
            "note": "建议不是资源授权；执行前重新检查身份、配额、占用和固定材料",
        }

    def profile(self, profile_id):
        profile = next((p for p in self.profiles() if p["id"] == profile_id), None)
        if not profile:
            raise ValueError("Executor profile is not registered")
        if not 1 <= profile["timeout"] <= 600 or not 1 <= len(profile["steps"]) <= 8:
            raise ValueError("Build must have bounded stages")
        for step in profile["steps"]:
            if (
                not 1 <= step["timeout"] <= profile["timeout"]
                or not step["argv"]
                or not isinstance(step.get("stage"), str)
                or not 1 <= len(step["stage"]) <= 80
            ):
                raise ValueError("Invalid bounded build step")
        return profile

    def present(self, task):
        if "pipeline_stages" not in task:
            snapshot = self.root / ".local/build-executors" / (task["id"] + ".json")
            if snapshot.is_file():
                profile = json.loads(snapshot.read_text(encoding="utf-8"))["profile"]
                if digest(profile) == task["profile_digest"]:
                    task = {**task, "pipeline_stages": [s["stage"] for s in profile["steps"]]}
        return with_validation(task)

    def catalog(self):
        profiles = [
            {
                **{k: p[k] for k in ("id", "label", "server_id", "image_digest")},
                "kind": p.get("kind", "build"),
                "platform": p.get("platform", "cpu"),
                "revision": p.get("revision", 0),
            }
            for p in self.profiles()
        ]
        with self.store.db() as db:
            tasks = [
                self.present(json.loads(r[0]))
                for r in db.execute("SELECT data FROM build_tasks ORDER BY rowid DESC")
            ]
        return {
            "profiles": profiles,
            "tasks": tasks,
            "gpu_execution": any(p["platform"] != "cpu" for p in profiles),
        }

    def task(self, task_id):
        BuildTarget(task_id=task_id)
        with self.store.db() as db:
            row = db.execute("SELECT data FROM build_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown build task")
        return self.present(json.loads(row[0]))

    def bound(self, session_id):
        with self.store.db() as db:
            row = db.execute(
                "SELECT data FROM build_tasks WHERE session_id=?", (session_id,)
            ).fetchone()
        return self.present(json.loads(row[0])) if row else None

    def update(self, task_id, **values):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM build_tasks WHERE id=?", (task_id,)).fetchone()
            task = {**json.loads(row[0]), **values}
            db.execute("UPDATE build_tasks SET data=? WHERE id=?", (json.dumps(task), task_id))
        return self.present(task)

    def create(self, args, session_id=None):
        request = BuildPlan.model_validate(args)
        if session_id:
            from .network_service import NetworkService

            if NetworkService(self.root).bound(session_id):
                raise ValueError("Use a new Session; this root belongs to a network task")
        if session_id and self.store.binding(session_id):
            raise ValueError("Use a new Session; this root belongs to a serving task")
        profile = self.profile(request.profile_id)
        from .asset_service import AssetService
        from .resource_registry import ResourceRegistry

        server = ResourceRegistry(self.root).get(profile["server_id"])
        executor = AssetService(self.root).executor(server["id"])
        gpu = profile.get("platform", "cpu") != "cpu"
        if profile.get("kind") == "pd" and len(request.gpu_ids) != 2:
            raise ValueError("当前PD验证仅支持同节点两块独立CUDA设备")
        if gpu != bool(request.gpu_ids) or len(set(request.gpu_ids)) != len(request.gpu_ids):
            raise ValueError("GPU配方需明确独立设备；CPU配方不接收GPU配额")
        scope = {
            "server": server,
            "executor": executor,
            "gpu_ids": request.gpu_ids,
            "platform": profile.get("platform", "cpu"),
            "worker_sha256": __import__("hashlib")
            .sha256(Path(build_worker.__file__).read_bytes())
            .hexdigest(),
        }
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", profile["image_digest"]):
            raise ValueError("Image must be immutable")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT data,session_id FROM build_tasks WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if old:
                task = json.loads(old[0])
                if task["plan"] != request.model_dump() or old[1] != session_id:
                    raise ValueError("Build request identity changed")
                return self.present(task)
            if (
                session_id
                and db.execute(
                    "SELECT 1 FROM build_tasks WHERE session_id=?", (session_id,)
                ).fetchone()
            ):
                raise ValueError("This Session already has a build plan")
            task_id = uuid.uuid4().hex
            snapshots = self.root / ".local/build-executors"
            snapshots.mkdir(parents=True, exist_ok=True)
            materials = self.materials(profile)
            if profile.get("kind") == "pd":
                asset = AssetService(self.root).asset(profile["model_asset_id"])
                if (
                    asset["server_id"] != server["id"]
                    or asset["kind"] != "model"
                    or asset["status"] != "ready"
                ):
                    raise ValueError("PD模型未在所选节点准备并核验")
                materials["profile"].update(model=asset, cpu_cores=2, memory_gib=4)
            (snapshots / (task_id + ".json")).write_text(
                json.dumps({"profile": profile, "materials": materials, "node_scope": scope}),
                encoding="utf-8",
            )
            task = {
                "id": task_id,
                "plan": request.model_dump(),
                "status": "draft",
                "profile_digest": digest(profile),
                "materials_digest": digest(materials),
                "pipeline_stages": [s["stage"] for s in profile["steps"]],
                "candidates": [],
                "results": [],
                "active_operation": None,
                "created": now(),
                "gpu_execution": gpu,
                "kind": profile.get("kind", "build"),
                "node_scope_digest": digest(scope),
                "server_id": server["id"],
                "session_id": session_id,
                "source_archive_version": 1,
                "execution_scope": {
                    "server_id": server["id"],
                    "directory": str(PurePosixPath(executor["root"]) / "workspaces" / ("pilot-workbench-" + task_id)),
                    "gpu_ids": list(request.gpu_ids),
                    "platform": scope["platform"],
                    "image_digest": profile["image_digest"],
                    "cpu_cores": materials["profile"].get("cpu_cores", 1),
                    "memory_gib": materials["profile"].get("memory_gib", 2),
                    "timeout_seconds": profile["timeout"],
                },
            }
            db.execute(
                "INSERT INTO build_tasks VALUES(?,?,?,?)",
                (task_id, request.request_id, session_id, json.dumps(task)),
            )
        return self.present(task)

    def bind(self, task_id, session_id):
        self.task(task_id)
        if self.store.binding(session_id):
            raise ValueError("Use a new Session; this root belongs to a serving task")
        with self.store.db() as db:
            old = db.execute(
                "SELECT session_id FROM build_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            if old and old != session_id:
                raise ValueError("Build task already has a root")
            data = json.loads(
                db.execute("SELECT data FROM build_tasks WHERE id=?", (task_id,)).fetchone()[0]
            )
            data["session_id"] = session_id
            db.execute(
                "UPDATE build_tasks SET session_id=?,data=? WHERE id=?",
                (session_id, json.dumps(data), task_id),
            )
        return self.task(task_id)

    def materials(self, profile):
        if profile.get("template"):
            from .software_templates import material

            value = material(profile["template"], profile["python"])
            return {
                "files": value["files"],
                "checks": value["checks"],
                "profile": {
                    **{k: profile[k] for k in ("image_digest", "python", "timeout")},
                    "kind": value["kind"],
                    "platform": value["platform"],
                    "steps": value["steps"],
                },
            }

        def read(relative):
            path = self.root / relative
            from pilot_core.resource_paths import resource_root

            roots = [self.root / ".local/workbench-seeds", resource_root(self.root) / "sources"]
            if any(p.is_symlink() for p in [path, *path.parents]) or not any(
                path.resolve().is_relative_to(r.resolve()) for r in roots
            ):
                raise ValueError("Preparation asset must be a project-private regular file")
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("Preparation input must be a regular non-hardlinked source file")
            return path.read_text(encoding="utf-8")

        return {
            "files": {name: read(path) for name, path in profile["files"].items()},
            "checks": {name: read(path) for name, path in profile["checks"].items()},
            "profile": {
                **{k: profile[k] for k in ("image_digest", "python", "steps", "timeout")},
                "platform": profile.get("platform", "cpu"),
                "kind": profile.get("kind", "build"),
            },
        }

    def remote(self, task, cleanup=False):
        snapshot = self.root / ".local/build-executors" / (task["id"] + ".json")
        profile = (
            json.loads(snapshot.read_text(encoding="utf-8"))["profile"]
            if snapshot.is_file()
            else self.profile(task["plan"]["profile_id"])
        )
        if task["profile_digest"] != digest(profile):
            raise ValueError("Executor profile changed; review a new build plan")
        saved = json.loads(snapshot.read_text(encoding="utf-8")) if snapshot.is_file() else {}
        scope = saved.get("node_scope")
        if scope and digest(scope) != task.get("node_scope_digest"):
            raise ValueError("Node execution scope changed after review")
        if not scope and self.remote_factory is RegisteredWorkbench:
            if cleanup:
                return SSHWorkbench(self.root, profile, task["id"])
            raise ValueError("旧工作台需在原目录清理后，按已登记节点重新准备")
        return self.remote_factory(self.root, {**profile, "node_scope": scope}, task["id"])

    def ready(self, task):
        if task["status"] != "prepared" or task["active_operation"]:
            raise ValueError("Workspace is not prepared or has an unsettled operation")

    def prepare(self, task_id):
        task = self.task(task_id)
        if task["status"] != "draft":
            raise ValueError("Prepare requires an unstarted reviewed draft")
        self.update(task_id, status="preparing")
        try:
            with self.remote(task) as remote:
                remote.bootstrap()
                snapshot = self.root / ".local/build-executors" / (task["id"] + ".json")
                materials = (
                    json.loads(snapshot.read_text(encoding="utf-8"))["materials"]
                    if snapshot.is_file()
                    else self.materials(self.profile(task["plan"]["profile_id"]))
                )
                if task.get("materials_digest") and digest(materials) != task["materials_digest"]:
                    raise ValueError("Reviewed preparation materials changed")
                result = remote.call("prepare", materials)
            return self.update(
                task_id,
                status="prepared",
                files=result["files"],
                checks_manifest=result.get("checks_manifest"),
            )
        except Exception:
            self.update(
                task_id,
                status="unknown",
                error="Preparation interrupted; cleanup original workspace before retrying",
            )
            raise

    def read(self, args):
        request = BuildRead.model_validate(args)
        task = self.task(request.task_id)
        self.ready(task)
        with self.remote(task) as remote:
            return remote.call("read", request.model_dump(exclude={"task_id"}))

    def patch(self, args):
        request = BuildPatch.model_validate(args)
        task = self.task(request.task_id)
        self.ready(task)
        with self.remote(task) as remote:
            result = remote.call("patch", request.model_dump(exclude={"task_id"}))
        self.update(task["id"], files={**task["files"], request.path: result["sha256"]})
        return result

    def edit(self, args):
        request = BuildEdit.model_validate(args)
        task = self.task(request.task_id)
        self.ready(task)
        with self.remote(task) as remote:
            result = remote.call("write", request.model_dump(exclude={"task_id"}))
        files = {**task["files"], request.path: result["sha256"]}
        self.update(task["id"], files=files)
        return result

    def freeze(self, task_id):
        task = self.task(task_id)
        self.ready(task)
        candidate_id = "c" + str(len(task["candidates"]) + 1)
        with self.remote(task) as remote:
            result = remote.call("freeze", {"candidate_id": candidate_id})
        self.update(task_id, candidates=[*task["candidates"], result])
        if task.get("source_archive_version"):
            return self.export_source({"task_id": task_id, "candidate_id": candidate_id})
        return self.task(task_id)

    def export_source(self, args, downloading=False):
        from .build_contracts import SourceBundle
        from .source_archive import archive, download

        request = SourceBundle.model_validate(args)
        task = self.task(request.task_id)
        candidate = next(
            (c for c in task["candidates"] if c["candidate_id"] == request.candidate_id), None
        )
        if not candidate:
            raise ValueError("源码候选不存在")
        if downloading:
            return download(self, task, candidate)
        valid = False
        if candidate.get("source_archive"):
            try:
                download(self, task, candidate)
                valid = True
            except ValueError:
                pass
        if not valid:
            candidate = {**candidate, **archive(self, task, candidate)}
            task = self.update(
                task["id"],
                candidates=[
                    candidate if c["candidate_id"] == request.candidate_id else c
                    for c in task["candidates"]
                ],
            )
        return task

    def run(self, args):
        request = BuildRun.model_validate(args)
        task = self.task(request.task_id)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload,result FROM build_operations WHERE task_id=? AND request_id=?",
                (task["id"], request.request_id),
            ).fetchone()
            if old:
                if json.loads(old[0]) != request.model_dump():
                    raise ValueError("Build request reused for a different candidate")
                if old[1]:
                    return json.loads(old[1])
                raise ValueError("Original operation is unsettled; recover before retrying")
            current = json.loads(
                db.execute("SELECT data FROM build_tasks WHERE id=?", (task["id"],)).fetchone()[0]
            )
            self.ready(current)
            if request.candidate_id not in {c["candidate_id"] for c in current["candidates"]}:
                raise ValueError("Candidate is not frozen")
            if current.get("source_archive_version"):
                self.export_source(
                    {"task_id": task["id"], "candidate_id": request.candidate_id}, downloading=True
                )
            operation_id = uuid.uuid4().hex
            if current.get("gpu_execution"):
                for gpu in current["plan"]["gpu_ids"]:
                    key = current["server_id"] + ":gpu:" + str(gpu)
                    holder = db.execute(
                        "SELECT task_id FROM locks WHERE resource=?", (key,)
                    ).fetchone()
                    if holder and holder[0] != current["id"]:
                        raise ValueError("设备已由其他Pilot任务预留")
                    db.execute("INSERT OR REPLACE INTO locks VALUES(?,?)", (key, current["id"]))
            db.execute(
                "INSERT INTO build_operations VALUES(?,?,?,?,NULL)",
                (operation_id, task["id"], request.request_id, json.dumps(request.model_dump())),
            )
            current.update(active_operation=operation_id)
            db.execute(
                "UPDATE build_tasks SET data=? WHERE id=?", (json.dumps(current), task["id"])
            )
        try:
            with self.remote(task) as remote:
                result = remote.call(
                    "run",
                    {
                        "operation_id": operation_id,
                        "candidate_id": request.candidate_id,
                        "expected_checks": task.get("checks_manifest"),
                    },
                )
        except Exception:
            if self.task(task["id"])["status"] not in {"restored", "cleaning"}:
                self.update(
                    task["id"],
                    status="unknown",
                    error="Connection lost; inspect the original operation, do not replay",
                )
            raise
        return self.settle(task["id"], operation_id, result)

    def settle(self, task_id, operation_id, result):
        current = self.task(task_id)
        if result.get("status") in {"running", "unknown"}:
            if current["status"] not in {"restored", "cleaning"}:
                self.update(task_id, status="unknown")
            return result
        with self.store.db() as db:
            db.execute(
                "UPDATE build_operations SET result=? WHERE id=?",
                (json.dumps(result), operation_id),
            )
        task = self.task(task_id)
        self.store.release(task_id)
        self.update(
            task_id,
            status=task["status"] if task["status"] in {"restored", "cleaning"} else "prepared",
            active_operation=None,
            results=[*task["results"], result],
            error=None,
        )
        return result

    def recover(self, task_id):
        task = self.task(task_id)
        if not task["active_operation"]:
            raise ValueError("No unsettled build operation")
        with self.remote(task) as remote:
            result = remote.call("inspect", {"operation_id": task["active_operation"]})
        return self.settle(task_id, task["active_operation"], result)

    def cleanup(self, task_id):
        task = self.task(task_id)
        if task["status"] == "restored":
            return task
        if task["status"] == "draft":
            return self.update(task_id, status="restored", restored=True)
        if task.get("source_archive_version"):
            for candidate in task["candidates"]:
                self.export_source({"task_id": task_id, "candidate_id": candidate["candidate_id"]})
        self.update(task_id, status="cleaning")
        try:
            with self.remote(task, cleanup=True) as remote:
                result = remote.call("cleanup", {})
        except Exception:
            self.update(
                task_id, status="unknown", error="Cleanup not confirmed; inspect and retry cleanup"
            )
            raise
        self.store.release(task_id)
        return self.update(
            task_id, status="restored", restored=result["restored"], active_operation=None
        )

    def tool(self, name, session_id, role, args):
        if name == "workbench_profiles":
            return {k: v for k, v in self.catalog().items() if k != "tasks"}
        if name == "workbench_recommend":
            return self.recommend(args)
        if name == "workbench_plan":
            if role != "infra-lead":
                raise ValueError("Only lead can plan environment/source work")
            return {"build_task": self.create(args, session_id), "requires_start": True}
        task = self.task(args["task_id"])
        bound = self.bound(session_id)
        if not bound or bound["id"] != task["id"]:
            raise ValueError("Build task belongs to another root Session")
        if name == "workbench_status":
            return task
        if name == "workbench_read":
            return self.read({**args, "start_line": args.get("start_line") or 1})
        if name == "workbench_write":
            return self.edit(args)
        if name == "workbench_patch":
            return self.patch(args)
        if role != "infra-lead":
            raise ValueError("Only lead commits candidates and bounded builds")
        if name == "workbench_freeze":
            return self.freeze(task["id"])
        if name == "workbench_run":
            return self.run(args)
        if name == "workbench_cleanup":
            return self.cleanup(task["id"])
        raise ValueError("Unknown workbench tool")


class SSHWorkbench:
    def __init__(self, project, profile, task_id):
        self.project, self.profile, self.task_id = project, profile, task_id
        base = profile["remote_base"]
        if not base.startswith("/") or ".." in PurePosixPath(base).parts:
            raise ValueError("Remote base must be an approved absolute directory")
        self.directory = str(PurePosixPath(base) / ("pilot-workbench-" + task_id))

    def __enter__(self):
        rows = json.loads((self.project / ".local/workbench-servers.json").read_text())["servers"]
        server = next(s for s in rows if s["id"] == self.profile["server_id"])
        self.client = paramiko.SSHClient()
        self.client.load_host_keys(str(self.project / ".local" / (server["id"] + "-known_hosts")))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(
            server["host"],
            port=server["port"],
            username=server["username"],
            password=server["password"],
            look_for_keys=False,
            allow_agent=False,
            timeout=12,
            banner_timeout=12,
            auth_timeout=12,
        )
        return self

    def __exit__(self, *exc):
        self.client.close()

    def bootstrap(self):
        script = "import json,os,sys;from pathlib import Path;p=Path(sys.argv[1]);assert p.parent.resolve()==p.parent;os.mkdir(p,0o700);(p/'lease.json').write_text(json.dumps({'task_id':sys.argv[2],'root':str(p)}))"
        _, out, err = self.client.exec_command(
            shlex.join(["python3", "-c", script, self.directory, self.task_id]), timeout=15
        )
        out.read()
        if out.channel.recv_exit_status():
            error = err.read().decode("utf-8", errors="replace")
            raise ValueError(
                "Workspace create failed; no existing directory is adopted: " + error[-600:]
            )
        with self.client.open_sftp() as sftp:
            data = Path(build_worker.__file__).read_bytes()
            with sftp.file(self.directory + "/worker.py", "wb") as stream:
                stream.write(data)
            sftp.chmod(self.directory + "/worker.py", 0o600)

    def call(self, action, args):
        command = shlex.join(
            ["python3", self.directory + "/worker.py", action, self.directory, self.task_id]
        )
        if action == "cleanup":
            script = "import json,runpy,sys;from pathlib import Path;p=Path(sys.argv[1]);identity=sys.argv[2];sys.argv=[str(p/'worker.py'),'cleanup',str(p),identity];runpy.run_path(str(p/'worker.py'),run_name='__main__') if p.exists() else print(json.dumps({'ok':True,'value':{'restored':True,'scope':'no workspace was created'}}))"
            command = shlex.join(["python3", "-c", script, self.directory, self.task_id])
        inp, out, err = self.client.exec_command(command, timeout=self.profile["timeout"] + 20)
        inp.write(json.dumps(args))
        inp.flush()
        inp.channel.shutdown_write()
        text = out.read().decode("utf-8")
        err.read()
        if out.channel.recv_exit_status():
            raise ValueError("Remote workbench command failed")
        reply = json.loads(text)
        if not reply["ok"]:
            raise ValueError(reply["error"])
        return reply["value"]
