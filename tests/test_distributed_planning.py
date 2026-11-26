"""Two-node topology and authorization preflight must fail closed."""

import copy
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pilot_infra.distributed_contracts import DistributedPlan
from pilot_infra.distributed_recipes import render_commands
from pilot_infra.distributed_service import DistributedService
from pilot_infra.resource_service import ResourceService
from tests.test_pilot_infra import setup


def plan(mode="sglang_pd"):
    roles = ("prefill", "decode") if mode == "sglang_pd" else ("head", "worker")
    ports = (
        ({"api": 31001, "bootstrap": 31002, "router": 31003}, {"api": 31001})
        if mode == "sglang_pd" else
        ({"gcs": 31001, "api": 31002, "nccl": 31003, "dashboard": 31004, "ray_client": 31005,
          "node_manager": 31006, "object_manager": 31007, "agent_http": 31008, "agent_grpc": 31009,
          "runtime_env": 31010, "metrics": 31011, "worker_1": 31012, "worker_2": 31013,
          "worker_3": 31014, "worker_4": 31015},
         {"node_manager": 31006, "object_manager": 31007, "agent_http": 31008, "agent_grpc": 31009,
          "runtime_env": 31010, "metrics": 31011, "worker_1": 31012, "worker_2": 31013,
          "worker_3": 31014, "worker_4": 31015})
    )
    return {
        "request_id": "distributed-123", "title": "双节点推理", "mode": mode,
        "served_model_name": "fixture", "context_length": 512,
        "nodes": [{"server_id": f"node-{index}", "role": role,
                   "address": f"10.0.0.{index+1}", "gpu_ids": [index],
                   "model_asset": f"model-{index}", "image_asset": f"image-{index}",
                   "rdma_device": "mlx5_0" if mode == "sglang_pd" else None, "ports": ports[index]}
                  for index, role in enumerate(roles)],
    }


def test_topology_rejects_duplicate_nodes_missing_ports_and_unsupported_addresses():
    value = plan()
    assert DistributedPlan.model_validate(value).nodes[0].role == "prefill"
    for mutate in (
        lambda v: v["nodes"][1].update(server_id=v["nodes"][0]["server_id"]),
        lambda v: v["nodes"][1].update(address=v["nodes"][0]["address"]),
        lambda v: v["nodes"][0].update(address="127.0.0.1"),
        lambda v: v["nodes"][0]["ports"].pop("bootstrap"),
        lambda v: v["nodes"][0]["ports"].update(router=31001),
        lambda v: v["nodes"][1].update(gpu_ids=[1, 2]),
    ):
        changed = copy.deepcopy(value)
        mutate(changed)
        with pytest.raises(ValidationError):
            DistributedPlan.model_validate(changed)


