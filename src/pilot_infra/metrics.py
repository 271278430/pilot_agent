"""Client-observed inference measurements; target attainment stays separate."""

import math
import statistics


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, math.ceil((len(values) - 1) * fraction))]


def samples(rows, key):
    return [
        row[key] for row in rows
        if type(row.get(key)) in {int, float} and math.isfinite(row[key]) and row[key] >= 0
    ]


def summarize(rows, elapsed_seconds):
    successful = [row for row in rows if row.get("ok") is True]
    attempted, accepted = len(rows), len(successful)
    ttft = samples(successful, "ttft_ms")
    latency = samples(successful, "latency_ms")
    tpot = samples(successful, "tpot_estimate_ms")
    elapsed_valid = type(elapsed_seconds) in {int, float} and math.isfinite(elapsed_seconds) and elapsed_seconds > 0
    return {
        "attempted": attempted,
        "successful": accepted,
        "failed": attempted - accepted,
        "failure_rate": (attempted - accepted) / attempted if attempted else None,
        "elapsed_seconds": elapsed_seconds,
        "requests_per_second": accepted / elapsed_seconds if elapsed_valid else None,
        "output_tokens_per_second": sum(
            row["output_tokens"] for row in successful
            if type(row.get("output_tokens")) is int and row["output_tokens"] > 0
        ) / elapsed_seconds if elapsed_valid else None,
        "ttft_p50_ms": statistics.median(ttft) if ttft else None,
        "ttft_p95_ms": percentile(ttft, .95),
        "ttft_p99_ms": percentile(ttft, .99),
        "latency_p50_ms": statistics.median(latency) if latency else None,
        "latency_p95_ms": percentile(latency, .95),
        "latency_p99_ms": percentile(latency, .99),
        "tpot_estimate_p50_ms": statistics.median(tpot) if tpot else None,
        "tpot_estimate_p95_ms": percentile(tpot, .95),
        "metric_samples": {"ttft": len(ttft), "latency": len(latency), "tpot_estimate": len(tpot)},
        "timing_scope": "client_stream; TPOT estimates use content-chunk timestamps, not per-token ITL",
    }


def target_assessment(cases, complete, ttft_slo_ms):
    if ttft_slo_ms is None:
        return {"status": "inconclusive", "configured": False, "ttft_slo_ms": None,
                "reason": "未设置性能目标；仅报告测试完整性与测量值。"}
    values = [case.get("ttft_p95_ms") for case in cases]
    measured = bool(values) and all(type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in values)
    status = "passed" if complete and measured and all(v <= ttft_slo_ms for v in values) else "failed"
    return {"status": status, "configured": True, "ttft_slo_ms": ttft_slo_ms,
            "reason": "按全部负载组的TTFT P95判断；失败请求或不完整测量不能通过。"}
