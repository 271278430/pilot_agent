from types import SimpleNamespace

import pytest

from pilot_apps.lifecycle import reconcile_delivery
from tests.test_live_app_acceptance import accepted


def test_controller_error_after_verified_delivery_is_reconciled(tmp_path, monkeypatch):
    store, row, _, _ = accepted(tmp_path, monkeypatch)
    store.update(row.id, status="running", verification=None)

    store.update(row.id, verification={"passed": True, "candidate": "c1"})
    assert reconcile_delivery(store, row.id, controller_outcome={
        "status": "error", "error": "controller transport interrupted"})
    assert store.get(row.id).status == "completed"
    event = store.events(row.id)[-1]
    assert event["kind"] == "delivery_reconciled"
    assert event["data"]["controller_outcome"]["status"] == "error"




def test_stopped_controller_can_independently_verify_deployed_candidate(tmp_path, monkeypatch):
    import json
    import uuid

    from pilot_apps.backend import AppBackend
    from tests.test_live_app_acceptance import response

    store, row, _, folder = accepted(tmp_path, monkeypatch)
    def genuine(self, path, message):
        value, receipt = response(message, row.plan.model_id)
        value["receipt_id"] = receipt["receipt_id"] = uuid.uuid4().hex
        with (folder / "inference-receipts.jsonl").open("a") as stream:
            stream.write(json.dumps(receipt) + "\n")
        return {"status": 200, "body": value}
    monkeypatch.setattr(AppBackend, "http", genuine)
    store.update(row.id, status="running", verification=None)
    assert reconcile_delivery(store, row.id, controller_outcome={
        "status": "error", "error": "release_token_budget_reservation_exhausted"})
    assert store.get(row.id).status == "completed"
    events = store.events(row.id)
    assert any(e["kind"] == "verification_reconciled" for e in events)
    assert events[-1]["data"]["controller_outcome"]["status"] == "error"


def test_fallback_does_not_accept_old_candidate_for_new_feedback_or_before_run(tmp_path, monkeypatch):
    store, row, _, _ = accepted(tmp_path, monkeypatch)
    store.update(row.id, status="running", verification=None)
    assert not reconcile_delivery(store, row.id, controller_outcome={"status": "resume_recheck"})
    store.update(row.id, feedback=[{"candidate": "c1", "message": "请修订界面"}])
    assert not reconcile_delivery(store, row.id, controller_outcome={"status": "completed"})
    assert store.get(row.id).status == "running" and not store.get(row.id).state["verification"]


@pytest.mark.parametrize("change", ["container", "model", "candidate", "paused"])
def test_reconciliation_rejects_drift_and_user_pause(tmp_path, monkeypatch, change):
    store, row, _, _ = accepted(tmp_path, monkeypatch)
    store.update(row.id, status="running")
    if change == "container":
        def reject(*args):
            raise ValueError("Container changed")
        monkeypatch.setattr("pilot_apps.backend.AppBackend.check_container", reject)
    elif change == "model":
        monkeypatch.setattr("pilot_apps.backend.call_engine", lambda *a, **k: {"model_identity": "new"})
    elif change == "candidate":
        store.update(row.id, verification={"passed": True, "candidate": "c2"})
    else:
        store.update(row.id, status="paused")
    assert not reconcile_delivery(store, row.id)
    assert store.get(row.id).status != "completed"




def test_completion_does_not_overwrite_pause_during_final_check(tmp_path, monkeypatch):
    from pilot_apps.service import AppService
    store, row, backend, _ = accepted(tmp_path, monkeypatch)
    store.update(row.id, status="running")
    monkeypatch.setattr(type(backend), "ensure_current_acceptance", lambda *a, **k: store.update(row.id, status="paused"))
    assert not AppService(store, row.id, "supervisor").call("finish", {})["ok"]
    assert store.get(row.id).status == "paused"




def test_public_recovery_keeps_failure_but_does_not_leak_controller_details(tmp_path, monkeypatch):
    from pilot_collab.public_trace import task_trace
    store, row, _, _ = accepted(tmp_path, monkeypatch)
    store.event(row.id, "supervisor", "session_end", {"status": "error", "error": "private path"})
    store.event(row.id, "system", "delivery_reconciled", {"candidate": "c1", "controller_outcome": {"error": "private prompt"}})
    steps = task_trace(store.db, SimpleNamespace(kind="app", id=row.id))
    assert steps[0]["outcome"] == "failed" and steps[0]["recovery"] == "delivery_rechecked"
    assert steps[-1]["outcome"] == "passed" and steps[-1]["role"] == "交付验收器"
    assert "private" not in str(steps)
