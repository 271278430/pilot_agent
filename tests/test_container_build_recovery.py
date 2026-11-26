import uuid

import pytest

from pilot_infra.worker import run
from tests.test_engine_source import candidate, fixture
from tests.test_pilot_infra import FakeEngine


def test_unknown_docker_build_keeps_claim_until_identity_reconciliation(tmp_path, monkeypatch):
    service, source, task = fixture(tmp_path)
    frozen = candidate(service, task)
    job = service.submit(task, "engine_build", frozen["id"])
    labels = {"pilot.infra.task": task["id"], "pilot.infra.build": job["job_id"]}

    def uncertain(*args):
        service.store.operation(
            job["job_id"], "running", build_container_name="uncertain-build", build_labels=labels
        )
        raise TimeoutError("Docker create response lost")

    monkeypatch.setattr("pilot_infra.engine_source.build_artifact", uncertain)
    monkeypatch.setattr(
        "pilot_infra.container_runtime.stop_owned",
        lambda *args: (_ for _ in ()).throw(ValueError("Container owner cannot be verified")),
    )
    result = run(tmp_path, job["job_id"], engine_factory=FakeEngine)
    assert not result["passed"] and "cleanup_error" in result
    assert service.store.operation(job["job_id"])["status"] == "unknown"
    current = service.store.get(task["id"])
    assert (
        current["state"]["active_job"] == job["job_id"] and current["state"]["status"] == "paused"
    )
    assert source.artifact(current, frozen, required=False) is None
    with pytest.raises(ValueError):
        service.submit(current, "engine_build", frozen["id"])
    with pytest.raises(ValueError, match="cannot be verified"):
        service.control(
            task["id"],
            "recover",
            {"request_id": uuid.uuid4().hex, "expected_revision": current["revision"]},
        )
    assert service.store.operation(job["job_id"])["status"] == "unknown"
    monkeypatch.setattr("pilot_infra.container_runtime.stop_owned", lambda *args: None)
    current = service.store.get(task["id"])
    service.control(
        task["id"],
        "recover",
        {"request_id": uuid.uuid4().hex, "expected_revision": current["revision"]},
    )
    assert service.store.operation(job["job_id"])["status"] == "failed"
    assert service.store.get(task["id"])["state"]["active_job"] is None
