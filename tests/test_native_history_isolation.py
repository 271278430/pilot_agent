"""Actual native Web, persisted events and compaction; a scripted model only."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from pilot_host.cli import composition
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.test_pilot_infra import setup


def test_native_roots_skill_compaction_and_cold_restore(tmp_path):
    project = Path(__file__).resolve().parents[1]
    setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    phase, step, a, b, seq = "seed", 0, None, None, None
    observed = []

    def respond(body):
        nonlocal step
        if any(
            "PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(m) for m in body.get("messages", [])
        ):
            request = json.loads(body["messages"][-1]["content"])["request"]
            return json.dumps(
                {
                    "relevance": "related",
                    "intent": "project_help",
                    "confidence": 0.99,
                    "reason": "scripted memory audit",
                    "evidence_quote": request[:80],
                    "missing_fields": [],
                }
            )
        if any("compaction engine" in str(m.get("content", "")) for m in body.get("messages", [])):
            return "Keep the Pilot memory audit and sentinel A. Exact earlier events can be read using history tools."
        observed.append(body)
        step += 1
        if step == 1:
            if phase == "foreign":
                return "session_event_read", {"session_id": b, "seq": seq}
            if phase == "skill":
                return "skill", {"name": "coordinate-inference-delivery"}
            if phase in {"own", "cold"}:
                return "session_event_read", {"session_id": a, "seq": seq}
        return "Scripted Pilot memory audit complete."

    with ScriptedModel([], responder=respond) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-memory",
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
            if row.get("id") == "llm-pi-ai":
                row["config"]["providers"]["apps"]["models"][0]["contextWindow"] = 18000
        (home / "patch.json").write_text(json.dumps(patch), encoding="utf-8")
        client = httpx.Client(follow_redirects=True, timeout=45)
        process = None

        def rpc(endpoint, args, channel="api"):
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
            value = r.json()["result"]
            assert value["ok"], value
            return value["value"]

        def stop():
            nonlocal process
            if process is None:
                return
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            process = None

        def launch():
            nonlocal process
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
            with (home / "host.log").open("ab") as log:
                process = subprocess.Popen(
                    command,
                    cwd=inbox,
                    stdout=log,
                    stderr=log,
                    env={**os.environ, "DSH_HOME": str(home), "REPOPILOT_API_KEY": "fixture-key"},
                )
            for _ in range(200):
                assert process.poll() is None, (home / "host.log").read_text(errors="replace")
                if (home / "entry.json").is_file():
                    try:
                        if (
                            client.get(
                                json.loads((home / "entry.json").read_text())["url"]
                            ).status_code
                            == 200
                        ):
                            return
                    except httpx.TransportError:
                        pass
                time.sleep(0.2)
            raise AssertionError("Native Host startup timed out")

        def events(sid):
            return rpc(
                "session/events",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": sid},
                        "maxMessages": 1000,
                    }
                },
            )["records"]

        def prompt(sid, text):
            nonlocal step
            step = 0
            count = len(observed)
            rpc(
                "session/prompt",
                {
                    "request": {
                        "sessionId": sid,
                        "requestId": uuid.uuid4().hex,
                        "mode": "queue",
                        "content": [{"type": "text", "text": text}],
                    }
                },
            )
            end = time.monotonic() + 150
            while time.monotonic() < end:
                idle = rpc("session/wait", {"request": {"sessionId": sid, "timeoutMs": 5000}})
                if idle.get("reason") == "idle" and len(observed) > count:
                    return events(sid)
            raise AssertionError("Native memory turn did not settle")

        try:
            launch()
            # Deliberately recreate the old shared cwd case. Native cwd alone
            # would permit querying the other root; Pilot adds family isolation.
            a = rpc(
                "session/create", {"request": {"cwd": str(inbox), "agentPreset": "infra-lead"}}
            )["sessionId"]
            b = rpc(
                "session/create", {"request": {"cwd": str(inbox), "agentPreset": "infra-lead"}}
            )["sessionId"]
            secret_a, secret_b = (
                "MEMORY_SENTINEL_A_" + uuid.uuid4().hex,
                "MEMORY_SENTINEL_B_" + uuid.uuid4().hex,
            )
            records_a = prompt(a, "Pilot记忆核验：" + secret_a)
            b_start = len(model.requests)
            prompt(b, "Pilot记忆核验：" + secret_b)
            b_calls = len(model.requests) - b_start
            seq = next(
                row["event"]["seq"] for row in records_a if row["event"]["type"] == "user/message"
            )
            phase = "foreign"
            records = prompt(a, "Pilot记忆核验：尝试外部会话读取，必须拒绝")
            errors = [
                row["event"]["data"]["message"]
                for row in records
                if row["event"]["type"] == "tool/result"
            ]
            assert errors[-1]["isError"]
            assert secret_b not in json.dumps(observed[-2:])
            phase = "skill"
            records = prompt(a, "Pilot角色技能核验")
            results = [
                row["event"]["data"]["message"]
                for row in records
                if row["event"]["type"] == "tool/result"
            ]
            assert not results[-1]["isError"], results[-1]
            assert "Docker" in json.dumps(results[-1])
            phase = "own"
            records = prompt(a, "Pilot同会话继承核验")
            results = [
                row["event"]["data"]["message"]
                for row in records
                if row["event"]["type"] == "tool/result"
            ]
            assert not results[-1]["isError"] and secret_a in json.dumps(results[-1])
            # Force compaction through the native command API, retaining exact
            # raw events rather than inferring compression from shortened text.
            phase = "seed"
            prompt(a, "Pilot记忆压力核验：" + ("这是一段可压缩的历史背景。" * 1200))
            stop()
            launch()
            phase = "cold"
            records = prompt(a, "Pilot冷恢复后读取原始事件")
            results = [
                row["event"]["data"]["message"]
                for row in records
                if row["event"]["type"] == "tool/result"
            ]
            assert not results[-1]["isError"] and secret_a in json.dumps(results[-1])
            assert secret_b not in json.dumps(observed[-2:])
            assert any(row["event"]["type"] == "compaction/summary" for row in records), (
                "Native automatic compaction did not commit a summary"
            )
            meter = json.loads((tmp_path / "runs/harness-policy" / a / "meter.json").read_text())
            assert meter["calls"] == len(model.requests) - b_calls, (
                "Root budget must include its classification and compaction, excluding the other root"
            )
            assert meter["input_tokens"] + meter["output_tokens"] == 120 * meter["calls"]
            assert a in [row["sessionId"] for row in rpc("session/list", {"_request": {}})["items"]]
        finally:
            stop()
            client.close()
