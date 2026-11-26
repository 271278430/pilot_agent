"""Behavioral authorization and machine decisions, without real GPU effects."""

import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import paramiko
import pytest

from pilot_infra.engine import model_manifest
from pilot_infra.resource_contracts import ResourceProposal
from pilot_infra.resource_probe import probe_program
from pilot_infra.resource_registry import ResourceVault, fingerprint
from pilot_infra.resource_service import ResourceService
from pilot_infra.resource_transport import ConnectionFailure, ResourceTransport
from pilot_infra.worker import run
from tests.test_pilot_infra import plan, setup, start


def fixture(root):
    infra = setup(root)
    path = root / "models/fixture"
    (path / "config.json").write_text(
        json.dumps(
            {
                "num_hidden_layers": 24,
                "num_key_value_heads": 8,
                "hidden_size": 1024,
                "num_attention_heads": 16,
            }
        )
    )
    assets_path = root / ".local/infra-assets.json"
    assets = json.loads(assets_path.read_text())
    assets["models"][0]["manifest_digest"] = model_manifest(path)["digest"]
    assets_path.write_text(json.dumps(assets))
    observation = {
        "schema_version": 1,
        "scope": "whole-machine",
        "observed_at": time.time(),
        "identity": {"hostname": "fixture", "machine_fingerprint": "f" * 64},
        "accelerator_platform": "cuda",
        "cpu": {"affinity_cores": 32, "logical_cores": 64},
        "memory": {"available_bytes": 64 * 1024**3},
        "disks": [{"mount": str(root.anchor), "available_bytes": 128 * 1024**3}],
        "gpus": [
            {
                "index": n,
                "uuid": f"gpu-{n}",
                "name": "fixture A100",
                "platform": "cuda",
                "total_mib": 81920,
                "free_mib": 70000,
            }
            for n in range(8)
        ],
        "gpu_processes": [{"gpu_uuid": "gpu-0", "pid": 55, "owner": "other-user"}],
        "gpu_processes_known": True,
        "raw": {},
    }
    transport = SimpleNamespace(
        observe=lambda _: copy.deepcopy(observation), test=lambda _: {"ready": True}
    )
    resources = ResourceService(infra, transport)
    infra.resources = resources
    infra.assets.authority_validator = resources.validate_plan
    resources.observe({"server_id": "a100-server"})
    return infra, resources, observation


def proposal(resources, root="native-root", **changes):
    request = {
        "request_id": uuid.uuid4().hex,
        "plan": plan(),
        "reason": "基于整机观测申请有界验证任务",
        **changes,
    }
    return resources.propose(request, root)


def target(value):
    return {"proposal_id": value["id"], "expected_digest": value["digest"]}


def approved(resources, value):
    resources.decide({**target(value), "decision": "allowed-once"}, owner=True)
    return resources.activate(target(value), owner=True)["task"]


def test_observation_all_devices_does_not_grant_other_cards(tmp_path):
    infra, r, _ = fixture(tmp_path)
    analysis = r.assessment({"server_id": "a100-server", "model_asset": "fixture"})
    assert len(analysis["devices"]) == 8 and analysis["suggested_gpu_ids"] == [7]
    assert analysis["devices"][0]["processes"][0]["owner"] == "other-user"
    with pytest.raises(ValueError, match="可审批"):
        proposal(r, plan=plan(gpu_ids=[0]))
    assert not infra.store.list()


def test_capacity_recommendation_stays_within_policy_and_reports_whole_host_pressure(tmp_path):
    _, resources, observation = fixture(tmp_path)
    observation['disks'].append({'mount': '/tmp', 'total_bytes': 1000, 'available_bytes': 1})
    resources.observe({'server_id': 'a100-server'})
    result = resources.assessment({'server_id': 'a100-server', 'model_asset': 'fixture', 'context_length': 2048, 'concurrency': 2})
    assert result['recommendation_ready']
    assert result['suggested_gpu_ids'] == [7]
    assert result['suggested_limits']['cpu_cores'] <= result['policy']['max_cpu_cores']
    assert result['suggested_limits']['memory_gib'] <= result['policy']['max_memory_gib']
    assert result['suggested_serving']['context_length'] == 2048
    assert result['health_findings'][0]['scope'] == '/tmp'
    observation['gpus'][7]['free_mib'] = None
    resources.observe({'server_id': 'a100-server'})
    assert not resources.assessment({'server_id': 'a100-server', 'model_asset': 'fixture'})['recommendation_ready']


