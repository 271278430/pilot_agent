"""Business behavior fixtures; these are not actual SGLang/GPU evaluations."""

import base64
import copy
import gzip
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pilot_infra.contracts import InfraPlan, Workload
from pilot_infra.engine import SGLangEngine, model_manifest
from pilot_infra.process_engine import SGLangProcessEngine
from pilot_infra.service import InfraService
from pilot_infra.worker import run


def setup(root):
    model = root / "models/fixture"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"vocab_size":152000}')
    (model / "model.safetensors").write_bytes(b"fixture, not real weights")
    local = root / ".local"
    local.mkdir(exist_ok=True)
    assets = {
        "machines": [
            {"id": "a100-server", "allowed_gpu_ids": [7], "observed_gpu_ids": list(range(8))}
        ],
        "models": [
            {
                "id": "fixture",
                "path": str(model),
                "manifest_digest": model_manifest(model)["digest"],
            }
        ],
        "images": [{"id": "sglang-fixture", "digest": "sha256:" + "a" * 64}],
        "adapter_image": "sha256:" + "b" * 64,
    }
    (local / "infra-assets.json").write_text(json.dumps(assets))
    service = InfraService(root, engine_factory=FakeEngine, spawn_jobs=False)
    return service


def plan(**values):
    return InfraPlan.model_validate(
        {
            "title": "推理部署测试",
            "goal": "验证登记服务的身份和输出协议",
            "model_asset": "fixture",
            "image_asset": "sglang-fixture",
            **values,
        }
    ).model_dump()


def create(s, **values):
    p = values.pop("plan", plan())
    t = s.create({"request_id": uuid.uuid4().hex, "plan": p})["task"]
    s.store.bind("session-" + t["id"], t["id"])
    s.store.update(t["id"], session_id="session-" + t["id"])
    return s.store.get(t["id"])


def test_chat_plan_preserves_root_and_review_receipt_on_identical_retry(tmp_path):
    s = setup(tmp_path)
    args = {"request_id": "chat-plan-receipt", "plan": plan()}
    first = s.create(args, "native-chat-root")
    retry = s.create(args, "native-chat-root")
    assert retry["task"]["id"] == first["task"]["id"]
    assert retry["task"]["state"]["session_id"] == "native-chat-root"
    assert first["requires_start"] and retry["requires_start"]
    assert first["task"]["state"]["status"] == "draft"
    assert not first["task"]["state"]["active_job"]
    assert len(s.store.list()) == 1
    s.store.update(first["task"]["id"], status="paused")
    assert not s.create(args, "native-chat-root")["requires_start"]


def test_legacy_planning_binding_recovers_same_root_without_duplicate_task(tmp_path):
    s = setup(tmp_path)
    args = {"request_id": "legacy-chat-plan", "plan": plan()}
    t = s.create(args)["task"]
    s.store.bind("legacy-planning-root", t["id"], "planning")
    # Reproduce the earlier format, where the binding existed but the Task
    # lacked its canonical root identity.
    s.store.update(t["id"], session_id=None)
    repaired = s.create(args, "legacy-planning-root")
    assert repaired["task"]["id"] == t["id"]
    assert repaired["task"]["state"]["session_id"] == "legacy-planning-root"
    assert repaired["requires_start"]


def test_owner_can_cancel_an_unstarted_draft_without_gpu_operations(tmp_path):
    s = setup(tmp_path)
    task = s.create({"request_id": "cancel-draft-request", "plan": plan()}, "draft-root")["task"]
    assert "cancel" in s.actions(task)
    result = s.control(
        task["id"],
        "cancel",
        {"request_id": "cancel-draft-control", "expected_revision": task["revision"]},
    )
    assert result["task"]["state"]["status"] == "cancelled"
    assert not result["task"]["state"]["active_job"]
    assert not result["candidates"] and "start" not in result["actions"]
    assert s.store.binding("draft-root")["id"] == task["id"]
    with s.store.db() as db:
        assert not db.execute("SELECT id FROM operations").fetchall()
        assert not db.execute("SELECT resource FROM locks").fetchall()


