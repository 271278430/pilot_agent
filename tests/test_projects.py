"""Project admission, persistent identity and actual model-selection boundaries."""



import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pilot_apps.contracts import ApplicationCreate, ApplicationPlan
from pilot_apps.service import AppService, capabilities
from pilot_apps.store import AppStore
from pilot_delivery.preview_contract import PreviewRequest
from pilot_projects.api_engine import generate_api
from pilot_projects.contracts import (
    GPUResource,
    Hardware,
    ProjectConfirm,
    ProjectPolicyRequest,
    ProjectRequest,
    ProjectUpdate,
)
from pilot_projects.github import canonical_url, metadata
from pilot_projects.resources import assess_model
from pilot_projects.store import ProjectStore, workspace_call


def uid():

    return uuid.uuid4().hex





def machine(**changes):

    return Hardware(

        **{

            "cpu_count": 8,

            "available_ram_gb": 32,

            "free_disk_gb": 50,

            "gpus": [],

            "gpu_probe": "absent",

            "api_available": True,

            "api_model": "glm-5.3",

            **changes,

        }

    )





def proposal(**changes):

    return ProjectRequest(**{"request_id": uid(), "title": "模型服务项目", **changes})





class Registry:

    def __init__(self):

        self.rows = {}



    def __call__(self, root, operation, **args):

        if operation == "create":

            id = uid()

            self.rows[id] = {"id": id, **args}

            return self.rows[id]

        row = self.rows[args["id"]]

        row["title"] = args["title"]

        return row





def setup(root, **options):

    return ProjectStore(

        root,

        probe=options.pop("probe", lambda _: machine()),

        registry=options.pop("registry", Registry()),

        **options,

    )





def confirm(report, **changes):

    return ProjectConfirm(

        **{

            "assessment_id": report.id,

            "request_id": uid(),

            "fingerprint": report.fingerprint,

            "choice": report.recommended,

            "acknowledge_warnings": True,

            **changes,

        }

    )





def ready(store, request=None, **choices):

    assessment = store.preflight(request or proposal())

    row, new = store.confirm(confirm(assessment, **choices))

    assert new

    return store.materialize(row.id)





@pytest.mark.parametrize(

    "url",

    [

        "http://github.com/o/r",

        "https://evil.example/o/r",

        "git@github.com:o/r",

        "https://token@github.com/o/r",

        "https://github.com/o/r/tree/main",

        "https://github.com/o/r?token=x",

        "https://github.com/../r",

        "https://github.com:443/o/r",

        "https://github.com/o/r#x",

    ],

)

def test_import_does_not_admit_arbitrary_or_credential_urls(url):

    with pytest.raises(ValueError):

        canonical_url(url)

    with pytest.raises(ValueError):

        proposal(source="github", github_url=url)





def test_canonical_address_and_branch_rules():

    assert (

        canonical_url("https://github.com/pypa/sampleproject.git/")

        == "https://github.com/pypa/sampleproject"

    )

    for ref in ["../main", "--upload-pack=evil", "main//child", "main/"]:

        with pytest.raises(ValueError):

            proposal(ref=ref)

    with pytest.raises(ValueError):

        proposal(source="github")

    with pytest.raises(ValueError):

        proposal(title="   ")





def test_preflight_does_not_clone_and_binds_exact_snapshot(tmp_path):

    imported = []

    store = setup(

        tmp_path,

        repo_probe=lambda _: {"size_mb": 150},

        importer=lambda *args: imported.append(args),

    )

    request = proposal(

        source="github",

        github_url="https://github.com/pypa/sampleproject",

        model_id="Qwen/Qwen3.5-27B",

    )

    report = store.preflight(request)

    assert not imported and not (tmp_path / "runs/projects").exists()

    assert report.recommended == "configured_api"

    assert {n.code for n in report.advisories} >= {

        "gpu_absent",

        "repo_large",

        "local_model_unsupported",

    }

    assert "local_cpu" not in report.options

    assert store.preflight(request).id == report.id

    with pytest.raises(ValueError, match="不同项目"):

        store.preflight(request.model_copy(update={"title": "变更"}))

    with pytest.raises(ValueError, match="阅读"):

        store.confirm(confirm(report, acknowledge_warnings=False))

    with pytest.raises(ValueError, match="改变"):

        store.confirm(confirm(report, fingerprint="0" * 64))

    assert not imported





def test_capacity_and_api_change_are_rechecked_before_writes(tmp_path):

    current = [machine()]

    store = setup(tmp_path, probe=lambda _: current[0])

    report = store.preflight(proposal(model_id="Qwen/Qwen3-0.6B"))

    current[0] = machine(available_ram_gb=1)

    with pytest.raises(ValueError, match="内存"):

        store.confirm(confirm(report, choice="local_cpu"))

    current[0] = machine(api_model="new-model")

    with pytest.raises(ValueError, match="API模型已改变"):

        store.confirm(confirm(report, choice="configured_api"))

    current[0] = machine(api_available=False)

    with pytest.raises(ValueError, match="不可用"):

        store.confirm(confirm(report, choice="configured_api"))

    current[0] = machine(free_disk_gb=0.1)

    with pytest.raises(ValueError, match="磁盘"):

        store.confirm(confirm(report))

    assert store.list() == []





