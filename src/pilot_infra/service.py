"""One machine domain service shared by official tools and Web RPC."""

import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from repopilot.storage import atomic_json, now

from .contracts import (
    AdapterEdit,
    BudgetUpdate,
    CandidateRequest,
    Control,
    CreateTask,
    Hypothesis,
    InfraPlan,
    JobRequest,
    ReviseTask,
)
from .engine import machine_probe
from .resource_service import RESOURCE_READ, RESOURCE_WRITE, ResourceService
from .store import AssetRegistry, InfraStore, digest

DEFAULT_ADAPTER = "def build_options():\n    return {}\n"


def result_summary(result):
    value = dict(result)
    if isinstance(value.get("artifact"), dict):
        artifact = value["artifact"]
        value["artifact"] = {k: v for k, v in artifact.items() if k != "runtime_files"}
        if "runtime_files" in artifact:
            value["artifact"].update(
                installed_file_count=len(artifact["runtime_files"]),
                installed_manifest_sha256=digest(artifact["runtime_files"]),
            )
    for key, omitted in [("quality", {"events"}), ("cases", {"rows", "warmup"})]:
        if key in value:
            value[key] = [{k: v for k, v in row.items() if k not in omitted} for row in value[key]]
    return value


READ_ONLY = {
    "status",
    "assets",
    "probe",
    "evidence",
    "source_read",
    "strategy",
    "asset_catalog",
    "asset_status",
    "stack_candidates",
    "model_metadata",
    "compatibility_docs",
    "workbench_profiles",
    "workbench_recommend",
    "network_recommend",
    "network_status",
    "workbench_status",
    "workbench_read",
    "engine_source_status",
    "engine_source_read",
    "engine_source_search",
} | RESOURCE_READ
ROLES = {
    "infra-lead": READ_ONLY
    | RESOURCE_WRITE
    | {
        "plan",
        "asset_request_prepare",
        "asset_propose",
        "stack_propose",
        "revise",
        "candidate",
        "launch",
        "test",
        "diagnose",
        "source_write",
        "adapter_check",
        "finish",
        "workbench_plan",
        "network_plan",
        "network_cleanup",
        "workbench_write",
        "workbench_patch",
        "workbench_freeze",
        "workbench_run",
        "workbench_cleanup",
        "engine_source_prepare",
        "engine_source_patch",
        "engine_build",
        "engine_request_approval",
        "engine_rollback",
    },
    "infra-expert": READ_ONLY | {"diagnose"},
    "repo-expert": READ_ONLY
    | {
        "source_write",
        "adapter_check",
        "diagnose",
        "workbench_write",
        "workbench_patch",
        "engine_source_patch",
    },
}


