import json
import uuid

import pytest

from pilot_apps.backend import AppBackend
from pilot_apps.contracts import ApplicationCreate, ApplicationFeedback, ApplicationPlan
from pilot_apps.fixtures import CLIENT
from pilot_apps.service import AppService
from pilot_apps.store import AppStore, source_hash


def plan(**changes):
    return ApplicationPlan(
        **{
            "title": "模型问答应用",
            "goal": "创建一个可真实调用模型的中文问答应用",
            "model_source": "existing_teacher8b",
            "application_requirements": ["输入问题并显示答案", "预置示例问题"],
            "acceptance_examples": ["1+2是多少", "解释二次函数"],
            **changes,
        }
    )


def new(root):
    store = AppStore(root)
    row = store.create(ApplicationCreate(request_id=uuid.uuid4().hex, plan=plan()))
    return store, row




def test_plan_model_source_and_safe_repository_are_consistent():
    for changes in [
        {"model_id": "another-model"},
        {"model_source": "modelscope", "model_id": "../../secret"},
        {"model_source": "modelscope", "model_id": "https://evil/x"},
        {"download_limit_mb": 99999},
        {"runtime": "cuda"},
    ]:
        with pytest.raises(ValueError):
            plan(**changes)
    assert plan(model_source="modelscope", model_id="Qwen/Qwen3-0.6B").runtime == "cpu"


def test_operation_journal_blocks_unknown_replay_and_stopped_task(tmp_path):
    store, row = new(tmp_path)
    store.update(row.id, status="running")
    claim = store.claim(row.id, "deploy")
    with pytest.raises(ValueError, match="unknown outcome"):
        store.claim(row.id, "deploy")
    store.settle(claim, {"ok": True})
    store.update(row.id, status="paused")
    assert (
        AppService(store, row.id, "repo").call("write", {"path": "app.py", "content": "x"})["ok"]
        is False
    )
    assert AppService(store, row.id, "infra").call("write", {})["ok"] is False


def test_model_preparation_recovers_workspace_without_overwriting_source(tmp_path, monkeypatch):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    config = backend.directory / "model-connection.json"
    config.write_text("{}")
    monkeypatch.setattr(
        "pilot_apps.backend.call_engine",
        lambda *a, **k: {
            "model_id": "Qwen3-8B/course-sft/teaching-lora",
            "model_identity": "i",
            "protocol": "model-preview-v2",
        },
    )
    assert backend.prepare_model()["reused"]
    assert (backend.workspace / "model_client.py").read_text() == CLIENT
    backend.write("app.py", "user-source")
    backend.prepare_model()
    assert (backend.workspace / "app.py").read_text() == "user-source"
    (backend.workspace / "model_client.py").write_text("modified")
    with pytest.raises(ValueError, match="Protected"):
        backend.prepare_model()


def test_candidate_changes_invalidate_acceptance_and_bound_retries(tmp_path):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("app.py", "a")
    backend.write("index.html", "b")
    store.update(
        row.id,
        model={"identity": "i"},
        source_test={"passed": True, "source_sha256": source_hash(backend.workspace)},
    )
    candidate = backend.candidate()
    store.update(
        row.id, status="running", verification={"passed": True, "candidate": candidate["id"]}
    )
    backend.write("app.py", "new")
    assert store.get(row.id).state["verification"] is None
    assert not AppService(store, row.id, "supervisor").call("finish", {})["ok"]
    store.update(row.id, candidates=[{}, {}, {}])
    with pytest.raises(ValueError, match="budget"):
        backend.candidate()


def test_finish_requires_real_current_candidate_acceptance(tmp_path, monkeypatch):
    store, row = new(tmp_path)
    store.update(
        row.id,
        status="running",
        candidate={"id": "c2"},
        verification={"passed": True, "candidate": "c1"},
    )
    service = AppService(store, row.id, "supervisor")
    assert not service.call("finish", {})["ok"]
    assert store.get(row.id).status == "running"
    store.update(row.id, verification={"passed": True, "candidate": "c2"})
    checked = []
    monkeypatch.setattr(AppBackend, "ensure_current_acceptance", lambda self, **kw: checked.append(kw))
    assert service.call("finish", {})["ok"] and store.get(row.id).status == "completed"
    assert checked == [{"completing": True}]


def test_operator_revision_preserves_source_and_waits_for_confirmation(tmp_path):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("app.py", "existing source")
    store.update(
        row.id,
        status="completed",
        candidate={"id": "c1"},
        model={"identity": "m"},
        verification={"passed": True, "candidate": "c1"},
    )
    request = ApplicationFeedback(
        request_id=uuid.uuid4().hex, message="将当前网页示例问题修正为三道"
    )
    revised = store.feedback(row.id, request)
    assert revised.status == "paused" and revised.state["verification"] is None
    assert revised.state["candidate"]["id"] == "c1" and revised.state["model"]["identity"] == "m"
    assert (backend.workspace / "app.py").read_text() == "existing source"
    assert store.feedback(row.id, request) == revised
    assert len(revised.state["feedback"]) == 1
    store.update(row.id, status="running")
    with pytest.raises(ValueError):
        store.feedback(
            row.id, ApplicationFeedback(request_id=uuid.uuid4().hex, message="修改另一个按钮")
        )




def test_model_download_rejects_path_budget_and_custom_code(tmp_path, monkeypatch):
    import httpx

    from pilot_apps.model_prepare import download

    files = [{"Path": "../bad.safetensors", "Size": 1, "Type": "blob"}]
    real = httpx.Client

    def handle(request):
        return httpx.Response(200, json={"Data": {"Files": files}})

    monkeypatch.setattr(
        "pilot_apps.model_prepare.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(handle), **kw),
    )
    with pytest.raises(ValueError, match="Unsafe"):
        download(plan(model_source="modelscope", model_id="Qwen/Qwen3-0.6B"), tmp_path / "assets")
    files[:] = [{"Path": "model.safetensors", "Size": 200 * 1024 * 1024, "Type": "blob"}]
    with pytest.raises(ValueError, match="budget"):
        download(
            plan(model_source="modelscope", model_id="Qwen/Qwen3-0.6B", download_limit_mb=100),
            tmp_path / "assets2",
        )


def test_proposal_retains_explicit_ui_example_count_not_only_test_inputs():
    from pilot_apps.contracts import propose

    class Router:
        def complete(self, *args, **kwargs):
            return json.dumps(
                {"explanation": "建议复用现有模型开发备课网页", "plan": plan().model_dump()}
            )

    _, result = propose(Router(), "设计教师备课助手网页，给出三条示例，先不执行", [], {})
    assert any("页面提供 3 条示例" in text for text in result.application_requirements)
    assert result.acceptance_examples == ["1+2是多少", "解释二次函数"]
