"""Locked Harness native approval outcomes with scripted model; no GPU build/run."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from test_engine_source import artifact, candidate, fixture
from test_pilot_infra import plan

from pilot_host.cli import composition
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.intent_fixture import with_intent_fixture


@pytest.mark.parametrize("outcome", ["allowed-once", "rejected", "cancelled", "unavailable"])
def test_exact_engine_artifact_uses_official_native_approval(tmp_path, outcome):
    project = Path(__file__).resolve().parents[1]
    infra, source, _ = fixture(tmp_path, create_task=False)
    home = tmp_path / ".local/harness-web"
    home.mkdir()
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    pending = None
    calls = []

    def respond(body):
        calls.append(body)
        if len(calls) == 1:
            return "pilot_infra_engine_request_approval", {
                "candidate_id": pending["candidate_id"],
                "expected_digest": pending["review_digest"],
            }
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
        value = response.json()["result"]
        if checked:
            assert value["ok"], value
            return value["value"]
        return value

    with ScriptedModel([], responder=with_intent_fixture(respond)) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-engine",
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
        answerer = tmp_path / "scripted-engine-answerer.mjs"
        answerer.write_text(
            "export function apply(ctx) {ctx.on('approval/request', (request, next) => request.toolName === 'pilot_infra_engine_request_approval' ? Promise.resolve("
            + json.dumps(outcome)
            + ") : next())}\n"
        )
        if outcome == "allowed-once":
            patch[-1]["insert"].append({"id": "test-engine-answerer", "name": (project / "src/pilot_eval/approval_authorizer.mjs").as_uri(), "config": {"root": str(tmp_path)}})
        else:
            patch[-1]["insert"].append({"id": "test-engine-answerer", "name": answerer.as_uri()})
        (home / "patch.json").write_text(json.dumps(patch))
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
            session = rpc(client, "new-session", {}, "pilot-infra-api")["sessionId"]
            (tmp_path / ".local/evaluation-approved-roots.json").write_text(json.dumps({"session_ids": [session]}))
            task = infra.create(
                {
                    "request_id": "native-engine-plan-" + uuid.uuid4().hex,
                    "plan": plan(engine_source_profile="fixture-engine"),
                },
                session,
            )["task"]
            infra.control(
                task["id"],
                "start",
                {"request_id": uuid.uuid4().hex, "expected_revision": task["revision"]},
            )
            infra.store.update(task["id"], execution_mode="manual")
            task = infra.store.get(task["id"])
            source.prepare(task)
            c = candidate(infra, task)
            artifact(infra, task, c)
            pending = source.candidate_view(task, c)
            denied = rpc(
                client,
                "engine-native-decision",
                {
                    "session_id": session,
                    "candidate_id": c["id"],
                    "expected_digest": pending["review_digest"],
                    "decision": "allowed-once",
                },
                "pilot-infra-api",
                False,
            )
            assert not denied["ok"]
            rpc(
                client,
                "session/prompt",
                {
                    "request": {
                        "sessionId": session,
                        "requestId": "engine-approval-input",
                        "mode": "queue",
                        "content": [
                            {
                                "type": "text",
                                "text": "Ask native Owner approval for the exact frozen engine artifact. Do not launch.",
                            }
                        ],
                    }
                },
            )
            until = time.monotonic() + 75
            while time.monotonic() < until:
                rpc(client, "session/wait", {"request": {"sessionId": session, "timeoutMs": 2000}})
                if source.decision(task, c):
                    break
            events = rpc(
                client,
                "session/events",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": session},
                        "maxMessages": 100,
                    }
                },
            )
            records = [r["event"] for r in events["records"] if "event" in r]
            asked = [r for r in records if r["type"] == "approval/asked"]
            decided = [r for r in records if r["type"] == "approval/decided"]
            assert len(asked) == len(decided) == 1, records
            assert asked[0]["data"]["id"] == decided[0]["data"]["id"]
            assert pending["review_digest"] in asked[0]["data"]["reason"]
            assert decided[0]["data"]["outcome"] == outcome
            assert source.decision(task, c) == outcome
            assert not infra.store.get(task["id"])["state"]["active_job"]
            assert not infra.store.get(task["id"])["state"].get("service_candidate")
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
