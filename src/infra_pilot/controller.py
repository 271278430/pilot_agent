"""Deterministic, state-driven control provider for the official Harness.

It selects tools from observed job state, including bounded diagnosis/repair.
It is not a trained language model and never reports autonomous LLM quality.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from repopilot.storage import read_json

from .service import next_stage


def choose(state):
    if state.get("active") or state.get("pending"):
        return "inspect", {"wait_seconds": 20}
    if state.get("failed"):
        diagnosis = state.get("diagnosis")
        if not diagnosis:
            return "diagnose", {}
        if diagnosis["repair"] and not any(row["action"] == diagnosis["repair"] for row in state["repairs"]):
            return "repair", {"action": diagnosis["repair"]}
        return None
    stage = next_stage(state)
    return ("advance", {"stage": stage}) if stage else None


class RulesProvider:
    def __init__(self, run):
        owner = self
        self.run, self.calls = run, 0
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls += 1
                state = read_json(run / "checkpoint.json")
                summary = str(body.get("messages", [{}])[-1].get("content", "")).startswith("You are now acting as a compaction engine")
                action = None if summary else choose(state)
                delta = {"role": "assistant"}
                if action:
                    delta["tool_calls"] = [{"index": 0, "id": f"infra-{owner.calls}", "type": "function", "function": {"name": "mcp__infra__" + action[0], "arguments": json.dumps(action[1])}}]
                else:
                    delta["content"] = "Deterministic InfraPilot workflow state: " + json.dumps({"completed": list(state["completed"]), "failed": state.get("failed"), "repairs": [row["action"] for row in state["repairs"]]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                base = {"id": f"infra-{owner.calls}", "object": "chat.completion.chunk", "created": int(time.time()), "model": body["model"]}
                for chunk in [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if action else "stop"}]}, {**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}]:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"
    def __enter__(self):
        self.thread.start()
        return self
    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
