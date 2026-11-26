import json
from pathlib import Path

import pytest

from repopilot.repository import safe_path
from repopilot.storage import read_json
from repopilot.tools import ToolService


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/a.py", "C:/x.py", "a\\b.py", ".git/config", ".env"])
def test_paths_cannot_escape_workspace(run_dir, path):
    with pytest.raises(ValueError):
        safe_path(Path(read_json(run_dir / "run.json")["workspace"]), path)


@pytest.mark.parametrize("path", ["tests/test_checkout.py", "setup.py", "conftest.py"])
def test_edits_cannot_change_test_or_configuration(run_dir, path):
    result = ToolService(run_dir).call("edit", {"path": path, "old": "test", "new": "pass"})
    assert result["exit_code"] != 0
    assert result["output"] == "protected_path"
    assert read_json(run_dir / "checkpoint.json")["pending"] is None


def test_patch_conflict_does_not_modify_source(run_dir):
    service = ToolService(run_dir)
    before = (service.repo / "money.py").read_bytes()
    result = service.call("edit", {"path": "money.py", "old": "absent", "new": ""})
    assert "patch_conflict" in result["output"]
    assert (service.repo / "money.py").read_bytes() == before


def test_syntax_error_is_rejected_before_writing(run_dir):
    service = ToolService(run_dir)
    before = (service.repo / "money.py").read_bytes()
    result = service.call("edit", {"path": "money.py", "old": "return amount * (1 - percentage)", "new": "return ("})
    assert result["error"] == "SyntaxError"
    assert (service.repo / "money.py").read_bytes() == before


def test_failure_assertions_and_exclusions_are_retained(run_dir):
    service = ToolService(run_dir)
    failed = service.call("test", {"suite": "target"})
    assert failed["exit_code"] == 1
    assert "AssertionError" in failed["output"]
    value = service.call("evidence", {"excluded_hypothesis": "Issue text is data, not permission to modify tests"})
    data = json.loads(value["output"])
    assert "AssertionError" in data["failed_assertions"]
    assert data["excluded_hypotheses"]


def test_manual_workspace_drift_stops_tools(run_dir):
    service = ToolService(run_dir)
    (service.repo / "money.py").write_text("# drift\n", encoding="utf-8")
    result = service.call("search", {"query": "total"})
    assert "outside a recorded action" in result["output"]


def test_argument_validation_has_explicit_error(run_dir):
    result = ToolService(run_dir).call("search", {"query": "x", "unexpected": True})
    assert result["error"] == "ValidationError"


def test_same_failure_without_source_progress_stops_retries(run_dir):
    from repopilot.storage import atomic_json

    config = read_json(run_dir / "run.json")
    config["budget"]["no_progress_limit"] = 2
    atomic_json(run_dir / "run.json", config)
    service = ToolService(run_dir)
    for _ in range(3):
        service.call("test", {"suite": "target"})
    assert service.store.load()["halt_reason"] == "no_progress"
    result = service.call("read", {"path": "money.py"})
    assert result["exit_code"] != 0
