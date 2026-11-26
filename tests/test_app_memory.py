import uuid

from pilot_apps.contracts import ApplicationFeedback
from pilot_apps.memory import refresh_sessions, snapshot
from tests.test_goal_applications import new


def test_role_context_retains_goal_failure_and_current_version_under_budget(tmp_path):
    store, row = new(tmp_path)
    store.update(
        row.id,
        status="completed",
        candidate={"id": "c2", "model_identity": "m"},
        last_error={"tool": "verify", "detail": "响应版本不符"},
        sessions=[{"id": str(i), "role": "repo", "directory": f"sessions/{i}"} for i in range(300)],
    )
    store.feedback(
        row.id,
        ApplicationFeedback(request_id=uuid.uuid4().hex, message="保留当前界面，将示例调整为三道"),
    )
    context = snapshot(store, row.id, "repo", 1500)
    text = "\n".join(b.content for b in context.blocks)
    assert context.total_tokens <= 1500
    assert row.plan.goal in text and "c2" in text and "三道" in text
    assert "sessions/" not in text
    assert context.model_token_count_exact is False


def test_context_refresh_is_task_scoped_and_preserves_recovery_logs(tmp_path):
    store, row = new(tmp_path)
    folder = store.path(row.id, "sessions/00-repo")
    folder.mkdir(parents=True)
    (folder / "checkpoint.json").write_text('{"actions":7}')
    store.update(
        row.id,
        sessions=[{"id": "repo", "role": "repo", "directory": "sessions/00-repo"}],
        last_error={"detail": "当前候选未通过接口检查"},
    )
    refresh_sessions(store, row.id)
    assert "未通过接口检查" in (folder / "context.json").read_text()
    assert (folder / "checkpoint.json").read_text() == '{"actions":7}'
    assert (folder / "context-manifest.json").is_file()




def test_tool_activity_records_pending_before_returned_result(tmp_path):
    from pilot_apps.service import AppService
    from pilot_collab.public_trace import project_event

    store, row = new(tmp_path)
    AppService(store, row.id, "repo").call("read", {"path": "app.py"})
    events = store.events(row.id)
    assert events[-2]["data"]["status"] == "requested"
    assert events[-1]["data"]["ok"] is True
    pending = project_event(events[-2])
    assert pending.outcome == "requested"
