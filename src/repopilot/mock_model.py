"""Explicit scripted Chat Completions server for real-Harness integration tests."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ScriptedModel:
    def __init__(self, actions: list[tuple[str, dict]], mode: str = "normal", *, responder=None):
        self.actions, self.mode = actions, mode
        self.requests: list[dict] = []
        self.position = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                tools = body.get("tools", [])
                last_content = str(body.get("messages", [{}])[-1].get("content", ""))
                is_summary = not tools or last_content.startswith("You are now acting as a compaction engine")
                if owner.mode == "rate_limit" and len(owner.requests) == 1:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"scripted rate limit","type":"rate_limit"}}')
                    return
                if responder is not None:
                    action = responder(body)
                elif owner.mode == "loop":
                    action = ("test", {"suite": "target"})
                elif is_summary:
                    action = None
                elif owner.position < len(owner.actions):
                    action = owner.actions[owner.position]
                    owner.position += 1
                else:
                    action = None
                native_names = {"skill", "repo_agent", "infra_agent", "infra_specialist", "repo_specialist", "send_message", "list_agents", "wait_agent", "interrupt_agent",
                                "session_search", "session_event_search", "session_trace", "session_event_trace", "session_event_read"}
                response_text = action if isinstance(action, str) else None
                if response_text is not None:
                    action = None
                tool_name = (action[0] if action[0] in native_names or action[0].startswith(("mcp__", "pilot_"))
                             else f"mcp__repopilot__{action[0]}") if action else None
                if action and tool_name not in [t["function"]["name"] for t in tools]:
                    owner.requests[-1]["missing_tool"] = tool_name
                if body.get("stream") is False:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "choices": [{"message": {"role": "assistant", "content": response_text or "OK"}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 1},
                    }).encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                base = {"id": f"scripted-{len(owner.requests)}", "object": "chat.completion.chunk", "created": int(time.time()), "model": body["model"]}
                delta = {"role": "assistant"}
                if action:
                    arguments = "{broken" if owner.mode == "invalid_json" else json.dumps(action[1])
                    delta["tool_calls"] = [{"index": 0, "id": f"call-{len(owner.requests)}", "type": "function", "function": {"name": tool_name, "arguments": arguments}}]
                else:
                    delta["content"] = response_text if response_text is not None else "Scripted integration fixture completed. Retain Issue, source diff, failing assertions and excluded hypotheses. This output is not an autonomous model result."
                input_tokens = 2500 if owner.mode == "pressure" and not is_summary else 100
                chunks = [
                    {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if action else "stop"}]},
                    {**base, "choices": [], "usage": {"prompt_tokens": input_tokens, "completion_tokens": 20, "total_tokens": input_tokens + 20}},
                ]
                for chunk in chunks:
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

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
