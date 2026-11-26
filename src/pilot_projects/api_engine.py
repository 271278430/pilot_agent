"""Trusted API worker; generated apps receive a socket, never the API key."""

import hashlib
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from pathlib import Path

import httpx

from pilot_core.model_protocol import chat_thinking
from pilot_delivery.preview_contract import PreviewRequest
from repopilot.storage import atomic_json


def stop_worker(folder):
    """Stop only the API process whose launch and command line belong to this task."""
    import signal

    folder = Path(folder).resolve()
    launch = folder / "launch.json"
    if not launch.is_file():
        return
    pid = json.loads(launch.read_text())["pid"]
    command = Path(f"/proc/{pid}/cmdline")
    if not command.exists():
        return
    args = command.read_bytes().split(b"\x00")
    if b"pilot_projects.api_engine" not in args or str(folder / "config.json").encode() not in args:
        raise ValueError("API Worker进程归属不符，不结束该进程")
    os.kill(pid, signal.SIGTERM)


def generate_api(config, request, client):
    from pilot_core.config import credentials

    current = credentials()
    if current["REPOPILOT_MODEL"] != config["model_id"] or not all(current.values()):
        raise ValueError("已批准的API模型或配置已变化，请重新确认")
    response = client.post(
        current["REPOPILOT_BASE_URL"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + current["REPOPILOT_API_KEY"]},
        json={
            "model": config["model_id"],
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "max_tokens": 2048,
            **chat_thinking(config["model_id"]),
        },
    )
    response.raise_for_status()
    body = response.json()
    choice = body["choices"][0]
    text = choice["message"].get("content")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("API没有返回完整答案；不展示历史结果或把思考作为答案")
    return {
        "text": text,
        "completion_tokens": body.get("usage", {}).get("completion_tokens"),
        "input_tokens": body.get("usage", {}).get("prompt_tokens"),
        "finish_reason": "length" if choice.get("finish_reason") == "length" else "stop",
    }


def main(config_path):
    config = json.loads(Path(config_path).read_text())
    root, folder = Path(config["project_root"]).resolve(), Path(config["worker_dir"]).resolve()
    sock = Path(config["socket"])
    if (
        not folder.is_relative_to(root / "runs/apps")
        or sock != folder / "socket.sock"
        or sock.exists()
    ):
        raise ValueError("API Worker必须在任务目录内且不能覆盖现有socket")
    identity = hashlib.sha256(
        json.dumps(
            {
                "model": config["model_id"],
                "source": "configured_api",
                "protocol": "model-preview-v2",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    ready = {
        "protocol": "model-preview-v2",
        "model_identity": identity,
        "model_id": config["model_id"],
        "runtime": "api",
        "pid": os.getpid(),
        "max_output_tokens": 2048,
        "max_input_tokens": 4096,
    }
    lock = threading.Lock()
    ledger = folder / "api-budget.json"
    if ledger.exists():
        raise ValueError("API预算已有记录，不能通过重启重置额度")
    atomic_json(ledger, {"requests": 0, "maximum": 50})

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                wire = json.loads(self.rfile.readline(131072))
                if not secrets.compare_digest(str(wire.get("token", "")), config["token"]):
                    raise ValueError("API模型连接未授权")
                if wire["operation"] == "ping":
                    result = ready
                elif wire["operation"] == "chat":
                    request = PreviewRequest.model_validate(wire["request"])
                    if sum(len(m.content) for m in request.messages) > 16000:
                        raise ValueError("API输入超过应用预算，请精简内容")
                    if not lock.acquire(blocking=False):
                        raise ValueError("模型正在处理另一请求")
                    try:
                        meter = json.loads(ledger.read_text())
                        if meter["requests"] >= meter["maximum"]:
                            raise ValueError("本次批准的50次API调用预算已用完")
                        atomic_json(ledger, {**meter, "requests": meter["requests"] + 1})
                        started = time.monotonic()
                        with httpx.Client(timeout=120, trust_env=False) as client:
                            result = generate_api(config, request, client)
                        result.update(
                            model_identity=identity,
                            model_id=config["model_id"],
                            protocol="model-preview-v2",
                            inference_ms=round((time.monotonic() - started) * 1000, 2),
                            receipt_id=secrets.token_hex(16),
                            model_input=request.messages[-1].content,
                        )
                        with (folder / "inference-receipts.jsonl").open("a") as stream:
                            stream.write(
                                json.dumps(
                                    {
                                        "receipt_id": result["receipt_id"],
                                        "model_identity": identity,
                                        "input_sha256": hashlib.sha256(
                                            result["model_input"].encode()
                                        ).hexdigest(),
                                        "answer_sha256": hashlib.sha256(
                                            result["text"].encode()
                                        ).hexdigest(),
                                        "api_model": config["model_id"],
                                    }
                                )
                                + "\n"
                            )
                    finally:
                        lock.release()
                else:
                    raise ValueError("未注册的API模型操作")
                response = {"ok": True, "result": result}
            except Exception as error:
                response = {"ok": False, "error": "API模型调用失败：" + type(error).__name__}
            self.wfile.write((json.dumps(response) + "\n").encode())

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    with Server(str(sock), Handler) as server:
        os.chmod(sock, 0o666)
        atomic_json(folder / "ready.json", ready)
        server.serve_forever(poll_interval=0.3)


if __name__ == "__main__":
    main(sys.argv[1])