def test_blocking_repository_and_expired_report_cannot_confirm(tmp_path):

    store = setup(tmp_path, repo_probe=lambda _: {"size_mb": 1200})

    report = store.preflight(proposal(source="github", github_url="https://github.com/o/r"))

    with pytest.raises(ValueError, match="阻断"):

        store.confirm(confirm(report))

    report = store.preflight(proposal())

    report.created_at = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()

    with store.db.connect() as c:

        c.execute(

            "UPDATE project_assessments SET report=? WHERE id=?",

            (report.model_dump_json(), report.id),

        )

    with pytest.raises(ValueError, match="15分钟"):

        store.confirm(confirm(report))

    assert store.list() == []





def test_duplicate_confirm_and_materialize_do_not_repeat_side_effects(tmp_path):

    imported = []



    def importer(url, ref, dest):

        imported.append(url)

        dest.mkdir()

        (dest / "main.py").write_text('model_id="Qwen/Qwen3.5-27B"\n')

        return {"commit": "a" * 40, "files": 1}



    store = setup(tmp_path, repo_probe=lambda _: {"size_mb": 1}, importer=importer)

    report = store.preflight(proposal(source="github", github_url="https://github.com/o/r"))

    request = confirm(report)

    with ThreadPoolExecutor(max_workers=2) as pool:

        results = list(pool.map(store.confirm, [request, request]))

    assert results[0][0].id == results[1][0].id and sum(r[1] for r in results) == 1

    row = store.materialize(results[0][0].id)

    assert row.status == "ready" and row.workspace_id

    assert row.state["analysis"]["model_candidates"][0]["model"]["parameter_billions"] == 27

    assert store.materialize(row.id).status == "ready" and len(imported) == 1

    with pytest.raises(ValueError, match="不能"):

        store.confirm(confirm(report))

    ProjectStore(tmp_path, probe=lambda _: machine(), registry=store.registry).get(row.id)





def test_official_workspace_create_rename_and_persistence(tmp_path):

    source = tmp_path / "runs/projects" / uid() / "workspace"

    source.mkdir(parents=True)

    result = workspace_call(tmp_path, "create", path=str(source), title="Official registry QA")

    assert result["id"]

    renamed = workspace_call(tmp_path, "rename", id=result["id"], title="Renamed QA")

    assert renamed["title"] == "Renamed QA"

    rows = workspace_call(tmp_path, "list")  # A fresh Node process reads persistent official state.

    assert any(r["id"] == result["id"] and r["title"] == "Renamed QA" for r in rows)

    with pytest.raises(ValueError, match="outside"):

        workspace_call(tmp_path, "create", path=str(tmp_path), title="escape")

    with pytest.raises(ValueError, match="mismatch"):

        workspace_call(tmp_path, "remove", id=result["id"], path=str(tmp_path))

    assert workspace_call(tmp_path, "remove", id=result["id"], path=str(source))["removed"]

    assert source.is_dir() and workspace_call(tmp_path, "list") == []





def test_archive_and_model_reconfiguration_preserve_source_and_identity(tmp_path):

    store = setup(tmp_path)

    row = ready(store)

    original = Path(row.path) / "README.md"

    before = original.read_bytes()

    report = store.policy_preflight(

        row.id, ProjectPolicyRequest(request_id=uid(), model_id="Qwen/Qwen3.5-27B")

    )

    assert report.project_id == row.id

    request = confirm(report, choice="configured_api")

    updated, new = store.confirm(request)

    assert not new and updated.id == row.id and updated.workspace_id == row.workspace_id

    assert updated.state["api_model"] == "glm-5.3"

    assert store.confirm(request)[0].id == row.id

    assert original.read_bytes() == before

    renamed = store.manage(row.id, ProjectUpdate(action="rename", title="新的名称"))

    assert renamed.title == "新的名称"

    store.manage(row.id, ProjectUpdate(action="archive"))

    with pytest.raises(ValueError):

        store.plan_selection(row.id)

    restored = store.manage(row.id, ProjectUpdate(action="restore"))

    assert restored.status == "ready" and original.read_bytes() == before





def test_incomplete_import_is_not_replayed_and_can_be_managed(tmp_path):

    imported = []

    store = setup(tmp_path, importer=lambda *a: imported.append(a))

    report = store.preflight(proposal())

    row, _ = store.confirm(confirm(report))

    store.update_state(row.id, import_started=True)

    assert store.materialize(row.id).status == "preparing" and not imported

    store.update_state(row.id, status="failed")

    assert store.manage(row.id, ProjectUpdate(action="rename", title="待核对")).title == "待核对"





