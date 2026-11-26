"""Candidate-scoped evidence; build and restoration never attest service quality."""


def build_validation(task):
    candidates = task.get("candidates", [])
    candidate = candidates[-1] if candidates else None
    candidate_id = candidate["candidate_id"] if candidate else None
    runs = [r for r in task.get("results", []) if r.get("candidate_id") == candidate_id]
    run = runs[-1] if runs else None
    state, reason = "not_run", "no_candidate_run"
    bound = None
    if task.get("active_operation") or task.get("status") == "unknown":
        state, reason = "unsettled", "original_operation_needs_review"
    elif run:
        bound = (
            bool(candidate.get("files"))
            and run.get("source_manifest") == candidate["files"]
            and run.get("frozen_source_unchanged") is True
            and (
                not task.get("checks_manifest")
                or run.get("checks_manifest") == task["checks_manifest"]
                and run.get("checks_unchanged") is True
            )
        )
        steps = run.get("steps", [])
        if not bound:
            state, reason = "unverified", "candidate_evidence_mismatch"
        elif run.get("status") == "failed" or run.get("passed") is False:
            state, reason = "failed", "registered_pipeline_failed"
        elif (
            run.get("status") == "completed"
            and run.get("passed") is True
            and task.get("pipeline_stages")
            and [s.get("stage") for s in steps] == task["pipeline_stages"]
            and all(s.get("exit_code") == 0 for s in steps)
        ):
            state, reason = "passed", "registered_pipeline_passed"
        else:
            state, reason = "unverified", "incomplete_pipeline_receipt"
    business, numerical, performance = "not_validated", "not_validated", "not_validated"
    evidence = run.get("business_evidence", {}) if run else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    if (
        state == "passed"
        and task.get("kind") == "operator"
        and run.get("gpu_execution") is True
        and task.get("checks_manifest")
    ):
        import math

        cases = evidence.get("cases", [])
        if (
            evidence.get("protocol") == "pilot-rmsnorm-cuda-v1"
            and isinstance(cases, list)
            and len(cases) == 8
            and all(
                isinstance(c, dict)
                and isinstance(c.get("shape"), list)
                and len(c["shape"]) == 2
                and all(type(n) is int for n in c["shape"])
                and isinstance(c.get("dtype"), str)
                for c in cases
            )
            and {(tuple(c.get("shape", [])), c.get("dtype")) for c in cases if isinstance(c, dict)}
            == {
                (shape, dtype)
                for shape in [(1, 32), (17, 137), (256, 1024), (2048, 4096)]
                for dtype in ["torch.float16", "torch.float32"]
            }
            and all(
                isinstance(c, dict)
                and type(c.get("max_abs_error")) in {int, float}
                and math.isfinite(c["max_abs_error"])
                and isinstance(c.get("tolerance"), (int, float))
                and math.isfinite(c["tolerance"])
                and 0 <= c["max_abs_error"] <= c["tolerance"]
                and c["tolerance"] == (0.003 if c.get("dtype") == "torch.float16" else 0.00003)
                for c in cases
            )
        ):
            numerical = "passed"
            speedup = evidence.get("speedup")
            times = [evidence.get("baseline_ms"), evidence.get("candidate_ms")]
            if (
                type(speedup) in {int, float}
                and math.isfinite(speedup)
                and speedup > 0
                and evidence.get("timing_method") == "cuda_events_median_20"
                and all(type(t) in {int, float} and math.isfinite(t) and t > 0 for t in times)
                and math.isclose(speedup, times[0] / times[1], rel_tol=0.01)
            ):
                performance = "improved" if speedup >= 1.05 else "measured_no_improvement"
                business = "passed" if performance == "improved" else "partial"
    elif state == "passed" and task.get("checks_manifest") and evidence.get("passed") is True:
        if (
            task.get("kind") == "script"
            and evidence.get("protocol") == "pilot-http-metrics-contract-v1"
        ):
            business = "passed_script_contract"
        elif task.get("kind") == "profile" and evidence.get("protocol") == "pilot-cpu-profile-v1":
            business = "passed_cpu_profile"
        elif (
            task.get("kind") == "pd"
            and evidence.get("protocol") == "pilot-pd-shared-storage-v1"
            and run.get("gpu_execution") is True
            and evidence.get("tokens_match") is True
            and evidence.get("external_cache_hit") is True
            and evidence.get("kv_files", 0) > 0
        ):
            business = "passed_same_node_pd_debug"
    return {
        "candidate_id": candidate_id,
        "operation_id": run.get("operation_id") if run else None,
        "pipeline": state,
        "reason": reason,
        "candidate_evidence_bound": bound,
        "expected_stages": task.get("pipeline_stages", []),
        "working_copy_matches_candidate": (
            task.get("files") == candidate["files"] if candidate and task.get("files") else None
        ),
        "business_goal": business,
        "inference_service": "not_validated",
        "gpu_numerical": numerical,
        "operator_performance": performance,
        "restoration": (
            "passed"
            if task.get("status") == "restored" and task.get("restored") is True
            else "pending"
        ),
    }


def with_validation(task):
    return {**task, "validation": build_validation(task)}
