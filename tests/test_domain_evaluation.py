"""Independent scoring, immutable admissions and public evidence boundaries."""
import json
from types import SimpleNamespace

import pytest

from pilot_eval.contracts import EvaluationPlan
from pilot_eval.grader import grade, matches
from pilot_eval.problems import problems, public_problem
from pilot_eval.report import current, publish, summarize
from pilot_eval.runner import Runner
from pilot_eval.store import EvaluationStore
from pilot_infra.service import InfraService


def plan(**changes):
    return EvaluationPlan(case_ids=["stop-1"], **changes)


def test_family_partition_and_public_catalog():
    rows = problems()
    assert len(rows) == 20
    assert len([r for r in rows if r["split"] == "development"]) == 12
    assert not {r["family"] for r in rows if r["split"] == "development"} & {r["family"] for r in rows if r["split"] == "held_out"}
    assert "argmax(logits, 0)" not in json.dumps(rows)
    for r in rows:
        assert "injection" not in public_problem(r) and "prompt" not in public_problem(r)
    with pytest.raises(ValueError, match="Held-out"):
        EvaluationPlan(case_ids=["runtime-1"])
    with pytest.raises(ValueError, match="Duplicate"):
        EvaluationPlan(case_ids=["stop-1", "stop-1"])
    with pytest.raises(ValueError, match="Both"):
        plan(input_price_per_million=1)


def test_campaign_budget_serializes_runs_and_prevents_replay(tmp_path):
    s = EvaluationStore(tmp_path)
    one, two = s.freeze(plan(), {}), s.freeze(plan(), {})
    a, created = s.admit(one, "stop-1", "single", 1)
    assert created
    assert s.admit(one, "stop-1", "single", 1) == (a, False)
    with pytest.raises(ValueError, match="unsettled"):
        s.admit(two, "stop-1", "single", 1)
    s.update(a["id"], phase="settled", usage={"input_tokens": 1495000, "output_tokens": 0})
    b, _ = s.admit(two, "stop-1", "single", 1)
    assert b["max_tokens"] == 5000
    s.update(b["id"], phase="settled", usage={"input_tokens": 5000})
    assert s.campaign()["used_tokens"] == 1500000
    three = s.freeze(plan(), {})
    with pytest.raises(ValueError, match="exhausted"):
        s.admit(three, "stop-1", "single", 1)
    with pytest.raises(ValueError, match="immutable"):
        s.update(a["id"], usage={})
    with pytest.raises(ValueError, match="authorized"):
        s.freeze(plan(max_tokens=1500001), {})


def test_frozen_manifest_detects_database_drift(tmp_path):
    s = EvaluationStore(tmp_path)
    identity = s.freeze(plan(), {"model": "original"})
    with s.db() as db:
        db.execute("UPDATE runs SET manifest=? WHERE id=?", ('{}', identity))
    with pytest.raises(ValueError, match="changed"):
        s.manifest(identity)


def test_unsettled_owned_service_blocks_new_trial_even_if_old_receipt_is_unavailable(tmp_path):
    runner = Runner.__new__(Runner)
    runner.store = EvaluationStore(tmp_path)
    runner.assert_identity = lambda _: {}
    runner.service = SimpleNamespace(store=SimpleNamespace(list=lambda: [{"state": {"service_candidate": "c1"}}]))
    with pytest.raises(ValueError, match="reconciliation"):
        runner.start("run", "stop-1", "single")


@pytest.mark.parametrize("healthy,fault,claim,probe,expected,false", [
    (True, True, True, True, True, False),
    (True, True, True, False, False, True),
    (True, True, False, True, False, False),
    (False, True, True, True, False, False),
    (True, False, True, True, False, False),
])
def test_grader_requires_actual_control_negative_and_current_acceptance(monkeypatch, healthy, fault, claim, probe, expected, false):
    task = {"id": "t", "state": {"status": "completed" if claim else "paused", "outcome": {"passed": claim}}}
    service = SimpleNamespace(store=SimpleNamespace(get=lambda _: task))
    monkeypatch.setattr("pilot_eval.grader.independent_probes", lambda *_: {"passed": probe})
    result = grade(service, task, {"healthy": {"passed": healthy}, "fault_detected": fault})
    assert result["passed"] is expected and result["false_completion"] is false


def test_changed_current_worker_identity_cannot_be_success(monkeypatch):
    task = {"id": "t", "state": {"status": "completed", "outcome": {"passed": True}}}
    service = SimpleNamespace(store=SimpleNamespace(get=lambda _: task))
    def rejected(*_):
        raise ValueError("Service identity changed")
    monkeypatch.setattr("pilot_eval.grader.independent_probes", rejected)
    result = grade(service, task, {"healthy": {"passed": True}, "fault_detected": True})
    assert result["false_completion"] and not result["passed"] and result["error"]
    assert not matches({"id": "json", "expected": {"ready": True}}, '```json\n{"ready":true}\n```')


