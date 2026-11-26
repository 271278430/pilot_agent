import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pilot_host.bridge import HostDomain
from tests.test_goal_applications import plan


def test_shared_request_reservations():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for budget policy")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("budget_reservations.test.mjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_budget_pause_retains_candidate_and_resume_does_not_reset_usage(tmp_path):
    service = HostDomain(tmp_path)
    app = service.call("plan", "planner", arguments={"plan": plan().model_dump(), "request_id": "budget-test"})["application"]
    service.call("bind", "executor", arguments={"application_id": app["id"]})
    service.call("start", "executor")
    service.apps.update(app["id"], candidate={"id": "c1"})
    meter_path = tmp_path / "runs/harness-policy/executor/meter.json"
    meter_path.parent.mkdir(parents=True)
    ledger = {"input_tokens": 310107, "output_tokens": 14378, "stop_reason": "token_budget_reservation_exhausted"}
    meter_path.write_text(json.dumps(ledger))
    paused = service.call("reconcile", "executor")
    assert paused["status"] == "paused" and paused["state"]["candidate"]["id"] == "c1"
    assert "324485" in paused["state"]["error"]
    resumed = service.call("start", "executor")
    assert resumed["state"]["error"] is None and resumed["state"]["budget_stop"] is None
    assert json.loads(meter_path.read_text()) == ledger