def test_image_probe_runs_in_isolated_python_and_reports_distributed_modules():
    from pilot_infra.fleet_worker import IMAGE_PROBE

    result = subprocess.run([sys.executable, "-I", "-c", IMAGE_PROBE], capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    assert {"ray", "sglang_ray", "sglang_router", "mooncake"} <= set(value["modules"])
    assert all(type(value["modules"][name]) is bool for name in ("ray", "sglang_ray", "sglang_router", "mooncake"))


def test_fixed_commands_use_recorded_pd_topology_and_bounded_ray_ports():
    pd = render_commands(plan())
    assert pd["prefill"][pd["prefill"].index("--disaggregation-mode") + 1] == "prefill"
    assert pd["decode"][pd["decode"].index("--disaggregation-bootstrap-port") + 1] == "31002"
    assert pd["router"][pd["router"].index("--host") + 1] == "127.0.0.1"
    ray = render_commands(plan("sglang_ray"))
    assert ray["worker"][ray["worker"].index("--address") + 1] == "10.0.0.1:31001"
    assert ray["head"][ray["head"].index("--worker-port-list") + 1] == "31012,31013,31014,31015"
    assert ray["serve"][ray["serve"].index("--nnodes") + 1] == "2"
    assert all(isinstance(part, str) for command in pd.values() for part in command)


def preflight_fixture(tmp_path, monkeypatch):
    now = time.time()
    servers, observations, executors, models, images = {}, {}, {}, [], []
    for index, node in enumerate(plan()["nodes"]):
        key = node["server_id"]
        executor = {"root": "/data/pilot-executor", "node_identity": str(index) * 64,
                    "machine_identity": "a" * 64, "platform": "musa"}
        executors[key] = executor
        server = {"id": key, "revision": 1, "policy_revision": 1, "enabled": True,
                  "connection": {"transport": "local"},
                  "last_node_check": {"ready": True, "server_revision": 1, "received_at": now,
                                      "node_identity": executor["node_identity"],
                                      "network_interfaces": [{"address": node["address"], "interface": "eth0"}],
                                      "rdma_inventory_known": True,
                                      "rdma_hcas": [{"device": "mlx5_0", "state": "4: ACTIVE", "interfaces": ["eth0"]}]},
                  "policy": {"mode": "review", "operations": ["network", "inference", "containers"],
                             "approvable_gpu_ids": node["gpu_ids"], "network_ports": list(node["ports"].values()),
                             "max_cpu_cores": 8, "max_memory_gib": 32, "max_disk_gib": 16,
                             "max_duration_seconds": 3600, "allow_source_edit": False}}
        servers[key] = server
        observations[key] = {
            "id": f"obs-{index}", "digest": f"digest-{index}", "server_revision": 1,
            "schema_version": 1, "scope": "whole-machine", "identity": {"machine_fingerprint": "a" * 64},
            "accelerator_platform": "musa",
            "policy_revision": 1, "received_at": now, "gpu_processes_known": True,
            "gpu_processes": [], "gpus": [{"index": index, "uuid": f"gpu-{index}",
                                            "platform": "musa", "total_mib": 32768, "free_mib": 30000}],
            "cpu": {"affinity_cores": 32}, "memory": {"available_bytes": 64 * 1024**3},
            "disks": [{"mount": "/data", "available_bytes": 64 * 1024**3}],
        }
        config = {"model_type": "fixture", "num_hidden_layers": 4, "num_key_value_heads": 4,
                  "hidden_size": 256, "num_attention_heads": 8}
        models.append({"id": node["model_asset"], "server_id": key, "path": "/data/model",
                       "manifest_digest": "f" * 64,
                       "manifest": {"config": config, "files": [{"path": "model.safetensors", "bytes": 1024**3}]}})
        images.append({"id": node["image_asset"], "server_id": key, "digest": "sha256:" + "a" * 64,
                       "backend": "docker", "platform": "musa", "engine": "sglang",
                       "stack_recipe": {"capabilities": ["pd", "tensor_parallel"], "model_types": ["fixture"]},
                       "environment": {"modules": {"sglang": True, "sglang_router": True, "ray": True,
                                                     "sglang_ray": True, "mooncake": True}}})
    infra = setup(tmp_path)
    class Resources:
        root = tmp_path
        clock = staticmethod(lambda: now)
        within_policy = ResourceService.within_policy
        fresh = ResourceService.fresh
        def latest(self, key):
            return observations[key]
    infra.resources = Resources()
    infra.assets = SimpleNamespace(read=lambda: {"models": models, "images": images})
    service = DistributedService(infra, clock=lambda: now)
    service.registry = SimpleNamespace(get=lambda key: servers[key])
    service.assets = SimpleNamespace(executor=lambda key: executors[key])
    monkeypatch.setattr("pilot_infra.distributed_service.worker_current", lambda _: True)
    return service, servers, observations, models, images


def test_preflight_requires_two_matching_assets_and_exclusive_device(tmp_path, monkeypatch):
    service, servers, observations, models, images = preflight_fixture(tmp_path, monkeypatch)
    ready = service.preflight(plan())
    assert ready["ready_for_review"] and not ready["ready_for_execution"]
    assert ready["execution_blockers"]
    models[1]["manifest_digest"] = "e" * 64
    images[1]["digest"] = "sha256:" + "b" * 64
    observations["node-1"]["gpu_processes_known"] = False
    servers["node-0"]["policy"]["network_ports"].remove(31002)
    value = service.preflight(plan())
    assert not value["ready_for_review"]
    issues = " ".join(value["issues"])
    assert "模型/分词器" in issues and "镜像" in issues and "占用未知" in issues and "端口" in issues


def test_pd_requires_active_hca_matching_selected_interface(tmp_path, monkeypatch):
    service, servers, *_ = preflight_fixture(tmp_path, monkeypatch)
    assert service.preflight(plan())["ready_for_review"]
    servers["node-1"]["last_node_check"]["rdma_hcas"][0]["interfaces"] = ["other-interface"]
    result = service.preflight(plan())
    assert not result["ready_for_review"] and "RDMA设备" in " ".join(result["issues"])


def test_s5000_routed_addresses_require_explicit_owner_network_and_verified_interface(tmp_path, monkeypatch):
    service, servers, *_ = preflight_fixture(tmp_path, monkeypatch)
    value = plan()
    for index, node in enumerate(value["nodes"]):
        node["address"] = f"100.63.105.{index + 2}"
        servers[node["server_id"]]["last_node_check"]["network_interfaces"][0]["address"] = node["address"]
    assert DistributedPlan.model_validate(value)
    assert "所有者允许的跨机网段" in " ".join(service.preflight(value)["issues"])
    for server in servers.values():
        server["policy"]["distributed_networks"] = ["100.63.105.0/24"]
    assert service.preflight(value)["ready_for_review"]
    servers["node-1"]["last_node_check"]["network_interfaces"][0]["address"] = "100.63.105.4"
    assert "不在节点已核验接口" in " ".join(service.preflight(value)["issues"])


def test_owner_network_policy_rejects_invalid_and_overbroad_ranges():
    from pilot_infra.resource_contracts import Authority

    assert Authority(distributed_networks=["100.63.105.0/24"]).distributed_networks == ["100.63.105.0/24"]
    for value in (["100.63.105.2/24"], ["0.0.0.0/0"], ["2001:db8::/32"],
                  ["100.63.105.0/24", "100.63.105.0/24"]):
        with pytest.raises(ValidationError):
            Authority(distributed_networks=value)


def test_network_authority_change_invalidates_review_before_any_effect(tmp_path, monkeypatch):
    service, servers, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    value = plan()
    for index, node in enumerate(value["nodes"]):
        node["address"] = f"100.63.105.{index + 2}"
        server = servers[node["server_id"]]
        server["last_node_check"]["network_interfaces"][0]["address"] = node["address"]
        server["policy"]["distributed_networks"] = ["100.63.105.0/24"]
    task = service.create(value)
    review = service.review(task["id"])
    servers["node-1"]["policy"]["distributed_networks"] = []
    servers["node-1"]["policy_revision"] += 1
    with pytest.raises(ValueError, match="重新审阅"):
        service.start(task["id"], review["review_digest"])
    assert not adapter.launched and lock_count(service) == 0


def test_preflight_blocks_observe_mode_stale_snapshot_and_missing_ray(tmp_path, monkeypatch):
    service, servers, observations, _, images = preflight_fixture(tmp_path, monkeypatch)
    servers["node-0"]["policy"]["mode"] = "observe"
    observations["node-1"]["received_at"] -= 601
    images[1]["environment"]["modules"]["ray"] = False
    value = plan("sglang_ray")
    for node in value["nodes"]:
        servers[node["server_id"]]["policy"]["network_ports"] = list(node["ports"].values())
    result = service.preflight(value)
    assert not result["ready_for_review"]
    assert "仅允许观察" in " ".join(result["issues"])
    assert "观测已过期" in " ".join(result["issues"])
    assert "Ray" in " ".join(result["issues"])
    assert "S5000设备映射" in " ".join(result["limitations"])


def test_draft_is_idempotent_and_request_id_cannot_be_rebound(tmp_path):
    service = DistributedService(setup(tmp_path))
    request = plan()
    first = service.create(request)
    assert service.create(request)["id"] == first["id"]
    changed = copy.deepcopy(request)
    changed["title"] = "不同任务"
    with pytest.raises(ValueError, match="不同拓扑"):
        service.create(changed)
    assert service.get(first["id"])["status"] == "draft"


class Adapter:
    def __init__(self):
        self.launched, self.stopped = [], []
        self.fail_launch = None
        self.fail_cleanup = False
        self.spoof = False

    def preflight(self, task, checked):
        assert checked["ready_for_review"]

    def launch(self, task, node):
        self.launched.append(node["server_id"])
        if node["server_id"] == self.fail_launch:
            raise TimeoutError("launch acknowledgement lost")

    def verify(self, task):
        if self.spoof:
            return {"passed": True}
        return {"passed": True, "http": {"status": 200, "model": "fixture", "output": "ok", "request_id": "r1"},
                "kv_transfer": {"backend": "mooncake", "prefill_server": "node-0", "decode_server": "node-1",
                                "request_id": "r1", "bytes": 128,
                                "source_evidence": "prefill-log", "destination_evidence": "decode-log"}}

    def fence_and_stop(self, task, node):
        self.stopped.append(node["server_id"])
        if self.fail_cleanup:
            raise TimeoutError("node state unknown")
        return {"restored": True, "fenced": True, "run_id": task["run_id"], "server_id": node["server_id"]}


def lock_count(service):
    with service.store.db() as db:
        return db.execute("SELECT COUNT(*) FROM locks").fetchone()[0]


def test_reviewed_start_reserves_both_nodes_and_stop_releases_locks(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    task = service.create(plan())
    review = service.review(task["id"])
    assert review["review_digest"]
    running = service.start(task["id"], review["review_digest"])
    assert running["status"] == "running" and lock_count(service) == 6
    assert adapter.launched == ["node-0", "node-1"]
    with pytest.raises(ValueError, match="待执行"):
        service.start(task["id"], review["review_digest"])
    assert service.stop(task["id"])["restored"]
    assert adapter.stopped == ["node-1", "node-0"] and lock_count(service) == 0


def test_launch_and_cleanup_follow_roles_even_if_plan_nodes_are_reversed(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    value = plan()
    value["nodes"].reverse()
    task = service.create(value)
    review = service.review(task["id"])
    assert service.start(task["id"], review["review_digest"])["status"] == "running"
    assert adapter.launched == ["node-0", "node-1"]
    service.stop(task["id"])
    assert adapter.stopped == ["node-1", "node-0"]


def test_ray_head_precedes_worker_when_plan_order_is_reversed(tmp_path, monkeypatch):
    service, servers, *_ = preflight_fixture(tmp_path, monkeypatch)
    value = plan("sglang_ray")
    for node in value["nodes"]:
        servers[node["server_id"]]["policy"]["network_ports"] = list(node["ports"].values())
    value["nodes"].reverse()
    class RayAdapter(Adapter):
        def verify(self, task):
            return {"passed": True, "http": {"status": 200, "model": "fixture", "output": "ok", "request_id": "r1"},
                    "ray": {"node_identities": [n["node_identity"] for n in task["review"]["scope"]["nodes"]],
                            "gpu_placement_servers": task["server_ids"], "placement_evidence": "fixture"}}
    adapter = service.adapter = RayAdapter()
    task = service.create(value)
    review = service.review(task["id"])
    assert service.start(task["id"], review["review_digest"])["status"] == "running"
    assert adapter.launched == ["node-0", "node-1"]
    service.stop(task["id"])
    assert adapter.stopped == ["node-1", "node-0"]


def test_expiry_during_acceptance_cleans_instead_of_reporting_running(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    class ExpiringAdapter(Adapter):
        def verify(self, task):
            evidence = super().verify(task)
            service.clock = lambda: task["deadline"]
            return evidence
    adapter = service.adapter = ExpiringAdapter()
    task = service.create(plan())
    review = service.review(task["id"])
    result = service.start(task["id"], review["review_digest"])
    assert result["status"] == "failed" and result["restored"]
    assert result["error_type"] == "TimeoutError"
    assert adapter.stopped == ["node-1", "node-0"] and lock_count(service) == 0


def test_lost_launch_ack_and_unknown_cleanup_keep_all_locks_until_confirmed(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    adapter.fail_launch, adapter.fail_cleanup = "node-1", True
    task = service.create(plan())
    review = service.review(task["id"])
    value = service.start(task["id"], review["review_digest"])
    assert value["status"] == "unknown" and lock_count(service) == 6
    assert value["started_nodes"] == ["node-0", "node-1"]
    adapter.fail_cleanup = False
    assert service.stop(task["id"])["status"] == "failed"
    assert lock_count(service) == 0


def test_recovery_fences_even_unsubmitted_nodes_before_releasing_locks(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    entered, resume = threading.Event(), threading.Event()
    class FencedAdapter(Adapter):
        def __init__(self):
            super().__init__()
            self.fenced, self.effects = set(), []

        def launch(self, task, node):
            entered.set()
            assert resume.wait(timeout=10)
            if node["server_id"] in self.fenced:
                raise ValueError("original run fenced")
            self.effects.append(node["server_id"])

        def fence_and_stop(self, task, node):
            self.fenced.add(node["server_id"])
            return super().fence_and_stop(task, node)

    adapter = service.adapter = FencedAdapter()
    task = service.create(plan())
    review = service.review(task["id"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.start, task["id"], review["review_digest"])
        assert entered.wait(timeout=10)
        assert lock_count(service) == 6
        recovered = service.recover(task["id"])
        assert recovered["status"] == "failed" and recovered["restored"]
        assert adapter.fenced == {"node-0", "node-1"} and lock_count(service) == 0
        resume.set()
        assert pending.result(timeout=10)["status"] == "failed"
    assert not adapter.effects


def test_recovery_keeps_locks_when_fencing_is_unconfirmed(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    class UnconfirmedAdapter(Adapter):
        def fence_and_stop(self, task, node):
            return {"restored": True, "fenced": False}
    adapter = service.adapter = UnconfirmedAdapter()
    adapter.fail_launch = "node-1"
    task = service.create(plan())
    review = service.review(task["id"])
    assert service.start(task["id"], review["review_digest"])["status"] == "unknown"
    assert service.recover(task["id"])["status"] == "unknown"
    assert lock_count(service) == 6
    service.adapter = Adapter()
    assert service.recover(task["id"])["status"] == "failed"
    assert lock_count(service) == 0


def test_foreign_run_cleanup_receipt_cannot_release_reservations(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    class ForeignReceiptAdapter(Adapter):
        def fence_and_stop(self, task, node):
            return {"restored": True, "fenced": True, "run_id": "other", "server_id": node["server_id"]}
    adapter = service.adapter = ForeignReceiptAdapter()
    adapter.fail_launch = "node-1"
    task = service.create(plan())
    review = service.review(task["id"])
    assert service.start(task["id"], review["review_digest"])["status"] == "unknown"
    assert lock_count(service) == 6
    service.adapter = Adapter()
    assert service.recover(task["id"])["status"] == "failed"
    assert lock_count(service) == 0


def test_recovery_takes_over_interrupted_stopping_without_replaying(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    task = service.create(plan())
    review = service.review(task["id"])
    running = service.start(task["id"], review["review_digest"])
    running.update(status="stopping", stop_id="interrupted", stop_failed=False)
    with service.store.db() as db:
        db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(running), task["id"]))
    adapter.fail_cleanup = True
    assert service.recover(task["id"])["status"] == "unknown" and lock_count(service) == 6
    adapter.fail_cleanup = False
    result = service.recover(task["id"])
    assert result["status"] == "stopped" and result["restored"] and lock_count(service) == 0
    assert adapter.launched == ["node-0", "node-1"]


def test_failed_start_recovery_remains_failed_after_cleanup_retry(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    task = service.create(plan())
    review = service.review(task["id"])
    running = service.start(task["id"], review["review_digest"])
    running.update(status="starting", error=None)
    with service.store.db() as db:
        db.execute("UPDATE pd_tasks SET data=? WHERE id=?", (json.dumps(running), task["id"]))
    adapter.fail_cleanup = True
    assert service.recover(task["id"])["status"] == "unknown"
    adapter.fail_cleanup = False
    assert service.stop(task["id"])["status"] == "failed"
    assert lock_count(service) == 0


def test_spoofed_success_or_changed_scope_never_leaves_a_running_service(tmp_path, monkeypatch):
    service, servers, *_ = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    task = service.create(plan())
    review = service.review(task["id"])
    servers["node-1"]["revision"] += 1
    with pytest.raises(ValueError, match="重新审阅"):
        service.start(task["id"], review["review_digest"])
    assert not adapter.launched and lock_count(service) == 0
    servers["node-1"]["revision"] -= 1
    adapter.spoof = True
    value = service.start(task["id"], review["review_digest"])
    assert value["status"] == "failed" and value["restored"] and lock_count(service) == 0


def test_asset_path_or_recipe_change_requires_new_review(tmp_path, monkeypatch):
    service, _, _, models, images = preflight_fixture(tmp_path, monkeypatch)
    adapter = service.adapter = Adapter()
    task = service.create(plan())
    review = service.review(task["id"])
    models[1]["path"] = "/data/different-location"
    with pytest.raises(ValueError, match="重新审阅"):
        service.start(task["id"], review["review_digest"])
    models[1]["path"] = "/data/model"
    images[0]["stack_recipe"]["model_types"].append("other")
    with pytest.raises(ValueError, match="重新审阅"):
        service.start(task["id"], review["review_digest"])
    assert not adapter.launched and lock_count(service) == 0


def test_unconfigured_runtime_cannot_reserve_or_start(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    task = service.create(plan())
    review = service.review(task["id"])
    with pytest.raises(ValueError, match="执行器尚未接入"):
        service.start(task["id"], review["review_digest"])
    assert service.get(task["id"])["status"] == "draft" and lock_count(service) == 0


def test_owner_api_can_save_but_does_not_expose_unimplemented_start(tmp_path):
    infra = setup(tmp_path)
    with pytest.raises(ValueError, match="Owner"):
        infra.dispatch("infra-distributed-create", "", "infra-lead", plan())
    task = infra.dispatch("infra-distributed-create", "", "owner", plan())
    assert infra.dispatch("infra-distributed-view", "", "owner", {"task_id": task["id"]})["status"] == "draft"
    with pytest.raises(ValueError, match="尚未开放"):
        infra.dispatch("infra-distributed-start", "", "owner", {"task_id": task["id"]})


def test_concurrent_start_admits_only_one_coordinator(tmp_path, monkeypatch):
    service, *_ = preflight_fixture(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    class CompetingAdapter(Adapter):
        def preflight(self, task, checked):
            barrier.wait(timeout=10)
    adapter = service.adapter = CompetingAdapter()
    task = service.create(plan())
    review = service.review(task["id"])

    def run():
        try:
            return service.start(task["id"], review["review_digest"])["status"]
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ["rejected", "running"]
    assert adapter.launched == ["node-0", "node-1"] and lock_count(service) == 6
