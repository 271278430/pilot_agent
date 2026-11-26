from pathlib import Path
from tempfile import mkdtemp

import pytest

from repopilot.demo import create_repo
from repopilot.harness import prepare


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Each invocation owns a short project-local temp root; parallel runs cannot delete each other."""
    project = Path(__file__).resolve().parents[1]
    local = (project / ".local").resolve()
    if local.parent != project:
        raise pytest.UsageError("Test scratch directory must stay inside this project")
    local.mkdir(exist_ok=True)
    scratch = local / "tmp/tests"
    scratch.mkdir(parents=True, exist_ok=True)
    target = (
        Path(config.option.basetemp).resolve()
        if config.option.basetemp
        else Path(mkdtemp(prefix="t-", dir=scratch))
    )
    if not target.is_relative_to(local) or target == local:
        raise pytest.UsageError(
            "Explicit --basetemp must be a dedicated child directory under project .local"
        )
    config.option.basetemp = str(target)


@pytest.fixture
def run_dir(tmp_path: Path):
    task = create_repo(tmp_path / "fixture")
    return prepare(
        task, tmp_path / "runs", "test-model", "http://127.0.0.1:1/v1", "local", policy="scripted"
    )
