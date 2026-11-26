"""Plan changes use the same domain operation from chat and Web, with no execution."""
import json
import uuid

import pytest

from pilot_host.bridge import HostDomain
from tests.test_goal_applications import plan


@pytest.fixture
def draft(tmp_path, monkeypatch):
    monkeypatch.setattr("pilot_core.config.credentials", lambda: {
        "REPOPILOT_MODEL": "glm-5.3", "REPOPILOT_API_KEY": "fixture-only", "REPOPILOT_BASE_URL": "http://fixture",
    })
    host = HostDomain(tmp_path)
    created = host.call("plan", "planner", arguments={"request_id": "original-plan-key", "plan": plan().model_dump()})
    host.call("bind", "executor", arguments={"application_id": created["application"]["id"]})
    return host, created["application"]["id"]


def api_revision(**changes):
    return {"request_id": "revision-api-key", "expected_version": 1,
            "plan": plan(model_source="configured_api", model_id="glm-5.3").model_dump(), **changes}


def test_unstarted_plan_revision_updates_both_bindings_without_execution(draft):
    host, id = draft
    original = host.application("planner")
    result = host.call("revise-plan", "planner", arguments=api_revision())
    assert result["application"]["id"] == id and result["requires_start"]
    row = host.application("executor")
    assert row.status == "planned" and row.state["harness_session"] == "executor"
    assert row.state["plan_version"] == 2 and row.plan.model_source == "configured_api"
    assert row.plan.application_requirements == original.plan.application_requirements
    assert not row.state.get("model") and not row.state.get("candidate")
    assert host.call("view", "planner")["actions"]["edit_plan"]
    assert json.loads((host.apps.directory(id) / "plan.json").read_text()) == row.plan.model_dump()
    with host.apps.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM application_operations").fetchone()[0] == 0
    event = host.apps.events(id)[-1]
    assert event["kind"] == "plan_revised" and event["data"]["previous_plan"] == original.plan.model_dump()


def test_revision_retry_is_idempotent_and_stale_edit_cannot_overwrite(draft):
    host, id = draft
    first = host.call("revise-plan", "planner", arguments=api_revision())
    assert host.call("revise-plan", "executor", arguments=api_revision()) == first
    with pytest.raises(ValueError, match="key reused"):
        host.call("revise-plan", "planner", arguments=api_revision(plan=plan(title="另一个标题").model_dump()))
    with pytest.raises(ValueError, match="方案已被更新"):
        host.call("revise-plan", "executor", arguments=api_revision(request_id="another-revision-key"))
    assert len(host.apps.events(id)) == 1


def test_start_requires_reviewed_version_and_prevents_future_plan_mutation(draft):
    host, id = draft
    host.call("revise-plan", "planner", arguments=api_revision())
    with pytest.raises(ValueError, match="重新查看"):
        host.call("start", "executor", arguments={"expected_version": 1})
    assert host.apps.get(id).status == "planned"
    host.call("start", "executor", arguments={"expected_version": 2})
    with pytest.raises(ValueError, match="方案已锁定"):
        host.call("revise-plan", "planner", arguments=api_revision(expected_version=2, request_id="after-start-key"))
    host.call("pause", "executor")
    assert not host.call("view", "planner")["actions"]["edit_plan"]
    assert host.apps.get(id).plan.model_id == "glm-5.3"


@pytest.mark.parametrize("changes", [
    {"plan": plan(model_source="configured_api", model_id="unregistered-api").model_dump()},
    {"plan": dict(plan().model_dump(), runtime="cuda")},
    {"plan": dict(plan().model_dump(), model_source="arbitrary")},
    {"expected_version": 0},
])
def test_bad_revision_preserves_original_draft(draft, changes):
    host, id = draft
    before = host.apps.get(id)
    with pytest.raises(ValueError):
        host.call("revise-plan", "planner", arguments=api_revision(**changes))
    assert host.apps.get(id) == before


def test_plan_revision_role_and_project_identity_are_enforced(draft):
    host, id = draft
    with pytest.raises(ValueError, match="Only Supervisor"):
        host.call("revise-plan", "planner", "repo", api_revision())
    with pytest.raises(ValueError):
        host.call("revise-plan", "planner", arguments=api_revision(plan=plan(project_id=uuid.uuid4().hex).model_dump()))
    assert host.apps.get(id).state["plan_version"] == 1


def test_unknown_operations_hide_controls_and_block_revision_or_feedback(draft):
    host, id = draft
    host.call("start", "executor")
    host.apps.claim(id, "prepare_model")
    host.call("pause", "executor")
    actions = host.call("view", "planner")["actions"]
    assert not actions["edit_plan"] and not actions["feedback"] and not actions["start"]
    assert actions["blocked_reason"]
    with pytest.raises(ValueError, match="尚未确认"):
        host.call("feedback", "planner", arguments={"request_id": "unknown-feedback-key", "message": "增加学习画像"})


def test_saved_functional_feedback_preserves_model_and_candidate_without_running(draft):
    host, id = draft
    host.apps.update(id, status="completed", candidate={"id": "c1"}, model={"identity": "m"}, verification={"passed": True})
    request = {"request_id": "feedback-chat-key", "message": "增加个性化学习画像，并支持对话历史"}
    result = host.call("feedback", "planner", arguments=request)
    assert result["status"] == "paused" and result["state"]["verification"] is None
    assert result["state"]["candidate"] == {"id": "c1"} and result["state"]["model"] == {"identity": "m"}
    assert host.call("feedback", "executor", arguments=request) == result
    assert host.call("view", "executor")["actions"]["start"]
    with pytest.raises(ValueError, match="Only Supervisor"):
        host.call("feedback", "planner", "infra", request)


def test_single_serialized_plan_revision_and_registered_model_options(draft):
    host, _ = draft
    options = host.call("plan-options", "planner")
    assert any(o["source"] == "configured_api" and o["model_id"] == "glm-5.3" for o in options["models"])
    assert "fixture-only" not in json.dumps(options)
    payload = api_revision()
    payload["plan"] = json.dumps(payload["plan"])
    assert host.call("revise-plan", "planner", arguments=payload)["application"]["plan"]["model_id"] == "glm-5.3"


def test_execution_artifact_locks_a_legacy_draft_even_without_state_flags(draft):
    host, id = draft
    (host.apps.directory(id) / "model-connection.json").write_text("{}")
    assert not host.call("view", "planner")["actions"]["edit_plan"]
    with pytest.raises(ValueError, match="方案已锁定"):
        host.call("revise-plan", "planner", arguments=api_revision())
