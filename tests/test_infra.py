
import pytest
from pydantic import ValidationError

from infra_pilot.controller import choose
from infra_pilot.service import InfraService, diagnose
from infra_pilot.spec import InfraSpec, resolve_plan
from infra_pilot.worker import valid_job_id
from repopilot.storage import Store, atomic_json


def probe():
    return {"python": "3.10.12", "disk_free_bytes": 30 * 1024**3, "gpus": [{"index": i, "name": "A100", "free_mib": 80000, "driver": "570.172.08"} for i in range(2)]}


class FakeRemote:
    def __init__(self):
        self.starts = 0
        self.fail_after_start = False
        self.writes = []
        self.job = None
    def worker(self, action, *args):
        if action == "probe":
            return probe()
        if action == "start":
            self.starts += 1
            self.job = {"stage": args[0], "job": args[1], "status": "running"}
            if self.fail_after_start:
                raise ConnectionError("Lost SSH acknowledgement")
            return self.job
        raise AssertionError(action)
    def wait(self, job, seconds):
        assert job == self.job["job"]
        return {**self.job, "status": "completed", "result": {"verified": True}}
    def fetch(self, remote, local):
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("torch==2.6.0\n")
    def write(self, path, data):
        self.writes.append((path, data))


@pytest.fixture
def service(tmp_path):
    spec = InfraSpec(host="test", username="test")
    atomic_json(tmp_path / "infra-run.json", {"spec": spec.model_dump(), "ssh_env": "never-read", "known_hosts": "never-read"})
    Store(tmp_path).checkpoint({"completed": {}, "jobs": [], "active": None, "pending": None, "failed": None, "diagnosis": None, "repairs": [], "actions": 0, "halt_reason": None})
    return InfraService(tmp_path, FakeRemote())


@pytest.mark.parametrize("root", ["/", "/data", "../escape", "/data/../etc"])
def test_infra_refuses_unscoped_directories(root):
    with pytest.raises(ValidationError):
        InfraSpec(host="test", username="test", root=root)


def test_infra_plan_refuses_incompatible_driver_and_busy_gpu():
    spec = InfraSpec(host="test", username="test")
    inventory = probe()
    inventory["gpus"][0]["driver"] = "535.0"
    with pytest.raises(ValueError, match="driver_incompatible"):
        resolve_plan(spec, inventory)
    inventory = probe()
    inventory["gpus"][1]["free_mib"] = 200
    with pytest.raises(ValueError, match="gpu_unavailable"):
        resolve_plan(spec, inventory)


def test_infra_refuses_unapproved_models_and_sources():
    with pytest.raises(ValidationError):
        InfraSpec(host="test", username="test", model_id="untrusted/custom-model")
    with pytest.raises(ValidationError):
        InfraSpec(host="test", username="test", model_endpoint="http://untrusted")


def test_infra_dependency_order_and_idempotent_completed_stage(service):
    assert not service.call("advance", {"stage": "train"})["ok"]
    assert service.call("advance", {"stage": "probe"})["ok"]
    assert service.call("advance", {"stage": "probe"})["result"]["idempotent"]
    assert service.call("advance", {"stage": "plan"})["ok"]
    assert service.call("advance", {"stage": "provision"})["ok"]
    assert not service.call("advance", {"stage": "provision"})["ok"]
    assert service.remote.starts == 1
    assert service.call("inspect", {"wait_seconds": 0})["ok"]
    assert choose(service.store.load()) == ("advance", {"stage": "download"})


def test_infra_lost_launch_ack_is_inspected_without_duplicate_job(service):
    service.call("advance", {"stage": "probe"})
    service.call("advance", {"stage": "plan"})
    service.remote.fail_after_start = True
    assert not service.call("advance", {"stage": "provision"})["ok"]
    pending = service.store.load()["pending"]
    assert pending["job"] == service.remote.job["job"]
    assert not service.call("advance", {"stage": "provision"})["ok"]
    assert service.remote.starts == 1
    assert service.call("inspect", {"wait_seconds": 0})["ok"]
    assert service.store.load()["pending"] is None
    assert "provision" in service.store.load()["completed"]


def test_infra_repairs_are_diagnosis_scoped_and_bounded(service):
    state = service.store.load()
    state["failed"] = {"job": "serve-test", "stage": "serve_benchmark", "error": "Address already in use"}
    service.store.checkpoint(state)
    assert not service.call("repair", {"action": "change_port"})["ok"]
    assert service.call("diagnose", {})["result"]["category"] == "port_conflict"
    assert service.call("repair", {"action": "change_port"})["ok"]
    assert service.spec.service_port == 18766
    assert not service.spec.inject_port_conflict
    assert service.remote.writes[0][0] == "spec.json"
    state = service.store.load()
    state["failed"] = {"error": "port_conflict"}
    state["diagnosis"] = diagnose("port_conflict")
    service.store.checkpoint(state)
    assert "repair_budget_exhausted" in service.call("repair", {"action": "change_port"})["error"]


def test_infra_unknown_failure_stops_rules_controller(service):
    state = service.store.load()
    state["failed"] = {"error": "unclassified unknown failure"}
    state["diagnosis"] = diagnose(state["failed"]["error"])
    assert choose(state) is None
    assert diagnose("Connection interrupted; incomplete-download")["repair"] == "package_source"
    assert diagnose("CUDA out of memory")["repair"] is None


def test_infra_job_id_supports_recipe_names_without_path_traversal():
    assert valid_job_id("cuda_smoke-abcd12345678")
    assert valid_job_id("nccl_smoke-9876abcdef12")
    assert not valid_job_id("../../outside")
    assert not valid_job_id("/data/job")


def test_infra_repeated_protocol_error_halts_without_relaunch(service):
    for _ in range(3):
        service.call("advance", {"stage": "train"})
    assert service.store.load()["halt_reason"].startswith("repeated_tool_error")
    assert service.remote.starts == 0


def test_infra_gpu_selection_refuses_duplicate_devices():
    with pytest.raises(ValidationError):
        InfraSpec(host="test", username="test", gpu_ids=[0, 0])
