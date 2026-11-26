"""Lifecycle effects are owned, bounded and recoverable; scripted GPU reads."""

import copy
import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pilot_infra import fleet_worker as worker
from pilot_infra.remote_engine import RemoteEngine


def test_compensating_stop_uses_distinct_stable_id_and_original_deadline(monkeypatch):
    engine = object.__new__(RemoteEngine)
    engine.task = {
        "id": uuid.uuid4().hex,
        "_operation_id": uuid.uuid4().hex,
        "plan": {"gpu_ids": [7]},
        "state": {"deadline_epoch": time.time() + 60},
    }
    engine.candidate = {"digest": "fixture"}
    engine.model = {"id": "model"}
    engine.image = {"stack_recipe": {}}
    engine.executor = {"machine_identity": "fixture", "probe": {
        "worker_sha256": hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()
    }}
    engine.limits = lambda: {}
    jobs, submits = {}, []

    def call(action, payload):
        if action == "probe":
            return {"boot_id": "boot"}
        op = payload["operation_id"]
        if action == "lookup":
            return jobs.get(op)
        if action == "submit":
            submits.append(copy.deepcopy(payload))
            jobs[op] = {
                "request": payload["request"],
                "status": "completed",
                "result": {"passed": True},
            }
        return jobs[op]

    engine.call = call
    engine.operation("launch")
    engine.operation("stop")
    engine.operation("stop")
    assert len(submits) == 2 and submits[0]["operation_id"] != submits[1]["operation_id"]
    assert submits[1]["request"]["action"] == "stop"


def contract(root, monkeypatch):
    task = uuid.uuid4().hex
    labels = {"pilot.infra.task": task, "pilot.infra.launch": uuid.uuid4().hex}
    record = {
        "name": "pilot-owned",
        "labels": labels,
        "container_id": "container",
        "image": "image",
        "machine_identity": "machine",
        "boot_id": "boot",
        "command": [
            "python3",
            "-c",
            worker.LEASE_SUPERVISOR,
            "deadline",
            "python3",
            "-m",
            "sglang.launch_server",
        ],
        "memory_bytes": 1024,
        "cpu_nanos": 2000000000,
        "deadline": time.time() + 60,
        "model_path": "/model-source",
        "platform": "cuda",
        "devices": ["GPU-7"],
        "gpu_ids": [7],
        "candidate_digest": "candidate",
        "model_manifest": "manifest",
        "engine": "sglang",
        "network": "private-net",
        "network_id": "network",
        "watchdog": {"pid": 99},
        "environment": {"CUDA_VISIBLE_DEVICES": "GPU-7", "HF_HUB_OFFLINE": "1"},
        "resources": [],
    }
    worker.atomic(worker.state_path(root, task), record)
    obj = {
        "Id": "container",
        "Image": "image",
        "State": {"Running": True, "StartedAt": "start"},
        "Config": {
            "Labels": labels,
            "User": "65534:65534",
            "Entrypoint": ["python3"],
            "Cmd": record["command"][1:],
            "Env": [k + "=" + v for k, v in record["environment"].items()],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 1024,
            "MemorySwap": 1024,
            "NanoCpus": 2000000000,
            "PidsLimit": 512,
            "NetworkMode": "private-net",
            "DeviceRequests": [{"Count": 0, "DeviceIDs": ["GPU-7"]}],
        },
        "Mounts": [
            {"Type": "bind", "Source": "/model-source", "Destination": "/model", "RW": False}
        ],
        "NetworkSettings": {
            "Ports": {"30000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1234"}]},
            "Networks": {"private-net": {}},
        },
    }
    network = {"Id": "network", "Internal": True, "Labels": labels, "Containers": {"container": {}}}
    monkeypatch.setattr(worker, "identity", lambda: "machine")
    monkeypatch.setattr(worker, "boot", lambda: "boot")
    monkeypatch.setattr(worker, "alive", lambda r: bool(r))
    monkeypatch.setattr(worker, "inspect", lambda _: obj)
    monkeypatch.setattr(worker, "network_info", lambda _: network)
    return task, obj, record, network


@pytest.mark.parametrize(
    "field", ["env", "security", "pids", "gpu", "mount", "network", "watchdog"]
)
def test_live_identity_rejects_contract_drift(tmp_path, monkeypatch, field):
    task, obj, record, network = contract(tmp_path, monkeypatch)
    assert worker.service_identity(tmp_path, task)["passed"]
    if field == "env":
        obj["Config"]["Env"] = ["CUDA_VISIBLE_DEVICES=7"]
    elif field == "security":
        obj["HostConfig"]["SecurityOpt"] = []
    elif field == "pids":
        obj["HostConfig"]["PidsLimit"] = -1
    elif field == "gpu":
        obj["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] = ["GPU-0"]
    elif field == "mount":
        obj["Mounts"].append(
            {"Type": "bind", "Source": "/etc", "Destination": "/foreign", "RW": False}
        )
    elif field == "network":
        network["Containers"]["foreign"] = {}
    else:
        monkeypatch.setattr(worker, "alive", lambda _: False)
    assert not worker.service_identity(tmp_path, task)["passed"]


def test_foreign_network_is_not_removed(tmp_path, monkeypatch):
    task, _, record, network = contract(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "inspect", lambda _: None)
    network["Id"] = "replacement"
    calls = []
    monkeypatch.setattr(worker, "docker", lambda args, **_: calls.append(args))
    with pytest.raises(ValueError, match="Foreign/replaced network"):
        worker.stop_service(tmp_path, task)
    assert not calls


@pytest.mark.skipif(os.name == "nt", reason="Linux container PID1 lease supervisor")
def test_pid1_lease_exits_without_any_external_controller(tmp_path):
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            worker.LEASE_SUPERVISOR,
            str(time.time() + 0.4),
            sys.executable,
            "-c",
            "import time;time.sleep(60)",
        ],
        timeout=7,
    )
    assert result.returncode != 0 and time.monotonic() - started < 7


def test_dead_model_operation_can_be_explicitly_reconciled(tmp_path, monkeypatch):
    op = uuid.uuid4().hex
    worker.atomic(
        worker.journal(tmp_path, op),
        {
            "status": "unknown",
            "operation_id": op,
            "request": {"action": "asset_prepare", "kind": "model"},
            "pid": 99,
        },
    )
    monkeypatch.setattr(worker, "alive", lambda _: False)
    assert worker.dispatch(tmp_path, "reconcile", {"operation_id": op})["status"] == "failed"
    monkeypatch.setattr(worker, "alive", lambda _: True)
    with pytest.raises(ValueError, match="still alive"):
        worker.dispatch(tmp_path, "reconcile", {"operation_id": op})
