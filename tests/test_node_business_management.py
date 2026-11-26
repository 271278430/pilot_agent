"""Permission, frozen checks and recovery failures cannot become business success."""

import copy
import json

import pytest

from pilot_infra.build_contracts import RecipeSave
from pilot_infra.build_validation import build_validation
from pilot_infra.network_service import NetworkPlan, NetworkService
from pilot_infra.node_authority import require_operation
from pilot_infra.resource_contracts import Authority, DockerAccess
from tests.test_build_validation import evidence


def test_legacy_permissions_do_not_implicitly_enable_network_or_profiling():
    server = {"enabled": True, "policy": Authority(mode="review").model_dump()}
    require_operation(server, "software")
    for name in ["network", "profiling"]:
        with pytest.raises(ValueError):
            require_operation(server, name)
    server["policy"]["operations"] = ["network"]
    require_operation(server, "network")
    with pytest.raises(ValueError):
        require_operation(server, "software")


def test_serving_respects_both_operation_switches_for_baseline_and_review(tmp_path):
    from tests.test_resource_authority import fixture, plan

    infra, resources, _ = fixture(tmp_path)
    task = infra.create({"request_id": "baseline-permission", "plan": plan()})["task"]
    limits = {"cpu_cores": 1, "memory_gib": 2, "disk_gib": 1, "duration_seconds": 600}
    for operations, missing in [(["inference"], "containers"), (["containers"], "inference")]:
        server = resources.registry.get("a100-server")
        server = resources.registry.policy(
            {"server_id": server["id"], "expected_revision": server["revision"],
             "policy": {**server["policy"], "operations": operations}}
        )
        with pytest.raises(ValueError, match=missing):
            resources.within_policy(server, task["plan"], limits)
        with pytest.raises(ValueError, match=missing):
            resources.check_task(task, "launch")
        assert resources.check_task(task, "stop") is None
    server = resources.registry.get("a100-server")
    resources.registry.policy(
        {"server_id": server["id"], "expected_revision": server["revision"],
         "policy": {**server["policy"], "operations": ["containers", "inference"]}}
    )
    assert resources.check_task(task, "launch") is None


def test_node_readiness_includes_serving_operation_permissions():
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace

    from pilot_infra import fleet_worker
    from pilot_infra.asset_service import AssetService

    server = {
        "id": "node", "enabled": True, "revision": 1, "connection": {"transport": "local"},
        "last_node_check": {"server_revision": 1, "ready": True},
        "policy": Authority(mode="review", approvable_gpu_ids=[0], operations=["inference"]).model_dump(),
    }
    service = object.__new__(AssetService)
    service.registry = SimpleNamespace(catalog=lambda: [server])
    service.rows = lambda _: [{"server_id": "node", "status": "ready", "kind": k} for k in ("model", "image")]
    executor = {"server_id": "node", "server_revision": 1, "enabled": True}
    service.executors = lambda: [executor]
    readiness = service.node_readiness()[0]
    assert not readiness["deployment_registered"]
    assert "尚未允许本任务容器" in readiness["reasons"]
    server["policy"]["operations"] = ["inference", "containers"]
    assert service.node_readiness()[0]["deployment_registered"]
    server["connection"]["transport"] = "ssh"
    assert not service.node_readiness()[0]["deployment_registered"]
    assert any("版本需更新" in item for item in service.node_readiness()[0]["reasons"])
    executor["probe"] = {"worker_sha256": hashlib.sha256(Path(fleet_worker.__file__).read_bytes()).hexdigest()}
    assert service.node_readiness()[0]["deployment_registered"]