def test_identical_request_from_another_chat_cannot_rebind_an_existing_task(tmp_path):
    s = setup(tmp_path)
    args = {"request_id": "original-chat-task", "plan": plan()}
    original = s.create(args, "original-root")["task"]
    with pytest.raises(ValueError, match="already has a root Session"):
        s.create(args, "another-root")
    assert s.store.get(original["id"])["state"]["session_id"] == "original-root"
    assert s.store.binding("another-root") is None
    assert len(s.store.list()) == 1


def test_concurrent_root_binding_commits_exactly_one_root(tmp_path):
    s = setup(tmp_path)
    t = s.create({"request_id": "concurrent-chat-roots", "plan": plan()})["task"]

    def bind(root_id):
        try:
            s.store.bind(root_id, t["id"], "planning")
            return root_id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [r for r in pool.map(bind, ["root-a", "root-b"]) if r]
    assert len(winners) == 1
    assert s.store.get(t["id"])["state"]["session_id"] == winners[0]
    assert sum(s.store.binding(r) is not None for r in ["root-a", "root-b"]) == 1


def start(s, t):
    return s.control(
        t["id"], "start", {"request_id": uuid.uuid4().hex, "expected_revision": t["revision"]}
    )["task"]


class FakeEngine:
    identity = {"passed": True, "container_id": "fixture", "started_at": "epoch-1"}

    def __init__(self, registry, task, candidate):
        self.candidate = candidate

    def verify_identity(self):
        return copy.deepcopy(self.identity)

    def launch(self, cancelled):
        return {"identity": self.verify_identity(), "endpoint": "fixture://no-real-service"}

    def stop(self):
        pass

    def logs(self):
        return "scripted-engine fixture"

    def test(self, adapter, cancelled, plan):
        return {
            "passed": True,
            "candidate_id": self.candidate["id"],
            "candidate_digest": self.candidate["digest"],
            "identity": self.verify_identity(),
            "gates": {
                "identity": True,
                "quality": True,
                "test_complete": True,
                "performance": True,
            },
            "quality": [],
            "cases": [],
            "limits": ["scripted behavior fixture"],
        }


def deploy_test(s, t, monkeypatch):
    monkeypatch.setattr(
        "pilot_infra.worker.machine_probe", lambda: {"scope": "whole-machine", "scripted": True}
    )
    c = s.candidate(
        t,
        {
            "request_id": uuid.uuid4().hex,
            "serving": t["plan"]["serving"],
            "reason": "创建可验收候选",
        },
    )
    for action in ("launch", "test"):
        job = s.submit(s.store.get(t["id"]), action, c["id"])
        run(s.root, job["job_id"], FakeEngine, lambda *args: {})
    return c


def test_whole_machine_discovery_is_separate_from_quota(tmp_path):
    s = setup(tmp_path)
    assert s.assets.read()["machines"][0]["observed_gpu_ids"] == list(range(8))
    assert s.validate(plan()).gpu_ids == [7]
    with pytest.raises(ValueError, match="quota"):
        s.create({"request_id": uuid.uuid4().hex, "plan": plan(gpu_ids=[6])})
    assert not s.store.list()


@pytest.mark.parametrize(
    "values",
    [
        {"gpu_ids": [7, 7]},
        {"kind": "test"},
        {"kind": "deploy_verify", "target_task_id": "a" * 32},
        {"kind": "compare", "target_task_id": "a" * 32},
        {"workload": {"input_lengths": [16384]}},
        {"unexpected": 1},
    ],
)
def test_contract_rejects_invalid_or_unsupported_task(values):
    with pytest.raises(ValidationError):
        plan(**values)


def test_workload_denominator_and_duplicate_limits():
    with pytest.raises(ValidationError):
        Workload(input_lengths=[16, 16])
    with pytest.raises(ValidationError):
        Workload(input_lengths=[16, 32, 64, 128], concurrency=[1, 2, 4, 8], requests_per_case=32)