class InfraService:
    def __init__(self, root, engine_factory=None, spawn_jobs=True):
        self.root = Path(root).resolve()
        self.store = InfraStore(root)
        self.assets = AssetRegistry(root)
        self.engine_factory = engine_factory
        self.spawn_jobs = spawn_jobs
        self.resources = ResourceService(self)
        self.assets.authority_validator = self.resources.validate_plan

    def task(self, session_id):
        task = self.store.binding(session_id)
        if not task:
            raise ValueError("No Infra plan in this Session; create a reviewable plan first")
        return task

    def owner(self, task):
        return self.store.get(task["plan"].get("target_task_id") or task["id"])

    def validate(self, plan):
        p = InfraPlan.model_validate(plan)
        self.assets.validate(p)
        if p.target_task_id:
            target = self.store.get(p.target_task_id)
            if target["plan"]["kind"] != "deploy_verify":
                raise ValueError("Target must be a registered deployment task")
            for key in (
                "machine_id",
                "gpu_ids",
                "model_asset",
                "image_asset",
                "engine_source_profile",
            ):
                if p.model_dump()[key] != target["plan"].get(key):
                    raise ValueError("A target-bound task must retain deployment " + key)
            ids = {c["id"] for c in target["state"]["candidates"]}
            if not ids:
                raise ValueError(
                    "Target has no frozen deployment candidate; start its deployment first"
                )
            if not set(p.candidate_ids).issubset(ids):
                raise ValueError("Comparison candidate does not exist in the target")
        return p

    def actions(self, task):
        status, active = task["state"]["status"], task["state"].get("active_job")
        try:
            self.resources.check_task(self.owner(task), "execute")
            executable = True
        except ValueError:
            executable = False
        result = ["open", "budget"]
        if status == "draft":
            result += ["start"] if task["plan"].get("authorization_id") else ["edit", "start"]
            result += ["cancel"]
        if status == "running":
            result += ["pause", "cancel"]
        if status in {"paused", "failed"} and not active:
            result += ["resume", "cancel"]
        if active and self.store.operation(active)["status"] == "unknown":
            result += ["recover"]
        if task["state"].get("outcome"):
            result += ["report"]
        if (
            self.owner(task)["state"].get("service_candidate")
            and not active
            and not self.owner(task)["state"].get("active_job")
        ):
            result += ["stop_service"]
        if not executable:
            result = [a for a in result if a not in {"start", "resume"}]
        return result

    def view(self, task):
        task = self.reconcile(task["id"])
        self.strategy(task)
        task = self.store.get(task["id"])
        owner = self.owner(task)
        state = task["state"]
        meter_path = self.root / "runs/harness-policy" / str(state.get("session_id")) / "meter.json"
        meter = json.loads(meter_path.read_text()) if meter_path.is_file() else {}
        used = meter.get("input_tokens", 0) + meter.get("output_tokens", 0)
        try:
            grant = self.resources.check_task(owner, "execute")
            authority = {
                "valid": True,
                "kind": "reviewed-proposal" if grant else "owner-start-baseline",
                "grant": grant,
            }
        except ValueError as error:
            authority = {"valid": False, "reason": str(error)}
        return {
            "task": {
                **task,
                "state": {
                    **state,
                    "candidates": [self.compact_candidate(c) for c in state["candidates"]],
                },
            },
            "candidates": [self.compact_candidate(c) for c in owner["state"]["candidates"]],
            "actions": self.actions(task),
            "events": self.store.events(task["id"])[-100:],
            "cwd": str(self.workspace(owner)),
            "budget": {
                "max_tokens": state["max_tokens"],
                "revision": state["budget_revision"],
                "used_tokens": used,
                "requests": meter.get("calls", 0),
                "estimated": bool(meter.get("estimated", False)),
                "pricing": "unpriced",
                "last_stop": meter.get("last_stop"),
            },
            "service_candidate": owner["state"].get("service_candidate"),
            "current_acceptance": self.current_acceptance(task, owner),
            "authority": authority,
            "engine_source": self.engine_source_view(owner),
        }

    def current_acceptance(self, task, owner):
        results = task["state"]["experiments"]
        last = results[-1] if results else None
        if not last or last.get("candidate_id") != owner["state"].get("service_candidate"):
            return {"current": False, "reason": "Historical or absent service acceptance"}
        loads = [
            e
            for e in self.store.events(owner["id"])
            if e["kind"] == "job_settled"
            and e["data"].get("action") in {"launch", "rollback"}
            and e["data"].get("status") == "completed"
        ]
        if loads and loads[-1]["data"]["result"].get("identity") != last.get("identity"):
            return {"current": False, "reason": "Service reloaded after this acceptance"}
        identity = last.get("identity", {})
        if "pid" in identity:
            from .process_engine import process_alive

            if not process_alive(identity):
                return {"current": False, "reason": "Accepted service process exited"}
        elif identity.get("container_id"):
            candidate = next(
                (
                    c
                    for c in owner["state"]["candidates"]
                    if c["id"] == owner["state"].get("service_candidate")
                ),
                None,
            )
            if candidate:
                try:
                    from .process_engine import engine_for

                    grant = self.resources.check_task(owner, "execute", candidate)
                    execution = {
                        **owner,
                        "_resource_limits": self.resources.execution_limits(owner, grant),
                    }
                    engine = (self.engine_factory or engine_for)(self.assets, execution, candidate)
                    if hasattr(engine, "live_acceptance") and not engine.live_acceptance(identity):
                        return {
                            "current": False,
                            "reason": "Accepted container exited or its runtime identity changed",
                        }
                except (ValueError, OSError, RuntimeError):
                    return {
                        "current": False,
                        "reason": "Accepted container identity cannot be verified",
                    }
        return {"current": True, "job_id": last.get("job_id")}

    def engine_source_view(self, task):
        if not task["plan"].get("engine_source_profile"):
            return None
        from .engine_source import EngineSource

        return EngineSource(self).status(task)

    def workspace(self, task):
        path = self.store.path(task["id"], "workspace")
        path.mkdir(parents=True, exist_ok=True)
        adapter = path / "adapter.py"
        if not adapter.exists():
            adapter.write_bytes(DEFAULT_ADAPTER.encode("utf-8"))
        return path

    def model_view(self, task, role="infra-lead"):
        view = self.view(task)
        state = view["task"]["state"]
        state["candidates"] = [self.compact_candidate(c) for c in state["candidates"]]
        view["candidates"] = [self.compact_candidate(c) for c in view["candidates"]]
        state["experiment_count"] = len(state["experiments"])
        state["experiments"] = [result_summary(r) for r in state["experiments"][-2:]]
        state["hypotheses"] = state["hypotheses"][-4:]
        state["strategy_history"] = state["strategy_history"][-4:]
        state["incidents"] = state.get("incidents", [])[-2:]
        state["outcome"] = self.compact_outcome(state.get("outcome", {}))
        state["outcome_history"] = [
            self.compact_outcome(r) for r in state.get("outcome_history", [])[-1:]
        ]
        view["events"] = [
            {**e, "data": {**e["data"], "result": result_summary(e["data"]["result"])}}
            if e["kind"] == "job_settled"
            else e
            for e in view["events"][-6:]
        ]
        if role in {"infra-expert", "repo-expert"}:
            view["events"] = [
                {
                    **e,
                    "data": {
                        **{
                            k: e["data"].get(k)
                            for k in (
                                "id",
                                "action",
                                "candidate_id",
                                "status",
                                "evidence",
                                "before",
                                "after",
                            )
                        },
                        "result": {
                            k: e["data"].get("result", {}).get(k)
                            for k in ("passed", "error", "failure_layer", "gates")
                        },
                    },
                }
                for e in view["events"]
                if e["kind"] in {"job_settled", "source_edit", "engine_source_patch"}
            ][-3:]
            view["expert_scope"] = {
                "role": role,
                "purpose": "Analyze environment/runtime and propose a discriminating experiment"
                if role == "infra-expert"
                else "Read and patch admitted source; return its actual SHA",
                "execution_owner": "infra-lead",
                "completion_owner": "independent acceptance then infra-lead finish",
            }
        view["candidates"] = view["candidates"][-4:]
        state["candidates"] = state["candidates"][-4:]
        view["raw_access"] = "Full streams remain in Web evidence and immutable Job files"
        return view

    @staticmethod
    def compact_outcome(outcome):
        return {
            k: outcome[k]
            for k in (
                "task_id",
                "kind",
                "passed",
                "current_candidate",
                "completed_at",
                "diagnosis",
                "engine_repair",
                "rollback",
                "limits",
            )
            if k in outcome
        }

    @staticmethod
    def compact_candidate(candidate):
        if not candidate.get("engine_source"):
            return candidate
        return {
            **candidate,
            "engine_source": {
                k: v for k, v in candidate["engine_source"].items() if k not in {"files", "profile"}
            },
        }

    def create(self, args, session_id=""):
        if isinstance(args.get("plan"), str):
            args = {**args, "plan": InfraPlan.model_validate_json(args["plan"]).model_dump()}
        request = CreateTask.model_validate(args)
        if request.plan.authorization_id:
            raise ValueError(
                "Reviewed authorization is created only from an approved resource proposal"
            )
        if session_id:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", session_id):
                raise ValueError("Invalid official Session identity")
            from .build_service import BuildService

            if BuildService(self.root).bound(session_id):
                raise ValueError("Use a new Session; this root belongs to a source workbench")
            from .network_service import NetworkService

            if NetworkService(self.root).bound(session_id):
                raise ValueError("Use a new Session; this root belongs to a network task")
            bound = self.store.binding(session_id)
            if bound:
                if bound["plan"] != request.plan.model_dump():
                    raise ValueError(
                        "Session already has a plan; use infra_revise for its current revision"
                    )
                if bound["state"]["session_id"] is None:
                    self.store.bind(session_id, bound["id"], "planning")
                view = self.view(bound)
                return {**view, "requires_start": view["task"]["state"]["status"] == "draft"}
            if any(
                p["status"] in {"pending", "approved"} for p in self.resources.context(session_id)
            ):
                raise ValueError(
                    "This root already owns a resource proposal; materialize it after approval or use a new Session"
                )
        self.validate(request.plan)
        task = self.store.create(request)
        self.workspace(self.owner(task))
        if session_id:
            self.store.bind(session_id, task["id"], "planning")
        return {**self.view(task), "requires_start": True}

    def strategy(self, task):
        from .routing import decision as route

        decision = route(task, self.store.events(task["id"]))
        if decision != task["state"]["strategy"]:
            history = [*task["state"]["strategy_history"], {**decision, "time": now()}]
            self.store.update(task["id"], strategy=decision, strategy_history=history)
            self.store.event(task["id"], "strategy", decision)
        return decision

    def running(self, task):
        if task["state"]["status"] != "running":
            raise ValueError("Task is stopped; use the reviewed Web Start/Resume action")
        if time.time() >= task["state"].get("deadline_epoch", 0):
            raise ValueError("Task deadline reached")
        self.resources.check_task(self.owner(task), "execute")

    def control(self, task_id, action, args, role="owner"):
        if role != "owner":
            raise ValueError("Task controls and budget require the Web owner")
        request = Control.model_validate(args)
        task = self.store.get(task_id)
        payload = {"action": action, **request.model_dump()}
        previous = False
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = {
                "id": task_id,
                "plan": json.loads(row["plan"]),
                "state": json.loads(row["state"]),
                "revision": row["revision"],
                "created": row["created"],
            }
            old = db.execute(
                "SELECT payload FROM controls WHERE task_id=? AND request_id=?",
                (task_id, request.request_id),
            ).fetchone()
            if old:
                if json.loads(old[0]) != payload:
                    raise ValueError("Control ID conflict")
                previous = True
            elif task["revision"] != request.expected_revision:
                raise ValueError("Task revision conflict; reload before retrying")
            elif action not in self.actions(task):
                raise ValueError("Action unavailable for the current task state")
            # Reserve control before any external side effect. Unsettled controls
            # remain auditable and may only reconcile, never blindly replay.
            if not previous:
                if action in {"start", "resume"}:
                    self.validate(task["plan"])
                    owner = self.owner(task)
                    self.resources.check_task(owner, action)
                    for gpu in owner["plan"]["gpu_ids"]:
                        resource = f"{owner['plan']['machine_id']}:gpu:{gpu}"
                        lock = db.execute(
                            "SELECT task_id FROM locks WHERE resource=?", (resource,)
                        ).fetchone()
                        if lock and lock[0] != owner["id"]:
                            raise ValueError("GPU resource is owned by another Pilot task")
                        db.execute(
                            "INSERT OR IGNORE INTO locks VALUES(?,?)", (resource, owner["id"])
                        )
                    task["state"].update(
                        status="running",
                        phase="inspect",
                        error=None,
                        execution_mode="agent",
                        deadline_epoch=time.time() + task["plan"]["deadline_seconds"],
                    )
                elif action in {"pause", "cancel"}:
                    task["state"].update(
                        status="paused" if action == "pause" else "cancelled", phase=action
                    )
                db.execute(
                    "INSERT INTO controls VALUES(?,?,?)",
                    (task_id, request.request_id, json.dumps(payload)),
                )
                db.execute(
                    "UPDATE tasks SET revision=revision+1,state=? WHERE id=?",
                    (json.dumps(task["state"]), task_id),
                )
        if previous:
            return self.view(task)
        if action in {"start", "resume"}:
            task = self.store.get(task_id)
            self.strategy(task)
        elif action in {"pause", "cancel"}:
            task = self.store.get(task_id)
            # Worker reads stopped state and tears down only a launch in flight.
            # Completed service is a separate asset and remains visible.
            if not task["state"]["active_job"] and not self.owner(task)["state"].get(
                "service_candidate"
            ):
                self.store.release(self.owner(task)["id"])
        elif action == "stop_service":
            if self.owner(task)["state"].get("active_job"):
                raise ValueError("Deployment operation still active")
            self.submit(task, "stop", self.owner(task)["state"]["service_candidate"])
        elif action == "recover":
            from .process_engine import engine_for

            job = self.store.operation(task["state"]["active_job"])
            owner = self.owner(task)
            candidate = next(
                c for c in owner["state"]["candidates"] if c["id"] == job["data"]["candidate_id"]
            )
            if job["action"] == "engine_build":
                from .engine_source import recover_build

                recover_build(self, owner, candidate, job)
                self.store.update(
                    task_id,
                    active_job=None,
                    status="paused",
                    error="原构建进程已按身份结算；可继续原任务，构建不会自动重放。",
                )
                self.store.update(owner["id"], active_job=None)
                if not owner["state"].get("service_candidate"):
                    self.store.release(owner["id"])
                self.store.event(task_id, "control", payload)
                return self.view(self.store.get(task_id))
            engine = (self.engine_factory or engine_for)(
                self.assets, {**owner, "_cleanup": True}, candidate
            )
            # Recovery settles the old operation after inspecting its effects.
            # It does not launch or rerun a test. Existing ownership is checked
            # by stop; the next reviewed Resume can perform a new operation.
            identity = engine.verify_identity()
            engine.stop()
            self.store.operation(job["id"], "failed", recovered=True, observed_identity=identity)
            self.store.update(owner["id"], active_job=None, service_candidate=None)
            self.store.update(
                task_id,
                active_job=None,
                status="paused",
                error="原操作已核对并停止自有服务；可继续原任务。",
            )
            self.store.release(owner["id"])
        self.store.event(task_id, "control", payload)
        return self.view(self.store.get(task_id))

    def budget(self, task_id, args, role):
        if role != "owner":
            raise ValueError("Only owner can change cumulative token allowance")
        request = BudgetUpdate.model_validate(args)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            state = json.loads(row[0])
            old = db.execute(
                "SELECT payload FROM controls WHERE task_id=? AND request_id=?",
                (task_id, request.request_id),
            ).fetchone()
            payload = {"budget": request.model_dump()}
            if old:
                if json.loads(old[0]) != payload:
                    raise ValueError("Budget control ID conflict")
            else:
                if state["budget_revision"] != request.expected_revision:
                    raise ValueError("Budget revision conflict")
                state.update(
                    max_tokens=request.max_tokens, budget_revision=state["budget_revision"] + 1
                )
                db.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(state), task_id))
                db.execute(
                    "INSERT INTO controls VALUES(?,?,?)",
                    (task_id, request.request_id, json.dumps(payload)),
                )
        task = self.store.get(task_id)
        self.store.sync(task)
        return self.view(task)

    def candidate(self, task, args):
        task = self.store.get(task["id"])
        self.running(task)
        request = CandidateRequest.model_validate(args)
        owner = self.owner(task)
        self.resources.check_task(owner, "candidate", {"serving": request.serving.model_dump()})
        if task["plan"]["kind"] in {"test", "compare"}:
            raise ValueError("Test/comparison cannot modify deployment candidates")
        if task["state"]["active_job"] or owner["state"]["active_job"]:
            raise ValueError("Wait for the active operation before making a new candidate")
        candidates = owner["state"]["candidates"]
        if request.parent_id and request.parent_id not in {c["id"] for c in candidates}:
            raise ValueError("Unknown parent candidate")
        if request.parent_id and task["state"]["repairs"] >= 3:
            raise ValueError(
                "Three repair candidates exhausted; inspect evidence before manual replanning"
            )
        source = (self.workspace(owner) / "adapter.py").read_text(encoding="utf-8")
        _, model, image = self.assets.validate(owner["plan"])
        from .container_runtime import admit_backend, runtime_digest

        admit_backend(image, request.serving.attention_backend)
        identity = {
            "serving": request.serving.model_dump(),
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "model_asset": owner["plan"]["model_asset"],
            "image_asset": owner["plan"]["image_asset"],
            "model_manifest_digest": model["manifest_digest"],
            "image_digest": image["digest"],
            "runtime_digest": runtime_digest(image),
        }
        engine_source = None
        if owner["plan"].get("engine_source_profile"):
            from .engine_source import EngineSource

            engine_source = EngineSource(self)
            identity["engine_source"] = engine_source.freeze(owner)
        for previous in candidates:
            if previous["request_id"] == request.request_id:
                if previous["digest"] != digest(identity):
                    raise ValueError("Candidate request key reused for changed source/config")
                return previous
        candidate = {
            "id": "c" + str(len(candidates) + 1),
            "parent_id": request.parent_id,
            "request_id": request.request_id,
            "reason": request.reason,
            **identity,
            "digest": digest(identity),
            "created": now(),
        }
        if engine_source:
            engine_source.freeze(owner, candidate["id"])
        atomic_json(
            self.store.path(owner["id"], f"candidates/{candidate['id']}/candidate.json"), candidate
        )
        self.store.path(owner["id"], f"candidates/{candidate['id']}/adapter.py").write_bytes(
            source.encode("utf-8")
        )
        self.store.update(owner["id"], candidates=[*candidates, candidate])
        if request.parent_id:
            self.store.update(task["id"], repairs=task["state"]["repairs"] + 1)
        self.store.event(task["id"], "candidate", self.compact_candidate(candidate))
        return candidate

    def submit(self, task, action, candidate_id):
        task = self.store.get(task["id"])
        if action not in {"launch", "test", "adapter_check", "stop", "engine_build", "rollback"}:
            raise ValueError("Unsupported Job action")
        owner = self.owner(task)
        if action != "stop":
            self.running(task)
        if candidate_id not in {c["id"] for c in owner["state"]["candidates"]}:
            raise ValueError("Unknown candidate")
        candidate = next(c for c in owner["state"]["candidates"] if c["id"] == candidate_id)
        self.resources.check_task(owner, action, candidate)
        if action in {"launch", "test", "rollback"} and candidate.get("engine_source"):
            from .engine_source import EngineSource

            EngineSource(self).admitted(owner, candidate)
        if action == "engine_build":
            from .engine_source import EngineSource

            self.resources.check_task(owner, "source_write", candidate)
            if not candidate.get("engine_source") or EngineSource(self).artifact(
                owner, candidate, required=False
            ):
                raise ValueError("Frozen engine source is missing or already built")
        if task["state"]["active_job"] or owner["state"]["active_job"]:
            raise ValueError("Existing job must settle before another operation")
        self.store.acquire(owner)
        operation = self.store.claim(
            owner["id"], action, {"consumer_task_id": task["id"], "candidate_id": candidate_id}
        )
        self.store.update(owner["id"], active_job=operation)
        if task["id"] != owner["id"]:
            self.store.update(task["id"], active_job=operation)
        self.store.event(
            task["id"],
            "job_submitted",
            {"id": operation, "action": action, "candidate_id": candidate_id},
        )
        if self.spawn_jobs:
            log = self.store.path(task["id"], f"jobs/{operation}/worker.log")
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("wb") as stream:
                kwargs = (
                    {"start_new_session": True}
                    if os.name != "nt"
                    else {"creationflags": subprocess.CREATE_NO_WINDOW}
                )
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "pilot_infra.worker", str(self.root), operation],
                        cwd=self.root,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        env={**self.worker_environment(), "PYTHONPATH": str(self.root / "src")},
                        **kwargs,
                    )
                    self.store.operation(
                        operation, "submitted", pid=process.pid, submitted_epoch=time.time()
                    )
                except Exception:
                    self.store.operation(
                        operation, "failed", error="Worker spawn failed before launch"
                    )
                    self.store.update(owner["id"], active_job=None)
                    self.store.update(task["id"], active_job=None)
                    raise
        return {
            "job_id": operation,
            "status": "submitted",
            "next": "Await the real job settlement notification; do not poll with model calls",
        }

    def worker_environment(self):
        from pilot_core.storage import StorageLayout

        return StorageLayout(self.root).environment()

    def reconcile(self, task_id):
        task = self.store.get(task_id)
        job_id = task["state"].get("active_job")
        if job_id:
            job = self.store.operation(job_id)
            if job["status"] in {"completed", "failed", "cancelled"}:
                owner = self.owner(task)
                self.store.update(owner["id"], active_job=None)
                self.store.update(task_id, active_job=None)
                task = self.store.get(task_id)
            elif (
                job["data"].get("pid")
                and time.time() - job["data"].get("submitted_epoch", time.time()) > 15
            ):
                try:
                    os.kill(job["data"]["pid"], 0)
                except ProcessLookupError:
                    self.store.operation(
                        job_id,
                        "unknown",
                        error="Worker absent; container effects require inspect/recovery",
                    )
                    self.store.update(
                        task_id,
                        status="paused",
                        error="执行器意外退出，原操作状态待核对。保留 Job 并禁止自动重放。",
                    )
        if task["state"]["status"] == "running" and time.time() >= task["state"].get(
            "deadline_epoch", float("inf")
        ):
            self.store.update(
                task_id, status="paused", error="任务时限已到，产物保留；检查后继续。"
            )
        return self.store.get(task_id)

    def reconcile_authority(self):
        """Expire unattended owned services without replaying unknown effects."""
        settled = []
        for task in self.store.list():
            if task["plan"].get("target_task_id") or not task["plan"].get("authorization_id"):
                continue
            task = self.reconcile(task["id"])
            try:
                self.resources.check_task(task, "execute")
                continue
            except ValueError as error:
                reason = str(error)
            state = task["state"]
            if state.get("authority_stop_reason") != reason:
                self.store.update(
                    task["id"],
                    authority_stop_reason=reason,
                    **(
                        {"status": "paused", "error": reason}
                        if state["status"] == "running"
                        else {}
                    ),
                )
                self.store.event(task["id"], "authority_invalidated", {"reason": reason})
            if state.get("active_job"):
                # The active Worker checks the same grant. Unknown effects
                # remain blocked for explicit identity-based recovery.
                settled.append({"task_id": task["id"], "status": "awaiting_worker_or_recovery"})
                continue
            if not state.get("service_candidate"):
                continue
            previous = state.get("authority_cleanup_job")
            if previous:
                settled.append(
                    {"task_id": task["id"], "status": "cleanup_requires_review", "job_id": previous}
                )
                continue
            job = self.submit(task, "stop", state["service_candidate"])
            self.store.update(task["id"], authority_cleanup_job=job["job_id"])
            self.store.event(task["id"], "authority_cleanup_submitted", {**job, "reason": reason})
            settled.append({**job, "task_id": task["id"], "status": "cleanup_submitted"})
        return {"tasks": settled}

    def evidence(self, task, args, summary=False):
        name = args.get("path", "")
        if not re.fullmatch(
            r"jobs/[a-f0-9]{32}/(result.json|engine.log|worker.log|probe.json)", name
        ):
            raise ValueError("Only registered evidence references can be read")
        allowed = {
            e["data"].get("id")
            for e in self.store.events(task["id"])
            if e["kind"] == "job_submitted"
        }
        if name.split("/")[1] not in allowed:
            raise ValueError("Evidence belongs to another task")
        path = self.store.path(task["id"], name)
        if not path.is_file():
            raise ValueError("Evidence not yet available")
        value = {
            "path": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "text": path.read_text(encoding="utf-8")[-60000:],
            "bytes": path.stat().st_size,
            "display_limit_chars": 60000,
        }
        if summary and name.endswith("result.json"):
            result = result_summary(json.loads(path.read_text(encoding="utf-8")))
            return {
                "path": name,
                "sha256": value["sha256"],
                "result": result,
                "raw_access": "Full raw evidence remains in the Web evidence panel",
            }
        if summary:
            value["text"] = value["text"][-6000:]
        return value

    def call_tool(self, name, session_id, role, args):
        if name not in ROLES.get(role, set()):
            raise ValueError("Tool outside this Infra role")
        if name.startswith("network_"):
            from .network_service import NetworkService

            return NetworkService(self.root).tool(name, session_id, role, args)
        if name.startswith("asset_") or name == "stack_candidates":
            from .asset_service import AssetService
            from .fleet_contracts import AssetStatus, StackMatch

            fleet = AssetService(self.root)
            if name == "asset_request_prepare":
                return fleet.request_prepare(args, session_id)
            if name == "asset_propose":
                return fleet.propose("asset", args, session_id)
            if name == "asset_catalog":
                value = fleet.catalog()
                value["operations"] = [
                    o for o in value["operations"] if o.get("root_session") == session_id
                ]
                value["proposals"] = [
                    p for p in value["proposals"] if p["root_session"] == session_id
                ]
                return value
            if name == "asset_status":
                operation = fleet.operation(AssetStatus.model_validate(args).operation_id)
                if operation.get("root_session") != session_id:
                    raise ValueError("资产操作不属于当前根会话")
                return fleet.refresh(operation["id"])
            request = StackMatch.model_validate(args)
            return fleet.match(request.server_id, request.asset_id, request.capabilities)
        if name == "stack_propose":
            from .asset_service import AssetService

            return AssetService(self.root).propose("stack", args, session_id)
        if name == "model_metadata":
            from .asset_service import AssetService

            return AssetService(self.root).resolve_model(args)
        if name == "compatibility_docs":
            from .compatibility_sources import read_reference
            from .fleet_contracts import CompatibilityReference

            return read_reference(CompatibilityReference.model_validate(args).source_id)
        if name.startswith("resource_"):
            if name == "resource_request_approval":
                return self.resources.proposal(args, session_id)
            return self.resources.dispatch(name.replace("_", "-"), session_id, role, args)
        if name.startswith("workbench_"):
            from .build_service import BuildService

            return BuildService(self.root).tool(name, session_id, role, args)
        if name == "assets":
            return self.assets.read()
        if name == "probe":
            return {"observation": machine_probe(), "quota": self.assets.read()["machines"]}
        if name == "plan":
            return self.create(args, session_id)
        if name == "status" and not self.store.binding(session_id):
            return {
                "task": None,
                "status": "unplanned",
                "assets": self.assets.read(),
                "next": "Answer the actual user request; no default task. Clarify unclear goals. Only an explicit task request permits saving an InfraPlan; Owner Start remains required.",
            }
        task = self.task(session_id)
        if task["state"].get("execution_mode") == "manual" and name in {
            "source_write",
            "candidate",
            "engine_source_prepare",
            "engine_source_patch",
            "engine_build",
            "launch",
            "test",
            "adapter_check",
            "finish",
            "engine_rollback",
        }:
            raise ValueError(
                "Task is under manual control; Owner must return control to the agent before autonomous changes"
            )
        if role in {"infra-expert", "repo-expert"} and name not in READ_ONLY:
            if role not in self.strategy(task)["experts"]:
                raise ValueError("This expert is not admitted by the current task strategy")
        if name.startswith("engine_"):
            return self.engine_tool(name, task, args, role)
        if name == "status":
            return self.model_view(task, role)
        if name == "revise":
            request = ReviseTask.model_validate(args)
            if task["plan"].get("authorization_id"):
                raise ValueError("获批方案不能修改或移除授权；请在新会话重新申请方案")
            self.validate(request.plan)
            return {**self.view(self.store.revise(task["id"], request)), "requires_start": True}
        if name == "strategy":
            return self.strategy(task)
        if name == "evidence":
            return self.evidence(task, args, summary=True)
        if name == "source_read":
            source = (self.workspace(self.owner(task)) / "adapter.py").read_text(encoding="utf-8")
            return {
                "path": "adapter.py",
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
                "content": source,
            }
        self.running(task)
        if name == "source_write":
            self.resources.check_task(self.owner(task), "source_write")
            if task["plan"]["kind"] in {"test", "compare"}:
                raise ValueError("Read-only experiment task")
            decision = self.strategy(task)
            if (
                decision["mode"] == "multi"
                and "repo-expert" in decision["experts"]
                and role != "repo-expert"
            ):
                raise ValueError(
                    "Observed adapter failure admits Repo expert repair; consult repo_specialist before freezing the candidate"
                )
            request = AdapterEdit.model_validate(args)
            path = self.workspace(self.owner(task)) / "adapter.py"
            if hashlib.sha256(path.read_bytes()).hexdigest() != request.expected_sha256:
                raise ValueError("Source version conflict")
            path.write_bytes(request.content.encode("utf-8"))
            self.store.event(
                task["id"],
                "source_edit",
                {
                    "before": request.expected_sha256,
                    "after": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            )
            return self.call_tool("source_read", session_id, role, {})
        if name == "candidate":
            return self.compact_candidate(self.candidate(task, args))
        if name in {"launch", "test", "adapter_check"}:
            if task["plan"]["kind"] == "test" and name == "launch":
                raise ValueError("A quality/performance task cannot redeploy its target")
            return self.submit(task, name, JobRequest.model_validate(args).candidate_id)
        if name == "diagnose":
            h = Hypothesis.model_validate(args)
            events = self.store.events(task["id"])
            ids = {str(e["seq"]) for e in events}
            if not set(h.evidence_ids).issubset(ids):
                raise ValueError("Hypothesis must reference observed evidence event IDs")
            self.store.update(
                task["id"],
                hypotheses=[
                    *task["state"]["hypotheses"],
                    {**h.model_dump(), "status": "provisional"},
                ],
            )
            self.store.event(task["id"], "hypothesis", h.model_dump())
            return {
                "hypothesis": h.model_dump(),
                "strategy": self.strategy(self.store.get(task["id"])),
            }
        if name == "finish":
            return self.finish(task)
        raise ValueError("Unknown Infra tool")

    def engine_tool(self, name, task, args, role):
        from .engine_source import EngineSource

        owner = self.owner(task)
        source = EngineSource(self)
        if name == "engine_source_status":
            return source.status(owner)
        if name == "engine_source_read":
            return source.read(owner, args)
        if name == "engine_source_search":
            return source.search(owner, args)
        self.running(task)
        if name == "engine_source_prepare":
            return source.prepare(owner)
        if name == "engine_source_patch":
            decision = self.strategy(task)
            if (
                decision["mode"] == "multi"
                and "repo-expert" in decision["experts"]
                and role != "repo-expert"
            ):
                raise ValueError("Admitted Repo expert must perform the engine source repair")
            return source.patch(owner, args)
        if name == "engine_build":
            return self.submit(task, "engine_build", JobRequest.model_validate(args).candidate_id)
        if name == "engine_request_approval":
            return source.review(owner, args)
        if name == "engine_rollback":
            return self.rollback(task, JobRequest.model_validate(args).candidate_id)
        raise ValueError("Unknown engine source operation")

    def rollback(self, task, candidate_id, manual=False):
        owner = self.owner(task)
        self.resources.check_task(owner, "execute")
        if (
            task["id"] != owner["id"]
            or task["state"].get("active_job")
            or owner["state"].get("service_candidate") == candidate_id
        ):
            raise ValueError(
                "Rollback requires an idle deployment and a different historical candidate"
            )
        previous = [
            r
            for r in task["state"]["experiments"]
            if r.get("candidate_id") == candidate_id and r.get("passed")
        ]
        if not previous:
            raise ValueError("Rollback candidate has no prior independent acceptance")
        measured = previous[-1]
        operation = self.store.operation(measured["job_id"])
        raw = self.store.path(task["id"], f"jobs/{operation['id']}/result.json")
        if (
            operation["status"] != "completed"
            or operation["action"] not in {"test", "rollback"}
            or not raw.is_file()
            or hashlib.sha256(raw.read_bytes()).hexdigest() != measured["evidence_sha256"]
        ):
            raise ValueError("Rollback acceptance evidence changed")
        candidate = next((c for c in owner["state"]["candidates"] if c["id"] == candidate_id), None)
        if not candidate or not candidate.get("engine_source"):
            raise ValueError("Rollback requires a historical engine artifact")
        from .engine_source import EngineSource

        EngineSource(self).admitted(owner, candidate)
        self.resources.check_task(owner, "rollback", candidate)
        if measured.get("candidate_digest") != candidate["digest"]:
            raise ValueError("Rollback candidate differs from historical acceptance")
        if task["state"]["status"] != "running":
            if task["state"]["status"] != "completed":
                raise ValueError("Start or resume the task before rollback")
            self.store.update(
                task["id"],
                status="running",
                phase="rollback",
                outcome={},
                outcome_history=[
                    *task["state"].get("outcome_history", []),
                    task["state"]["outcome"],
                ],
                deadline_epoch=time.time() + task["plan"]["deadline_seconds"],
            )
        if manual:
            self.store.update(task["id"], execution_mode="manual")
        self.store.event(
            task["id"],
            "rollback_requested",
            {"from": owner["state"].get("service_candidate"), "to": candidate_id},
        )
        return self.submit(self.store.get(task["id"]), "rollback", candidate_id)

    def verified_acceptance(self, task):
        """Verify current Worker evidence without changing task state."""
        task = self.store.get(task["id"])
        if task["state"]["active_job"]:
            raise ValueError("Job still active; idle Session is not business completion")
        results = task["state"]["experiments"]
        if not results or not results[-1].get("passed"):
            raise ValueError("Current candidate has not passed independent acceptance")
        owner = self.owner(task)
        current = owner["state"].get("service_candidate")
        result = results[-1]
        required = {"identity", "quality", "test_complete"} | (
            {"performance"} if task["plan"]["performance_required"] else set()
        )
        if not all(result.get("gates", {}).get(g) is True for g in required):
            raise ValueError("Required independent gates failed")
        job = self.store.operation(result["job_id"])
        path = self.store.path(task["id"], f"jobs/{job['id']}/result.json")
        if (
            job["status"] != "completed"
            or job["action"] not in {"test", "rollback"}
            or job["data"]["consumer_task_id"] != task["id"]
            or not path.is_file()
        ):
            raise ValueError("Acceptance has no completed independent test Job")
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["evidence_sha256"]:
            raise ValueError("Acceptance evidence changed")
        if result.get("candidate_id") != current:
            raise ValueError("Acceptance belongs to another service candidate")
        from .process_engine import engine_for

        candidate = next(c for c in owner["state"]["candidates"] if c["id"] == current)
        if candidate.get("engine_source"):
            from .engine_source import EngineSource

            EngineSource(self).admitted(owner, candidate)
            if not result["identity"].get("checks", {}).get("engine_artifact"):
                raise ValueError("No actual engine artifact load evidence")
        source = self.store.path(owner["id"], f"candidates/{current}/adapter.py")
        if (
            result["candidate_digest"] != candidate["digest"]
            or hashlib.sha256(source.read_bytes()).hexdigest() != candidate["source_sha256"]
        ):
            raise ValueError("Acceptance candidate source/version changed")
        grant = self.resources.check_task(owner, "execute", candidate)
        execution = {**owner, "_resource_limits": self.resources.execution_limits(owner, grant)}
        engine = (self.engine_factory or engine_for)(self.assets, execution, candidate)
        if engine.verify_identity() != result["identity"]:
            raise ValueError("Service restarted or changed after acceptance; retest")
        return owner, current, result, candidate, engine

    def finish(self, task):
        task = self.store.get(task["id"])
        self.running(task)
        owner, current, result, candidate, engine = self.verified_acceptance(task)
        results = task["state"]["experiments"]
        if task["plan"]["kind"] == "compare":
            if len(task["plan"]["candidate_ids"]) != 2 or len(set(task["plan"]["candidate_ids"])) != 2:
                raise ValueError("Comparison requires exactly two distinct candidate IDs")
            relevant = [r for r in results if r["candidate_id"] in task["plan"]["candidate_ids"]]
            for measured in relevant:
                operation = self.store.operation(measured["job_id"])
                raw = self.store.path(task["id"], f"jobs/{operation['id']}/result.json")
                if (
                    operation["status"] != "completed"
                    or operation["action"] != "test"
                    or operation["data"]["consumer_task_id"] != task["id"]
                    or not raw.is_file()
                    or hashlib.sha256(raw.read_bytes()).hexdigest() != measured["evidence_sha256"]
                ):
                    raise ValueError("Comparison evidence is incomplete or changed")
            ids = {
                r["candidate_id"]
                for r in relevant
                if r["gates"]["identity"] and r["gates"]["test_complete"]
            }
            if ids != set(task["plan"]["candidate_ids"]):
                raise ValueError("Both comparison candidates need complete measurements")
            if "cache_mode" in task["plan"]["workload"]:
                from .benchmark import identity

                latest = {r["candidate_id"]: r for r in relevant}
                if any(r.get("protocol_digest") != identity(task["plan"]["workload"])
                       for r in latest.values()):
                    raise ValueError("Comparison measurement protocols differ; retest both candidates")
        diagnosis = None
        if task["plan"]["kind"] == "diagnose_repair":
            failures = [r for r in results[:-1] if not r.get("passed")]
            failures += [
                {"candidate_id": e["data"].get("candidate_id")}
                for e in self.store.events(task["id"])
                if e["kind"] == "job_settled"
                and (e["data"]["status"] == "failed" or e["data"]["result"].get("passed") is False)
            ]
            if task["state"]["repairs"]:
                if (
                    not failures
                    or not task["state"]["hypotheses"]
                    or candidate.get("parent_id") not in {r["candidate_id"] for r in failures}
                ):
                    raise ValueError(
                        "Repair requires observed failing baseline, an evidence-linked hypothesis and a tested descendant"
                    )
                diagnosis = {
                    "result": "controlled_repair_verified",
                    "baseline": candidate["parent_id"],
                    "accepted": current,
                    "causal_limit": "A controlled change followed by passing retest supports this repair; it does not prove an arbitrary kernel/root-cause claim",
                }
            else:
                if failures:
                    raise ValueError("Observed fault has not been repaired")
                diagnosis = {
                    "result": "no_fault_observed_in_registered_tests",
                    "limits": "Uncovered workloads may still fail",
                }
        report = {
            "task_id": task["id"],
            "kind": task["plan"]["kind"],
            "passed": True,
            "current_candidate": current,
            "results": results,
            "strategy_history": task["state"]["strategy_history"],
            "completed_at": now(),
            "limits": result["limits"],
            "diagnosis": diagnosis,
            "engine_repair": self.engine_repair_summary(task, candidate),
            "engine_source": self.engine_source_view(owner),
            "rollback": result.get("rollback"),
            "outcome_history": task["state"].get("outcome_history", []),
            "comparison": {
                "candidate_ids": task["plan"]["candidate_ids"],
                "workload": task["plan"]["workload"],
                "policy": "Sequential A/B on the same registered model, image and GPUs; shared load and short samples prevent significance claims",
            }
            if task["plan"]["kind"] == "compare"
            else None,
        }
        atomic_json(self.store.path(task["id"], "report.json"), report)
        self.store.update(task["id"], status="completed", phase="done", outcome=report)
        self.store.event(task["id"], "completed", {"passed": True, "candidate_id": current})
        # Native tool/UI receipt needs identity and acceptance, not every source
        # manifest and historical stream. Full evidence remains on disk/Web.
        return {
            "passed": True,
            "task": {
                "id": task["id"],
                "revision": task["revision"],
                "plan": {k: task["plan"][k] for k in ("title", "goal", "kind")},
                "state": {"status": "completed", "outcome": self.compact_outcome(report)},
            },
            "report": "report.json",
        }

    def engine_repair_summary(self, task, candidate):
        parent = next(
            (
                c
                for c in self.owner(task)["state"]["candidates"]
                if c["id"] == candidate.get("parent_id")
            ),
            None,
        )
        if not parent or not candidate.get("engine_source") or not parent.get("engine_source"):
            return None
        changes = [
            k
            for k, v in candidate["engine_source"]["files"].items()
            if v != parent["engine_source"]["files"].get(k)
        ]
        failures = [
            e
            for e in self.store.events(task["id"])
            if e["kind"] == "job_settled"
            and e["data"]["candidate_id"] == parent["id"]
            and e["data"]["action"] in {"launch", "test", "rollback"}
            and (e["data"]["status"] == "failed" or e["data"]["result"].get("passed") is False)
        ]
        if not changes or not failures or not task["state"]["hypotheses"]:
            return None
        return {
            "result": "controlled_engine_change_retested",
            "baseline": parent["id"],
            "accepted": candidate["id"],
            "changed_files": changes,
            "baseline_evidence": [e["data"]["evidence"] for e in failures],
            "causal_limit": "A known failing candidate and a tested source descendant support this bounded repair; they do not prove a blind diagnosis or arbitrary kernel correctness.",
        }

    def dispatch(self, operation, session_id, role, args):
        endpoint = operation.removeprefix("infra-")
        if endpoint.startswith("distributed-"):
            if role != "owner":
                raise ValueError("跨机拓扑管理需要Owner")
            from .distributed_service import DistributedService

            distributed = DistributedService(self)
            action = endpoint.removeprefix("distributed-")
            if action == "catalog":
                return distributed.list()
            if action == "preflight":
                return distributed.preflight(args)
            if action == "create":
                return distributed.create(args)
            if action == "view":
                return distributed.get(args["task_id"])
            if action == "review":
                return distributed.review(args["task_id"])
            raise ValueError("跨机执行器尚未开放；当前只能保存并预检方案")
        if endpoint.startswith("network-"):
            if role != "owner":
                raise ValueError("网络任务管理需要Owner")
            from .network_service import NetworkService

            service = NetworkService(self.root)
            action = endpoint.removeprefix("network-")
            if action == "catalog":
                return service.list()
            if action == "recommend":
                return service.recommend(args["server_ids"])
            if action == "create":
                return service.create(args)
            if action in {"run", "view", "cleanup"}:
                return {"run": service.run, "view": service.get, "cleanup": service.cleanup}[action](args["task_id"])
            raise ValueError("未知网络任务操作")
        if endpoint == "asset-native-decision":
            if role != "native-approval":
                raise ValueError("资产审批决定只能由原生审批桥提交")
            from .asset_service import AssetService

            return AssetService(self.root).decide_prepare(args, session_id)
        if endpoint.startswith("asset-") or endpoint == "resource-executor":
            if role != "owner":
                raise ValueError("资产、栈配方与执行器管理只允许Owner")
            from .asset_service import AssetService

            fleet = AssetService(self.root)
            handlers = {
                "asset-catalog": lambda _: fleet.catalog(),
                "asset-save": fleet.save,
                "asset-prepare": fleet.prepare,
                "asset-stack-save": fleet.save_stack,
                "asset-refresh": lambda a: fleet.refresh(a["operation_id"]),
                "asset-cancel": lambda a: fleet.refresh(a["operation_id"], cancel=True),
                "asset-reconcile": lambda a: fleet.refresh(a["operation_id"], reconcile=True),
                "asset-match": lambda a: fleet.match(
                    a["server_id"], a["asset_id"], a.get("capabilities", [])
                ),
                "asset-monitor": lambda _: fleet.monitor(session_id),
                "asset-notified": lambda a: fleet.notified(a["operation_id"], session_id),
                "asset-register-proposal": fleet.register_proposal,
                "asset-resolve-image": fleet.resolve_image,
                "resource-executor": fleet.configure_executor,
                "asset-executor-check": fleet.inspect_executor,
                "asset-docker-inventory": fleet.docker_inventory,
                'asset-executor-cleanup':fleet.cleanup_executor,
            }
            if endpoint == "asset-resolve-model":
                return fleet.resolve_model(args)
            if endpoint not in handlers:
                raise ValueError("未知资产操作")
            return handlers[endpoint](args)
        if endpoint == "routing-context":
            if role not in {"infra-lead", "supervisor", "owner"}:
                raise ValueError("Pilot root routing context required")
            from .build_service import BuildService

            assets = self.assets.read()
            from .usage_guide import catalog as guide_catalog

            guide = guide_catalog()
            return {
                "serving_registered": assets["ready"],
                "cpu_build_executors": len(BuildService(self.root).profiles()),
                "engine_source_profiles": len(assets.get("engine_source_profiles", [])),
                "models": [m.get("label", m["id"]) for m in assets["models"]],
                "engines": [m.get("label", m["id"]) for m in assets["images"]],
                "ui_help": [
                    {
                        k: f[k]
                        for k in (
                            "title",
                            "entry",
                            "summary",
                            "steps",
                            "limits",
                            "roles",
                            "strategies",
                        )
                        if k in f
                    }
                    for f in guide.get("features", [])
                ],
                "note": "登记不是连通或验收。宿主驱动、他人服务与未登记执行器不在可执行范围。",
            }
        if endpoint == "evaluation-catalog":
            if role != "owner":
                raise ValueError("Evaluation reports require authenticated Owner")
            from pilot_eval.report import current

            return current(self.root, args.get("report_id"))
        if endpoint == "architecture":
            if role != "owner":
                raise ValueError("Architecture requires authenticated Owner")
            from .architecture import current_architecture

            return current_architecture(self)
        if endpoint == "guide":
            if role != "owner":
                raise ValueError("Usage guide requires authenticated Owner")
            from .usage_guide import current_guide

            return current_guide(self)
        if endpoint == "engine-native-decision":
            if role != "native-approval":
                raise ValueError("Only the native approval bridge can submit this outcome")
            from .engine_source import EngineSource

            return EngineSource(self).decide(self.owner(self.task(session_id)), args, "native-turn")
        if endpoint.startswith("engine-"):
            if role != "owner":
                raise ValueError("Engine management requires authenticated Owner")
            task = self.task(session_id)
            if endpoint == "engine-manual":
                if (
                    not task["plan"].get("engine_source_profile")
                    or task["id"] != self.owner(task)["id"]
                    or task["state"].get("active_job")
                ):
                    raise ValueError(
                        "Manual source operations require an idle engine deployment task"
                    )
                self.resources.check_task(task, "source_write")
                action = "start" if task["state"]["status"] == "draft" else "resume"
                if task["state"]["status"] in {"draft", "paused", "failed"}:
                    self.control(task["id"], action, args, role)
                elif task["state"]["status"] != "running":
                    raise ValueError("Completed deployments only allow reviewed rollback")
                self.store.update(task["id"], execution_mode="manual")
                self.store.event(
                    task["id"], "execution_mode", {"mode": "manual", "source": "owner-web"}
                )
                return self.view(self.store.get(task["id"]))
            if endpoint == "engine-finish":
                return self.finish(task)
            if endpoint == "engine-agent":
                if task["state"].get("execution_mode") != "manual" or task["state"].get(
                    "active_job"
                ):
                    raise ValueError("Idle manual task required before returning to Agent")
                self.running(task)
                self.store.update(task["id"], execution_mode="agent")
                return self.view(self.store.get(task["id"]))
            if endpoint == "engine-decision":
                from .engine_source import EngineSource

                return EngineSource(self).decide(self.owner(task), args, "owner-web")
            if endpoint == "engine-rollback":
                return self.rollback(
                    task, JobRequest.model_validate(args).candidate_id, manual=True
                )
            return self.engine_tool(endpoint.replace("-", "_"), task, args, "infra-lead")
        if endpoint == "authority-reconcile":
            if role != "owner":
                raise ValueError("Only the owner lifecycle can reconcile resource authority")
            return self.reconcile_authority()
        if endpoint.startswith("resource-"):
            return self.resources.dispatch(endpoint, session_id, role, args)
        if endpoint.startswith("workbench-"):
            from .build_service import BuildService

            builds = BuildService(self.root)
            if role != "owner":
                raise ValueError("Workbench management requires owner")
            action = endpoint.removeprefix("workbench-")
            if action == "catalog":
                return builds.catalog()
            if action == "save-profile":
                return builds.save_profile(args)
            if action in {"source-export", "source-download"}:
                return builds.export_source(args, downloading=action == "source-download")
            if action == "recommend":
                return builds.recommend(args)
            if action == "create":
                return builds.create(args)
            if action == "bind":
                return builds.bind(args["task_id"], args["session_id"])
            if action == "read":
                return builds.read(args)
            if action == "write":
                return builds.edit(args)
            if action == "patch":
                return builds.patch(args)
            if action == "run":
                return builds.run(args)
            handlers = {
                "view": builds.task,
                "prepare": builds.prepare,
                "freeze": builds.freeze,
                "recover": builds.recover,
                "cleanup": builds.cleanup,
            }
            if action in handlers:
                return handlers[action](args["task_id"])
            raise ValueError("Unknown workbench action")
        if endpoint == "tool":
            return self.call_tool(args["name"], session_id, role, args.get("input", {}))
        if endpoint == "catalog":
            from .build_service import BuildService

            builds = BuildService(self.root).catalog()
            return {
                "tasks": [self.view(t) for t in self.store.list()],
                "assets": self.assets.read(),
                "source_workbench": {
                    "available": bool(builds["profiles"] or builds["tasks"]),
                    "registered_executors": len(builds["profiles"]),
                    "task_count": len(builds["tasks"]),
                },
            }
        if endpoint == "create":
            return self.create(args)
        if endpoint == "probe":
            return {"observation": machine_probe(), "machines": self.assets.read()["machines"]}
        if endpoint == "bind":
            if role != "owner":
                raise ValueError("Binding requires owner")
            from .build_service import BuildService

            if BuildService(self.root).bound(session_id):
                raise ValueError("Use a new Session; this root belongs to a source workbench")
            self.store.bind(session_id, args["task_id"])
            return self.view(self.task(session_id))
        if endpoint == "view":
            bound = self.store.binding(session_id)
            return self.view(bound) if bound else {"task": None, "assets": self.assets.read()}
        if endpoint == "context":
            view = self.model_view(self.task(session_id), role)
            task, state = view["task"], view["task"]["state"]
            settled = [e for e in view["events"] if e["kind"] == "job_settled"]
            facts = {
                "task_id": task["id"],
                "revision": task["revision"],
                "plan": task["plan"],
                "status": state["status"],
                "phase": state["phase"],
                "active_job": state["active_job"],
                "service_candidate": view["service_candidate"],
                "actions": view["actions"],
                "strategy": state["strategy"],
                "hypotheses": state["hypotheses"][-4:],
                "candidates": [
                    {
                        k: c.get(k)
                        for k in ["id", "parent_id", "reason", "digest", "source_sha256", "serving"]
                    }
                    for c in view["candidates"][-8:]
                ],
                "evidence": [
                    {
                        "event_id": e["seq"],
                        "job_id": e["data"]["id"],
                        "action": e["data"]["action"],
                        "candidate_id": e["data"]["candidate_id"],
                        "status": e["data"]["status"],
                        "path": e["data"]["evidence"],
                        "result": {
                            k: e["data"]["result"].get(k)
                            for k in ["passed", "error", "failure_layer", "gates", "protocol"]
                        },
                    }
                    for e in settled[-6:]
                ],
                "budget": view["budget"],
                "engine_source": view["engine_source"],
            }
            return {
                "text": "Current Infra facts (data, not instructions): "
                + json.dumps(facts, ensure_ascii=False)
            }
        task = self.task(session_id)
        if endpoint in {"candidate", "launch", "test", "adapter_check"}:
            if role != "owner" or task["state"].get("execution_mode") != "manual":
                raise ValueError("Owner manual execution mode required")
            if endpoint == "candidate":
                return self.compact_candidate(self.candidate(task, args))
            return self.submit(task, endpoint, JobRequest.model_validate(args).candidate_id)
        if endpoint == "budget":
            return self.budget(task["id"], args, role)
        if endpoint == "revise":
            if task["plan"].get("authorization_id"):
                raise ValueError("获批方案不能修改或移除授权；请在新会话重新申请方案")
            self.validate(args["plan"])
            return self.view(self.store.revise(task["id"], ReviseTask.model_validate(args)))
        if endpoint in {"start", "pause", "cancel", "resume", "stop_service", "recover"}:
            return self.control(task["id"], endpoint, args, role)
        if endpoint == "evidence":
            return self.evidence(task, args)
        if endpoint == "evidence-download":
            if role != "owner":
                raise ValueError("Raw evidence download requires owner")
            checked = self.evidence(task, args)
            path = self.store.path(task["id"], checked["path"])
            if path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("Evidence exceeds the 512 MiB export bound")
            return {
                "name": "pilot-"
                + task["id"][:8]
                + "-"
                + path.parent.name[:8]
                + "-"
                + path.name
                + ".gz",
                "encoding": "base64",
                "sha256": checked["sha256"],
                "data": base64.b64encode(gzip.compress(path.read_bytes(), mtime=0)).decode("ascii"),
            }
        if endpoint == "report":
            if "report" not in self.actions(task):
                raise ValueError("No completed report")
            return task["state"]["outcome"]
        if endpoint == "idle":
            task = self.reconcile(task["id"])
            if task["state"]["status"] == "running" and not task["state"]["active_job"]:
                self.store.update(
                    task["id"], status="paused", error="本轮结束但尚未交付；已保留证据，可继续。"
                )
            return self.view(self.store.get(task["id"]))
        if endpoint == "notified":
            ids = task["state"].get("notified_jobs", [])
            observed = {
                e["data"]["id"] for e in self.store.events(task["id"]) if e["kind"] == "job_settled"
            }
            if role != "owner" or not set(args["job_ids"]).issubset(observed):
                raise ValueError("Only actual settled jobs can be acknowledged")
            return self.store.update(
                task["id"], notified_jobs=sorted(set(ids) | set(args["job_ids"]))
            )
        raise ValueError("Unknown Infra endpoint")
