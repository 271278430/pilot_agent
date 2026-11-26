"""Installed native Harness approvals, scripted model, no node/GPU workload."""

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
from tests.test_fleet_assets import fleet, model_request


@pytest.mark.parametrize("outcome", ["allowed-once", "rejected", "cancelled", "unavailable"])
def test_native_asset_approval_and_unreachable_preflight_never_create_task(tmp_path, outcome):
    project = Path(__file__).resolve().parents[1]
    service, _, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    request = dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
    home = tmp_path / ".local/harness-web"
    home.mkdir()
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    calls = []

    def respond(body):
        calls.append(body)
        return ("pilot_infra_asset_request_prepare", request) if len(calls) == 1 else None

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-asset",
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
        answerer = tmp_path / "asset-answerer.mjs"
        answerer.write_text(
            "export function apply(ctx){ctx.on('approval/request',(r,next)=>r.toolName==='pilot_infra_asset_request_prepare'?Promise.resolve("
            + json.dumps(outcome)
            + "):next())}\n",
            encoding="utf-8",
        )
        patch[-1]["insert"].append({"id": "test-asset-answerer", "name": answerer.as_uri()})
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
        with (home / "host.log").open("w") as log:
            process = subprocess.Popen(
                command,
                cwd=inbox,
                stdout=log,
                stderr=log,
                env={
                    **os.environ,
                    "DSH_HOME": str(home),
                    "REPOPILOT_API_KEY": "fixture-only",
                    "PYTHONPATH": str(project / "src"),
                },
            )
        with httpx.Client(follow_redirects=True, timeout=30) as client:

            def rpc(method, payload, channel="api"):
                value = client.post(
                    f"http://127.0.0.1:{port}/{channel}/{method}",
                    json={
                        "type": "client-request",
                        "rpcId": uuid.uuid4().hex,
                        "method": method,
                        "payload": {"args": payload} if channel == "api" else payload,
                    },
                ).json()["result"]
                assert value["ok"], value
                return value["value"]

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
                sid = rpc("new-session", {}, "pilot-infra-api")["sessionId"]
                rpc(
                    "session/prompt",
                    {
                        "request": {
                            "sessionId": sid,
                            "requestId": uuid.uuid4().hex,
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
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    rpc("session/wait", {"request": {"sessionId": sid, "timeoutMs": 2000}})
                    with service.registry.db() as db:
                        row = db.execute("SELECT data FROM fleet_reviews").fetchone()
                    if row and json.loads(row[0])["decision"]:
                        break
                assert row
                review = json.loads(row[0])
                events = rpc(
                    "session/events",
                    {
                        "request": {
                            "address": {"kind": "session", "sessionId": sid},
                            "maxMessages": 100,
                        }
                    },
                )
                records = [r["event"] for r in events["records"] if "event" in r]
                asked = [e for e in records if e["type"] == "approval/asked"]
                decided = [e for e in records if e["type"] == "approval/decided"]
                assert len(asked) == len(decided) == 1
                assert decided[0]["data"]["outcome"] == outcome and review["decision"] == outcome
                assert review["digest"] in asked[0]["data"]["reason"]
                assert review["root_session"] == sid and not service.operation_rows()
                assert service.asset(asset["asset_id"])["status"] == "registered"
                assert not rpc("catalog", {}, "pilot-infra-api")["tasks"]
            finally:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    process.terminate()
                process.wait(timeout=15)
