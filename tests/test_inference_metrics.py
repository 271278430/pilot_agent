import json
import time
from types import SimpleNamespace

import httpx
import pytest

from pilot_infra.inference_http import request
from pilot_infra.metrics import summarize, target_assessment


def test_failed_requests_remain_in_rate_denominator_and_missing_metrics_stay_missing():
    rows = [
        {"ok": True, "output_tokens": 8, "ttft_ms": 10, "latency_ms": 100, "tpot_estimate_ms": 8},
        {"ok": False, "output_tokens": 99, "error": "timeout", "ttft_ms": 999},
        {"ok": True, "output_tokens": 12, "ttft_ms": 30, "latency_ms": 200},
        {"ok": False, "error": "incomplete stream"},
    ]
    result = summarize(rows, 2)
    assert result["attempted"] == 4 and result["successful"] == 2
    assert result["failure_rate"] == .5 and result["requests_per_second"] == 1
    assert result["output_tokens_per_second"] == 10
    assert result["ttft_p50_ms"] == 20 and result["ttft_p95_ms"] == 30
    assert result["latency_p95_ms"] == 200
    assert result["metric_samples"] == {"ttft": 2, "latency": 2, "tpot_estimate": 1}
    assert summarize([], 0)["failure_rate"] is None
    assert summarize([{"ok": True, "ttft_ms": float('nan')}], 1)["ttft_p95_ms"] is None


def test_no_slo_never_claims_performance_target_attainment():
    cases = [{"ttft_p95_ms": 10}, {"ttft_p95_ms": 50}]
    assert target_assessment(cases, True, None)["status"] == "inconclusive"
    assert target_assessment(cases, True, 50)["status"] == "passed"
    assert target_assessment(cases, True, 49)["status"] == "failed"
    assert target_assessment(cases, False, 100)["status"] == "failed"
    assert target_assessment([], True, 100)["status"] == "failed"
    assert target_assessment([{"ttft_p95_ms": None}], True, 100)["status"] == "failed"


@pytest.mark.parametrize("native", [False, True])
def test_tpot_estimate_excludes_slow_usage_and_done_trailers(monkeypatch, native):
    if native:
        events = [{"text": "A"}, {"text": "AB", "meta_info": {"completion_tokens": 4, "prompt_tokens": 8}}]
    else:
        events = [{"choices": [{"delta": {"content": "A"}}]}, {"choices": [{"delta": {"content": "B"}}]},
                  {"choices": [], "usage": {"completion_tokens": 4, "prompt_tokens": 8}}]
    data = ''.join('data: ' + json.dumps(item) + '\n\n' for item in events) + 'data: [DONE]\n\n'
    original = httpx.AsyncClient
    mocked = httpx.MockTransport(lambda req: httpx.Response(200, text=data))
    monkeypatch.setattr('pilot_infra.inference_http.httpx.AsyncClient',
                        lambda **kwargs: original(transport=mocked, **kwargs))
    stamps = iter([0, .01, .04, .2])
    monkeypatch.setattr('pilot_infra.inference_http.time', SimpleNamespace(
        time=time.time, perf_counter=lambda: next(stamps)))
    result = request('http://fixture.invalid/generate', {}, sglang_native=native)
    assert result['ok'] and result['output'] == 'AB'
    assert result['ttft_ms'] == pytest.approx(10)
    assert result['latency_ms'] == pytest.approx(200)
    assert result['tpot_estimate_ms'] == pytest.approx(10)
    assert result['content_chunks'] == 2


def test_single_batched_content_chunk_cannot_claim_token_latency(monkeypatch):
    data = 'data: {"choices":[{"text":"ABCD"}],"usage":{"completion_tokens":4,"prompt_tokens":8}}\n\ndata: [DONE]\n\n'
    original = httpx.AsyncClient
    mocked = httpx.MockTransport(lambda req: httpx.Response(200, text=data))
    monkeypatch.setattr('pilot_infra.inference_http.httpx.AsyncClient',
                        lambda **kwargs: original(transport=mocked, **kwargs))
    result = request('http://fixture.invalid/v1/completions', {})
    assert result['ok'] and result['content_chunks'] == 1
    assert result['tpot_estimate_ms'] is None
