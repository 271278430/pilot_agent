import json
import uuid

import pytest

from pilot_apps.contracts import ApplicationBudgetChange
from pilot_host.bridge import HostDomain
from tests.test_goal_applications import plan


def test_owner_budget_control_retains_ledger_and_blocks_stale_or_running_change(tmp_path):
    domain = HostDomain(tmp_path)
    app = domain.call("plan", "planner", arguments={"request_id": uuid.uuid4().hex, "plan": plan().model_dump()})["application"]
    domain.call("bind", "executor", arguments={"application_id": app["id"]})
    meter = tmp_path / "runs/harness-policy/executor/meter.json"
    meter.parent.mkdir(parents=True)
    ledger = {"input_tokens": 472134, "output_tokens": 17134, "calls": 35, "stop_reason": "token_budget_reservation_exhausted"}
    meter.write_text(json.dumps(ledger))
    args = {"request_id": "owner-budget-change", "expected_revision": 1, "max_tokens": 750000}
    with pytest.raises(ValueError, match="只有Web"):
        domain.call("budget-update", "executor", arguments=args)
    updated = domain.call("budget-update", "executor", "owner", args)
    assert updated["max_tokens"] == 750000 and updated["used_tokens"] == 489268
    assert domain.call("budget-update", "executor", "owner", args) == updated
    assert json.loads(meter.read_text()) == ledger
    assert domain.application("executor").status == "planned"
    with pytest.raises(ValueError, match="其他位置"):
        domain.call("budget-update", "executor", "owner", {**args, "request_id": "stale-change"})
    with pytest.raises(ValueError, match="不同设置"):
        domain.call("budget-update", "executor", "owner", {**args, "max_tokens": None})
    domain.call("start", "executor")
    with pytest.raises(ValueError, match="执行中"):
        domain.call("budget-update", "executor", "owner", {**args, "request_id": "running-change", "expected_revision": 2})
    domain.call("pause", "executor")
    disabled = domain.call("budget-update", "executor", "owner", {**args, "request_id": "disable-ceiling", "expected_revision": 2, "max_tokens": None})
    assert disabled["max_tokens"] is None and disabled["used_tokens"] == 489268
    assert domain.application("executor").status == "paused"
    assert json.loads(meter.read_text()) == ledger
    assert json.loads((domain.apps.directory(app["id"])/"budget-policy.json").read_text())["max_tokens"] is None


def test_budget_control_refuses_unknown_effects_and_invalid_limits(tmp_path):
    domain = HostDomain(tmp_path)
    app = domain.call("plan", "planner", arguments={"request_id": uuid.uuid4().hex, "plan": plan().model_dump()})["application"]
    domain.apps.update(app["id"], status="running")
    domain.apps.claim(app["id"], "deploy")
    domain.apps.stop(app["id"], "pause")
    with pytest.raises(ValueError, match="未确认"):
        domain.call("budget-update", "planner", "owner", {"request_id": "unknown-budget", "expected_revision": 1, "max_tokens": 999999})
    for value in (-1, 0, 1000000001, "not-a-number"):
        with pytest.raises(ValueError):
            ApplicationBudgetChange(request_id="invalid-limit", expected_revision=1, max_tokens=value)
    with pytest.raises(ValueError):
        ApplicationBudgetChange(request_id="missing-limit", expected_revision=1)
