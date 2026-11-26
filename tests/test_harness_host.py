"""Domain and real official Web behavior; scripted LLM is not a quality eval."""

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pilot_apps.backend import AppBackend
from pilot_apps.contracts import ApplicationCreate
from pilot_apps.store import source_hash
from pilot_host.bridge import HostDomain
from pilot_host.cli import composition
from pilot_projects.store import ProjectStore
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.test_goal_applications import new, plan
from tests.test_projects import machine


def test_multifile_candidate_identity_and_protected_paths(tmp_path):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("app.py", "import helper\n")
    backend.write("index.html", "<p>hello</p>")
    first = source_hash(backend.workspace)
    store.update(row.id, model={"identity": "test-model"}, source_test={"passed": True, "source_sha256": first})
    backend.write("modules/helper.py", "VALUE = 1\n")
    assert source_hash(backend.workspace) != first
    with pytest.raises(ValueError, match="pass protected"):
        backend.candidate()
    for path in ("../outside.py", "model_client.py", "tests/test_contract.py", "repository/tests/test_original.py", ".env", "x/.local/key.json", "C:/escape.py"):
        with pytest.raises(ValueError):
            backend.write(path, "unsafe")
    digest = source_hash(backend.workspace)
    store.update(row.id, source_test={"passed": True, "source_sha256": digest})
    candidate = backend.candidate()
    assert candidate["source_sha256"] == digest
    assert (backend.candidate_path(candidate) / "modules/helper.py").read_text() == "VALUE = 1\n"
    backend.write("modules/helper.py", "VALUE = 2\n")
    assert source_hash(backend.workspace) != digest
    assert source_hash(backend.candidate_path(candidate)) == digest


def test_import_working_copy_preserves_repository_and_tests(tmp_path, monkeypatch):
    origin = tmp_path / "original"
    (origin / "tests").mkdir(parents=True)
    (origin / "README.md").write_text("Original readme")
    (origin / "lib.py").write_text("VALUE = 1\n")
    (origin / "tests/test_original.py").write_text("assert True\n")
    project_id = uuid.uuid4().hex
    monkeypatch.setattr("pilot_projects.store.ProjectStore.get", lambda self, id: SimpleNamespace(id=project_id, path=str(origin), status="ready"))
    store, _ = new(tmp_path)
    row = store.create(ApplicationCreate(request_id=uuid.uuid4().hex, plan=plan(project_id=project_id)))
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("repository/lib.py", "VALUE = 2\n")
    backend.initialize_workspace()
    assert (origin / "lib.py").read_text() == "VALUE = 1\n"
    assert backend.read("repository/README.md")["content"] == "Original readme"
    assert backend.read("repository/tests/test_original.py")["content"] == "assert True\n"
    assert "repository/lib.py" in backend.tree()["files"]


def test_domain_binding_review_unknown_replay_and_scope(tmp_path):
    service = HostDomain(tmp_path)
    result = service.call("plan", "official-planner", arguments={"plan": plan().model_dump(), "request_id": uuid.uuid4().hex})
    app = result["application"]
    assert app["status"] == "planned" and result["requires_start"]
    with pytest.raises(ValueError, match="reviewed Start"):
        service.call("tool", "official-planner", "infra", {"name": "prepare_model"})
    service.call("bind", "official-executor", arguments={"application_id": app["id"]})
    service.call("start", "official-executor")
    assert not service.call("tool", "official-executor", "infra", {"name": "write"})["ok"]
    service.call("pause", "official-executor")
    assert not service.call("tool", "official-executor", "repo", {"name": "write", "input": {"path": "app.py", "content": "pass"}})["ok"]
    service.apps.update(app["id"], status="running")
    service.apps.claim(app["id"], "deploy")
    service.call("pause", "official-executor")
    with pytest.raises(ValueError, match="unknown outcome"):
        service.call("start", "official-executor")
    assert service.call("view", "unbound-session")["application"] is None
    with pytest.raises(ValueError, match="No application plan"):
        service.call("tool", "unbound-session", "repo", {"name": "read"})