def test_review_revision_budget_and_web_action_projection(tmp_path):
    s = setup(tmp_path)
    t = create(s)
    assert {"edit", "start"} <= set(s.view(t)["actions"])
    with pytest.raises(ValueError, match="stopped"):
        s.candidate(
            t,
            {
                "request_id": uuid.uuid4().hex,
                "serving": t["plan"]["serving"],
                "reason": "未经开始不得执行",
            },
        )
    changed = {**t["plan"], "title": "修改后的推理目标", "max_tokens": 123000}
    sid = t["state"]["session_id"]
    s.dispatch(
        "infra-revise",
        sid,
        "owner",
        {"request_id": "revision-one", "expected_revision": 1, "plan": changed},
    )
    revised = s.store.get(t["id"])
    assert revised["revision"] == 2 and revised["state"]["max_tokens"] == 123000
    with pytest.raises(ValueError, match="revision conflict"):
        s.dispatch(
            "infra-revise",
            sid,
            "owner",
            {"request_id": "revision-two", "expected_revision": 1, "plan": changed},
        )
    t = start(s, revised)
    assert "edit" not in s.view(t)["actions"] and "pause" in s.view(t)["actions"]
    with pytest.raises(ValueError, match="owner"):
        s.budget(
            t["id"],
            {"request_id": "budget-update", "expected_revision": 2, "max_tokens": None},
            "infra-lead",
        )
    s.budget(
        t["id"],
        {"request_id": "budget-update", "expected_revision": 2, "max_tokens": None},
        "owner",
    )
    assert json.loads(s.store.path(t["id"], "budget-policy.json").read_text())["max_tokens"] is None


def test_budget_sync_does_not_publish_an_older_task_snapshot(tmp_path):
    s = setup(tmp_path)
    stale = create(s)
    s.store.update(stale["id"], max_tokens=500, budget_revision=2)
    s.store.sync(stale)
    policy = json.loads(s.store.path(stale["id"], "budget-policy.json").read_text())
    assert policy == {"max_tokens": 500, "revision": 2}


def test_operation_claim_and_machine_device_locks(tmp_path):
    s = setup(tmp_path)
    a, b = create(s), create(s)
    a = start(s, a)
    with pytest.raises(ValueError, match="owned"):
        start(s, b)
    with ThreadPoolExecutor(2) as pool:

        def claim(_):
            try:
                return s.store.claim(a["id"], "launch", {})
            except ValueError:
                return None

        results = list(pool.map(claim, range(2)))
    assert sum(r is not None for r in results) == 1
    s.store.operation(next(r for r in results if r), "unknown")
    with pytest.raises(ValueError, match="existing operation"):
        s.store.claim(a["id"], "launch", {})


