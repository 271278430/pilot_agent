"""Inherited Git config must remain valid after MCP secret filtering."""
from repopilot.demo import create_repo
from repopilot.environment import Environment, git_environment
from repopilot.repository import git


def test_incomplete_inline_config_does_not_break_owned_repository(tmp_path, monkeypatch):
    task = create_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "inherited-helper")
    assert git(tmp_path / "repo", "rev-parse", "HEAD").stdout.strip() == task.commit
    result = Environment(tmp_path / "repo", "local").execute(["git", "rev-parse", "HEAD"], 20)
    assert result["exit_code"] == 0 and task.commit in result["output"]
    clean = git_environment()
    assert not any(key.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for key in clean)