def test_plan_transport_validation_idempotency_and_unplanned_status(tmp_path):
    service = HostDomain(tmp_path)
    status = service.call("tool", "new-planner", "supervisor", {"name": "status"})
    assert status["ok"] and status["result"]["status"] == "unplanned"
    assert service.binding("new-planner") is None
    for raw in ("{invalid", "[]", json.dumps(json.dumps(plan().model_dump())),
                dict(plan().model_dump(), title="x")):
        with pytest.raises(ValueError, match="Invalid pilot_plan"):
            service.call("plan", "new-planner", arguments={"plan": raw, "request_id": "stable-plan-key"})
    with pytest.raises(ValueError, match="Invalid official Session"):
        service.call("plan", "../escape", arguments={"plan": plan().model_dump(), "request_id": "stable-plan-key"})
    with service.apps.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    result = service.call("plan", "new-planner", arguments={"plan": json.dumps(plan().model_dump(), ensure_ascii=False), "request_id": "stable-plan-key"})
    retry = service.call("plan", "new-planner", arguments={"plan": plan().model_dump(), "request_id": "stable-plan-key"})
    assert result["application"]["id"] == retry["application"]["id"]
    assert result["requires_start"] and result["application"]["status"] == "planned"
    for changes in ({"request_id": "different-plan-key"}, {"plan": plan(title="Changed plan").model_dump()}):
        with pytest.raises(ValueError, match="already has a plan"):
            service.call("plan", "new-planner", arguments={"plan": plan().model_dump(), "request_id": "stable-plan-key", **changes})
    with service.apps.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    with pytest.raises(ValueError, match="No application plan"):
        service.call("tool", "unbound-repo", "repo", {"name": "status"})


def test_legacy_three_file_candidate_digest_is_compatible(tmp_path):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("app.py", "pass")
    backend.write("index.html", "hello")
    expected = hashlib.sha256()
    for name in ("app.py", "index.html", "model_client.py"):
        expected.update(name.encode() + b"\0" + (backend.workspace / name).read_bytes())
    assert source_hash(backend.workspace) == expected.hexdigest()


def test_host_project_admission_uses_main_registry_and_selected_policy(tmp_path, monkeypatch):
    def no_compatibility_registry(*args, **kwargs):
        raise AssertionError("Host must use its own live official registry")
    projects = ProjectStore(tmp_path, probe=lambda root: machine(), registry=no_compatibility_registry)
    monkeypatch.setattr("pilot_host.bridge.ProjectStore", lambda root: projects)
    service = HostDomain(tmp_path)
    report = service.call("project-preflight", arguments={"request_id": uuid.uuid4().hex, "title": "Host project"})
    selected = service.call("project-confirm", arguments={"assessment_id": report["id"], "request_id": uuid.uuid4().hex,
        "fingerprint": report["fingerprint"], "choice": "configured_api", "acknowledge_warnings": True})
    assert selected["status"] == "ready" and selected["workspace_id"] is None
    assert (Path(selected["path"]) / "README.md").is_file()
    service.call("project-bind", "official-project", arguments={"project_id": selected["id"], "workspace_id": "verified-by-host"})
    assert service.call("view", "official-project")["project"]["id"] == selected["id"]
    assert '"model_id": "glm-5.3"' in service.call("context", "official-project")["text"]
    with pytest.raises(ValueError, match="selected project"):
        service.call("plan", "official-project", arguments={"plan": plan().model_dump(), "request_id": uuid.uuid4().hex})
    service.call("project-manage", arguments={"project_id": selected["id"], "request": {"action": "rename", "title": "Renamed"}})
    assert projects.get(selected["id"]).title == "Renamed"


