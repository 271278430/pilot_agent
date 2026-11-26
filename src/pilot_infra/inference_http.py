"""Cancellable inference streams with a wall-clock bound, independent of the Agent loop."""

import asyncio
import json
import threading
import time

import httpx


class RequestGuard:
    def __init__(self, cancelled=lambda: False, deadline_epoch=None):
        self.callback, self.deadline = cancelled, deadline_epoch
        self.stopped, self.closed = threading.Event(), threading.Event()
        self.lock = threading.Lock()

    def interrupted(self):
        if self.deadline is not None and time.time() >= self.deadline:
            self.stopped.set()
        return self.stopped.is_set()

    def cancel(self):
        self.stopped.set()

    def check(self):
        with self.lock:
            if self.interrupted() or self.callback():
                self.stopped.set()
                raise InterruptedError("Inference operation cancelled or deadline reached")

    def monitor(self):
        while not self.closed.wait(0.2):
            try:
                self.check()
            except Exception:
                self.stopped.set()
                return

    def __enter__(self):
        self.check()
        self.thread = threading.Thread(target=self.monitor, daemon=True, name="pilot-request-guard")
        self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.stopped.set()
        self.closed.set()
        self.thread.join(timeout=0.5)


async def _stream(url, payload, sglang_native, timeout, guard):
    started = time.perf_counter()
    first, last_content, content_chunks = None, None, 0
    final, events, ended, output = None, [], False, ""

    async def consume():
        nonlocal first, last_content, content_chunks, final, ended, output
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream("POST", url, json={**payload, "stream": True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:") and line[5:].strip() == "[DONE]":
                        ended = True
                        continue
                    if not line.startswith("data:"):
                        continue
                    item = json.loads(line[5:].strip())
                    events.append(item)
                    if sglang_native:
                        text = item.get("text", "")
                        changed = bool(text and text != output)
                        output, final = text, item.get("meta_info", {})
                    else:
                        choice = (item.get("choices") or [{}])[0]
                        delta = choice.get("text") or choice.get("delta", {}).get("content") or ""
                        changed = bool(delta)
                        output += delta
                        if item.get("usage"):
                            final = item["usage"]
                    if changed:
                        stamp = time.perf_counter() - started
                        if first is None:
                            first = stamp
                        last_content = stamp
                        content_chunks += 1

    async def interrupted():
        while not guard.interrupted():
            await asyncio.sleep(0.05)

    consumer = asyncio.create_task(consume())
    tasks = {consumer, asyncio.create_task(interrupted())}
    remaining = timeout
    task_bound = False
    if guard.deadline is not None:
        task_remaining = max(0, guard.deadline - time.time())
        task_bound = task_remaining <= timeout
        remaining = min(remaining, task_remaining)
    try:
        done, _ = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if guard.interrupted():
            raise InterruptedError("Inference operation cancelled or total deadline reached")
        if consumer not in done:
            if task_bound:
                raise InterruptedError("Inference task total deadline reached")
            return {"ok": False, "error": "Request total timeout reached", "events": events,
                    "latency_ms": (time.perf_counter() - started) * 1000}
        consumer.result()
        tokens = (final or {}).get("completion_tokens")
        complete = ended and bool(output) and first is not None and type(tokens) is int and tokens > 0
        details = (final or {}).get("prompt_tokens_details") or {}
        return {
            "ok": complete, "output": output,
            "ttft_ms": first * 1000 if first is not None else None,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "input_tokens": (final or {}).get("prompt_tokens"), "output_tokens": tokens,
            "cached_input_tokens": (final or {}).get("cached_tokens", details.get("cached_tokens")),
            "tpot_estimate_ms": (last_content - first) * 1000 / (tokens - 1)
            if complete and tokens > 1 and content_chunks > 1 else None,
            "content_chunks": content_chunks,
            "timing_scope": "client_stream_chunks; usage and DONE trailers excluded from TPOT estimate",
            "events": events,
            "error": None if complete else "Incomplete stream, empty output or missing actual token usage",
        }
    except InterruptedError:
        raise
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:1000], "events": events,
                "latency_ms": (time.perf_counter() - started) * 1000}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def request(url, payload, *, sglang_native=False, timeout=90, guard=None):
    if guard is None:
        with RequestGuard() as own_guard:
            return request(url, payload, sglang_native=sglang_native, timeout=timeout, guard=own_guard)
    guard.check()
    return asyncio.run(_stream(url, payload, sglang_native, timeout, guard))
