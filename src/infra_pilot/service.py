from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pilot_core.context import write_context
from repopilot.storage import Store, atomic_json, read_json

from .spec import STAGES, InfraSpec, resolve_plan
from .transport import Remote


def diagnose(error: str) -> dict:
    text = error.lower()
    rules = [
        ("model_rate_limit", ["http error 429", "too many requests"], None, "The pinned artifact endpoint rate limited the bounded download retries; retain evidence instead of changing model versions."),
        ("port_conflict", ["address already in use", "port_conflict"], "change_port", "Occupied listener; change only the owned service port."),
        ("download_interrupted", ["incomplete-download", "network connectivity", "connection interrupted"], "package_source", "The package transfer failed; retry the same pinned release from official PyPI."),
        ("model_network", ["network is unreachable", "huggingface.co"], "model_source", "Use the approved mirror and retain the same model revision/hash checks."),
        ("cuda_oom", ["out of memory", "cuda_oom"], None, "Inspect the selected GPU memory and training configuration; no other process is killed."),
        ("driver_or_library", ["driver version is insufficient", "libcuda.so", "undefined symbol"], None, "Check driver/runtime/wheel compatibility; host drivers are outside the recipe."),
        ("native_instruction", ["illegal instruction", "sigill"], None, "Isolate the native import/operator and CPU ISA compatibility."),
        ("collective_timeout", ["nccl", "collective timeout"], None, "Inspect selected devices and collective logs; single-host smoke does not diagnose RDMA."),
    ]
    for category, needles, repair, reason in rules:
        if any(needle in text for needle in needles):
            return {"category": category, "repair": repair, "reason": reason}
    return {"category": "unknown", "repair": None, "reason": "Retain evidence and stop; unknown side effects are not replayed."}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdvanceArgs(Args):
    stage: Literal["probe", "plan", "provision", "download", "cuda_smoke", "nccl_smoke", "inference", "train", "serve_benchmark"]


class InspectArgs(Args):
    wait_seconds: int = Field(default=20, ge=0, le=20)


class RepairArgs(Args):
    action: Literal["change_port", "package_source", "model_source"]


SPECS = {
    "status": (Args, "Inspect the current durable workflow checkpoint; no remote mutation."),
    "advance": (AdvanceArgs, "Execute the next authorized dependency-ordered recipe stage. Jobs are idempotent by journaled ID."),
    "inspect": (InspectArgs, "Reconnect to the known remote job and wait at most 20 seconds; never relaunch an unknown job."),
    "diagnose": (Args, "Classify the latest failed job using deployment/compatibility/communication evidence."),
    "repair": (RepairArgs, "Apply only the diagnosis-approved bounded port/source repair; host drivers and other processes are untouched."),
}


def next_stage(state):
    return next((stage for stage in STAGES if stage not in state["completed"]), None)


def context_summary(state):
    failure = state.get('failed') or {}
    return {'completed': list(state['completed']), 'active': state.get('active') or state.get('pending'),
        'next_stage': next_stage(state), 'failure': {k: str(failure[k])[-800:] for k in ('job','stage','error','log_tail') if k in failure},
        'diagnosis': state.get('diagnosis'), 'repairs': [r['action'] for r in state['repairs']],
        'halt_reason': state.get('halt_reason'), 'artifacts': {stage: 'artifacts/' + stage + '.json' for stage in state['completed']},
        'actions': state['actions']}


