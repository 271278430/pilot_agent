from click import unstyle
from typer.testing import CliRunner

from repopilot.cli import app


def test_documented_task_option_and_missing_credentials(monkeypatch):
    monkeypatch.delenv("REPOPILOT_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["run", "--task", "missing.json"])
    assert result.exit_code != 0
    assert "Configure REPOPILOT_MODEL" in result.output


def test_local_execution_needs_explicit_selection():
    result = CliRunner().invoke(app, ["run", "--task", "missing.json", "--environment", "local"])
    assert result.exit_code != 0
    assert "--allow-local" in unstyle(result.output)
