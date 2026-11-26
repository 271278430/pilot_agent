"""Ensure the evidence report cannot hide failures or overstate certainty."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "delivery_analysis", Path(__file__).resolve().parents[1] / "scripts/analyze_delivery.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def test_failure_cost_and_intervention_accounting(tmp_path):
    rows = []
    for repeat in range(2):
        for profile in ["code", "environment"]:
            for mode in ["single", "multi"]:
                failed = repeat == 1 and profile == "environment" and mode == "single"
                rows.append(
                    {
                        "repeat": repeat,
                        "profile": profile,
                        "orchestration": mode,
                        "status": "failed" if failed else "completed",
                        "seconds": 100 if failed else 10 if mode == "single" else 20,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "calls": 1},
                        "state": {"human_interventions": 0, "rounds": 1, "candidate_count": 1},
                    }
                )
    (tmp_path / "protocol.json").write_text(
        json.dumps({"profiles": ["code", "environment"], "repeats": 2})
    )
    (tmp_path / "records.json").write_text(json.dumps(rows))
    result = analysis.analyze(tmp_path)
    assert result["comparison"]["success_gain_pp"] == 25
    assert result["comparison"]["median_time_change_pct"] == 100
    assert result["comparison"]["multi_only_success"] == 1
    assert result["groups"]["single"]["unresolved_tasks"] == 1
    assert result["groups"]["single"]["human_interventions"] == 0
    assert result["groups"]["single"]["controller_tokens"] == 60  # Failed attempt counts.
    (tmp_path / "records.json").write_text(json.dumps(rows[:-1]))
    with pytest.raises(ValueError, match="incomplete"):
        analysis.analyze(tmp_path)


def test_perfect_small_sample_is_not_certain():
    low, high = analysis.wilson(24, 24)
    assert 0.86 < low < 0.87 and high == 1
