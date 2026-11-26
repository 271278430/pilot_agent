"""Scripted model, genuine native Host/MCP/approvals; no GPU or paid model."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from pilot_host.cli import composition
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.intent_fixture import with_intent_fixture
from tests.test_pilot_infra import setup


@pytest.mark.parametrize("outcome", ["allowed-once", "rejected"])
def test_native_mcp_same_config_route_approval_and_real_catalog(tmp_path, outcome):
    project = Path(__file__).resolve().parents[1]
    setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    server = {
        "name": "fixture",
        "description": "native test",
        "transport": "stdio",
        "enabled": True,
        "roles": ["infra-lead"],
        "access": "reference",
        "command": sys.executable,
        "args": [str(project / "tests/mcp_fixture.py")],
        "image": "sha256:" + "b" * 64,
    }
    definition = tmp_path / "server.json"
    definition.write_text(json.dumps(server), encoding="utf-8")
    cli = [sys.executable, "-m", "pilot_host.cli", "tools", "--root", str(tmp_path)]
    prepared = subprocess.run(
        [*cli, "put", "--file", str(definition), "--revision", "0"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(prepared.stdout)["revision"] == 1
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    calls = []
    count = 0

    def respond(body):
        nonlocal count
        count += 1
        calls.append({t["function"]["name"] for t in body.get("tools", [])})
        return ("mcp__fixture__echo", {"text": "native mcp evidence"}) if count == 1 else None

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-mcp",
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
        answerer = tmp_path / "test-mcp-answerer.mjs"
        answerer.write_text(
            "export function apply(ctx){ctx.on('approval/request',(r,next)=>r.toolName.startsWith('mcp__')?Promise.resolve("
            + json.dumps(outcome)
            + "):next())}",
            encoding="utf-8",
        )
        patch[-1]["insert"].append({"id": "test-mcp-answerer", "name": answerer.as_uri()})
        for row in patch[-1]["insert"]:
            if row.get("id") == "pilot-execution-sandbox":
                row["name"] = (project / "tests/sandbox_stub.mjs").as_uri()
        config_data = json.loads((tmp_path / ".local/tool-system.json").read_text())
        for row in config_data["servers"]:
            row["image"] = "sha256:" + "b" * 64
        (tmp_path / ".local/tool-system.json").write_text(json.dumps(config_data))
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
                env={**os.environ, "DSH_HOME": str(home), "REPOPILOT_API_KEY": "fixture-only"},
            )
        client = httpx.Client(follow_redirects=True, timeout=35)

        def rpc(method, payload, channel="api"):
            result = client.post(
                f"http://127.0.0.1:{port}/{channel}/{method}",
                json={
                    "type": "client-request",
                    "rpcId": uuid.uuid4().hex,
                    "method": method,
                    "payload": {"args": payload} if channel == "api" else payload,
                },
            ).json()["result"]
            assert result["ok"], result
            return result["value"]

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
            else:
                pytest.fail("Host failed to start")
            for _ in range(75):
                catalog = rpc("catalog", {}, "pilot-tools-api")
                if catalog["servers"][0]["status"] == "verified":
                    break
                time.sleep(0.2)
            assert catalog["servers"][0]["registered_tools"]
            assert (
                catalog["revision"]
                == json.loads(
                    subprocess.run(
                        [*cli, "list"], capture_output=True, text=True, check=True
                    ).stdout
                )["revision"]
            )
            for prompt, allowed in [("给我写一首关于月亮的诗", False), ("你能部署Qwen吗", True)]:
                count = 0
                start = len(calls)
                sid = rpc("new-session", {}, "pilot-infra-api")["sessionId"]
                rpc(
                    "session/prompt",
                    {
                        "request": {
                            "sessionId": sid,
                            "requestId": uuid.uuid4().hex,
                            "mode": "queue",
                            "content": [{"type": "text", "text": prompt}],
                        }
                    },
                )
                events = []
                for _ in range(60):
                    rpc("session/wait", {"request": {"sessionId": sid, "timeoutMs": 2000}})
                    events = [r["event"] for r in rpc(
                        "session/events",
                        {"request": {"address": {"kind": "session", "sessionId": sid}, "maxMessages": 150}},
                    )["records"] if "event" in r]
                    if (
                        any(e["type"] == "assistant/message" for e in events)
                        and not next(
                            s
                            for s in rpc("session/list", {"_request": {}})["items"]
                            if s["sessionId"] == sid
                        )["running"]
                    ):
                        break
                else:
                    pytest.fail("MCP turn did not settle")
                if bool(calls[start:]) != allowed or (allowed and "mcp__fixture__echo" not in calls[start]):
                    diagnostic = rpc("catalog", {"session_id": sid}, "pilot-tools-api")
                    pytest.fail(json.dumps({"servers": diagnostic["servers"], "selected_session": diagnostic["selected_session"]}))
                records = rpc(
                    "session/events",
                    {
                        "request": {
                            "address": {"kind": "session", "sessionId": sid},
                            "maxMessages": 150,
                        }
                    },
                )["records"]
                events = [r["event"] for r in records if "event" in r]
                asked = [e for e in events if e["type"] == "approval/asked"]
                assert len(asked) == int(allowed)
                results = [e["data"]["message"] for e in events if e["type"] == "tool/result"]
                if not allowed:
                    assert not results and len(calls) == start
                if allowed:
                    assert results and results[0]["isError"] == (outcome == "rejected")
                    assert (
                        next(e for e in events if e["type"] == "approval/decided")["data"][
                            "outcome"
                        ]
                        == outcome
                    )
                    tools = rpc("catalog", {"session_id": sid}, "pilot-tools-api")
                    assert any(t["name"] == "pilot_infra_status" for t in tools["local_tools"])
                    assert any(
                        s["name"] == "coordinate-inference-delivery" for s in tools["skills"]
                    )
                    assert tools["selected_session"]["role"] == "infra-lead"
            disabled = rpc(
                "disable", {"name": "fixture", "expected_revision": 1}, "pilot-tools-api"
            )
            assert disabled["servers"][0]["status"] == "disabled"
            assert not disabled["servers"][0]["registered_tools"]
            assert not json.loads(
                subprocess.run([*cli, "list"], capture_output=True, text=True, check=True).stdout
            )["servers"][0]["enabled"]
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
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
