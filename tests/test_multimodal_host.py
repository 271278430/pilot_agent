"""Installed native Host: actual image bytes on the wire, isolation and restore."""

import base64
import json
import os
import socket
import struct
import subprocess
import time
import uuid
import zlib
from pathlib import Path

import httpx

from pilot_host.cli import composition, prepare_profile
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.test_pilot_infra import setup


def png():
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 32) * 32))
        + chunk(b"IEND", b"")
    ).decode()


def test_native_multimodal_model_choice_isolation_restart_and_resource_settings(tmp_path):
    project = Path(__file__).resolve().parents[1]
    service = setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    captured = []

    def respond(body):
        captured.append(body)
        if any("compaction engine" in str(m.get("content", "")) for m in body.get("messages", [])):
            return "Retain the Pilot image analysis and user read-only scope. Earlier image references remain in exact history."
        if "PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(body.get("messages", [])):
            return json.dumps(
                {
                    "relevance": "related",
                    "intent": "domain_question",
                    "confidence": 0.99,
                    "reason": "scripted image analysis",
                    "evidence_quote": "请分析图片中的环境报错，只解释不要修改",
                    "missing_fields": [],
                }
            )
        return "图片协议已收到（脚本测试，不是识图质量结论）"

    with ScriptedModel([], responder=respond) as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-text",
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

        # Mount the official manual command only in this fixture. Retaining
        # zero tail makes a small, balanced picture history compactable.
        def fixture_compaction(value):
            if isinstance(value, dict):
                if value.get("id") == "compaction-basic":
                    value["config"]["retainTokens"] = 0
                if value.get("id") == "compaction" and isinstance(value.get("config"), list):
                    value["config"].append(
                        {
                            "id": "command-compact-fixture",
                            "name": "@deepseek-ai/dsh-command-compact",
                        }
                    )
                for item in value.values():
                    fixture_compaction(item)
            elif isinstance(value, list):
                for item in value:
                    fixture_compaction(item)

        fixture_compaction(patch)
        patch[-1]["insert"].append(
            {
                "id": "native-multimodal-probe",
                "name": (project / "tests/native_multimodal_probe.mjs").as_uri(),
                "config": {"root": str(tmp_path)},
            }
        )
        path = home / "patch.json"
        path.write_text(json.dumps(patch))
        process = None
        client = httpx.Client(follow_redirects=True, timeout=45, trust_env=False)

        def launch():
            nonlocal process
            prepare_profile(home, path)
            command = [dsh_executable(), "--profile", "web", "--no-open", "--port", str(port)]
            if os.name == "nt":
                command = ["cmd.exe", "/c", *command]
            with (home / "host.log").open("ab") as log:
                process = subprocess.Popen(
                    command,
                    cwd=inbox,
                    stdout=log,
                    stderr=log,
                    env={
                        **os.environ,
                        "DSH_HOME": str(home),
                        "REPOPILOT_API_KEY": "fixture-only",
                        "TMPDIR": str(tmp_path),
                        "TMP": str(tmp_path),
                        "TEMP": str(tmp_path),
                    },
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
            raise AssertionError("Native multimodal Host startup timeout")

        def stop():
            nonlocal process
            if process:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True
                    )
                else:
                    process.terminate()
                process.wait(timeout=20)
                process = None

        def rpc(method, args=None, channel="api", reject=False):
            response = client.post(
                f"http://127.0.0.1:{port}/{channel}/{method}",
                json={
                    "type": "client-request",
                    "rpcId": uuid.uuid4().hex,
                    "method": method,
                    "payload": {"args": args or {}} if channel == "api" else args or {},
                },
            )
            response.raise_for_status()
            value = response.json()["result"]
            if reject:
                assert not value["ok"], value
                return value["error"]
            assert value["ok"], value
            return value["value"]

        def setting(op, payload=None):
            return rpc(op, payload, "pilot-host-settings-api")

        def create():
            return rpc(
                "session/create", {"request": {"cwd": str(inbox), "agentPreset": "infra-lead"}}
            )["sessionId"]

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

        def prompt(sid, content):
            count = len(captured)
            prior_events = len(events(sid))
            rpc(
                "session/prompt",
                {
                    "request": {
                        "sessionId": sid,
                        "requestId": uuid.uuid4().hex,
                        "mode": "queue",
                        "content": content,
                    }
                },
            )
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                settled = rpc("session/wait", {"request": {"sessionId": sid, "timeoutMs": 5000}})
                if settled.get("reason") == "idle" and any(
                    r["event"]["type"] == "assistant/message" for r in events(sid)[prior_events:]
                ):
                    return captured[count:]
            raise AssertionError("Native multimodal turn failed to settle")

        image = {"type": "image", "mediaType": "image/png", "data": png(), "name": "环境截图.png"}
        try:
            launch()
            a, b = create(), create()
            view = setting("view")
            vision = {
                "model_id": "scripted-vision",
                "display_name": "视觉诊断",
                "base_url": model.url,
                "context_window": 16384,
                "max_output_tokens": 4096,
                "auth_required": False,
                "vision": True,
                "reasoning": "off",
            }
            view = setting(
                "save",
                {
                    "section": "model",
                    "add_new": True,
                    "model": vision,
                    "expected_revision": view["revision"],
                },
            )
            selected = view["desired"]["models"][1]
            view = setting("sync", {"expected_revision": view["revision"]})
            assert not view["pending"]
            before = len(captured)
            error = rpc(
                "session/prompt",
                {
                    "request": {
                        "sessionId": a,
                        "requestId": uuid.uuid4().hex,
                        "mode": "queue",
                        "content": [image],
                    }
                },
                reject=True,
            )
            assert error["code"] == "session/attachment-invalid" and len(captured) == before
            rpc(
                "session/selectModel",
                {
                    "request": {
                        "sessionId": a,
                        "provider": selected["provider"],
                        "model": selected["model_id"],
                    }
                },
            )
            assert setting("view")["live_model"]["model_id"] == "scripted-text"
            calls = prompt(
                a, [{"type": "text", "text": "请分析图片中的环境报错，只解释不要修改"}, image]
            )
            assert all(c["model"] == "scripted-vision" for c in calls)
            assert all(
                any(
                    p.get("type") == "image_url"
                    for m in c["messages"]
                    if isinstance(m.get("content"), list)
                    for p in m["content"]
                )
                for c in calls
            )
            durable = json.dumps(events(a))
            assert (
                "image" in durable and image["data"] not in durable and "data:image" not in durable
            )
            # Session B never sees A's image and keeps its own text model.
            calls = prompt(b, [{"type": "text", "text": "请解释推理环境的部署流程"}])
            assert calls[-1]["model"] == "scripted-text" and "image_url" not in json.dumps(calls)
            c = create()
            calls = prompt(c, [{"type": "text", "text": "请解释推理环境的部署流程"}])
            assert calls[-1]["model"] == "scripted-text"
            # Pure image asks clarification without a semantic auxiliary call.
            calls = prompt(a, [image])
            assert not any("PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(c) for c in calls)
            target = tmp_path / "downloads"
            view = setting(
                "save",
                {
                    "section": "resources",
                    "resource_root": str(target),
                    "create_missing": True,
                    "expected_revision": view["revision"],
                },
            )
            view = setting("sync", {"expected_revision": view["revision"]})
            assert (
                view["applied"]["resource_root"] == str(target)
                and (target / ".pilot-resources.json").is_file()
            )
            stop()
            cold = composition(
                tmp_path, {"REPOPILOT_MODEL": "stale", "REPOPILOT_BASE_URL": model.url}
            )
            profiles = next(r for r in cold if r.get("id") == "llm-pi-ai")["config"]["providers"]
            profiles[selected["provider"]].update(maxRequestImages=1, imageOffloadCountQuantum=1)
            for row in patch:
                if row.get("id") == "llm-pi-ai":
                    row["config"]["providers"] = profiles
            path.write_text(json.dumps(patch))
            launch()
            assert not setting("view")["pending"]
            native = next(
                v for v in rpc("settings/describe")["namespaces"] if v["ns"] == "llm-pi-ai"
            )
            rpc(
                "settings/update",
                {
                    "ns": native["ns"],
                    "patch": {
                        "providers": {
                            selected["provider"]: {
                                "maxRequestImages": 1,
                                "imageOffloadCountQuantum": 1,
                            }
                        }
                    },
                    "expectedRevision": native["revision"],
                },
            )
            calls = prompt(a, [{"type": "text", "text": "继续解释部署截图中的环境报错"}])
            assert calls[-1]["model"] == "scripted-vision" and "image_url" in json.dumps(calls[-1])
            assert any(row["event"]["type"] == "image/offload" for row in events(a))
            before_summary = len(captured)
            compact = rpc("compact", {"session_id": a}, "pilot-test-multimodal")
            assert compact["result"]["kind"] in {"success", "noop"}, compact
            calls = captured[before_summary:]
            records = events(a)
            assert any(row["event"]["type"] == "compaction/summary" for row in records)
            summary = [
                body
                for body in calls
                if any(
                    "compaction engine" in str(m.get("content", ""))
                    for m in body.get("messages", [])
                )
            ]
            assert summary and all(body["model"] == "scripted-vision" for body in summary)
            assert len(service.store.list()) == 0
        finally:
            stop()
            client.close()
