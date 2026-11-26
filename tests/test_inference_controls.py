import asyncio
import copy
import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from pilot_infra.benchmark import input_tokens
from pilot_infra.inference_http import RequestGuard, request
from tests.test_pilot_infra import SGLangEngine, create, plan, setup, start


def transport(monkeypatch):
    state = {"closed": False}

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            state["closed"] = True

    original = httpx.AsyncClient
    mocked = httpx.MockTransport(lambda req: httpx.Response(200, stream=StalledStream()))
    monkeypatch.setattr("pilot_infra.inference_http.httpx.AsyncClient",
                        lambda **kwargs: original(transport=mocked, **kwargs))
    return state


def test_active_stream_closes_on_cancellation(monkeypatch):
    state = transport(monkeypatch)
    stopped = threading.Event()
    timer = threading.Timer(0.05, stopped.set)
    timer.start()
    begin = time.monotonic()
    try:
        with RequestGuard(stopped.is_set) as guard:
            with pytest.raises(InterruptedError):
                request("http://fixture.invalid", {}, timeout=5, guard=guard)
    finally:
        timer.join()
    assert state["closed"] and time.monotonic() - begin < 1.5


def test_task_deadline_closes_stream_even_without_a_read_timeout(monkeypatch):
    state = transport(monkeypatch)
    with RequestGuard(deadline_epoch=time.time() + 0.1) as guard:
        with pytest.raises(InterruptedError):
            request("http://fixture.invalid", {}, timeout=5, guard=guard)
    assert state["closed"]


def test_request_wall_timeout_is_a_failed_sample_and_closes_stream(monkeypatch):
    state = transport(monkeypatch)
    result = request("http://fixture.invalid", {}, timeout=0.1)
    assert result["ok"] is False and "total timeout" in result["error"]
    assert state["closed"]


def test_warmup_cancellation_sends_no_further_samples(tmp_path, monkeypatch):
    service = setup(tmp_path)
    task = start(service, create(service))
    candidate = service.candidate(task, {"request_id": "cancel-warmup",
                                        "serving": task["plan"]["serving"], "reason": "Test bounded cancellation"})
    engine = SGLangEngine(service.assets, task, candidate)
    monkeypatch.setattr(engine, "verify_identity", lambda: {"passed": True})
    state = {"cancelled": False, "native_calls": 0}

    def stub(payload, native=False, **kwargs):
        if native:
            state["native_calls"] += 1
            state["cancelled"] = True
        return {"ok": True, "output": "A"}

    monkeypatch.setattr(engine, "request", stub)
    with pytest.raises(InterruptedError):
        engine.test({}, cancelled=lambda: state["cancelled"], plan=plan(workload={
            "input_lengths": [128], "concurrency": [1], "requests_per_case": 32,
        }))
    assert state["native_calls"] == 1


def test_comparison_requires_distinct_candidates_at_schema_and_finish(tmp_path, monkeypatch):
    with pytest.raises(ValidationError, match="distinct"):
        plan(kind="compare", target_task_id="a" * 32, candidate_ids=["c1", "c1"])
    service = setup(tmp_path)
    task = start(service, create(service))
    damaged = copy.deepcopy(task["plan"])
    damaged.update(kind="compare", candidate_ids=["c1", "c1"])
    with service.store.db() as db:
        db.execute("UPDATE tasks SET plan=? WHERE id=?", (json.dumps(damaged), task["id"]))
    monkeypatch.setattr(service, "verified_acceptance", lambda t: (t, "c1", {}, {}, None))
    with pytest.raises(ValueError, match="distinct"):
        service.finish(task)


def test_cold_payloads_are_unique_reproducible_and_warm_prefix_is_explicit():
    workload = {"cache_mode": "cold", "seed": 17}
    values = [input_tokens(128, workload, f"sample-{n}") for n in range(32)]
    assert len({tuple(v) for v in values}) == 32
    assert values == [input_tokens(128, workload, f"sample-{n}") for n in range(32)]
    assert all(16 <= token < 1024 for value in values for token in value)
    warm = {**workload, "cache_mode": "warm_prefix"}
    assert input_tokens(128, warm, "warmup") == input_tokens(128, warm, "sample")
    assert input_tokens(128, {}, "legacy") == [1024] * 128