def test_stale_observation_cannot_be_used_as_an_actionable_recommendation(tmp_path):
    _, resources, _ = fixture(tmp_path)
    resources.clock = lambda: time.time() + 601
    result = resources.assessment({"server_id": "a100-server", "model_asset": "fixture"})
    assert not result["recommendation_ready"]
    assert any("观测已过期" in item for item in result["limitations"])


def test_task_entry_readiness_respects_container_and_inference_permissions(tmp_path):
    infra, resources, _ = fixture(tmp_path)
    assert infra.assets.read()["ready"]
    server = resources.registry.get("a100-server")
    resources.registry.policy({"server_id": server["id"], "expected_revision": server["revision"],
                               "policy": {**server["policy"], "operations": ["inference"]}})
    assert not infra.assets.read()["ready"]
    server = resources.registry.get("a100-server")
    resources.registry.policy({"server_id": server["id"], "expected_revision": server["revision"],
                               "policy": {**server["policy"], "operations": ["inference", "containers"]}})
    assert infra.assets.read()["ready"]


def test_occupied_approved_device_is_rejected_for_exclusive_proposal(tmp_path):
    _, resources, observation = fixture(tmp_path)
    observation["gpu_processes"].append({"gpu_uuid": "gpu-7", "pid": 66, "owner": "other"})
    resources.observe({"server_id": "a100-server"})
    with pytest.raises(ValueError, match="已有业务占用"):
        proposal(resources)


def test_unknown_gpu_occupancy_never_becomes_a_free_device_or_proposal(tmp_path):
    _, resources, observation = fixture(tmp_path)
    observation["gpu_processes"] = []
    observation["gpu_processes_known"] = False
    resources.observe({"server_id": "a100-server"})
    result = resources.assessment({"server_id": "a100-server", "model_asset": "fixture"})
    assert not result["recommendation_ready"] and not result["suggested_gpu_ids"]
    assert any("占用未知" in item for item in result["limitations"])
    with pytest.raises(ValueError, match="占用未知"):
        proposal(resources)


@pytest.mark.parametrize("decision", ["rejected", "cancelled", "unavailable"])
def test_only_allowed_once_grants(tmp_path, decision):
    infra, r, _ = fixture(tmp_path)
    p = proposal(r)
    assert r.decide({**target(p), "decision": decision}, owner=True)["status"] == "denied"
    with pytest.raises(ValueError, match="未获"):
        r.activate(target(p), owner=True)
    with pytest.raises(ValueError, match="不能覆盖"):
        r.decide({**target(p), "decision": "allowed-once"}, owner=True)
    assert not infra.store.list()


def test_exact_authorization_is_idempotent_and_never_starts(tmp_path):
    infra, r, _ = fixture(tmp_path)
    p = proposal(r)
    with pytest.raises(ValueError, match="其他会话"):
        r.proposal(target(p), "different-root")
    with pytest.raises(ValueError, match="摘要"):
        r.decide({**target(p), "expected_digest": "0" * 64, "decision": "allowed-once"}, owner=True)
    t = approved(r, p)
    assert r.activate(target(p), owner=True)["task"]["id"] == t["id"]
    assert len(infra.store.list()) == 1 and t["state"]["status"] == "draft"
    assert t["state"]["session_id"] == "native-root" and not t["state"]["active_job"]
    assert r.check_task(t, "launch")["task_id"] == t["id"]
    forged = copy.deepcopy(t)
    forged["state"]["session_id"] = "other-session"
    with pytest.raises(ValueError, match="会话"):
        r.check_task(forged, "launch")
    with pytest.raises(ValueError, match="created only"):
        infra.create({"request_id": uuid.uuid4().hex, "plan": t["plan"]})
    stripped = {**t["plan"], "authorization_id": None}
    with pytest.raises(ValueError, match="移除授权"):
        infra.dispatch("infra-revise", t["state"]["session_id"], "owner", {"request_id": "strip-grant", "expected_revision": t["revision"], "plan": stripped})


