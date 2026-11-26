import importlib.util
from pathlib import Path

import pytest

from repopilot.storage import atomic_json

script = Path(__file__).resolve().parents[1] / "scripts/benchmark_infra.py"
spec = importlib.util.spec_from_file_location("infra_benchmark", script)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_boundary_safe_stop_remains_a_failed_deployment(tmp_path):
    common = {"expected_diagnosis": None, "duration_seconds": 10, "recovery_seconds": None}
    rows = [
        {**common, "task_id": "normal-r1", "scenario": "normal", "passed": True, "recovery_supported": True, "benchmark": {"samples": [], "requests": 0, "wall_seconds": 0}, "training": {"peak_allocated_mib": 512, "resume_max_parameter_delta": 0}},
        {**common, "task_id": "ssh_drop-r1", "scenario": "ssh_drop", "passed": False, "recovery_supported": True},
        {**common, "task_id": "cuda_oom-r1", "scenario": "cuda_oom", "passed": False, "recovery_supported": False, "expected_diagnosis": "cuda_oom", "diagnosis_correct": True, "safe_stop": True},
    ]
    for row in rows:
        atomic_json(tmp_path / "tasks" / row["task_id"] / "result.json", row)
    result = benchmark.summarize(tmp_path)
    assert result["overall_completion"]["rate"] == pytest.approx(1/3)
    assert result["supported_completion"]["rate"] == .5
    assert result["all_injected_fault_recovery"]["rate"] == 0
    assert result["boundary_safe_stop"]["rate"] == 1
    assert result["diagnosis_accuracy"]["total"] == 1


def test_small_sample_interval_and_percentile_convention():
    assert benchmark.rate(0, 0)["rate"] is None
    assert benchmark.rate(18, 24)["wilson_95"] == pytest.approx([.5510055599484609, .8800063377140499])
    assert benchmark.quantile([4, 1, 3, 2], .95) == pytest.approx(3.85)