def test_source_scope_parent_candidate_and_frozen_identity(tmp_path):
    s = setup(tmp_path)
    t = start(s, create(s))
    sid = t["state"]["session_id"]
    read = s.call_tool("source_read", sid, "repo-expert", {})
    with pytest.raises(ValueError, match="outside"):
        s.call_tool("launch", sid, "repo-expert", {"candidate_id": "c1"})
    content = 'def build_options():\n    return {"max_tokens":64}\n'
    with pytest.raises(ValueError, match="not admitted"):
        s.call_tool(
            "source_write",
            sid,
            "repo-expert",
            {"expected_sha256": read["sha256"], "content": content},
        )
    s.store.event(
        t["id"],
        "job_settled",
        {
            "id": "a" * 32,
            "action": "adapter_check",
            "status": "failed",
            "candidate_id": "c1",
            "result": {"passed": False, "failure_layer": "adapter"},
        },
    )
    s.call_tool(
        "source_write", sid, "repo-expert", {"expected_sha256": read["sha256"], "content": content}
    )
    c = s.candidate(
        t,
        {
            "request_id": "candidate-one",
            "serving": t["plan"]["serving"],
            "reason": "冻结适配源码与配置",
        },
    )
    assert c["source_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(ValueError, match="version conflict"):
        s.call_tool(
            "source_write",
            sid,
            "repo-expert",
            {"expected_sha256": read["sha256"], "content": content},
        )
    with pytest.raises(ValueError, match="escape"):
        s.store.path(t["id"], "../../outside")


def test_finish_is_independent_and_invalidates_after_restart(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    deploy_test(s, t, monkeypatch)
    task = s.store.get(t["id"])
    job = task["state"]["experiments"][-1]["job_id"]
    assert s.evidence(task, {"path": f"jobs/{job}/result.json"})["sha256"]
    monkeypatch.setattr(
        FakeEngine, "identity", {"passed": True, "container_id": "fixture", "started_at": "epoch-2"}
    )
    with pytest.raises(ValueError, match="restarted"):
        s.finish(task)
    assert s.store.get(t["id"])["state"]["status"] == "running"


def test_false_success_and_evidence_tampering_cannot_finish(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    deploy_test(s, t, monkeypatch)
    task = s.store.get(t["id"])
    results = task["state"]["experiments"]
    results[-1]["gates"]["test_complete"] = False
    s.store.update(t["id"], experiments=results)
    with pytest.raises(ValueError, match="gates failed"):
        s.finish(s.store.get(t["id"]))
    results[-1]["gates"]["test_complete"] = True
    s.store.update(t["id"], experiments=results)
    s.store.path(t["id"], f"jobs/{results[-1]['job_id']}/result.json").write_text('{"passed":true}')
    with pytest.raises(ValueError, match="evidence changed"):
        s.finish(s.store.get(t["id"]))


def test_full_evidence_export_requires_owner_and_keeps_original_bytes(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    deploy_test(s, t, monkeypatch)
    task = s.store.get(t["id"])
    job = task["state"]["experiments"][-1]["job_id"]
    path = f"jobs/{job}/result.json"
    sid = task["state"]["session_id"]
    with pytest.raises(ValueError, match="owner"):
        s.dispatch("infra-evidence-download", sid, "infra-lead", {"path": path})
    result = s.dispatch("infra-evidence-download", sid, "owner", {"path": path})
    assert (
        gzip.decompress(base64.b64decode(result["data"]))
        == s.store.path(task["id"], path).read_bytes()
    )


def test_target_bound_test_and_comparison_keep_actual_deployment_identity(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    deploy_test(s, t, monkeypatch)
    s.finish(s.store.get(t["id"]))
    target = s.store.get(t["id"])
    new = create(s, plan=plan(kind="test", target_task_id=t["id"]))
    new = start(s, new)
    with pytest.raises(ValueError, match="cannot modify"):
        s.candidate(
            new,
            {
                "request_id": "bad-candidate",
                "serving": new["plan"]["serving"],
                "reason": "只读实验不得改候选",
            },
        )
    with pytest.raises(ValueError, match="quota"):
        s.validate(plan(kind="test", target_task_id=target["id"], gpu_ids=[6]))
    job = s.submit(new, "test", "c1")
    run(s.root, job["job_id"], FakeEngine, lambda *a: {})
    assert s.finish(s.store.get(new["id"]))["passed"]
    with pytest.raises(ValueError, match="another task"):
        s.evidence(target, {"path": f"jobs/{job['job_id']}/result.json"})


def test_strategy_changes_same_root_only_with_observed_source_hypothesis(tmp_path):
    s = setup(tmp_path)
    t = start(s, create(s))
    sid = t["state"]["session_id"]
    assert s.strategy(t)["mode"] == "single"
    s.store.event(t["id"], "observed_fault", {"error": "incorrect request serialization"})
    event = s.store.events(t["id"])[-1]
    s.call_tool(
        "diagnose",
        sid,
        "infra-lead",
        {
            "symptom": "请求协议异常导致没有输出",
            "cause": "请求适配源码可能传入错误参数",
            "evidence_ids": [str(event["seq"])],
            "experiment": "隔离运行适配源码并检查生成选项",
            "needs_source": True,
        },
    )
    task = s.store.get(t["id"])
    assert task["state"]["strategy"]["mode"] == "multi" and task["state"]["strategy"][
        "experts"
    ] == ["repo-expert"]
    assert task["state"]["session_id"] == sid


def test_engine_identity_checks_actual_image_mount_command_gpu_and_weights(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = s.candidate(
        t,
        {
            "request_id": "candidate-one",
            "serving": t["plan"]["serving"],
            "reason": "验证实际部署身份",
        },
    )
    e = SGLangEngine(s.assets, t, c)
    from pilot_infra import container_runtime
    from repopilot.storage import atomic_json

    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        container_runtime,
        "gpu_spec",
        lambda image, ids: ([], [], {"CUDA_VISIBLE_DEVICES": "GPU-fixture"}),
    )
    monkeypatch.setattr(e, "network", lambda: {"Id": "owned-net"})
    nonce = "frozen-nonce"
    _, mounts, command, env = e.layout(nonce)
    atomic_json(
        e.paths()[0] / "container.json",
        {
            "container_id": "real-inspect-fixture",
            "launch_nonce": nonce,
            "candidate_digest": c["digest"],
        },
    )
    info = {
        "Id": "real-inspect-fixture",
        "Image": "sha256:" + "a" * 64,
        "Config": {
            "Labels": {**e.labels(), "pilot.infra.launch": nonce},
            "Entrypoint": command[:1],
            "Cmd": command[1:],
            "User": "1000:1000",
            "Env": [k + "=" + v for k, v in env.items()],
        },
        "Mounts": [
            {"Type": "bind", "Destination": t, "Source": s, "RW": not ro} for s, t, ro in mounts
        ],
        "HostConfig": {
            "DeviceRequests": [{"DeviceIDs": ["GPU-fixture"]}],
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 20 * 1024**3,
            "MemorySwap": 20 * 1024**3,
            "NanoCpus": 8 * 10**9,
            "PidsLimit": 512,
            "NetworkMode": e.network_name(),
        },
        "State": {"Running": True, "StartedAt": "one"},
    }
    monkeypatch.setattr(e, "inspect", lambda: info)
    assert e.verify_identity()["passed"], e.verify_identity()
    for key, altered in [("Image", "wrong-image"), ("Id", "replacement-container")]:
        old = info[key]
        info[key] = altered
        assert not e.verify_identity()["passed"]
        info[key] = old
    info["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] = ["GPU-other"]
    assert not e.verify_identity()["passed"]
    info["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] = ["GPU-fixture"]
    info["Mounts"][0]["RW"] = True
    assert not e.verify_identity()["passed"]
    info["Mounts"][0]["RW"] = False
    info["Config"]["Cmd"] += ["--unexpected-option"]
    assert not e.verify_identity()["passed"]
    info["Config"]["Cmd"] = command[1:]
    (tmp_path / "models/fixture/model.safetensors").write_bytes(b"different weights")
    assert not e.verify_identity()["passed"]


def test_latest_candidate_and_pause_cannot_be_bypassed_with_stale_snapshot(tmp_path):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = s.candidate(
        t,
        {
            "request_id": "candidate-latest",
            "serving": t["plan"]["serving"],
            "reason": "冻结后使用旧调用快照",
        },
    )
    job = s.submit(t, "adapter_check", c["id"])
    assert s.store.operation(job["job_id"])["data"]["candidate_id"] == c["id"]
    s.store.operation(job["job_id"], "failed")
    s.reconcile(t["id"])
    s.control(
        t["id"], "pause", {"request_id": "owner-pause-now", "expected_revision": t["revision"]}
    )
    with pytest.raises(ValueError, match="stopped"):
        s.submit(t, "launch", c["id"])


def test_stream_requires_actual_content_usage_and_end_marker(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = s.candidate(
        t,
        {
            "request_id": "stream-candidate",
            "serving": t["plan"]["serving"],
            "reason": "检查流式协议完整性",
        },
    )
    e = SGLangEngine(s.assets, t, c)
    monkeypatch.setattr(e, "endpoint", lambda: "http://fixture.invalid")
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"content": "A"}}]},
        {"usage": {"prompt_tokens": 20, "completion_tokens": 2}},
    ]

    import httpx

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text="\n".join(lines) + "\n", headers={"content-type": "text/event-stream"}
    ))
    monkeypatch.setattr("pilot_infra.inference_http.httpx.AsyncClient",
                        lambda **kwargs: original(transport=transport, **kwargs))
    lines = ["data: " + json.dumps(v) for v in chunks] + ["data: [DONE]"]
    assert e.request({})["ok"]
    lines.pop()
    assert not e.request({})["ok"]
    lines = ["data: " + json.dumps(v) for v in chunks[:-1]] + ["data: [DONE]"]
    assert not e.request({})["ok"]


def test_cancelled_test_results_do_not_become_acceptance(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = deploy_test(s, t, monkeypatch)

    def stopped(self, adapter, cancelled, plan):
        s.store.update(t["id"], status="cancelled")
        return FakeEngine.test(self, adapter, cancelled, plan)

    class CancelEngine(FakeEngine):
        test = stopped

    before = len(s.store.get(t["id"])["state"]["experiments"])
    job = s.submit(t, "test", c["id"])
    run(s.root, job["job_id"], CancelEngine, lambda *a: {})
    assert s.store.operation(job["job_id"])["status"] == "cancelled"
    assert len(s.store.get(t["id"])["state"]["experiments"]) == before


def test_unknown_cleanup_keeps_job_and_prevents_replay_until_recovery(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    monkeypatch.setattr("pilot_infra.worker.machine_probe", lambda: {})
    c = s.candidate(
        t,
        {
            "request_id": "unknown-candidate",
            "serving": t["plan"]["serving"],
            "reason": "验证副作用无法结算的恢复",
        },
    )

    class UnknownEngine(FakeEngine):
        def launch(self, cancelled):
            raise RuntimeError("fixture launch failed")

        def stop(self):
            raise RuntimeError("fixture cleanup failed")

    job = s.submit(t, "launch", c["id"])
    run(s.root, job["job_id"], UnknownEngine, lambda *a: {})
    assert s.store.operation(job["job_id"])["status"] == "unknown"
    view = s.view(t)
    assert view["task"]["state"]["active_job"] == job["job_id"]
    assert "recover" in view["actions"] and "resume" not in view["actions"]
    with pytest.raises(ValueError, match="existing operation"):
        s.store.claim(t["id"], "launch", {})
    s.control(
        t["id"], "recover", {"request_id": "recover-existing", "expected_revision": t["revision"]}
    )
    assert s.store.operation(job["job_id"])["data"]["recovered"]
    assert s.store.get(t["id"])["state"]["active_job"] is None


def test_performance_failure_stays_in_denominator(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = s.candidate(
        t,
        {
            "request_id": "matrix-candidate",
            "serving": t["plan"]["serving"],
            "reason": "检查失败请求计量",
        },
    )
    e = SGLangEngine(s.assets, t, c)
    monkeypatch.setattr(e, "verify_identity", lambda: {"passed": True})
    count = 0

    def request(payload, native=False, **kw):
        nonlocal count
        if not native:
            return {"ok": True, "output": "A"}
        count += 1
        if count == 6:
            return {"ok": False, "error": "fixture timeout"}
        return {
            "ok": True,
            "input_tokens": len(payload["input_ids"]),
            "output_tokens": payload["sampling_params"]["max_new_tokens"],
            "ttft_ms": 10,
        }

    monkeypatch.setattr(e, "request", request)
    p = plan(workload={"input_lengths": [128], "concurrency": [1], "requests_per_case": 4})
    result = e.test({}, plan=p)
    assert result["cases"][0]["attempted"] == 4 and result["cases"][0]["successful"] == 3
    assert not result["gates"]["test_complete"] and not result["gates"]["performance"]


@pytest.mark.skipif(os.name == "nt", reason="Linux Unix socket path limit")
def test_sglang_ipc_uses_private_short_project_directory():
    with tempfile.TemporaryDirectory(prefix="", dir=Path.cwd()) as directory:
        root = Path(directory).resolve()
        engine = SGLangProcessEngine.__new__(SGLangProcessEngine)
        engine.store = SimpleNamespace(root=root)
        long_candidate = root / "runs/pilot-infra" / ("a" * 32) / "candidates/c1"
        long_candidate.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as sock:
            with pytest.raises(OSError):
                sock.bind(str(long_candidate / "tmp12345678"))
            ipc = engine.ipc_tempdir()
            assert ipc.resolve().is_relative_to(root)
            assert ipc.stat().st_mode & 0o777 == 0o700
            sock.bind(str(ipc / "tmp12345678"))


@pytest.mark.skipif(os.name == "nt", reason="Linux Unix socket path limit")
def test_sglang_ipc_rejects_escape_and_oversized_root(tmp_path):
    engine = SGLangProcessEngine.__new__(SGLangProcessEngine)
    engine.store = SimpleNamespace(root=tmp_path.resolve())
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".ipc").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes project"):
        engine.ipc_tempdir()
    (tmp_path / ".ipc").unlink()
    engine.store = SimpleNamespace(root=tmp_path / ("long-root-" * 10))
    engine.store.root.mkdir()
    with pytest.raises(ValueError, match="too long"):
        engine.ipc_tempdir()


@pytest.mark.skipif(os.name == "nt", reason="Linux process group ownership")
def test_stop_settles_owned_children_after_launcher_exit(tmp_path):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = s.candidate(
        t,
        {
            "request_id": "process-candidate",
            "serving": t["plan"]["serving"],
            "reason": "检查自有进程组残留",
        },
    )
    e = SGLangProcessEngine(s.assets, t, c)
    e.record.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-c",
        'import os,time;from pathlib import Path;p=os.fork();Path("child.txt").write_text(str(p)) if p else time.sleep(30)',
    ]
    p = subprocess.Popen(
        command,
        cwd=e.record.parent,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": "7"},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    p.wait(timeout=10)
    child = int((e.record.parent / "child.txt").read_text())
    e.record.write_text(
        json.dumps(
            {
                "pid": p.pid,
                "start_ticks": "exited",
                "task_id": t["id"],
                "candidate_digest": c["digest"],
                "port": 1,
            }
        )
    )
    try:
        e.stop()
        assert not e.record.exists()
        until = time.monotonic() + 5
        while time.monotonic() < until:
            path = Path("/proc") / str(child) / "stat"
            if not path.exists() or path.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.1)
        else:
            pytest.fail("Owned child survived explicit stop")
    finally:
        try:
            os.kill(child, 9)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("requested", ["auto", "multi", "single"])
def test_observed_adapter_failure_routes_repair_without_model_flag(
    tmp_path, monkeypatch, requested
):
    s = setup(tmp_path)
    t = start(s, create(s, plan=plan(strategy=requested)))
    c = deploy_test(s, t, monkeypatch)
    sid = "session-" + t["id"]

    def invalid_adapter(*args):
        raise ValueError("Adapter options require temperature=0")

    job = s.submit(t, "test", c["id"])
    run(s.root, job["job_id"], FakeEngine, invalid_adapter)
    result = s.store.operation(job["job_id"])
    assert result["status"] == "failed"
    view = s.view(t)
    settled = [e for e in view["events"] if e["kind"] == "job_settled"][-1]
    assert settled["data"]["result"]["failure_layer"] == "adapter"
    assert not view["task"]["state"]["hypotheses"]
    decision = view["task"]["state"]["strategy"]
    assert decision["mode"] == ("single" if requested == "single" else "multi")
    assert decision["experts"] == ([] if requested == "single" else ["repo-expert"])
    read = s.call_tool("source_read", sid, "infra-lead", {})
    edit = {"expected_sha256": read["sha256"], "content": "def build_options():\n    return {}\n"}
    if requested != "single":
        with pytest.raises(ValueError, match="Repo expert"):
            s.call_tool("source_write", sid, "infra-lead", edit)
        repaired = s.call_tool("source_write", sid, "repo-expert", edit)
    else:
        repaired = s.call_tool("source_write", sid, "infra-lead", edit)
    assert repaired["content"] == edit["content"]


def test_model_status_keeps_results_but_omits_raw_streams(tmp_path, monkeypatch):
    s = setup(tmp_path)
    t = start(s, create(s))
    c = deploy_test(s, t, monkeypatch)
    result = {
        "candidate_id": c["id"],
        "gates": {"quality": True},
        "quality": [{"passed": True, "output": "4", "events": ["large raw stream"]}],
        "cases": [{"successful": 3, "attempted": 4, "rows": ["raw"], "warmup": ["raw"]}],
    }
    s.store.update(t["id"], experiments=[result])
    s.store.event(t["id"], "job_settled", {"result": result})
    view = s.call_tool("status", "session-" + t["id"], "infra-lead", {})
    settled = [event for event in view["events"] if event["kind"] == "job_settled"][-1]
    for compact in [view["task"]["state"]["experiments"][-1], settled["data"]["result"]]:
        assert compact["quality"] == [{"passed": True, "output": "4"}]
        assert compact["cases"] == [{"successful": 3, "attempted": 4}]
    assert s.store.get(t["id"])["state"]["experiments"][-1] == result


def test_unstarted_deployment_is_not_an_experiment_target(tmp_path):
    s = setup(tmp_path)
    t = create(s)
    with pytest.raises(ValueError, match="no frozen deployment candidate"):
        s.validate(plan(kind="test", target_task_id=t["id"]))
