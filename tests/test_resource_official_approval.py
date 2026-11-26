"""Real installed Harness turn/approval/preset integration; scripted model, no GPU."""

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
from tests.test_resource_authority import fixture, proposal, target


@pytest.mark.parametrize("outcome", ["allowed-once", "rejected", "cancelled", "unavailable"])
def test_native_approval_audit_and_task_authority(tmp_path, outcome):
    project = Path(__file__).resolve().parents[1]
    infra, resources, _ = fixture(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir()
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    calls = []
    pending = None

    def respond(body):
        calls.append(body)
        if len(calls) == 1:
            return "pilot_infra_resource_request_approval", target(pending)
        if outcome == "allowed-once" and len(calls) == 2:
            return "pilot_infra_resource_execute", target(pending)
        return None

    def rpc(client, endpoint, args, channel="api", checked=True):
        response = client.post(
            f"http://127.0.0.1:{port}/{channel}/{endpoint}",
            json={
                "type": "client-request",
                "rpcId": uuid.uuid4().hex,
                "method": endpoint,
                "payload": {"args": args} if channel == "api" else args,
            },
        )
        response.raise_for_status()
        result = response.json()["result"]
        if checked:
            assert result["ok"], result
            return result["value"]
        return result

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-resource",
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
            if row.get("id") == "sandbox-policy":
                row["config"]["workspaceRoot"] = str(inbox)
        # Scoped deterministic answerer, never installed into production.
        answerer = tmp_path / "scripted-answerer.mjs"
        answerer.write_text(
            "export function apply(ctx) {ctx.on('approval/request', (request, next) => request.toolName === 'pilot_infra_resource_request_approval' ? Promise.resolve("
            + json.dumps(outcome)
            + ") : next())}\n",
            encoding="utf-8",
        )
        patch[-1]["insert"].append({"id": "test-resource-answerer", "name": answerer.as_uri()})
        (home / "patch.json").write_text(json.dumps(patch), encoding="utf-8")
        command = [
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
            command = ["cmd.exe", "/c", *command]
        with (home / "host.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=inbox,
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "DSH_HOME": str(home),
                    "REPOPILOT_API_KEY": "scripted-fixture",
                    "PYTHONPATH": str(project / "src"),
                },
            )
        client = httpx.Client(timeout=35, follow_redirects=True)
        try:
            for _ in range(180):
                assert process.poll() is None, (home / "host.log").read_text(errors="replace")
                if (home / "entry.json").is_file():
                    try:
                        if (
                            client.get(
                                json.loads((home / "entry.json").read_text())["url"]
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.TransportError:
                        pass
                time.sleep(0.2)
            stale = rpc(
                client,
                "resource-catalog",
                {"session_id": "deleted-resource-selection"},
                "pilot-infra-api",
            )
            assert stale["servers"] and stale["session_permission"] is None
            assert stale["session_selection"]["status"] == "missing"
            denied = rpc(
                client,
                "resource-session-permission",
                {"session_id": "deleted-resource-selection", "mode": "review"},
                "pilot-infra-api",
                checked=False,
            )
            assert not denied["ok"]
            session_id = rpc(client, "new-session", {}, "pilot-infra-api")["sessionId"]
            pending = proposal(resources, session_id)
            # Owner mode is the official permission-presets service, not a
            # duplicated boolean in domain state.
            rpc(
                client,
                "resource-session-permission",
                {"session_id": session_id, "mode": "observe"},
                "pilot-infra-api",
            )
            current = rpc(client, "resource-catalog", {"session_id": session_id}, "pilot-infra-api")
            assert current["session_permission"]["mode"] == "observe"
            rpc(
                client,
                "resource-session-permission",
                {"session_id": session_id, "mode": "review"},
                "pilot-infra-api",
            )
            rpc(
                client,
                "session/prompt",
                {
                    "request": {
                        "sessionId": session_id,
                        "requestId": "scripted-resource-input",
                        "mode": "queue",
                        "content": [
                            {
                                "type": "text",
                                "text": "Ask approval for the frozen proposal; materialize only if allowed.",
                            }
                        ],
                    }
                },
            )
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                rpc(
                    client,
                    "session/wait",
                    {"request": {"sessionId": session_id, "timeoutMs": 2000}},
                )
                value = resources.proposal(target(pending), session_id)
                if value["decision"] and (outcome != "allowed-once" or value["task_id"]):
                    break
            events = rpc(
                client,
                "session/events",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": session_id},
                        "maxMessages": 100,
                    }
                },
            )
            records = [r["event"] for r in events["records"] if "event" in r]
            asked = [e for e in records if e["type"] == "approval/asked"]
            decided = [e for e in records if e["type"] == "approval/decided"]
            assert len(asked) == len(decided) == 1, records
            assert asked[0]["data"]["id"] == decided[0]["data"]["id"]
            assert decided[0]["data"]["outcome"] == outcome
            assert pending["digest"] in asked[0]["data"]["reason"]
            assert value["decision"] == outcome and value["decision_source"] == "native-approval"
            if outcome == "allowed-once":
                task = infra.store.get(value["task_id"])
                assert task["state"]["status"] == "draft" and not task["state"]["active_job"]
                assert task["state"]["session_id"] == session_id
                # Backend materialization precedes official tool/turn commit.
                # Changing a Session's permission correctly requires it idle.
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    rpc(client, "session/wait", {"request": {"sessionId": session_id, "timeoutMs": 5000}})
                    sessions = rpc(client, "session/list", {"_request": {}})["items"]
                    if not next(s for s in sessions if s["sessionId"] == session_id)["running"]:
                        break
                else:
                    raise AssertionError("Native resource planning turn did not settle")
                assert len(calls) == 2
                rpc(
                    client,
                    "resource-session-permission",
                    {"session_id": session_id, "mode": "observe"},
                    "pilot-infra-api",
                )
                denied = rpc(
                    client,
                    "start",
                    {
                        "session_id": session_id,
                        "request_id": "test-start-observe",
                        "expected_revision": task["revision"],
                    },
                    "pilot-infra-api",
                    False,
                )
                assert (
                    not denied["ok"] and infra.store.get(task["id"])["state"]["status"] == "draft"
                )
            else:
                assert not infra.store.list()
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
