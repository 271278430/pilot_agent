import os
from pathlib import Path

import pytest

from repopilot.environment import Environment


def test_timeout_kills_process_and_returns_diagnostic(tmp_path):
    result = Environment(tmp_path, "local").execute(
        ["python", "-c", "import time; time.sleep(10)"], 1
    )
    assert result["exit_code"] == 124
    assert result["error"] == "timeout"
    assert result["duration_ms"] < 7000


def test_child_does_not_receive_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOPILOT_API_KEY", "secret-fixture-for-test")
    result = Environment(tmp_path, "local").execute(
        ["python", "-c", "import os; print(os.getenv('REPOPILOT_API_KEY', 'absent'))"], 5
    )
    assert result["output"].strip() == "absent"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH regression")
def test_deep_local_workspace_can_discover_pytest_files(tmp_path):
    # Reproduce the observed case: valid subprocess cwd, but its test file
    # crosses MAX_PATH. Windows itself still limits process cwd length.
    repo = tmp_path / ("x" * (240 - len(str(tmp_path)) - 1))
    assert len(str(repo / "tests/test_sample.py")) > 260
    # Create the fixture through the extended namespace too; the regression
    # concerns child test discovery, not the host's own MAX_PATH mkdir limit.
    tests = Path("\\\\?\\" + str(repo)) / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text(
        "def test_sample():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    result = Environment(repo, "local").execute(
        ["python", "-m", "pytest", "-q", "tests/test_sample.py"], 30
    )
    assert result["exit_code"] == 0, result
    assert "1 passed" in result["output"]