def test_concurrent_activation_has_one_task_and_one_grant(tmp_path):
    infra, r, _ = fixture(tmp_path)
    p = proposal(r)
    r.decide({**target(p), "decision": "allowed-once"}, owner=True)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: r.activate(target(p), owner=True), range(2)))
    assert results[0]["task"]["id"] == results[1]["task"]["id"]
    with infra.store.db() as db:
        assert db.execute("SELECT count(*) FROM resource_grants").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["expiry", "revoke", "policy", "assets", "plan"])
def test_invalid_authority_blocks_start_but_allows_cleanup(tmp_path, change):
    infra, r, _ = fixture(tmp_path)
    t = approved(r, proposal(r))
    if change == "expiry":
        r.clock = lambda: time.time() + 3600
    elif change == "revoke":
        r.revoke({"grant_id": t["plan"]["authorization_id"]})
    elif change == "policy":
        s = r.registry.get("a100-server")
        r.registry.policy(
            {
                "server_id": s["id"],
                "expected_revision": s["revision"],
                "policy": {**s["policy"], "mode": "observe"},
            }
        )
    elif change == "assets":
        path = tmp_path / ".local/infra-assets.json"
        assets = json.loads(path.read_text())
        assets["images"][0]["digest"] = "sha256:" + "f" * 64
        path.write_text(json.dumps(assets))
    else:
        t["plan"]["gpu_ids"] = [0]
    with pytest.raises(ValueError):
        r.check_task(t, "launch")
    assert r.check_task(t, "stop") is None
    if change != "plan":
        with pytest.raises(ValueError):
            start(infra, t)


def test_unknown_or_insufficient_capacity_and_stale_snapshot_fail_closed(tmp_path):
    _, r, observation = fixture(tmp_path)
    observation["gpus"][7]["free_mib"] = None
    r.observe({"server_id": "a100-server"})
    with pytest.raises(ValueError, match="未知"):
        proposal(r)
    observation["gpus"][7]["free_mib"] = 100
    r.observe({"server_id": "a100-server"})
    with pytest.raises(ValueError, match="不足"):
        proposal(r)
    observation["gpus"][7]["free_mib"] = 70000
    r.observe({"server_id": "a100-server"})
    r.clock = lambda: time.time() + 601
    with pytest.raises(ValueError, match="过期"):
        proposal(r)


def test_policy_expansion_is_not_execution_permission(tmp_path):
    infra, r, observation = fixture(tmp_path)
    server = r.registry.get("a100-server")
    r.registry.policy(
        {
            "server_id": server["id"],
            "expected_revision": server["revision"],
            "policy": {**server["policy"], "approvable_gpu_ids": [0, 7]},
        }
    )
    assert infra.assets.read()["machines"][0]["allowed_gpu_ids"] == [7]
    with pytest.raises(ValueError, match="quota"):
        infra.create({"request_id": uuid.uuid4().hex, "plan": plan(gpu_ids=[0])})
    observation["gpu_processes"] = []
    r.observe({"server_id": "a100-server"})
    t = approved(r, proposal(r, plan=plan(gpu_ids=[0])))
    assert r.check_task(t, "launch") and t["plan"]["gpu_ids"] == [0]


def test_owner_only_policy_secrets_and_decisions(tmp_path):
    infra, r, _ = fixture(tmp_path)
    for endpoint in (
        "resource-save",
        "resource-policy",
        "resource-secret",
        "resource-trust",
        "resource-decision",
        "resource-revoke",
    ):
        with pytest.raises(ValueError, match="所有者"):
            r.dispatch(endpoint, "native-root", "infra-lead", {})
    with pytest.raises(ValueError, match="审批桥"):
        r.dispatch("resource-native-decision", "native-root", "infra-lead", {})
    with pytest.raises(ValueError):
        ResourceProposal.model_validate(
            {"request_id": "test-proposal", "plan": plan(), "reason": "测试审批", "approved": True}
        )


