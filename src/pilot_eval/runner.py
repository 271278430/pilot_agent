"""Finite trial scheduling; the installed Harness owns every Agent turn."""

import hashlib
import json
import re
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from pilot_core.config import credentials
from pilot_infra.contracts import InfraPlan
from pilot_infra.engine_source import EngineSource, file_manifest, profile
from pilot_infra.service import InfraService
from pilot_infra.store import digest
from pilot_infra.worker import run
from repopilot.storage import atomic_json

from .grader import PROBES, grade, independent_probes
from .problems import VERSION
from .report import summarize
from .store import EvaluationStore


class Runner:
    def __init__(self, root, origin):
        self.root = Path(root).resolve()
        marker = self.root / ".local/domain-evaluation-qa.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("gpu_ids") != [7]:
            raise ValueError("Real evaluations require an explicitly prepared GPU7 QA root")
        self.store = EvaluationStore(root)
        self.service = InfraService(root, spawn_jobs=False)
        self.origin = origin.rstrip("/")
        parsed = urlsplit(self.origin)
        marker_data = json.loads(marker.read_text())
        if parsed.hostname != "127.0.0.1" or parsed.scheme != "http" or marker_data.get("origin") != self.origin:
            raise ValueError("Evaluation Host must be the registered loopback QA origin")
        entry = urlsplit(json.loads((self.root / ".local/harness-web/entry.json").read_text())["url"])
        self.client = httpx.Client(follow_redirects=True, timeout=45)
        try:
            self.client.get(urlunsplit((parsed.scheme, parsed.netloc, entry.path, entry.query, entry.fragment))).raise_for_status()
        except httpx.HTTPError:
            raise ValueError("QA Owner login failed; inspect private entry without publishing it") from None

    def rpc(self, method, payload, channel="pilot-infra-api"):
        response = self.client.post(self.origin + "/" + channel + "/" + method,
                                    json={"type": "client-request", "rpcId": uuid.uuid4().hex, "method": method,
                                          "payload": {"args": payload} if channel == "api" else payload})
        response.raise_for_status()
        value = response.json()["result"]
        if not value["ok"]:
            raise ValueError(str(value["error"])[:1500])
        return value["value"]

    def identities(self):
        assets = self.service.assets.read()
        recipe = profile(self.root, "sglang-046-python")
        if digest(file_manifest(Path(recipe["source_root"]))) != recipe["source_digest"]:
            raise ValueError("Baseline recipe identity changed")
        return {"protocol": VERSION, "model": credentials()["REPOPILOT_MODEL"],
                "sdk": version("deepseek-harness-sdk"), "runtime": json.loads((self.root / "harness/runtime/node_modules/@deepseek-ai/dsh-session/package.json").read_text())["version"],
                "tool_bundle_sha256": digest({str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for folder in ("src/pilot_eval", "src/pilot_infra", "src/pilot_host", "src/pilot_core", "harness/plugin", "harness/runtime/plugins")
                    for path in sorted((self.root / folder).rglob("*")) if path.is_file() and path.suffix in {".py", ".mjs", ".js"}}),
                "assets_sha256": digest({key: assets[key] for key in ("models", "images", "adapter_image")}),
                "source_digest": recipe["source_digest"], "grader_sha256": digest(PROBES),
                "approval_authorizer_sha256": hashlib.sha256((self.root / ".local/domain-answerer.mjs").read_bytes()).hexdigest(),
                "gpu_ids": [7], "serving": self.serving(), "workload": self.workload()}

    @staticmethod
    def serving():
        return {"context_length": 2048, "mem_fraction_static": .12, "max_new_tokens": 96}

    @staticmethod
    def workload():
        return {"input_lengths": [128], "concurrency": [1], "requests_per_case": 2,
                "max_new_tokens": 24, "timeout_seconds": 60}

    def assert_identity(self, run_id):
        manifest = self.store.manifest(run_id)
        if self.identities() != manifest["identities"]:
            raise ValueError("Evaluation model/assets/protocol changed after freeze")
        return manifest

    def create_task(self, title, strategy, goal, token_limit, deadline):
        assets = self.service.assets.read()
        plan = InfraPlan(title=title, goal=goal, model_asset=assets["models"][0]["id"],
                         image_asset=assets["images"][0]["id"], engine_source_profile="sglang-046-python",
                         strategy=strategy, gpu_ids=[7], serving=self.serving(), workload=self.workload(),
                         max_tokens=token_limit, deadline_seconds=deadline).model_dump()
        return self.rpc("create", {"request_id": uuid.uuid4().hex, "plan": plan})

    def manual(self, task_id, session_id):
        task = self.service.store.get(task_id)
        self.rpc("engine-manual", {"session_id": session_id, "request_id": uuid.uuid4().hex,
                                   "expected_revision": task["revision"]})
        source = EngineSource(self.service)
        source.prepare(self.service.store.get(task_id))
        return source

    def job(self, task_id, action, candidate):
        task = self.service.store.get(task_id)
        job = self.service.submit(task, action, candidate)
        value = run(self.root, job["job_id"])
        path = self.service.store.path(task_id, f"jobs/{job['job_id']}/result.json")
        return {**value, "evaluation_job_id": job["job_id"], "worker_evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def approve(self, task_id, candidate):
        task = self.service.store.get(task_id)
        source = EngineSource(self.service)
        view = source.candidate_view(task, candidate)
        source.decide(task, {"candidate_id": candidate["id"], "expected_digest": view["review_digest"],
                             "decision": "allowed-once"}, "owner-evaluation-setup")

    def healthy_control(self, run_id):
        manifest = self.assert_identity(run_id)
        receipt = self.store.directory / (run_id + "-control.json")
        if receipt.exists():
            value = json.loads(receipt.read_text())
            if value.get("phase") != "settled" or value["identities"] != manifest["identities"]:
                raise ValueError("Original control operation needs inspection; do not replay it")
            return value
        atomic_json(receipt, {"phase": "creating", "identities": manifest["identities"]})
        value = self.create_task("评测健康对照", "single", "验证冻结评测环境的实际服务与独立探针。", 1000, 1800)
        task_id, session_id = value["task"]["id"], value["sessionId"]
        atomic_json(receipt, {"phase": "preparing", "task_id": task_id, "session_id": session_id,
                              "identities": manifest["identities"]})
        checks, cleanup, error = {}, {}, None
        try:
            self.manual(task_id, session_id)
            candidate = self.service.candidate(self.service.store.get(task_id),
                        {"request_id": uuid.uuid4().hex, "reason": "评测初始版本", "serving": self.serving()})
            if not self.job(task_id, "engine_build", candidate["id"])["passed"]:
                raise ValueError("Healthy build failed")
            self.approve(task_id, candidate)
            if not self.job(task_id, "launch", candidate["id"])["identity"]["passed"]:
                raise ValueError("Healthy launch failed")
            if not self.job(task_id, "test", candidate["id"])["passed"]:
                raise ValueError("Healthy acceptance failed")
            checks = independent_probes(self.service, self.service.store.get(task_id))
        except Exception as exc:
            error = str(exc)[:2000]
        finally:
            try:
                cleanup = self.cleanup(task_id)
            except Exception as exc:
                cleanup = {"passed": False, "error": str(exc)[:2000]}
        value = {"phase": "settled", "identities": manifest["identities"], "task_id": task_id,
                 "healthy": checks, "cleanup": cleanup, "error": error, "controller_kind": "deterministic_control"}
        atomic_json(receipt, value)
        if not checks.get("passed") or not cleanup["passed"]:
            raise ValueError("Clean control cannot meet frozen probes; the problem set is unavailable")
        return value

    def cleanup(self, task_id):
        task = self.service.store.get(task_id)
        if task["state"].get("active_job"):
            raise ValueError("Original active Job needs reconciliation before cleanup")
        if task["state"].get("service_candidate"):
            result = self.job(task_id, "stop", task["state"]["service_candidate"])
            if not result.get("passed"):
                raise ValueError("Owned evaluation service cleanup failed")
        task = self.service.store.get(task_id)
        if task["state"]["status"] == "running":
            self.service.control(task_id, "pause", {"request_id": uuid.uuid4().hex, "expected_revision": task["revision"]})
        self.service.store.release(task_id)
        return {"passed": not self.service.store.get(task_id)["state"].get("service_candidate"), "scope": "owned-evaluation-task"}

    def start(self, run_id, case_id, strategy, repetition=1):
        manifest = self.assert_identity(run_id)
        try:
            return self.store.get(digest([run_id, case_id, strategy, repetition])[:32])
        except ValueError:
            pass
        if any(t["state"].get("active_job") or t["state"].get("service_candidate") for t in self.service.store.list()):
            raise ValueError("An owned QA Job/service needs original reconciliation before admission")
        row, created = self.store.admit(run_id, case_id, strategy, repetition)
        if not created:
            return row
        case = next(item for item in manifest["cases"] if item["id"] == case_id)
        if not case["supported"]:
            return self.store.update(row["id"], phase="unavailable", result="unavailable", error=case["unavailable_reason"])
        try:
            control = self.healthy_control(run_id)
        except Exception as exc:
            return self.store.update(row["id"], phase="unavailable", result="unavailable", error=str(exc)[:2000])
        if not control.get("healthy", {}).get("passed") or not control.get("cleanup", {}).get("passed"):
            return self.store.update(row["id"], phase="unavailable", result="unavailable", error="Original healthy control failed; no replay")
        self.store.update(row["id"], phase="creating")
        value = self.create_task("领域评测 · " + case_id, strategy, case["prompt"], row["max_tokens"], manifest["plan"]["deadline_seconds"])
        task_id, session_id = value["task"]["id"], str(value["sessionId"])
        row = self.store.update(row["id"], phase="preparing", task_id=task_id, session_id=session_id)
        source = self.manual(task_id, session_id)
        task = self.service.store.get(task_id)
        injection = case["injection"]
        serving = self.serving()
        self.store.update(row["id"], phase="injecting")
        if injection["kind"] == "stop":
            serving = {**serving, "stop": injection["value"]}
        elif injection["kind"] == "adapter":
            path = self.service.workspace(task) / "adapter.py"
            path.write_text("def build_options():\n    return " + injection["value"] + "\n")
        else:
            path = source.source_path(task, injection["path"])
            text = path.read_text()
            if text.count(injection["old"]) != 1:
                cleanup = self.cleanup(task_id)
                return self.store.update(row["id"], phase="unavailable", cleanup=cleanup, result="unavailable", error="Frozen fixture anchor differs from registered engine source")
            source.patch(task, {"path": injection["path"], "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "old_text": injection["old"], "new_text": injection["new"]})
        candidate = self.service.candidate(self.service.store.get(task_id),
                    {"request_id": uuid.uuid4().hex, "reason": "评测当前版本", "serving": serving})
        self.store.update(row["id"], phase="reproducing", seeded_candidate=candidate["id"])
        build = self.job(task_id, "engine_build", candidate["id"])
        if not build.get("passed"):
            observed = build
        else:
            self.approve(task_id, candidate)
            launch = self.job(task_id, "launch", candidate["id"])
            observed = self.job(task_id, "test", candidate["id"]) if launch.get("identity", {}).get("passed") else launch
        detected = observed.get("passed") is False or bool(observed.get("error"))
        if self.service.store.get(task_id)["state"].get("service_candidate"):
            self.job(task_id, "stop", self.service.store.get(task_id)["state"]["service_candidate"])
        negative_path = self.store.directory / (row["id"] + "-negative.json")
        atomic_json(negative_path, observed)
        baseline = {"healthy": control["healthy"], "fault_detected": detected,
                    "control_sha256": digest(control), "negative_receipt_sha256": digest(observed),
                    "negative_job_id": observed["evaluation_job_id"], "negative_worker_sha256": observed["worker_evidence_sha256"], "seeded_candidate": candidate["id"]}
        if not detected:
            cleanup = self.cleanup(task_id)
            return self.store.update(row["id"], phase="unavailable", baseline=baseline, cleanup=cleanup, result="unavailable", error="Injected version did not fail; it cannot count as a repair problem")
        self.service.store.update(task_id, execution_mode="agent", status="running", error=None,
                                  notified_jobs=[e["data"]["id"] for e in self.service.store.events(task_id) if e["kind"] == "job_settled"],
                                  deadline_epoch=time.time() + manifest["plan"]["deadline_seconds"])
        row = self.store.update(row["id"], phase="prompting", baseline=baseline, agent_started_at=time.time())
        atomic_json(self.root / ".local/evaluation-approved-roots.json", {"session_ids": [session_id]})
        self.rpc("session/prompt", {"request": {"sessionId": session_id, "requestId": "evaluation-" + row["id"],
                 "mode": "queue", "content": [{"type": "text", "text": case["prompt"]}]}}, "api")
        return self.store.update(row["id"], phase="running")

    def poll(self, trial_id):
        row = self.store.get(trial_id)
        self.assert_identity(row["run_id"])
        if row["phase"] in {"settled", "unavailable"}:
            return row
        if row["phase"] != "running":
            raise ValueError("Unknown preparation/prompt side effect; inspect the original trial")
        task = self.service.reconcile(row["task_id"])
        state = task["state"]
        sessions = self.rpc("session/list", {"_request": {}}, "api")["items"]
        running = any(item.get("running") for item in sessions)
        elapsed = time.time() - row["agent_started_at"]
        if elapsed >= self.store.manifest(row["run_id"])["plan"]["deadline_seconds"] and state["status"] == "running" and not row.get("deadline_stop"):
            self.rpc("pause", {"session_id": row["session_id"], "request_id": uuid.uuid4().hex, "expected_revision": task["revision"]})
            return self.store.update(trial_id, deadline_stop=True)
        self.service.view(task)
        usage_path = self.root / "runs/harness-policy" / row["session_id"] / "meter.json"
        usage = json.loads(usage_path.read_text()) if usage_path.is_file() else {}
        self.store.update(trial_id, usage=usage, elapsed_seconds=time.time() - row["started_at"])
        if running or state.get("active_job") or state["status"] == "running":
            return self.store.get(trial_id)
        records = self.events({"kind": "session", "sessionId": row["session_id"]})
        atomic_json(self.store.directory / (trial_id + "-trajectory.json"), records)
        children = set()
        for record in records:
            event = record.get("event", {})
            if event.get("type") == "tool/result":
                for block in event["data"].get("message", {}).get("content", []):
                    children.update(re.findall(r"started subagent ([a-z0-9-]+)", block.get("text", "")))
        for child in children:
            child_records = self.events({"kind": "subagent", "parentSessionId": row["session_id"], "childSessionId": child, "mode": "continuable"})
            atomic_json(self.store.directory / (trial_id + "-child-" + child + ".json"), child_records)
        baseline = row["baseline"]
        control = json.loads((self.store.directory / (row["run_id"] + "-control.json")).read_text())
        negative = json.loads((self.store.directory / (trial_id + "-negative.json")).read_text())
        if digest(control) != baseline["control_sha256"] or digest(negative) != baseline["negative_receipt_sha256"]:
            raise ValueError("Original control/negative evidence changed")
        negative_job = self.service.store.operation(baseline["negative_job_id"])
        negative_raw = self.service.store.path(row["task_id"], f"jobs/{baseline['negative_job_id']}/result.json")
        if negative_job["task_id"] != row["task_id"] or negative_job["data"]["candidate_id"] != row["seeded_candidate"] or hashlib.sha256(negative_raw.read_bytes()).hexdigest() != baseline["negative_worker_sha256"]:
            raise ValueError("Original negative Worker receipt changed")
        verdict = grade(self.service, task, baseline)
        atomic_json(self.store.directory / (trial_id + "-grader.json"), verdict)
        self.store.update(trial_id, phase="cleaning", grade=verdict, usage=usage, experts=sorted(children),
                          expert_calls=[{"tool": e["event"]["data"]["name"], "seq": e["event"]["seq"]} for e in records if e["event"]["type"] == "tool/call" and e["event"]["data"]["name"] in {"repo_specialist", "infra_specialist"}],
                          intervention_count=len(row["interventions"]), trajectory_complete=True,
                          result="passed" if verdict["passed"] else "budget_stopped" if usage.get("stop_reason") else "failed",
                          error=state.get("error"), native_approvals=sum(record.get("event", {}).get("type") == "approval/asked" for record in records))
        cleanup = self.cleanup(row["task_id"])
        return self.store.update(trial_id, phase="settled", cleanup=cleanup, elapsed_seconds=time.time() - row["started_at"])

    def report(self, run_id):
        rows = []
        for trial in self.store.trials(run_id):
            if trial.get("task_id"):
                strategy = self.service.store.get(trial["task_id"])["state"]["strategy"]
                trial = {**trial, "resolved_mode": strategy["mode"], "admitted_expert_roles": strategy["experts"]}
            rows.append(trial)
        return {**summarize(self.store.manifest(run_id), rows), "campaign": self.store.campaign()}

    def events(self, address):
        records, cursor = [], None
        for _ in range(100):
            request = {"address": address, "maxMessages": 200}
            if cursor is not None:
                request["beforeSeq"] = cursor
            page = self.rpc("session/events", {"request": request}, "api")
            records = page["records"] + records
            if not page["hasMore"]:
                return records
            next_cursor = min(r["event"]["seq"] for r in page["records"])
            if cursor is not None and next_cursor >= cursor:
                raise ValueError("Native history cursor did not progress")
            cursor = next_cursor
        raise ValueError("Native history incomplete; cannot report expert/approval totals")

    def recover(self, trial_id):
        """Only resume already recorded cleanup. Never repeat preparation or prompts."""
        row = self.store.get(trial_id)
        if row["phase"] in {"settled", "unavailable"}:
            return row
        if row["phase"] == "running":
            return self.poll(trial_id)
        if row["phase"] != "cleaning":
            raise ValueError("Inspect original side effects; use abort after reconciliation, never replay")
        cleanup = self.cleanup(row["task_id"])
        return self.store.update(trial_id, phase="settled", cleanup=cleanup, elapsed_seconds=time.time() - row["started_at"])

    def intervention(self, trial_id, kind, note):
        row = self.store.get(trial_id)
        return self.store.update(trial_id, interventions=[*row["interventions"], {"kind": kind, "note": note, "time": time.time()}])

    def abort(self, trial_id, note):
        row = self.store.get(trial_id)
        if row["phase"] in {"settled", "unavailable"}:
            return row
        sessions = self.rpc("session/list", {"_request": {}}, "api")["items"]
        if any(item.get("running") for item in sessions):
            if row.get("session_id"):
                task = self.service.store.get(row["task_id"])
                self.rpc("pause", {"session_id": row["session_id"], "request_id": uuid.uuid4().hex, "expected_revision": task["revision"]})
            raise ValueError("Pause requested; wait for original family to settle before abort")
        cleanup = self.cleanup(row["task_id"]) if row.get("task_id") else {"passed": True, "scope": "no admitted task"}
        path = self.root / "runs/harness-policy" / row.get("session_id", "missing") / "meter.json"
        usage = json.loads(path.read_text()) if path.is_file() else row["usage"]
        baseline = row.get("baseline", {})
        valid = baseline.get("fault_detected") and baseline.get("healthy", {}).get("passed")
        return self.store.update(trial_id, phase="settled", result="failed", usage=usage, cleanup=cleanup,
                                 grade={"baseline_valid": bool(valid), "passed": False, "false_completion": False},
                                 error="Owner aborted: " + note, interventions=[*row["interventions"], {"kind": "abort", "note": note, "time": time.time()}],
                                 elapsed_seconds=time.time() - row["started_at"])
