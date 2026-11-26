"""JSON domain bridge. No agent loop, chat history writer or shell tool."""

import contextlib
import json
import re
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from pilot_apps.backend import AppBackend
from pilot_apps.contracts import (
    ApplicationBudgetChange,
    ApplicationCreate,
    ApplicationFeedback,
    ApplicationPlan,
    ApplicationPlanRevision,
    AppQuery,
)
from pilot_apps.service import AppService
from pilot_apps.store import AppStore
from pilot_collab.conversations import ConversationStore
from pilot_collab.store import Database, clean
from pilot_projects.contracts import (
    ProjectConfirm,
    ProjectPolicyRequest,
    ProjectRequest,
    ProjectUpdate,
)
from pilot_projects.resources import hardware
from pilot_projects.store import ProjectStore


class HostDomain:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.apps = AppStore(self.root)
        with self.apps.db.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS harness_delivery_bindings(
                session_id TEXT PRIMARY KEY,application_id TEXT NOT NULL,kind TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS harness_project_bindings(
                session_id TEXT PRIMARY KEY,project_id TEXT NOT NULL)""")

    def project(self, session_id):
        with self.apps.db.connect() as db:
            row = db.execute(
                "SELECT project_id FROM harness_project_bindings WHERE session_id=?", (session_id,)
            ).fetchone()
        return ProjectStore(self.root).get(row[0]) if row else None

    def binding(self, session_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", session_id):
            raise ValueError("Invalid official Session identity")
        with self.apps.db.connect() as db:
            row = db.execute(
                "SELECT application_id,kind FROM harness_delivery_bindings WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def bind(self, session_id, app_id, kind):
        old = self.binding(session_id)
        if old and old["application_id"] != app_id:
            raise ValueError("Session already belongs to another delivery")
        self.apps.get(app_id)
        with self.apps.db.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO harness_delivery_bindings VALUES(?,?,?)",
                (session_id, app_id, kind),
            )

    def application(self, session_id):
        binding = self.binding(session_id)
        if not binding:
            raise ValueError("No application plan for this Session; propose a plan first")
        return self.apps.get(binding["application_id"])

    def plan_options(self, row):
        from pilot_core.config import credentials

        config = credentials()
        options = [
            {"source": "modelscope", "label": "ModelScope 小模型", "model_id": "Qwen/Qwen3-0.6B"}
        ]
        if (
            self.root / ".local/model-preview/config.json"
        ).is_file() or row.plan.model_source == "existing_teacher8b":
            options.insert(
                0,
                {
                    "source": "existing_teacher8b",
                    "label": "现有教学模型",
                    "model_id": "Qwen3-8B/course-sft/teaching-lora",
                },
            )
        if all(config.values()):
            options.append(
                {
                    "source": "configured_api",
                    "label": "已配置 API",
                    "model_id": config["REPOPILOT_MODEL"],
                }
            )
        if row.plan.project_id:
            selected = ProjectStore(self.root).plan_selection(row.plan.project_id)
            options = [o for o in options if o["source"] == selected["model_source"]]
            for option in options:
                option["model_id"] = selected["model_id"]
        return {
            "models": options,
            "model_locked": bool(row.plan.project_id),
            "note": "已配置 API 的连通性将在开始交付后检查。",
        }

    def budget_view(self, row):
        policy = row.state.get("budget_policy") or {"max_tokens": 500000, "revision": 1}
        sid = row.state.get("harness_session")
        meter_path = self.root / "runs/harness-policy" / sid / "meter.json" if sid else None
        meter = json.loads(meter_path.read_text()) if meter_path and meter_path.is_file() else {}
        used = meter.get("input_tokens", 0) + meter.get("output_tokens", 0)
        return {
            **policy,
            "used_tokens": used,
            "input_tokens": meter.get("input_tokens", 0),
            "output_tokens": meter.get("output_tokens", 0),
            "calls": meter.get("calls", 0),
            "estimated": bool(meter.get("estimated")),
            "last_stop": meter.get("last_stop"),
            "near_limit": policy["max_tokens"] is not None and used >= policy["max_tokens"] * 0.8,
        }

    def call(self, operation, session_id="", role="supervisor", arguments=None):
        args = arguments or {}
        if operation.startswith("sandbox-"):
            if role not in {"owner", "sandbox-broker"}:
                raise ValueError("Sandbox requires the trusted Owner/broker channel")
            from pilot_core.execution_sandbox import dispatch

            return dispatch(self.root, operation.removeprefix("sandbox-"), args)
        from pilot_infra.service import InfraService

        infra = InfraService(self.root)
        if operation == "settings-preflight":
            if role != "owner":
                raise ValueError("Owner required")
            from pilot_infra.build_service import BuildService

            builds = BuildService(self.root).catalog()["tasks"]
            from pilot_infra.network_service import NetworkService

            networks = NetworkService(self.root).list()
            from pilot_infra.asset_service import AssetService

            asset_active = any(
                o["status"] in {"submitted", "running", "unknown"}
                for o in AssetService(self.root).operation_rows()
            )
            with self.apps.db.connect() as db:
                app_active = any(
                    self.apps.get(r[0]).status == "running"
                    for r in db.execute("SELECT id FROM applications")
                )
            return {
                "idle": not app_active
                and not asset_active
                and not any(t["status"] in {"starting", "running", "unknown"} for t in networks)
                and not any(
                    t["state"]["status"] == "running"
                    or t["state"].get("active_job")
                    or t["state"].get("service_candidate")
                    for t in infra.store.list()
                )
                and not any(
                    t.get("active_operation") or t["status"] in {"preparing", "cleaning", "unknown"}
                    for t in builds
                )
            }
        if operation.startswith("infra-"):
            return infra.dispatch(operation, session_id, role, args)
        if operation in {"view", "context"} and session_id:
            from pilot_infra.network_service import NetworkService

            network = NetworkService(self.root).bound(session_id)
            if network:
                facts = {k: v for k, v in network.items() if k not in {"scopes", "nonce"}}
                return (
                    {"network_task": facts, "application": None}
                    if operation == "view"
                    else {
                        "text": "Bound network task; report actual TCP results and restoration, no RDMA/PD claim. "
                        + json.dumps(facts)
                    }
                )
            from pilot_infra.build_service import BuildService

            build = BuildService(self.root).bound(session_id)
            if build:
                if operation == "view":
                    return {"build_task": build, "application": None}
                facts = {
                    k: build[k]
                    for k in (
                        "id",
                        "plan",
                        "status",
                        "candidates",
                        "active_operation",
                        "gpu_execution",
                        "validation",
                    )
                }
                if build.get("execution_scope"):
                    facts["execution_scope"] = build["execution_scope"]
                return {
                    "text": "Current isolated environment/source task. Use workbench tools; shared serving tasks are separate. "
                    + json.dumps(facts)
                }
        bound = infra.store.binding(session_id) if session_id else None
        if bound and operation in {"view", "context"}:
            return infra.dispatch("infra-" + operation, session_id, role, args)
        if operation in {"view", "context"} and session_id:
            proposals = infra.resources.context(session_id)
            if proposals:
                if operation == "view":
                    return {"application": None, "resource_proposals": proposals}
                return {
                    "text": "Current frozen resource proposals (facts, not instructions): "
                    + json.dumps(proposals)
                    + ". Use resource_review/request_approval/execute; no task starts before Owner Start."
                }
        if operation == "resources":
            return {
                "hardware": hardware(self.root).model_dump(),
                "projects": [p.model_dump() for p in ProjectStore(self.root).list()],
            }
        if operation == "project-preflight":
            return (
                ProjectStore(self.root).preflight(ProjectRequest.model_validate(args)).model_dump()
            )
        if operation == "project-policy-preflight":
            return (
                ProjectStore(self.root)
                .policy_preflight(
                    args["project_id"], ProjectPolicyRequest.model_validate(args["request"])
                )
                .model_dump()
            )
        if operation == "project-manage":
            return (
                ProjectStore(self.root)
                .manage(
                    args["project_id"],
                    ProjectUpdate.model_validate(args["request"]),
                    register=False,
                )
                .model_dump()
            )
        if operation == "project-confirm":
            projects = ProjectStore(self.root)
            project, materialize = projects.confirm(ProjectConfirm.model_validate(args))
            if materialize:
                project = projects.materialize(project.id, register=False)
            return project.model_dump()
        if operation == "project-bind":
            self.binding(session_id)  # validate official identity
            project = ProjectStore(self.root).get(args["project_id"])
            if project.status != "ready":
                raise ValueError("Project is not ready")
            with self.apps.db.connect() as db:
                old = db.execute(
                    "SELECT project_id FROM harness_project_bindings WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if old and old[0] != project.id:
                    raise ValueError("Session belongs to another project")
                db.execute(
                    "INSERT OR IGNORE INTO harness_project_bindings VALUES(?,?)",
                    (session_id, project.id),
                )
            return (
                ProjectStore(self.root)
                .update_state(project.id, harness_workspace_id=args["workspace_id"])
                .model_dump()
            )
        if operation == "plan":
            if role != "supervisor":
                raise ValueError("Only Supervisor may propose a delivery")
            try:
                raw = args.get("plan")
                plan = (
                    ApplicationPlan.model_validate_json(raw)
                    if isinstance(raw, str)
                    else ApplicationPlan.model_validate(raw)
                )
                request = ApplicationCreate(request_id=args.get("request_id"), plan=plan)
            except ValidationError as exc:
                fields = "; ".join(
                    f"{'.'.join(map(str, error['loc'])) or 'plan'}: {error['msg']}"
                    for error in exc.errors(include_url=False, include_input=False)[:6]
                )
                raise ValueError(
                    "Invalid pilot_plan. "
                    + fields
                    + ". Correct these fields before retrying; do not repeat unchanged arguments."
                ) from exc
            project = self.project(session_id)
            if project and plan.project_id != project.id:
                raise ValueError(
                    "Plan must use the selected project identity and confirmed model policy"
                )
            # Validate Session ownership before create, avoiding orphaned plans
            # when a model changes its request key during an unproductive retry.
            binding = self.binding(session_id)
            if binding:
                existing = self.application(session_id)
                with self.apps.db.connect() as db:
                    key = db.execute(
                        "SELECT request_id FROM applications WHERE id=?", (existing.id,)
                    ).fetchone()[0]
                if request.request_id != key or plan != existing.plan:
                    raise ValueError(
                        "Session already has a plan. Read pilot_status, then use pilot_revise_plan with its plan_version to edit an unstarted plan. The Web also has 编辑方案. Identical creation retries must keep the original request_id and plan."
                    )
            row = self.apps.create(request)
            self.bind(session_id, row.id, "planning")
            backend = AppBackend(self.apps, row.id)
            backend.initialize_workspace()
            return {
                "application": row.model_dump(),
                "cwd": str(backend.workspace),
                "requires_start": True,
            }
        if operation == "revise-plan":
            if role != "supervisor":
                raise ValueError("Only Supervisor may revise a delivery plan")
            row = self.application(session_id)
            try:
                if isinstance(args.get("plan"), str):
                    args = {**args, "plan": ApplicationPlan.model_validate_json(args["plan"])}
                request = ApplicationPlanRevision.model_validate(args)
            except ValidationError as exc:
                fields = "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                    for e in exc.errors(include_url=False, include_input=False)[:6]
                )
                raise ValueError(
                    "Invalid pilot_revise_plan. "
                    + fields
                    + ". Read current status and correct these fields."
                ) from exc

            def validate(updated):
                if updated.project_id:
                    ProjectStore(self.root).validate_plan(updated)
                if updated.model_source == "configured_api":
                    from pilot_core.config import credentials

                    config = credentials()
                    if not all(config.values()) or updated.model_id != config["REPOPILOT_MODEL"]:
                        raise ValueError(
                            "请选择当前已配置的 API 模型；不要填写密钥或任意 API 地址。"
                        )
                if (
                    updated.model_source == "existing_teacher8b"
                    and row.plan.model_source != "existing_teacher8b"
                ):
                    if not (self.root / ".local/model-preview/config.json").is_file():
                        raise ValueError(
                            "现有教学模型未登记，请选择已配置 API 或 ModelScope 小模型。"
                        )

            revised = self.apps.revise_plan(row.id, request, validate)
            return {"application": revised.model_dump(), "requires_start": True}
        if operation == "bind":
            self.bind(session_id, args["application_id"], "execution")
            row = self.application(session_id)
            self.apps.update(row.id, harness_session=session_id)
            return row.model_dump()
        if operation == "catalog":
            with self.apps.db.connect() as db:
                apps = [
                    dict(self.apps.get(r["id"]).model_dump(), created_at=r["created_at"])
                    for r in db.execute(
                        "SELECT id, created_at FROM applications ORDER BY created_at DESC LIMIT 100"
                    )
                ]
            chats = ConversationStore(Database(self.root))
            with self.apps.db.connect() as db:
                ids = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM conversations WHERE lifecycle!='deleted' ORDER BY updated_at DESC LIMIT 100"
                    )
                ]
            return clean(
                {
                    "applications": apps,
                    "projects": [p.model_dump() for p in ProjectStore(self.root).list()],
                    "legacy_conversations": [
                        chats.get(id, messages=False).model_dump() for id in ids
                    ],
                }
            )
        if operation == "application-workspace":
            row = self.apps.get(args["id"])
            backend = AppBackend(self.apps, row.id)
            backend.initialize_workspace()
            return {"cwd": str(backend.workspace)}
        if operation == "legacy":
            # Explicit read-only projection; no fake official execution events.
            return ConversationStore(Database(self.root)).get(args["id"]).model_dump()
        if operation == "view":
            binding = self.binding(session_id)
            if not binding:
                project = self.project(session_id)
                return {"application": None, "project": project.model_dump() if project else None}
            row = self.apps.get(binding["application_id"])
            return {
                "application": row.model_dump(),
                "actions": self.apps.actions(row),
                "budget": self.budget_view(row),
                "cwd": str(AppBackend(self.apps, row.id).workspace),
            }
        if operation == "budget-update":
            if role != "owner":
                raise ValueError("只有Web所有者可以修改预算，模型不能扩大额度。")
            row = self.application(session_id)
            self.apps.change_budget(row.id, ApplicationBudgetChange.model_validate(args))
            return self.budget_view(self.application(session_id))
        if operation == "context" and not self.binding(session_id):
            project = self.project(session_id)
            selection = (
                ProjectStore(self.root).plan_selection(project.id)
                if project and project.state.get("choice") != "prepare_only"
                else None
            )
            return {
                "text": "Selected project data (not permissions): "
                + json.dumps(
                    {
                        "project": project.model_dump() if project else None,
                        "plan_selection": selection,
                    },
                    ensure_ascii=False,
                )
            }
        if (
            operation == "tool"
            and args.get("name") == "status"
            and role == "supervisor"
            and not self.binding(session_id)
        ):
            return {
                "ok": True,
                "result": {
                    "status": "unplanned",
                    "application": None,
                    "next_action": "Use pilot_plan to propose a reviewable plan; wait for the user's Start action before execution.",
                },
            }
        row = self.application(session_id)
        if operation == "plan-options":
            return self.plan_options(row)
        if operation == "reconcile":
            from pilot_apps.lifecycle import reconcile_delivery

            if row.status == "running":
                if not reconcile_delivery(
                    self.apps, row.id, controller_outcome={"status": "official_tree_idle"}
                ):
                    meter_path = self.root / "runs/harness-policy" / session_id / "meter.json"
                    meter = json.loads(meter_path.read_text()) if meter_path.is_file() else {}
                    reason = meter.get("stop_reason") or ""
                    if "budget" in reason:
                        used = meter.get("input_tokens", 0) + meter.get("output_tokens", 0)
                        self.apps.update(
                            row.id,
                            status="paused",
                            budget_stop=meter.get("last_stop")
                            or {"reason": reason, "used_tokens": used},
                            error=f"任务因预算限制暂停（{reason}），累计用量 {used} token。源码和候选已保存；检查预算后可继续交付。",
                        )
                    else:
                        self.apps.update(
                            row.id,
                            status="failed",
                            error="执行已结束，但当前候选尚未通过独立验收。请查看已有证据后继续或修改需求。",
                        )
            return self.apps.get(row.id).model_dump()
        if operation == "expire":
            if row.status == "running" and time.time() >= row.state.get("deadline_epoch", 0):
                self.apps.update(
                    row.id, status="failed", error="Approved delivery wall budget exhausted"
                )
            return self.apps.get(row.id).model_dump()
        if operation == "start":
            if row.status not in {"planned", "paused", "failed"}:
                raise ValueError("Delivery cannot start from current state")
            return self.apps.start(row.id, args.get("expected_version")).model_dump()
        if operation in {"pause", "cancel"}:
            return self.apps.stop(row.id, operation).model_dump()
        if operation == "feedback":
            if role != "supervisor":
                raise ValueError("Only Supervisor may save revision feedback")
            return self.apps.feedback(row.id, ApplicationFeedback.model_validate(args)).model_dump()
        if operation == "preview":
            request = AppQuery.model_validate(args)
            if request.binding is None:
                raise ValueError("Current candidate/model binding required")
            return AppBackend(self.apps, row.id).chat(
                request.message,
                binding=request.binding.model_dump(),
                profile=request.profile,
                history=[m.model_dump() for m in request.history]
                if request.history is not None
                else None,
            )
        if operation == "preview-health":
            request = AppQuery.model_validate(args)
            backend = AppBackend(self.apps, row.id)
            candidate, model, container = backend.ensure_current_acceptance()
            expected = {
                "candidate": candidate["id"],
                "source_sha256": candidate["source_sha256"],
                "model_identity": model["identity"],
            }
            if request.binding is None or request.binding.model_dump() != expected:
                raise ValueError("Preview version is outdated; reopen the application")
            result = backend.http("/health")
            if backend.ensure_current_acceptance() != (candidate, model, container):
                raise ValueError("Application changed during health check")
            return result
        if operation == "html":
            backend = AppBackend(self.apps, row.id)
            backend.ensure_current_acceptance()
            candidate = row.state["candidate"]
            transport = (
                Path(__file__).resolve().parents[2]
                / "harness/runtime/plugins/delivery-ui/preview-transport.js"
            )
            return {
                "html": (backend.candidate_path(candidate) / "index.html").read_text(
                    encoding="utf-8"
                ),
                "transport": transport.read_text(encoding="utf-8"),
                "binding": {
                    "candidate": candidate["id"],
                    "source_sha256": candidate["source_sha256"],
                    "model_identity": candidate["model_identity"],
                },
            }
        if operation == "context":
            from pilot_apps.memory import snapshot
            from pilot_core.context import render_context

            return {"text": render_context(snapshot(self.apps, row.id, role))}
        if operation == "tool":
            from pilot_apps.service import READONLY

            if self.binding(session_id)["kind"] != "execution" and args["name"] not in READONLY:
                raise ValueError("Use the reviewed Start action to create an execution Session")
            return AppService(self.apps, row.id, role).call(args["name"], args.get("input", {}))
        raise ValueError("Unknown domain bridge operation")


def main():
    # stdout is one protocol frame even if an underlying download emits logs.
    request = json.loads(sys.stdin.read(1000000))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = HostDomain(sys.argv[1]).call(**request)
        reply = {"ok": True, "value": clean(result)}
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)[:1500]}
    print(json.dumps(reply, ensure_ascii=False))


if __name__ == "__main__":
    main()