def test_remote_assessment_uses_its_executor_assets_and_unoccupied_devices():
    from pilot_infra.resource_assessment import assess

    server = {
        "id": "node-a", "enabled": True, "connection": {"transport": "ssh"},
        "policy": Authority(mode="review", approvable_gpu_ids=[0], operations=["inference", "containers"]).model_dump(),
    }
    model = {
        "id": "model-a", "path": "/mnt/data/model", "server_id": "node-a",
        "manifest": {"config": {"num_hidden_layers": 24, "num_key_value_heads": 8,
                                "hidden_size": 1024, "num_attention_heads": 16},
                     "files": [{"path": "model.safetensors", "bytes": 1024**3}]},
    }
    assets = {"models": [model], "images": [{"id": "image-a", "server_id": "node-a", "platform": "cuda", "engine": "sglang"}]}
    observation = {
        "id": "observation", "accelerator_platform": "cuda",
        "gpus": [{"index": 0, "uuid": "gpu-0", "free_mib": 70000, "total_mib": 80000}],
        "gpu_processes": [], "cpu": {"affinity_cores": 8},
        "gpu_processes_known": True,
        "memory": {"available_bytes": 32 * 1024**3},
        "disks": [{"mount": "/mnt/data", "available_bytes": 50 * 1024**3}],
    }
    result = assess(server, observation, assets, "model-a", root="/mnt/data/pilot-executor")
    assert result["executor_available"] and result["recommendation_ready"]
    assert result["suggested_gpu_ids"] == [0]
    observation["gpu_processes_known"] = False
    assert not assess(server, observation, assets, "model-a", root="/mnt/data/pilot")["recommendation_ready"]
    observation["gpu_processes_known"] = True
    assert not assess(server, observation, assets, "model-a", root=None)["recommendation_ready"]
    observation["gpu_processes"] = [{"gpu_uuid": "gpu-0"}]
    assert not assess(server, observation, assets, "model-a", root="/mnt/data/pilot")["suggested_gpu_ids"]
    observation["gpu_processes"] = []
    model["server_id"] = "node-b"
    assert not assess(server, observation, assets, "model-a", root="/mnt/data/pilot")["recommendation_ready"]
    model["server_id"] = "node-a"
    server["policy"]["operations"] = ["inference"]
    assert not assess(server, observation, assets, "model-a", root="/mnt/data/pilot")["recommendation_ready"]


def test_docker_access_separates_encrypted_reference_from_connection_and_jobs():
    with pytest.raises(ValueError):
        DockerAccess(mode="sudo_password")
    with pytest.raises(ValueError):
        DockerAccess(mode="direct", credential_ref="secret-" + "a" * 32)
    access = DockerAccess(mode="sudo_password", credential_ref="secret-" + "a" * 32)
    assert "password" not in access.model_dump()


def operator():
    task = evidence()
    task.update(kind="operator", checks_manifest={"verify.py": "gold"})
    result = task["results"][0]
    result.update(
        gpu_execution=True, checks_manifest=task["checks_manifest"], checks_unchanged=True
    )
    cases = [
        {
            "shape": list(shape),
            "dtype": dtype,
            "max_abs_error": 0.0,
            "tolerance": 0.003 if dtype == "torch.float16" else 0.00003,
        }
        for shape in [(1, 32), (17, 137), (256, 1024), (2048, 4096)]
        for dtype in ["torch.float16", "torch.float32"]
    ]
    result["business_evidence"] = {
        "protocol": "pilot-rmsnorm-cuda-v1",
        "cases": cases,
        "speedup": 2.0,
        "baseline_ms": 2.0,
        "candidate_ms": 1.0,
        "timing_method": "cuda_events_median_20",
    }
    return task


def test_operator_requires_actual_gpu_all_cases_and_consistent_finite_timings():
    task = operator()
    assert build_validation(task)["operator_performance"] == "improved"
    task["results"][0]["business_evidence"]["speedup"] = 100.0
    assert build_validation(task)["operator_performance"] == "not_validated"
    task = operator()
    task["results"][0]["business_evidence"]["cases"][1] = copy.deepcopy(
        task["results"][0]["business_evidence"]["cases"][0]
    )
    assert build_validation(task)["gpu_numerical"] == "not_validated"
    task = operator()
    task["results"][0]["checks_unchanged"] = False
    assert build_validation(task)["pipeline"] == "unverified"


