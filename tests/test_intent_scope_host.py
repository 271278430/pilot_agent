"""The pinned native stream gate settles scope notices without provider or tool work."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from pilot_host.cli import composition, prepare_profile
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.test_multimodal_host import png
from tests.test_pilot_infra import setup


def test_native_scope_reply_blocks_generic_image_answers_but_allows_business_help(tmp_path):
    project = Path(__file__).resolve().parents[1]
    service = setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    main_calls, classification_calls = [], []

    def respond(body):
        if "PILOT_BUSINESS_INTENT_CLASSIFIER" in json.dumps(body.get("messages", [])):
            content = body["messages"][-1]["content"]
            raw = content if isinstance(content, str) else next(p["text"] for p in content if p["type"] == "text")
            request = json.loads(raw)["request"]
            classification_calls.append(body)
            if request == "看不懂":
                return "invalid classification"
            intent = "out_of_scope" if request == "这是什么" else "project_help"
            return json.dumps({"relevance": "unrelated" if intent == "out_of_scope" else "related",
                               "intent": intent, "confidence": 0.99, "reason": "explicit scope fixture",
                               "evidence_quote": request, "missing_fields": []})
        main_calls.append(body)
        return "Pilot主控负责理解目标，Infra专家分析环境，Repo专家处理受控源码修复。"

    with ScriptedModel([], responder=respond) as model:
        patch = composition(project, {"REPOPILOT_MODEL": "scripted-vision", "REPOPILOT_BASE_URL": model.url,
                                      "PILOT_CLIENT_URL": f"http://127.0.0.1:{port}"}, settings_root=tmp_path)
        for row in patch[-1]["insert"]:
            if "root" in row.get("config", {}):
                row["config"]["root"] = str(tmp_path)
        for row in patch:
            if row.get("id") == "working-directory":
                row["config"]["defaultDirectory"] = str(inbox)
            if row.get("id") == "llm-pi-ai":
                row["config"]["providers"]["apps"]["models"][0]["input"] = ["text", "image"]
        path = home / "patch.json"
        path.write_text(json.dumps(patch), encoding="utf-8")
        prepare_profile(home, path)
        command = [dsh_executable(), "--profile", "web", "--no-open", "--port", str(port)]
        if os.name == "nt":
            command = ["cmd.exe", "/c", *command]
        with (home / "host.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=inbox, stdout=log, stderr=log,
                                       env={**os.environ, "DSH_HOME": str(home), "REPOPILOT_API_KEY": "fixture-only",
                                            "TMPDIR": str(tmp_path), "TMP": str(tmp_path), "TEMP": str(tmp_path)})
        try:
            with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
                for _ in range(200):
                    assert process.poll() is None
                    if (home / "entry.json").is_file():
                        try:
                            if client.get(json.loads((home / "entry.json").read_text())["url"]).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                    time.sleep(0.2)
                else:
                    raise AssertionError("Native scope Host startup timeout")

                def rpc(method, args):
                    response = client.post(f"http://127.0.0.1:{port}/api/{method}", json={
                        "type": "client-request", "rpcId": uuid.uuid4().hex, "method": method, "payload": {"args": args}})
                    response.raise_for_status()
                    result = response.json()["result"]
                    assert result["ok"], result
                    return result["value"]

                sid = rpc("session/create", {"request": {"cwd": str(inbox), "agentPreset": "infra-lead"}})["sessionId"]

                def events():
                    return rpc("session/events", {"request": {"address": {"kind": "session", "sessionId": sid},
                                                                "maxMessages": 1000}})["records"]

                image = {"type": "image", "mediaType": "image/png", "data": png(), "name": "数学题截图.png"}
                for text, extra, expected, count in [("这是什么", [image], "Pilot 业务范围提示", 0),
                                                     ("Pilot有哪些角色？", [], "Infra专家", 1),
                                                     ("看不懂", [], "请说明具体的推理基础设施目标", 1),
                                                     ("你好", [], "我是 Pilot", 1)]:
                    start = len(events())
                    rpc("session/prompt", {"request": {"sessionId": sid, "requestId": uuid.uuid4().hex, "mode": "queue",
                                                      "content": [{"type": "text", "text": text}, *extra]}})
                    deadline = time.monotonic() + 80
                    while time.monotonic() < deadline:
                        settled = rpc("session/wait", {"request": {"sessionId": sid, "timeoutMs": 5000}})
                        records = events()[start:]
                        if settled.get("reason") == "idle" and any(r["event"]["type"] == "assistant/message" for r in records):
                            break
                    else:
                        raise AssertionError("Scope turn did not settle")
                    messages = [r["event"]["data"] for r in records if r["event"]["type"] == "assistant/message"]
                    assert expected in json.dumps(messages, ensure_ascii=False)
                    assert len(main_calls) == count
                    if text != "Pilot有哪些角色？":
                        assert all("usage" not in message for message in messages), "Fixed replies must not fabricate provider context usage"
                    assert not any(r["event"]["type"] == "tool/call" for r in records)
                    assert any(r["event"]["type"] == "turn/end" and r["event"]["data"]["reason"]["kind"] == "completed"
                               for r in records)
                assert len(classification_calls) == 3
                assert any(p.get("type") == "image_url" for m in classification_calls[0]["messages"]
                           if isinstance(m.get("content"), list) for p in m["content"])
                assert not service.store.list()
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.terminate()
            process.wait(timeout=20)