class InfraService:
    def __init__(self, run: Path, remote=None):
        self.run, self.store = run, Store(run)
        self.config = read_json(run / "infra-run.json")
        self.spec = InfraSpec.model_validate(self.config["spec"])
        self.remote = remote

    def connection(self):
        if self.remote is None:
            if self.config.get('transport') == 'local':
                from .local_transport import LocalRemote

                self.remote = LocalRemote(self.spec)
            else:
                self.remote = Remote(self.spec, Path(self.config["ssh_env"]), Path(self.config["known_hosts"]))
        return self.remote

    def call(self, tool: str, arguments: dict) -> dict:
        action = "infra-action-" + uuid.uuid4().hex[:12]
        self.store.event("action_start", action_id=action, tool=tool, arguments=arguments)
        state = self.store.load()
        try:
            args = SPECS[tool][0].model_validate(arguments)
            if state.get("pending") and tool not in {"inspect", "status"}:
                raise ValueError("A journaled job needs inspection before another mutation")
            if tool == "status":
                result = context_summary(state)
            elif tool == "advance":
                if state.get("active") or state.get("failed"):
                    raise ValueError("Inspect or diagnose the existing job first")
                if args.stage in state["completed"]:
                    result = {"idempotent": True, "stage": args.stage, "result": state["completed"][args.stage]}
                elif args.stage != next_stage(state):
                    raise ValueError("stage_dependency_violation")
                elif args.stage == "probe":
                    result = self.connection().worker("probe")
                    state["completed"]["probe"] = result
                elif args.stage == "plan":
                    result = resolve_plan(self.spec, state["completed"]["probe"])
                    state["completed"]["plan"] = result
                    atomic_json(self.run / "plan.json", result)
                else:
                    if args.stage in {"cuda_smoke", "nccl_smoke", "inference", "train", "serve_benchmark"}:
                        resolve_plan(self.spec, self.connection().worker("probe"))
                    job = args.stage + "-" + uuid.uuid4().hex[:12]
                    state["pending"] = {"stage": args.stage, "job": job}
                    self.store.checkpoint(state)
                    result = self.connection().worker("start", args.stage, job)
                    state["active"] = state["pending"]
                    state["pending"] = None
                    state["jobs"].append(result)
            elif tool == "inspect":
                active = state.get("active") or state.get("pending")
                if not active:
                    raise ValueError("No recorded remote job to inspect")
                result = self.connection().wait(active["job"], args.wait_seconds)
                if result["status"] == "unknown":
                    raise ValueError("Unknown remote outcome: inspect files; automatic replay is forbidden")
                if result["status"] == "completed":
                    state["completed"][active["stage"]] = result["result"]
                    state["active"], state["pending"] = None, None
                    atomic_json(self.run / "artifacts" / (active["stage"] + ".json"), result["result"])
                    if active["stage"] == "provision":
                        self.connection().fetch("requirements.remote.lock", self.run / "artifacts/requirements.remote.lock")
                elif result["status"] == "failed":
                    state["failed"] = result
                    state["active"], state["pending"] = None, None
                state["latest_job"] = result
                if any(row["job"] == result["job"] for row in state["jobs"]):
                    state["jobs"] = [result if row["job"] == result["job"] else row for row in state["jobs"]]
                else:
                    state["jobs"].append(result)
            elif tool == "diagnose":
                if not state.get("failed"):
                    raise ValueError("No failed job")
                result = diagnose(state["failed"].get("error", "") + "\n" + state["failed"].get("log_tail", ""))
                if result["repair"] == "model_source" and self.spec.model_endpoint == "https://hf-mirror.com":
                    result.update(repair=None, reason="Approved mirror already selected; preserve failure evidence instead of replaying")
                state["diagnosis"] = result
            elif tool == "repair":
                if not state.get("failed") or args.action != state.get("diagnosis", {}).get("repair"):
                    raise ValueError("Repair must match the recorded diagnosis")
                if any(row["action"] == args.action for row in state["repairs"]):
                    raise ValueError("repair_budget_exhausted")
                updated = self.spec.model_dump()
                if args.action == "change_port":
                    updated.update(service_port=self.spec.service_port + 1, inject_port_conflict=False)
                elif args.action == "package_source":
                    updated.update(package_source="pypi")
                else:
                    if self.spec.model_endpoint == "https://hf-mirror.com":
                        raise ValueError("Approved mirror already selected; no alternate source to replay")
                    updated.update(model_endpoint="https://hf-mirror.com")
                self.spec = InfraSpec.model_validate(updated)
                self.connection().write("spec.json", self.spec.model_dump())
                self.config["spec"] = self.spec.model_dump()
                atomic_json(self.run / "infra-run.json", self.config)
                result = {"action": args.action, "failure": state["failed"], "updated_spec": self.spec.model_dump()}
                state["repairs"].append(result)
                state["failed"], state["diagnosis"] = None, None
            else:
                raise ValueError("Unknown tool")
            state["actions"] += 1
            state["error_streak"] = 0
            state["error_signature"] = None
            self.store.checkpoint(state)
            self.store.event("action_end", action_id=action, tool=tool, result=result)
            write_context(self.run, 'infra', self.spec.project, context_summary(state), 'checkpoint.json', budget_tokens=1536)
            if tool == 'inspect' and result.get('status') == 'completed':
                result = {k: result[k] for k in ('job','stage','status')}
                result['artifact'] = 'artifacts/' + result['stage'] + '.json'
            return {"ok": True, "result": result, "next_stage": next_stage(state)}
        except Exception as exc:
            signature = f"{tool}: {exc}"
            state["error_streak"] = state.get("error_streak", 0) + 1 if state.get("error_signature") == signature else 1
            state["error_signature"] = signature
            if state["error_streak"] >= 3:
                state["halt_reason"] = "repeated_tool_error: " + signature
            self.store.checkpoint(state)
            self.store.event("action_error", action_id=action, tool=tool, error=str(exc))
            return {"ok": False, "error": str(exc)}


def serve_mcp(run: Path):
    service = InfraService(run)
    from pilot_core.capabilities import for_role
    from pilot_core.mcp_server import serve

    serve('infra', for_role('ml'), service.call)


if __name__ == "__main__":
    import sys
    serve_mcp(Path(sys.argv[1]))
