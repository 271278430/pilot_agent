import json
from pathlib import Path

from repopilot.demo import DEMO_ACTIONS, create_repo, run_demo
from repopilot.harness import execute, prepare
from repopilot.mock_model import ScriptedModel
from repopilot.models import Budget
from repopilot.storage import read_json


def test_real_harness_runtime_calls_tools_and_accepts_patch(tmp_path):
    report = run_demo(tmp_path / "demo")
    assert report["framework"] == "deepseek-harness"
    assert report["status"] == "completed"
    assert report["passed"]
    assert report["actions"] == 10
    assert report["usage_source"] == "synthetic_fixture"
    assert report["meter"]["calls"] >= 11
    requests = read_json(Path(report["run"]) / "mock-requests.json")
    assert all(name.startswith("mcp__repopilot__") or name == "skill" for req in requests for name in [t["function"]["name"] for t in req.get("tools", [])])


def test_native_policy_stops_a_loop_at_step_budget(tmp_path):
    report = run_demo(tmp_path / "loop", mode="loop", budget=Budget(max_steps=3))
    assert report["meter"]["steps"] == 3
    assert report["meter"]["stop_reason"] == "step_budget_exhausted"
    assert not report["passed"]


def test_native_session_restores_history_and_workspace(tmp_path):
    task = create_repo(tmp_path / "fixture")
    with ScriptedModel(DEMO_ACTIONS) as model:
        run = prepare(task, tmp_path / "runs", "scripted-fixture", model.url, "local", policy="scripted")
        first = execute(run, "scripted-key")
        second = execute(run, "scripted-key", resume=True)
    assert first["passed"] and second["passed"]
    assert first["session_id"] == second["session_id"]
    assert second["actions"] == first["actions"]
    assert second["meter"]["steps"] > first["meter"]["steps"]


def test_native_compaction_keeps_external_evidence(tmp_path):
    report = run_demo(tmp_path / "pressure", mode="pressure", budget=Budget(context_tokens=4096, output_tokens=256, max_tokens=200000))
    run = Path(report["run"])
    events = [json.loads(line)["event"] for line in (run / "harness-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "compaction/summary" for e in events)
    assert read_json(run / "evidence.json")["excluded_hypotheses"]
    assert report["passed"]


def test_native_provider_retries_transient_rate_limit(tmp_path):
    report = run_demo(tmp_path / "rate-limit", mode="rate_limit")
    assert report["passed"]
    assert report["meter"]["calls"] > 11


def test_native_policy_refuses_request_beyond_token_budget(tmp_path):
    report = run_demo(tmp_path / "tokens", budget=Budget(max_tokens=1500))
    assert not report["passed"]
    assert report["meter"]["calls"] == 0
    assert report["meter"]["stop_reason"] == "token_budget_reservation_exhausted"