def test_network_partial_bootstrap_is_in_compensation_scope(tmp_path, monkeypatch):
    service = NetworkService(tmp_path)
    task = {
        "id": "a" * 32,
        "plan": {
            "peers": [
                {"server_id": "a", "address": "10.0.0.1", "port": 23181},
                {"server_id": "b", "address": "10.0.0.2", "port": 23181},
            ],
            "duration_seconds": 30,
            "rounds": 1,
            "payload_bytes": 1024,
        },
        "status": "draft",
        "scopes": [
            {"server": {"id": key, "policy": {"max_duration_seconds": 60}}, "port": 23181}
            for key in ["a", "b"]
        ],
        "results": [],
        "restored": False,
    }
    with service.store.db() as db:
        db.execute(
            "INSERT INTO network_tasks VALUES(?,?,?)", (task["id"], "fixture", json.dumps(task))
        )
    calls = []

    class Remote:
        def admit(self):
            pass

        def bootstrap(self):
            raise TimeoutError("created before lost reply")

        def call(self, action, args):
            calls.append(action)
            return {"restored": True}

    monkeypatch.setattr(service, "peer", lambda *a: Remote())
    result = service.run(task["id"])
    assert result["passed"] is False and result["restored"] and calls == ["cleanup"]
    with service.store.db() as db:
        assert not db.execute("SELECT * FROM locks").fetchall()
    with pytest.raises(ValueError, match="不得重放"):
        service.run(task["id"])


def test_pd_recipe_requires_ready_model_contract_and_network_nodes_are_distinct():
    with pytest.raises(ValueError):
        RecipeSave(
            recipe={
                "id": "pd",
                "label": "PD验证",
                "server_id": "node",
                "image_digest": "sha256:" + "a" * 64,
                "template": "pd_vllm_shared",
            }
        )
    with pytest.raises(ValueError):
        NetworkPlan(
            request_id="fixture-request",
            title="网络验证",
            peers=[{"server_id": "a", "address": "10.0.0.1"}] * 2,
        )


def test_device_recommendation_skips_occupied_unknown_and_disabled_executor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pilot_infra.resource_registry import ResourceRegistry
    from tests.test_build_workbench import service as build_fixture

    builds = build_fixture(tmp_path)
    registry = ResourceRegistry(tmp_path)
    server = registry.policy(
        {
            "server_id": "fixture-server",
            "expected_revision": 1,
            "policy": Authority(mode="review", approvable_gpu_ids=[0, 1, 4]).model_dump(),
        }
    )
    with registry.db() as db:
        executor = json.loads(db.execute("SELECT data FROM fleet_executors").fetchone()[0])
        executor["server_revision"] = server["revision"]
        db.execute("UPDATE fleet_executors SET data=?", (json.dumps(executor),))
    builds.save_profile(
        {
            "recipe": {
                "id": "cuda-case",
                "label": "CUDA案例",
                "server_id": "fixture-server",
                "image_digest": "sha256:" + "d" * 64,
                "template": "rmsnorm_cuda",
            }
        }
    )
    observation = {
        "accelerator_platform": "cuda",
        "gpus": [
            {"index": 0, "uuid": "busy", "free_mib": 8000},
            {"index": 1, "uuid": "free", "free_mib": 8000},
            {"index": 4, "uuid": "unknown", "free_mib": None},
        ],
        "gpu_processes": [{"gpu_uuid": "busy"}],
        "gpu_processes_known": True,
    }
    monkeypatch.setattr(
        "pilot_infra.fleet_transport.FleetTransport",
        lambda _: SimpleNamespace(transport=SimpleNamespace(observe=lambda _: observation)),
    )
    result = builds.recommend({"profile_id": "cuda-case"})
    assert result["available"] and result["gpu_ids"] == [1]
    observation["gpu_processes_known"] = False
    result = builds.recommend({"profile_id": "cuda-case"})
    assert not result["available"] and any("占用未知" in r for r in result["reasons"])
    observation["gpu_processes_known"] = True
    with registry.db() as db:
        executor["enabled"] = False
        db.execute("UPDATE fleet_executors SET data=?", (json.dumps(executor),))
    assert not builds.recommend({"profile_id": "cuda-case"})["available"]


def test_complete_candidate_source_survives_remote_cleanup_and_rejects_archive_drift(tmp_path):
    import base64
    import io
    import tarfile

    from tests.test_build_workbench import plan, service

    builds = service(tmp_path)
    task = builds.create(plan())
    builds.prepare(task["id"])
    builds.freeze(task["id"])
    assert builds.cleanup(task["id"])["restored"]
    payload = builds.export_source({"task_id": task["id"], "candidate_id": "c1"}, downloading=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload["data"])), mode="r:gz") as bundle:
        assert bundle.extractfile("core.py").read() == b"original"
    candidate = builds.task(task["id"])["candidates"][0]
    (tmp_path / candidate["source_archive"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="变化"):
        builds.export_source({"task_id": task["id"], "candidate_id": "c1"}, downloading=True)
