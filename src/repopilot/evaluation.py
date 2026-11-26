from __future__ import annotations

import csv
from pathlib import Path

from .demo import DEMO_ACTIONS
from .harness import execute, prepare
from .mock_model import ScriptedModel
from .models import Budget, Task
from .storage import atomic_json


def evaluate_tasks(tasks: list[Task], output: Path, model: str, url: str, key: str, budget: Budget, scripted: bool = False, environment: str = "docker") -> dict:
    if not tasks:
        raise ValueError("Evaluation needs at least one task")
    if output.exists():
        raise ValueError("Evaluation destination exists; select a new output folder")
    if not scripted and not all((model, url, key)):
        raise ValueError("Configure model, endpoint and credentials for real model evaluation")
    if scripted and any(task.id != "checkout-cross-file" for task in tasks):
        raise ValueError("Scripted policy is only defined for the built-in checkout fixture")
    output.mkdir(parents=True)
    atomic_json(output / "protocol.json", {"tasks": [t.model_dump() for t in tasks], "budget": budget.model_dump(), "variants": ["search", "repomap", "compressed"], "sampling": "provider_defaults", "policy": "scripted" if scripted else "model", "model": "scripted-fixture" if scripted else model, "environment": "local" if scripted else environment, "note": "Scripted results validate infrastructure; they do not measure autonomous repair capability." if scripted else "Single trial per task/variant; sampling variance is not estimated."})
    rows = []
    for task in tasks:
        for variant in ("search", "repomap", "compressed"):
            if scripted:
                with ScriptedModel(DEMO_ACTIONS) as fixture:
                    run = prepare(task, output / "runs", "scripted-fixture", fixture.url, "local", variant, budget, "scripted")
                    report = execute(run, "scripted-evaluation-key")
                    atomic_json(run / "mock-requests.json", fixture.requests)
            else:
                run = prepare(task, output / "runs", model, url, environment, variant, budget)
                report = execute(run, key)
            meter = report.get("meter", {})
            rows.append({"task": task.id, "variant": variant, "passed": report["passed"], "reason": report["acceptance"]["reason"], "status": report["status"], "steps": meter.get("steps", 0), "input_tokens": meter.get("input_tokens", 0), "output_tokens": meter.get("output_tokens", 0), "usage_source": report["usage_source"], "cost": meter.get("cost") if report["cost_priced"] else None, "duration_seconds": report["duration_seconds"], "run": str(run), "baseline": report["baseline"]})
            atomic_json(output / "tasks.json", rows)
    summary = {"framework": "deepseek-harness", "policy": "scripted" if scripted else "model", "task_count": len(tasks), "trial_count": len(rows), "variants": {variant: {"passed": sum(r["passed"] for r in rows if r["variant"] == variant), "total": len(tasks), "mean_seconds": round(sum(r["duration_seconds"] for r in rows if r["variant"] == variant) / len(tasks), 3)} for variant in ("search", "repomap", "compressed")}, "results": rows, "autonomous_quality_measured": not scripted}
    atomic_json(output / "summary.json", summary)
    with (output / "tasks.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return summary