def test_worker_rechecks_revoke_after_queue_and_settles_without_engine(tmp_path, monkeypatch):
    infra, r, _ = fixture(tmp_path)
    t = start(infra, approved(r, proposal(r)))
    candidate = infra.candidate(
        t,
        {
            "request_id": "reviewed-candidate",
            "serving": t["plan"]["serving"],
            "reason": "冻结获批方案",
        },
    )
    job = infra.submit(t, "launch", candidate["id"])
    r.revoke({"grant_id": t["plan"]["authorization_id"]})
    made = []
    result = run(tmp_path, job["job_id"], engine_factory=lambda *args: made.append(args))
    assert not made and not result["passed"]
    assert infra.store.operation(job["job_id"])["status"] == "failed"
    assert infra.store.get(t["id"])["state"]["active_job"] is None


@pytest.mark.parametrize("invalid", ["expiry", "revoke"])
def test_unattended_completed_service_gets_one_cleanup_job(tmp_path, invalid):
    infra, resources, _ = fixture(tmp_path)
    task = start(infra, approved(resources, proposal(resources)))
    candidate = infra.candidate(task, {"request_id": "lease-candidate", "serving": task["plan"]["serving"], "reason": "测试闲置服务到期清理"})
    infra.store.update(task["id"], status="completed", service_candidate=candidate["id"])
    if invalid == "expiry":
        resources.clock = lambda: time.time() + 3600
    else:
        resources.revoke({"grant_id": task["plan"]["authorization_id"]})
    first = infra.reconcile_authority()["tasks"]
    assert first[0]["status"] == "cleanup_submitted"
    operation = infra.store.operation(first[0]["job_id"])
    assert operation["action"] == "stop"
    infra.reconcile_authority()
    with infra.store.db() as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
    # Failed cleanup requires owner review, not timer retries.
    infra.store.operation(operation["id"], "failed", error="identity requires review")
    again = infra.reconcile_authority()["tasks"]
    assert again[0]["status"] == "cleanup_requires_review"
    with infra.store.db() as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


def test_valid_lease_keeps_service_and_unknown_job_never_replays(tmp_path):
    infra, resources, _ = fixture(tmp_path)
    task = start(infra, approved(resources, proposal(resources)))
    candidate = infra.candidate(task, {"request_id": "unknown-candidate", "serving": task["plan"]["serving"], "reason": "验证未知操作不能重放"})
    infra.store.update(task["id"], status="completed", service_candidate=candidate["id"])
    assert not infra.reconcile_authority()["tasks"]
    job = infra.submit(task, "stop", candidate["id"])
    infra.store.operation(job["job_id"], "unknown")
    resources.revoke({"grant_id": task["plan"]["authorization_id"]})
    assert infra.reconcile_authority()["tasks"][0]["status"] == "awaiting_worker_or_recovery"
    assert infra.store.operation(job["job_id"])["status"] == "unknown"


def test_ssh_unknown_fingerprint_never_submits_credentials(tmp_path, monkeypatch):
    key = paramiko.RSAKey.generate(1024)
    auth = []

    class Transport:
        def __init__(self, _):
            pass

        def close(self):
            pass

        def start_client(self, timeout):
            pass

        def get_remote_server_key(self):
            return key

        def auth_password(self, *args):
            auth.append(args)

    monkeypatch.setattr(
        "pilot_infra.resource_transport.socket.create_connection",
        lambda *args, **kw: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr("pilot_infra.resource_transport.paramiko.Transport", Transport)
    transport = ResourceTransport(tmp_path)
    server = {
        "connection": {
            "transport": "ssh",
            "jumps": [],
            "target": {
                "host": "fixture",
                "port": 22,
                "username": "owner",
                "auth": "password",
                "credential_ref": None,
                "fingerprint": None,
            },
        }
    }
    with pytest.raises(ConnectionFailure) as error:
        with transport.connect(server):
            pass
    assert error.value.result["fingerprint"] == fingerprint(key) and not auth


def test_vault_catalog_has_no_secret_and_fixed_probe_is_self_contained(tmp_path):
    vault = ResourceVault(tmp_path)
    stored = vault.put(
        {"label": "test credential", "kind": "password", "value": "private-fixture-password"}
    )
    assert vault.get(stored["credential_ref"], "password") == "private-fixture-password"
    assert "private-fixture-password" not in json.dumps(vault.catalog())
    assert (
        "private-fixture-password"
        not in next((tmp_path / ".local/resources/secrets").glob("*.json")).read_text()
    )
    compile(probe_program(), "probe", "exec")