@pytest.mark.parametrize("plan_encoding", ["object", "serialized", "failure-limit", "budget-refusal", "terminal"])
def test_official_web_plan_role_tools_workspace_and_cold_history(tmp_path, plan_encoding):
    """Exercise installed Web/Controller/Loop/Preset/Connection, including restart."""
    project = Path(__file__).resolve().parents[1]
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    counts = {"planning": 0, "supervisor": 0, "infra": 0}
    requests = []
    service = HostDomain(tmp_path)
    planning_id = "harness-qa-planning"
    delivery_id = None
    dialogue = None
    dialogue_calls = 0
    if plan_encoding == "terminal":
        # Transport fixture supplies a settled domain success. The installed
        # official Loop/Tools still decide whether another model call is made.
        shim = tmp_path / ".local/terminal-shim/pilot_host"
        shim.mkdir(parents=True)
        (shim / "__init__.py").write_text(f"__path__.append({str(project / 'src/pilot_host')!r})\n")
        (shim / "bridge.py").write_text(
            "import importlib.util,json,sys\n"
            f"spec=importlib.util.spec_from_file_location('fixture_bridge',{str(project / 'src/pilot_host/bridge.py')!r})\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "request=json.loads(sys.stdin.read());domain=module.HostDomain(sys.argv[1])\n"
            "if request['operation']=='tool' and request['arguments'].get('name')=='finish':\n"
            " row=domain.application(request['session_id']);domain.apps.update(row.id,status='completed',candidate={'id':'c1'},verification={'passed':True,'candidate':'c1','cases':[{},{}]})\n"
            " value={'ok':True,'result':{'passed':True}}\n"
            "else:\n"
            " try:value=domain.call(**request)\n"
            " except Exception as error:print(json.dumps({'ok':False,'error':str(error)}));sys.exit(0)\n"
            "print(json.dumps({'ok':True,'value':value}))\n"
        )

    def respond(body):
        nonlocal delivery_id, dialogue_calls
        names = {t["function"]["name"] for t in body.get("tools", [])}
        serialized = json.dumps(body, ensure_ascii=False)
        role = "infra" if "pilot_prepare_model" in names else "supervisor" if delivery_id and delivery_id in serialized else "planning"
        requests.append((role, names, body))
        if dialogue:
            dialogue_calls += 1
            if dialogue_calls == 1:
                return "pilot_status", {}
            if dialogue_calls == 2 and dialogue == "revision":
                return "pilot_revise_plan", {"request_id": "official-chat-revision", "expected_version": 1,
                    "plan": plan(title="对话修订后的应用").model_dump()}
            if dialogue_calls == 2 and dialogue == "feedback":
                return "pilot_feedback", {"request_id": "official-chat-feedback", "message": "通过对话增加学习画像"}
            return None
        counts[role] += 1
        if role == "planning" and counts[role] == 1:
            return "pilot_status", {}
        invalid_steps = range(2, 6) if plan_encoding == "failure-limit" else range(2, 3)
        if role == "planning" and counts[role] in invalid_steps:
            return "pilot_plan", {"plan": "{invalid", "request_id": "harness-qa-plan"}
        if role == "planning" and counts[role] == invalid_steps.stop:
            assert service.binding(planning_id) is None
            raw = plan().model_dump()
            return "pilot_plan", {"plan": json.dumps(raw) if plan_encoding == "serialized" else raw,
                                  "request_id": "harness-qa-plan"}
        if role == "supervisor" and counts[role] == 1:
            return "pilot_finish", {}  # domain failure must become official isError
        if role == "supervisor" and counts[role] == 2:
            return "infra_agent", {"description": "Read approved state", "cwd": str(tmp_path / "runs/apps" / delivery_id.removeprefix("delivery-") / "workspace"),
                                   "prompt": "Read pilot_status only and report; do not prepare or deploy."}
        if role == "infra" and counts[role] == 1:
            return "pilot_status", {}
        if role == "infra" and counts[role] == 2:
            return "send_message", {"agent_id": delivery_id, "message": "Observed approved state. No mutation performed."}
        return None

    def configure(model):
        patch = composition(project, {"REPOPILOT_MODEL": "scripted-web", "REPOPILOT_BASE_URL": model.url,
                                     "PILOT_CLIENT_URL": f"http://127.0.0.1:{port}"}, settings_root=tmp_path)
        for item in patch[-1]["insert"]:
            if "root" in item.get("config", {}):
                item["config"]["root"] = str(tmp_path)
            if item.get("id") == "pilot-policy" and plan_encoding == "budget-refusal":
                item["config"]["budget"]["max_tokens"] = 1
            if item.get("id") == "pilot-workspace" and plan_encoding == "terminal":
                item["config"]["pythonPath"] = str(shim.parent) + os.pathsep + str(project / "src")
        for item in patch:
            if item.get("id") == "working-directory":
                item["config"]["defaultDirectory"] = str(inbox)
        (home / "patch.json").write_text(json.dumps(patch), encoding="utf-8")

    def launch():
        command = [dsh_executable(), "--profile", "web", "--patch", str(home / "patch.json"), "--no-open", "--port", str(port)]
        if os.name == "nt":
            command = ["cmd.exe", "/c", *command]
        log = (home / "test-host.log").open("ab")
        process = subprocess.Popen(command, cwd=inbox, stdout=log, stderr=log,
                                   env={**os.environ, "DSH_HOME": str(home), "REPOPILOT_API_KEY": "fixture-key", "PYTHONPATH": str(project / "src")})
        log.close()
        for _ in range(200):
            if process.poll() is not None:
                raise AssertionError((home / "test-host.log").read_text(errors="replace"))
            if (home / "entry.json").exists():
                try:
                    client = httpx.Client(follow_redirects=True, timeout=180)
                    response = client.get(json.loads((home / "entry.json").read_text())["url"])
                    if response.status_code == 200:
                        assert "@pilot/delivery-ui" in response.text
                        assert "@pilot/brand" in response.text
                        assert "<title>Pilot</title>" in response.text
                        assert "favicon-dark.svg" not in response.text
                        assert "ui-brand-official" not in response.text
                        for retired in ("dsh-client-ui-plugin-manager", "dsh-entry-customization",
                                        "dsh-client-ui-schedule", "dsh-client-ui-sidebar-terminal"):
                            assert retired not in response.text
                        logo = client.get(f"http://127.0.0.1:{port}/pilot-brand.svg")
                        assert logo.status_code == 200 and 'viewBox="0 0 32 32"' in logo.text
                        manifest = client.get(f"http://127.0.0.1:{port}/pilot.webmanifest").json()
                        assert manifest["name"] == "Pilot"
                        import tomllib

                        expected_version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
                        assert manifest["version"] == expected_version
                        assert f'name="pilot-version" content="{expected_version}"' in response.text
                        return process, client
                    client.close()
                except httpx.TransportError:
                    pass
            time.sleep(0.2)
        stop(process)
        raise AssertionError("Official Web did not become ready")

    def stop(process):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            process.terminate()
        process.wait(timeout=15)

    def rpc(client, endpoint, args, channel="api"):
        for _ in range(5):
            response = client.post(f"http://127.0.0.1:{port}/{channel}/{endpoint}", json={
                "type": "client-request", "rpcId": uuid.uuid4().hex, "method": endpoint, "payload": {"args": args} if channel == "api" else args})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result["ok"], result
            # Official wait's default is a bounded 30-second snapshot. A
            # timeout is still running, never proof that its turn completed.
            if endpoint != "session/wait" or result["value"]["reason"] != "timeout":
                return result["value"]
        raise AssertionError("Official Session did not settle within bounded waits")

    with ScriptedModel([], responder=respond) as model:
        configure(model)
        process, client = launch()
        try:
            created = rpc(client, "session/create", {"request": {"sessionId": planning_id, "cwd": str(inbox), "agentPreset": "supervisor"}})
            assert created["sessionId"] == planning_id
            rpc(client, "session/prompt", {"request": {"sessionId": planning_id, "requestId": "qa-prompt", "mode": "queue", "content": [{"type": "text", "text": "Propose the approved fixture application plan."}]}})
            rpc(client, "session/wait", {"request": {"sessionId": planning_id}})
            if plan_encoding == "budget-refusal":
                events = rpc(client, "session/events", {"request": {"address": {"kind": "session", "sessionId": planning_id}, "maxMessages": 100}})
                ended = [r["event"]["data"]["reason"] for r in events["records"] if r.get("event", {}).get("type") == "turn/end"]
                assert ended[-1]["error"]["code"] == "PILOT_BUDGET_LIMIT"
                assert "新建会话" in json.dumps(ended[-1], ensure_ascii=False)
                assert "任务或交付面板" not in json.dumps(ended[-1], ensure_ascii=False)
                assert counts["planning"] == 0
                assert rpc(client, "view", {"session_id": planning_id}, "pilot-api")["application"] is None
                meter = json.loads((tmp_path / "runs/harness-policy" / planning_id / "meter.json").read_text())
                assert meter["last_stop"]["measurement"].startswith("official_")
                return
            if plan_encoding == "failure-limit":
                failed_events = rpc(client, "session/events", {"request": {"address": {"kind": "session", "sessionId": planning_id}, "maxMessages": 100}})
                assert "pilot_plan failed 4 times" in json.dumps(failed_events)
                assert counts["planning"] == 5 and service.binding(planning_id) is None
                rpc(client, "session/prompt", {"request": {"sessionId": planning_id, "requestId": "qa-recover", "mode": "queue", "content": [{"type": "text", "text": "Correct the plan in this new turn."}]}})
                rpc(client, "session/wait", {"request": {"sessionId": planning_id}})
            view = rpc(client, "view", {"session_id": planning_id}, "pilot-api")
            events = rpc(client, "session/events", {"request": {"address": {"kind": "session", "sessionId": planning_id}, "maxMessages": 100}})
            assert view["application"] and view["application"]["status"] == "planned", json.dumps({"counts": counts, "requests": [(role, sorted(names)) for role, names, _ in requests], "events": [r for r in events["records"] if r.get("event", {}).get("type") in {"tool/call", "tool/result", "turn/end", "error"}]}, ensure_ascii=False)
            # Plan success is terminal through the official tool API: no
            # further model request can delegate before the reviewed Start.
            assert counts["planning"] == (6 if plan_encoding == "failure-limit" else 3)
            results = [r["event"]["data"]["message"] for r in events["records"]
                       if r.get("event", {}).get("type") == "tool/result"]
            failures = 4 if plan_encoding == "failure-limit" else 1
            assert [r["isError"] for r in results] == [False, *([True] * failures), False]
            assert "unplanned" in json.dumps(results[0]) and "Invalid pilot_plan" in json.dumps(results[1])
            planner_meter = json.loads((tmp_path / "runs/harness-policy" / planning_id / "meter.json").read_text())
            assert planner_meter["last_reservation"]["source"].startswith("official_")
            if plan_encoding == "object":
                dialogue, dialogue_calls = "revision", 0
                rpc(client, "session/prompt", {"request": {"sessionId": planning_id, "requestId": "qa-revision-prompt",
                    "mode": "queue", "content": [{"type": "text", "text": "修改待开始方案的应用名称，保存后等待我开始。"}]}})
                rpc(client, "session/wait", {"request": {"sessionId": planning_id}})
                revised = rpc(client, "view", {"session_id": planning_id}, "pilot-api")
                assert revised["application"]["id"] == view["application"]["id"]
                assert revised["application"]["title"] == "对话修订后的应用", rpc(client, "session/events", {
                    "request": {"address": {"kind": "session", "sessionId": planning_id}, "maxMessages": 100}})
                assert revised["application"]["state"]["plan_version"] == 2
                assert dialogue_calls == 2 and revised["actions"]["edit_plan"]
                with service.apps.db.connect() as db:
                    assert db.execute("SELECT COUNT(*) FROM application_operations").fetchone()[0] == 0
                dialogue = None
            delivery_id = "delivery-" + view["application"]["id"]
            result = rpc(client, "start", {"session_id": planning_id}, "pilot-api")
            assert result["sessionId"] == delivery_id
            rpc(client, "session/wait", {"request": {"sessionId": delivery_id}})
            if plan_encoding == "terminal":
                events = rpc(client, "session/events", {"request": {"address": {"kind": "session", "sessionId": delivery_id}, "maxMessages": 100}})
                assert counts["supervisor"] == 1 and counts["infra"] == 0
                ended = [r["event"]["data"]["reason"] for r in events["records"] if r.get("event", {}).get("type") == "turn/end"]
                assert ended[-1]["kind"] == "completed"
                outputs = [r["event"]["data"]["message"] for r in events["records"] if r.get("event", {}).get("type") == "tool/result"]
                terminal = json.loads(outputs[-1]["content"][0]["text"])
                assert terminal["delivery_completed"] and terminal["application"]["status"] == "completed"
                assert rpc(client, "view", {"session_id": delivery_id}, "pilot-api")["application"]["status"] == "completed"
                return
            settled = rpc(client, "wait", {"session_id": delivery_id}, "pilot-api")
            assert settled["status"] == "failed"  # no fabricated acceptance
            assert counts["infra"] >= 3, (counts, requests)
            execution_events = rpc(client, "session/events", {"request": {"address": {"kind": "session", "sessionId": delivery_id}, "maxMessages": 100}})
            failed_finish = [r["event"]["data"]["message"] for r in execution_events["records"]
                             if r.get("event", {}).get("type") == "tool/result"
                             and "independent model acceptance" in json.dumps(r)]
            assert len(failed_finish) == 1 and failed_finish[0]["isError"]
            # Root and child share cwd; every role has only its configured tools.
            listing = rpc(client, "session/list", {"_request": {}})
            assert any(item["cwd"] == view["cwd"] for item in listing["items"])
            for role, names, body in requests:
                assert "bash" not in names and "write" not in names
                if role == "planning":
                    assert "Discussion before execution:" in json.dumps(body)
                    assert "Do not invent a task from available assets" in json.dumps(body)
                    schema = next(t["function"]["parameters"] for t in body["tools"] if t["function"]["name"] == "pilot_plan")
                    object_schema = schema["properties"]["plan"]["oneOf"][0]
                    assert object_schema["type"] == "object" and not object_schema["additionalProperties"]
                    assert {"title", "goal", "model_source", "application_requirements", "acceptance_examples"} <= set(object_schema["required"])
                    assert object_schema["properties"]["application_requirements"]["items"]["type"] == "string"
                if role == "infra":
                    assert "pilot_write" not in names and "pilot_status" in names
                    directories = [json.loads(value) for message in body["messages"]
                        for value in re.findall(r'Current working directory: ("(?:\\.|[^"\\])*")\.', str(message.get("content", "")))]
                    assert view["cwd"] in directories
                    assert "Session:" in json.dumps(body) and "approved-goal" in json.dumps(body)
            # A stale pause must not change a delivery already reconciled as
            # failed. The Web hides Pause from this same capability projection.
            with pytest.raises(ValueError, match="当前状态"):
                service.call("pause", delivery_id)
            assert service.application(delivery_id).status == "failed"
            feedback = rpc(client, "feedback", {"session_id": delivery_id, "request_id": uuid.uuid4().hex,
                "message": "Add a concise status label"}, "pilot-api")
            assert feedback["state"]["feedback"][-1]["message"] == "Add a concise status label"
            report = rpc(client, "project-preflight", {"session_id": delivery_id, "request_id": uuid.uuid4().hex, "title": "Official project QA"}, "pilot-api")
            assert "prepare_only" in report["options"]
        finally:
            client.close()
            stop(process)
        # Same official persistence, fresh host, no duplicated business messages.
        process, client = launch()
        try:
            listing = rpc(client, "session/list", {"_request": {}})
            assert planning_id in json.dumps(listing) and delivery_id in json.dumps(listing)
            assert rpc(client, "view", {"session_id": delivery_id}, "pilot-api")["application"]["status"] == "paused"
            if plan_encoding == "object":
                dialogue, dialogue_calls = "feedback", 0
                rpc(client, "session/prompt", {"request": {"sessionId": delivery_id, "requestId": "qa-feedback-prompt",
                    "mode": "queue", "content": [{"type": "text", "text": "增加学习画像，保存修改要求，等待我继续。"}]}})
                rpc(client, "session/wait", {"request": {"sessionId": delivery_id}})
                after = service.application(delivery_id)
                assert after.status == "paused" and after.state["feedback"][-1]["message"] == "通过对话增加学习画像"
                assert dialogue_calls == 2
            assert "warning:" not in (home / "test-host.log").read_text(errors="replace")
        finally:
            client.close()
            stop(process)
