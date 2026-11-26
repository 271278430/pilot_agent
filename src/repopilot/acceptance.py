from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from .environment import Environment
from .models import Acceptance, RunConfig
from .repository import allowed_source, changes, clone, git, snapshot
from .storage import atomic_json, read_json


def verify(run: Path) -> Acceptance:
    config = RunConfig.model_validate(read_json(run / "run.json"))
    report = Acceptance(reason="pending", baseline=config.baseline, environment=config.environment)
    agent_repo = Path(config.workspace)
    report.changed_files = changes(config.source_hashes, snapshot(agent_repo))
    if git(agent_repo, "rev-parse", "HEAD").stdout.strip() != config.baseline:
        report.reason = "baseline_commit_changed"
    elif not report.changed_files:
        report.reason = "empty_patch"
    elif any((agent_repo / p).is_symlink() for p in report.changed_files):
        report.reason = "symlink_patch_rejected"
    elif any(not allowed_source(p, config.task.source_roots) for p in report.changed_files):
        report.reason = "protected_files_modified"
    else:
        destination = run / "acceptance-workspaces" / uuid.uuid4().hex
        clone(Path(config.task.repo), destination, config.baseline)
        runner = Environment(destination, config.environment, config.task.image)
        try:
            report.baseline_target = runner.execute(config.task.target, config.budget.command_timeout)
            if report.baseline_target["exit_code"] != 1 or "failed" not in report.baseline_target["output"].lower():
                report.reason = "baseline_failure_not_reproduced_as_test_assertion"
            else:
                patch_path = run / "patch.diff"
                checked = git(destination, "apply", "--check", str(patch_path.resolve()), check=False)
                if checked.returncode:
                    report.reason = "patch_conflict: " + checked.stderr[-2000:]
                else:
                    git(destination, "apply", str(patch_path.resolve()))
                    after_patch = snapshot(destination)
                    if changes(config.source_hashes, after_patch) != report.changed_files:
                        report.reason = "patch_does_not_match_agent_workspace"
                    elif any(after_patch.get(p) != snapshot(agent_repo).get(p) for p in report.changed_files):
                        report.reason = "patch_contents_do_not_match_agent_workspace"
                    else:
                        report.target = runner.execute(config.task.target, config.budget.command_timeout)
                        report.regression = runner.execute(config.task.regression, config.budget.command_timeout)
                        report.passed = report.target["exit_code"] == report.regression["exit_code"] == 0
                        report.reason = "passed" if report.passed else "target_or_regression_failed"
            report.image_id = runner.image_id
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            report.reason = "environment_error: " + str(exc)
    atomic_json(run / "acceptance.json", report.model_dump())
    return report
