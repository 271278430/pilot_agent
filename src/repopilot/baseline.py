"""Optional upstream mini-SWE baseline; never used as the primary framework."""

from __future__ import annotations

import shutil
from pathlib import Path

from .acceptance import verify
from .harness import prepare
from .models import Budget, Task
from .storage import Store, atomic_json, redact
from .tools import ToolService


def run_baseline(task: Task, output: Path, model_name: str, base_url: str, api_key: str, budget: Budget) -> dict:
    if not shutil.which("docker"):
        raise ValueError("mini-SWE baseline requires Docker")
    try:
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.config import get_config_from_spec
        from minisweagent.exceptions import Submitted
        from minisweagent.models import get_model
    except ImportError as exc:
        raise ValueError("Install the optional baseline extra: pip install -e '.[baseline]'") from exc

    run = prepare(task, output, model_name, base_url, "docker", "search", budget)
    service = ToolService(run)
    config = get_config_from_spec("mini.yaml")
    config["agent"].update(step_limit=budget.max_steps, cost_limit=budget.max_cost, wall_time_limit_seconds=budget.wall_timeout, output_path=None)
    config["model"].update(model_name="openai/" + model_name, cost_tracking="ignore_errors")
    config["model"].setdefault("model_kwargs", {}).update(api_base=base_url, api_key=api_key, max_tokens=budget.output_tokens, temperature=0)

    class AuditedDocker:
        def execute(self, action: dict) -> dict:
            command = action.get("command", "")
            if command.strip() == "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT":
                raise Submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": ""}})
            result = service.call("shell", {"argv": ["bash", "-lc", command]})
            return {"returncode": result["exit_code"], "output": result["output"], "exception_info": result["error"] or ""}

        def get_template_vars(self):
            return {"system": "Linux", "release": "Docker", "version": "fixed-image", "machine": "container"}

        def serialize(self):
            return {"info": {"environment": "RepoPilot audited Docker", "image": task.image}}

    agent = DefaultAgent(get_model(config=config["model"]), AuditedDocker(), **config["agent"])
    result = agent.run(task.issue + "\nTests/config are protected. Target: " + str(task.target) + "; regression: " + str(task.regression))
    # Upstream model serialization includes model_kwargs, so remove the secret
    # explicitly as well as the common trace redaction before writing it.
    data = agent.serialize()
    data["info"]["config"]["model"]["model_kwargs"]["api_key"] = "[REDACTED]"
    atomic_json(run / "mini-trajectory.json", redact(data))
    report = {"framework": "mini-swe-agent", "baseline_version": "2.4.6", "run": str(run), "result": result, "acceptance": verify(run).model_dump(), "token_budget_equivalence": False, "note": "Upstream mini-SWE enforces step/cost/wall budgets but not RepoPilot total-token reservation; do not claim an identical-token-budget comparison."}
    atomic_json(run / "mini-report.json", redact(report))
    Store(run).event("mini_baseline_finished", passed=report["acceptance"]["passed"])
    return report