def test_public_report_preserves_failed_denominator_and_hides_private_material(tmp_path):
    s = EvaluationStore(tmp_path)
    identity = s.freeze(plan(strategies=["single", "multi", "auto"]), {"model": "model"})
    for strategy in ["single", "multi", "auto"]:
        row, _ = s.admit(identity, "stop-1", strategy, 1)
        s.update(row["id"], phase="settled", result="budget_stopped", usage={"input_tokens": 100},
                 grade={"baseline_valid": True, "passed": strategy == "single"},
                 error="private-sentinel", interventions=[{"kind": "inspection", "time": 1, "note": "private-sentinel"}])
    report = summarize(s.manifest(identity), s.trials(identity))
    assert report["fully_paired"] and report["used_tokens"] == 300
    assert [r["executed_valid"] for r in report["strategies"]] == [1, 1, 1]
    assert report["pricing"] == "unpriced" and all(r["cost"] is None for r in report["trials"])
    single = {**s.manifest(identity), "plan": {**s.manifest(identity)["plan"], "strategies": ["single"]}}
    assert not summarize(single, s.trials(identity)[:1])["fully_paired"]
    assert "private-sentinel" not in json.dumps(report)
    publish(tmp_path, identity, report)
    assert current(tmp_path)["report"] == report
    older = "a" * 32
    publish(tmp_path, older, {**report, "used_tokens": 123})
    assert len(current(tmp_path)["history"]) == 2
    assert current(tmp_path, identity)["report"]["used_tokens"] == 300
    assert current(tmp_path, older)["report"]["used_tokens"] == 123
    assert current(tmp_path, "../../private")["error"]
    publish(tmp_path, identity, {"protocol": report["protocol"]})
    assert current(tmp_path)["error"] and current(tmp_path)["report"] is None


def test_catalog_owner_read_does_not_resolve_session_or_create_jobs(tmp_path, monkeypatch):
    infra = InfraService(tmp_path, spawn_jobs=False)
    monkeypatch.setattr(infra, "task", lambda *_: pytest.fail("Must not resolve Session"))
    monkeypatch.setattr(infra, "submit", lambda *_: pytest.fail("Must not submit Job"))
    assert len(infra.dispatch("evaluation-catalog", "deleted", "owner", {})["cases"]) == 20
    assert not infra.store.list()
    with pytest.raises(ValueError, match="Owner"):
        infra.dispatch("evaluation-catalog", None, "infra-lead", {})


def test_model_build_evidence_omits_file_manifest_but_web_keeps_exact_raw(tmp_path):
    from test_pilot_infra import create, setup, start
    infra = setup(tmp_path)
    task = start(infra, create(infra))
    job_id = "a" * 32
    infra.store.event(task["id"], "job_submitted", {"id": job_id})
    path = infra.store.path(task["id"], f"jobs/{job_id}/result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {"passed": True, "artifact": {"wheel_sha256": "wheel", "source_digest": "source", "runtime_files": {f"file-{i}": "b" * 64 for i in range(668)}}}
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    projected = infra.evidence(task, {"path": f"jobs/{job_id}/result.json"}, summary=True)
    assert projected["result"]["artifact"]["installed_file_count"] == 668
    assert projected["result"]["artifact"]["wheel_sha256"] == "wheel"
    assert "runtime_files" not in projected["result"]["artifact"]
    assert len(json.dumps(projected)) < 1000
    assert json.loads(infra.evidence(task, {"path": f"jobs/{job_id}/result.json"})["text"]) == raw
    assert path.read_bytes() == before


def test_events_use_official_backwards_cursor_and_unknown_operations_do_not_replay(tmp_path):
    runner = Runner.__new__(Runner)
    runner.store = EvaluationStore(tmp_path)
    identity = runner.store.freeze(plan(), {})
    row, _ = runner.store.admit(identity, "stop-1", "single", 1)
    runner.assert_identity = lambda _: None
    with pytest.raises(ValueError, match="Unknown"):
        runner.poll(row["id"])
    with pytest.raises(ValueError, match="Inspect"):
        runner.recover(row["id"])
    requests = []
    def rpc(_, payload, __):
        requests.append(payload["request"])
        return {"records": [{"event": {"seq": 2 if len(requests) == 1 else 1}}], "hasMore": len(requests) == 1}
    runner.rpc = rpc
    assert [r["event"]["seq"] for r in runner.events({"kind": "session", "sessionId": "s"})] == [1, 2]
    assert requests[1]["beforeSeq"] == 2
