from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from .acceptance import verify
from .demo import run_demo
from .harness import PROJECT, dsh_executable, execute, prepare
from .models import Budget, Task
from .retrieval import RepoMap
from .storage import read_json
from .tools import recover

app = typer.Typer(no_args_is_help=True, help="RepoPilot: DeepSeek Harness based Python issue repair")


def emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def settings() -> tuple[str, str, str, Budget]:
    load_dotenv(PROJECT / ".env")
    budget = Budget(context_tokens=int(os.getenv("REPOPILOT_CONTEXT_TOKENS", "12000")), max_tokens=int(os.getenv("REPOPILOT_MAX_TOKENS", "60000")), command_timeout=int(os.getenv("REPOPILOT_TIMEOUT", "60")), max_cost=float(os.getenv("REPOPILOT_MAX_COST", "5")), input_price=float(os.getenv("REPOPILOT_INPUT_PRICE", "0")), output_price=float(os.getenv("REPOPILOT_OUTPUT_PRICE", "0")))
    return os.getenv("REPOPILOT_MODEL", ""), os.getenv("REPOPILOT_BASE_URL", ""), os.getenv("REPOPILOT_API_KEY", ""), budget


@app.command()
def doctor(probe: bool = False):
    """Check tools and model configuration without making a paid model call."""
    import importlib.metadata
    import platform

    model, url, key, _ = settings()
    try:
        executable = dsh_executable()
    except ValueError as exc:
        executable = str(exc)
    value = {"python": platform.python_version(), "git": shutil.which("git"), "docker": shutil.which("docker"), "node": shutil.which("node"), "harness_sdk": importlib.metadata.version("deepseek-harness-sdk"), "harness_cli": executable or "bundled runtime", "model": model or None, "base_url": url or None, "key_configured": bool(key), "project": str(PROJECT)}
    if probe and url and key:
        import httpx

        try:
            response = httpx.get(url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}, timeout=10)
            value["models_endpoint_status"] = response.status_code
        except httpx.HTTPError as exc:
            value["models_endpoint_error"] = type(exc).__name__
    emit(value)


@app.command()
def demo(output: Path = PROJECT / "runs", variant: str = "compressed"):
    """Run real Harness against an explicitly scripted local model fixture."""
    report = run_demo(output, variant)
    emit(report)
    if not report["passed"]:
        raise typer.Exit(1)


@app.command()
def index(repo: Path, query: str = "", output: Path = PROJECT / ".cache/index.json"):
    """Build/incrementally update a Python RepoMap, then rank issue symbols."""
    emit(RepoMap(repo.resolve(), output).search(query))


@app.command()
def run(task: Annotated[Path, typer.Option("--task")], output: Path = PROJECT / "runs", environment: str = "docker", allow_local: bool = False, variant: str = "compressed", max_steps: int = 40):
    """Repair a fixed-commit repository task and independently verify its patch."""
    if environment == "local" and not allow_local:
        raise typer.BadParameter("Local repository code runs with host permissions; explicitly pass --allow-local for trusted code")
    model, url, key, budget = settings()
    if not model or not url or not key:
        raise typer.BadParameter("Configure REPOPILOT_MODEL, REPOPILOT_BASE_URL and REPOPILOT_API_KEY in local .env")
    budget = budget.model_copy(update={"max_steps": max_steps})
    spec = Task.model_validate(read_json(task))
    location = prepare(spec, output, model, url, environment, variant, budget)
    typer.echo(f"Run directory: {location}")
    report = execute(location, key)
    emit(report)
    if not report["passed"]:
        raise typer.Exit(1)


@app.command()
def resume(run_dir: Annotated[Path, typer.Option("--run-dir")], acknowledge_unknown: bool = False, inspect_only: bool = False):
    """Reconcile interrupted actions, restore the native Harness session, continue."""
    if inspect_only:
        emit(recover(run_dir.resolve(), acknowledge_unknown))
        return
    _, _, key, _ = settings()
    report = execute(run_dir, key, resume=True, acknowledge_unknown=acknowledge_unknown)
    emit(report)
    if not report["passed"]:
        raise typer.Exit(1)


@app.command()
def accept(run_dir: Annotated[Path, typer.Option("--run-dir")]):
    """Validate an existing patch on a fresh independent baseline."""
    result = verify(run_dir.resolve())
    emit(result.model_dump())
    if not result.passed:
        raise typer.Exit(1)


@app.command()
def fixture(output: Path = PROJECT / "tasks/demo-evaluation.json"):
    """Create a fresh fixed-commit demo repository and evaluation manifest."""
    import uuid

    from .demo import create_repo
    from .storage import atomic_json

    task = create_repo(PROJECT / ".local" / ("evaluation-fixture-" + uuid.uuid4().hex[:8]))
    atomic_json(output, [task.model_dump()])
    emit({"manifest": str(output.resolve()), "repo": task.repo, "commit": task.commit})


@app.command()
def evaluate(manifest: Annotated[Path, typer.Option("--manifest")], output: Path = PROJECT / "reports/evaluation", scripted: bool = False, environment: str = "docker", allow_local: bool = False):
    """Run fixed tasks across search, repomap and compressed variants."""
    from .evaluation import evaluate_tasks

    if environment == "local" and not allow_local and not scripted:
        raise typer.BadParameter("Pass --allow-local for trusted repository execution")
    model, url, key, budget = settings()
    tasks = [Task.model_validate(t) for t in json.loads(manifest.read_text(encoding="utf-8"))]
    emit(evaluate_tasks(tasks, output, model, url, key, budget, scripted, environment))


@app.command()
def baseline(task: Annotated[Path, typer.Option("--task")], output: Path = PROJECT / "runs/mini-baseline"):
    """Run optional mini-SWE external baseline in Docker and apply the same acceptance."""
    from .baseline import run_baseline

    model, url, key, budget = settings()
    if not all((model, url, key)):
        raise typer.BadParameter("Configure local model endpoint and credentials")
    emit(run_baseline(Task.model_validate(read_json(task)), output, model, url, key, budget))


if __name__ == "__main__":
    app()