def test_project_paths_and_credentials_are_not_readable(tmp_path):

    store = setup(tmp_path)

    row = ready(store)

    (Path(row.path) / "settings.py").write_text('api_key = "fixture-secret-1"\n')

    assert "fixture-secret-1" not in store.read(row.id, "settings.py")["content"]

    for path in ["../project.json", "/etc/passwd", ".git/config", ".env", "node_modules/x"]:

        with pytest.raises(ValueError):

            store.read(row.id, path)

    try:

        (Path(row.path) / "outside").symlink_to(tmp_path, target_is_directory=True)

    except OSError:

        return

    with pytest.raises(ValueError):

        store.read(row.id, "outside/README.md")





def test_project_model_choice_is_enforced_and_read_tools_are_readonly(tmp_path):

    projects = setup(tmp_path)

    row = ready(projects, proposal(model_id="Qwen/Qwen3.5-27B"))

    selection = projects.plan_selection(row.id)

    plan = ApplicationPlan(

        title="实际调用应用",

        goal="调用指定的API模型",

        application_requirements=["显示回复"],

        acceptance_examples=["问候", "1+2"],

        **selection,

    )

    projects.validate_plan(plan)

    with pytest.raises(ValueError):

        projects.validate_plan(plan.model_copy(update={"model_source": "existing_teacher8b"}))

    apps = AppStore(tmp_path)

    app = apps.create(ApplicationCreate(request_id=uid(), plan=plan))

    assert AppService(apps, app.id, "repo").call("project_read", {"path": "README.md"})["ok"]

    assert not AppService(apps, app.id, "infra").call("project_read", {"path": "README.md"})["ok"]

    for c in capabilities("repo"):

        if c.name in {"project_read", "project_tree"}:

            assert c.effect == "read"

    report = projects.policy_preflight(row.id, ProjectPolicyRequest(request_id=uid()))

    with pytest.raises(ValueError, match="关联"):

        projects.confirm(confirm(report))

    assert not AppService(apps, app.id, "repo").call("write", {"path": "app.py", "content": "x"})[

        "ok"

    ]





def test_no_gpu_busy_gpu_unknown_model_and_small_cpu_assessments():

    big = proposal(model_id="Qwen/Qwen3.5-27B")

    result, notes, cpu = assess_model(big, machine())

    assert result["estimated_fp16_vram_gb"] > 70 and not cpu

    busy = machine(

        gpu_probe="available", gpus=[GPUResource(index=0, name="A100", total_gb=80, free_gb=2)]

    )

    assert "gpu_memory" in {n.code for n in assess_model(big, busy)[1]}

    assert assess_model(proposal(model_id="Qwen/Qwen3-0.6B"), machine())[2]

    assert not assess_model(proposal(model_id="glm-5.3"), machine())[2]

    assert not assess_model(big, machine(available_ram_gb=200))[2]













def test_api_model_identity_empty_answer_and_serialized_messages(monkeypatch):

    config = {

        "REPOPILOT_API_KEY": "test-private",

        "REPOPILOT_BASE_URL": "https://example.invalid/v1",

        "REPOPILOT_MODEL": "glm-5.3",

    }

    monkeypatch.setattr("pilot_core.config.credentials", lambda: config)

    seen = []



    def transport(request):

        payload = json.loads(request.content)

        seen.append(payload)

        assert request.headers["authorization"] == "Bearer test-private"

        return httpx.Response(

            200,

            json={

                "choices": [{"message": {"content": "真实响应格式"}, "finish_reason": "stop"}],

                "usage": {"prompt_tokens": 4, "completion_tokens": 5},

            },

        )



    request = PreviewRequest(messages=[{"role": "user", "content": "问题"}])

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:

        assert generate_api({"model_id": "glm-5.3"}, request, client)["text"] == "真实响应格式"

        assert seen[0]["messages"] == [{"role": "user", "content": "问题"}]

        assert seen[0]["thinking"] == {"type": "enabled"}

        with pytest.raises(ValueError, match="配置已变化"):

            generate_api({"model_id": "glm-5"}, request, client)

    with httpx.Client(

        transport=httpx.MockTransport(

            lambda _: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

        )

    ) as client:

        with pytest.raises(ValueError, match="完整答案"):

            generate_api({"model_id": "glm-5.3"}, request, client)





def test_github_metadata_failures_are_non_mutating(monkeypatch):

    factory = httpx.Client

    monkeypatch.setattr(

        "pilot_projects.github.httpx.Client",

        lambda **kw: factory(transport=httpx.MockTransport(lambda _: httpx.Response(404)), **kw),

    )

    with pytest.raises(ValueError, match="尚未下载"):

        metadata("https://github.com/pypa/sampleproject")
