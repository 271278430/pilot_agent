"""Installed official Preset/Subagent/Web integration with a scripted model."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from pilot_host.cli import composition
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.intent_fixture import with_intent_fixture
from tests.test_pilot_infra import plan, setup


@pytest.mark.parametrize(
    "repo_repair,chat_plan",
    [(False, False), (True, False), ("both", False), (False, True), (False, "workbench")],
)
def test_native_machine_task_and_optional_expert_keep_one_root(tmp_path, repo_repair, chat_plan):
    project = Path(__file__).resolve().parents[1]
    service = setup(tmp_path)
    if chat_plan == "workbench":
        from tests.test_build_workbench import service as build_fixture

        builds = build_fixture(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    seen, counts = [], {"root": 0, "expert": 0}
    root_id = None
    task_id = None
    source_hash = None
    stage = 1
    trace_observations = []

    def respond(body):
        names = {t["function"]["name"] for t in body.get("tools", [])}
        role = "root" if names & {"infra_specialist", "repo_specialist"} else "expert"
        counts[role] += 1
        seen.append((role, names))
        if chat_plan:
            if chat_plan == "workbench":
                from tests.test_build_workbench import plan as build_plan

                if counts[role] == 1:
                    return ("pilot_infra_workbench_plan", build_plan())
                if counts[role] == 2:
                    return ("pilot_infra_workbench_status", {})
                return None
            return ("pilot_infra_plan", {"request_id": "native-chat-plan", "plan": plan()})
        if counts[role] == 1:
            return "pilot_infra_status", {}
        if role == "root" and counts[role] == 2:
            return "repo_specialist" if repo_repair and (
                repo_repair != "both" or stage == 2
            ) else "infra_specialist", {
                "description": "Analyze current approved scope",
                "prompt": "Read pilot_infra_status and send the observed machine/quota to the parent. No deployment.",
            }
        if not repo_repair and role == "expert" and counts[role] == 2:
            return "session_trace", {"session_id": root_id}
        if not repo_repair and role == "expert" and counts[role] == 3:
            trace_observations.append(body["messages"][-1])
            return "send_message", {
                "agent_id": root_id,
                "message": "Same-root history verified after execution cwd change; GPU quota remains [7].",
            }
        repair = repo_repair and "pilot_infra_source_write" in names
        if role == "expert" and repair and counts[role] == 2:
            return "pilot_infra_source_write", {
                "expected_sha256": source_hash,
                "content": "def build_options():\n    return {}\n",
            }
        if role == "expert" and counts[role] == (3 if repair else 2):
            return "send_message", {
                "agent_id": root_id,
                "message": "Whole-machine business scope; admitted execution GPUs are [7]. No GPU action performed.",
            }
        return None

    def rpc(client, endpoint, args, channel="api"):
        r = client.post(
            f"http://127.0.0.1:{port}/{channel}/{endpoint}",
            json={
                "type": "client-request",
                "rpcId": uuid.uuid4().hex,
                "method": endpoint,
                "payload": {"args": args} if channel == "api" else args,
            },
        )
        r.raise_for_status()
        result = r.json()["result"]
        assert result["ok"], result
        return result["value"]

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-infra",
                "REPOPILOT_BASE_URL": model.url,
                "PILOT_CLIENT_URL": f"http://127.0.0.1:{port}",
            },
        settings_root=tmp_path,
        )
        for row in patch[-1]["insert"]:
            if "root" in row.get("config", {}):
                row["config"]["root"] = str(tmp_path)
        for row in patch:
            if row.get("id") == "working-directory":
                row["config"]["defaultDirectory"] = str(inbox)
        (home / "patch.json").write_text(json.dumps(patch), encoding="utf-8")
        cmd = [
            dsh_executable(),
            "--profile",
            "web",
            "--patch",
            str(home / "patch.json"),
            "--no-open",
            "--port",
            str(port),
        ]
        if os.name == "nt":
            cmd = ["cmd.exe", "/c", *cmd]
        with (home / "host.log").open("wb") as log:
            process = subprocess.Popen(
                cmd,
                cwd=inbox,
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "DSH_HOME": str(home),
                    "REPOPILOT_API_KEY": "fixture-key",
                    "PYTHONPATH": str(project / "src"),
                },
            )
        client = httpx.Client(follow_redirects=True, timeout=90)
        try:
            for _ in range(150):
                assert process.poll() is None, (home / "host.log").read_text(errors="replace")
                if (home / "entry.json").is_file():
                    try:
                        html = client.get(json.loads((home / "entry.json").read_text())["url"])
                        if html.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                time.sleep(0.2)
            assert "@pilot/infra-domain" in html.text
            if chat_plan:
                root_id = rpc(client, "new-session", {}, "pilot-infra-api")["sessionId"]
                rpc(
                    client,
                    "session/prompt",
                    {
                        "request": {
                            "sessionId": root_id,
                            "requestId": "native-chat-input",
                            "mode": "queue",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "请创建登记源码配方的隔离编译方案，保存草稿等Owner准备。"
                                        if chat_plan == "workbench"
                                        else "请为登记模型创建推理部署和验证方案，保存草稿等Owner开始。"
                                    ),
                                }
                            ],
                        }
                    },
                )
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    idle = rpc(
                        client,
                        "session/wait",
                        {"request": {"sessionId": root_id, "timeoutMs": 5000}},
                    )
                    if chat_plan == "workbench":
                        build = builds.bound(root_id)
                        if build:
                            sessions = rpc(client, "session/list", {"_request": {}})["items"]
                            row = next(s for s in sessions if s["sessionId"] == root_id)
                            if not row["running"]:
                                break
                        time.sleep(0.1)
                        continue
                    view = rpc(client, "view", {"session_id": root_id}, "pilot-infra-api")
                    if view.get("task") and idle.get("reason") == "idle":
                        break
                    time.sleep(0.3)
                assert counts["root"] == 1, (counts, idle)
                if chat_plan == "workbench":
                    assert build["status"] == "draft" and not build["active_operation"]
                    assert (
                        "pilot_infra_workbench_plan" in seen[0][1]
                        and "pilot_infra_workbench_patch" not in seen[0][1]
                    )
                    sessions = rpc(client, "session/list", {"_request": {}})["items"]
                    row = next(s for s in sessions if s["sessionId"] == root_id)
                    assert row["projections"]["values"]["title"] == build["plan"]["title"]
                    builds.prepare(build["id"])
                    rpc(
                        client,
                        "session/prompt",
                        {
                            "request": {
                                "sessionId": root_id,
                                "requestId": "native-workbench-review",
                                "mode": "queue",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Read the prepared workbench status only. Do not build or delegate.",
                                    }
                                ],
                            }
                        },
                    )
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        idle = rpc(
                            client,
                            "session/wait",
                            {"request": {"sessionId": root_id, "timeoutMs": 5000}},
                        )
                        if counts["root"] >= 3:
                            sessions = rpc(client, "session/list", {"_request": {}})["items"]
                            row = next(s for s in sessions if s["sessionId"] == root_id)
                            if not row["running"]:
                                break
                    assert counts == {"root": 3, "expert": 0}, (counts, idle)
                    assert not row["running"], idle
                    # Official context deltas become model messages, while
                    # first-turn facts remain history. Check the latest domain
                    # block without assuming its provider transport role.
                    workbench_contexts = [
                        [
                            json.dumps(message)
                            for message in request.get("messages", [])
                            if f"Session: {root_id}; parent:" in json.dumps(message)
                        ]
                        for request in [
                            r
                            for r in model.requests
                            if not any(
                                "PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(m)
                                for m in r.get("messages", [])
                            )
                        ][1:]
                    ]
                    assert all(messages for messages in workbench_contexts)
                    current_contexts = [messages[-1] for messages in workbench_contexts]
                    assert all("Prepared node software workbench" in text for text in current_contexts)
                    assert all(
                        "No task is approved" not in text and "Planning only:" not in text
                        for text in current_contexts
                    )
                    assert all("not_validated" in text for text in current_contexts)
                    assert not builds.task(build["id"])["results"]
                    return
                assert view["task"]["state"]["session_id"] == root_id
                assert view["task"]["state"]["status"] == "draft"
                assert not view["task"]["state"]["active_job"]
                sessions = rpc(client, "session/list", {"_request": {}})["items"]
                row = next(s for s in sessions if s["sessionId"] == root_id)
                assert row["projections"]["values"]["title"] == plan()["title"]
                return
            result = rpc(
                client,
                "create",
                {"request_id": "native-infra-plan", "plan": plan(strategy="multi")},
                "pilot-infra-api",
            )
            root_id = str(result["sessionId"])
            t = result["task"]
            task_id = t["id"]
            if repo_repair:
                from hashlib import sha256

                source = service.workspace(t) / "adapter.py"
                source.write_text('def build_options():\n    return {"temperature": 1}\n')
                source_hash = sha256(source.read_bytes()).hexdigest()
                fixture_id = "a" * 32
                service.store.event(
                    task_id,
                    "job_settled",
                    {
                        "id": fixture_id,
                        "action": "adapter_check",
                        "status": "failed",
                        "candidate_id": "c1",
                        "evidence": f"jobs/{fixture_id}/result.json",
                        "result": {"passed": False, "failure_layer": "adapter"},
                    },
                )
                service.store.update(task_id, notified_jobs=[fixture_id])
                if repo_repair == "both":
                    infra_fixture_id = "b" * 32
                    service.store.event(
                        task_id,
                        "job_settled",
                        {
                            "id": infra_fixture_id,
                            "action": "launch",
                            "status": "failed",
                            "candidate_id": "c1",
                            "evidence": f"jobs/{infra_fixture_id}/result.json",
                            "result": {
                                "passed": False,
                                "failure_layer": "runtime",
                                "error": "CUDA out of memory",
                            },
                        },
                    )
                    service.store.update(task_id, notified_jobs=[fixture_id, infra_fixture_id])
            rpc(
                client,
                "start",
                {
                    "session_id": root_id,
                    "request_id": "native-infra-start",
                    "expected_revision": t["revision"],
                },
                "pilot-infra-api",
            )
            # Official root idle can precede child notification and the domain
            # settlement timer. Wait for business settlement, not a fixed number
            # of immediate session/wait responses (eight could take only 2.4 s).
            deadline = time.monotonic() + (180 if os.name == "nt" else 60)
            while time.monotonic() < deadline:
                rpc(client, "session/wait", {"request": {"sessionId": root_id, "timeoutMs": 5000}})
                view = rpc(client, "view", {"session_id": root_id}, "pilot-infra-api")
                if view["task"]["state"]["status"] == "paused":
                    break
                time.sleep(0.3)
            assert counts["expert"] >= 2, (counts, seen)
            if not repo_repair:
                assert trace_observations and root_id in json.dumps(trace_observations)
                assert not any("Error:" in json.dumps(value) for value in trace_observations)
            if repo_repair == "both":
                assert view["task"]["state"]["strategy"]["experts"] == [
                    "infra-expert",
                    "repo-expert",
                ]
                stage = 2
                counts.update(root=0, expert=0)
                rpc(
                    client,
                    "resume",
                    {
                        "session_id": root_id,
                        "request_id": "native-second-expert",
                        "expected_revision": view["task"]["revision"],
                    },
                    "pilot-infra-api",
                )
                deadline = time.monotonic() + (180 if os.name == "nt" else 60)
                while time.monotonic() < deadline:
                    rpc(
                        client,
                        "session/wait",
                        {"request": {"sessionId": root_id, "timeoutMs": 5000}},
                    )
                    view = rpc(client, "view", {"session_id": root_id}, "pilot-infra-api")
                    if view["task"]["state"]["status"] == "paused":
                        break
                    time.sleep(0.3)
                assert counts["expert"] >= 3, (counts, seen)
                experts = [names for role, names in seen if role == "expert"]
                assert any("pilot_infra_source_write" in names for names in experts)
                assert any("pilot_infra_source_write" not in names for names in experts)
            assert view["task"]["id"] == t["id"] and view["task"]["state"]["session_id"] == root_id
            assert view["task"]["state"]["status"] == "paused", view
            assert not view["task"]["state"]["experiments"]
            assert all("bash" not in names for _, names in seen)
            assert all(
                "pilot_infra_launch" not in names for role, names in seen if role == "expert"
            )
            if repo_repair:
                assert source.read_text() == "def build_options():\n    return {}\n"
                assert any(e["kind"] == "source_edit" for e in service.store.events(task_id))
        finally:
            client.close()
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                process.terminate()
            process.wait(timeout=15)
