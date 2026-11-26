"""Contract behavior fixtures; real sandbox/GPU evidence is recorded separately."""

import json
import sys
import uuid

import pytest
from test_pilot_infra import FakeEngine, create, plan, setup, start

from pilot_infra.engine_source import EngineSource, file_manifest, file_sha
from pilot_infra.presets import tools
from pilot_infra.store import digest
from pilot_infra.worker import run


def fixture(tmp_path, create_task=True):
    service = setup(tmp_path)
    assets = service.assets.read()
    assets["images"][0].update(backend="docker", engine="sglang", python=sys.executable)
    service.assets.path.write_text(json.dumps(assets))
    base = tmp_path / ".local/engine-source-inputs/fixture-engine"
    (base / "sglang/srt/layers").mkdir(parents=True)
    (base / "sglang/__init__.py").write_text('__version__="0.1"\n')
    (base / "sglang/srt/layers/sampler.py").write_text("def sample():\n    return 7\n")
    (base / "sglang/launch_server.py").write_text("pass\n")
    (base / "METADATA").write_text("Metadata-Version: 2.1\nName: sglang\nVersion: 0.1\n")
    recipe = dict(
        id="fixture-engine",
        label="fixture",
        backend="sglang-python-wheel",
        version="0.1",
        image_id="sglang-fixture",
        source_root=str(base),
        source_digest=digest(file_manifest(base)),
        build_timeout=60,
        sandbox_binary="/fixture/bwrap",
        sandbox_sha256="a" * 64,
        python_base_prefix=sys.base_prefix,
    )
    (tmp_path / ".local/engine-source-profiles.json").write_text(json.dumps({"profiles": [recipe]}))
    if not create_task:
        return service, EngineSource(service), None
    task = start(service, create(service, plan=plan(engine_source_profile="fixture-engine")))
    source = EngineSource(service)
    source.prepare(task)
    return service, source, task


def candidate(service, task, parent=None):
    return service.candidate(
        task,
        dict(
            request_id=uuid.uuid4().hex,
            reason="behavior fixture",
            parent_id=parent,
            serving=task["plan"]["serving"],
        ),
    )


def artifact(service, task, c):
    """Scripted build receipt for integrity/admission behavior, not a real wheel."""
    job = service.store.claim(
        task["id"], "engine_build", {"candidate_id": c["id"], "consumer_task_id": task["id"]}
    )
    base = service.store.path(task["id"], f"candidates/{c['id']}")
    (base / "runtime/sglang").mkdir(parents=True)
    (base / "runtime/sglang/__init__.py").write_text('__version__="0.1"\n')
    (base / "fixture.whl").write_bytes(b"behavior fixture, not a real wheel")
    value = dict(
        candidate_digest=c["digest"],
        source_digest=c["engine_source"]["source_digest"],
        job_id=job,
        wheel="fixture.whl",
        wheel_sha256=file_sha(base / "fixture.whl"),
        runtime="runtime",
        runtime_files=file_manifest(base / "runtime"),
        version="0.1",
    )
    (base / "engine-artifact.json").write_text(json.dumps(value))
    service.store.operation(job, "completed", result={"passed": True})
    return value


def approve(service, source, task, c):
    review = source.candidate_view(task, c)
    return source.decide(
        task,
        dict(
            candidate_id=c["id"], expected_digest=review["review_digest"], decision="allowed-once"
        ),
        "owner-web",
    )


def test_source_scope_hash_conflict_and_frozen_identity(tmp_path):
    service, source, task = fixture(tmp_path)
    read = source.read(task, {"path": "sglang/srt/layers/sampler.py"})
    assert source.search(task, {"text": "return 7"})["matches"][0]["line"] == 2
    source.patch(
        task,
        dict(
            path=read["path"],
            expected_sha256=read["sha256"],
            old_text="return 7",
            new_text="return 8",
        ),
    )
    with pytest.raises(ValueError, match="version conflict"):
        source.patch(
            task,
            dict(
                path=read["path"],
                expected_sha256=read["sha256"],
                old_text="return 8",
                new_text="return 9",
            ),
        )
    with pytest.raises(ValueError, match="only existing"):
        source.patch(
            task,
            dict(
                path="sglang/launch_server.py",
                expected_sha256="a" * 64,
                old_text="pass",
                new_text="exit()",
            ),
        )
    with pytest.raises(ValueError, match="escape"):
        source.read(task, {"path": "sglang/../../secret"})
    c = candidate(service, task)
    assert c["engine_source"]["changed_files"] == ["sglang/srt/layers/sampler.py"]
    assert (
        source.read(task, {"path": read["path"]})["sha256"]
        == c["engine_source"]["files"][read["path"]]
    )


@pytest.mark.parametrize("which", ["wheel", "runtime", "source"])
def test_approval_cannot_survive_artifact_tamper(tmp_path, which):
    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    artifact(service, task, c)
    approve(service, source, task, c)
    assert source.admitted(task, c)["version"] == "0.1"
    base = service.store.path(task["id"], f"candidates/{c['id']}")
    path = {
        "wheel": base / "fixture.whl",
        "runtime": base / "runtime/sglang/__init__.py",
        "source": base / "engine-source/sglang/srt/layers/sampler.py",
    }[which]
    path.write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        source.admitted(task, c)
    view = source.candidate_view(task, c)
    assert view["build"] == "unverified" and view["review_digest"] is None


def test_artifact_approval_is_exact_owner_outcome_and_final(tmp_path):
    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    artifact(service, task, c)
    with pytest.raises(ValueError, match="Owner approval"):
        service.submit(task, "launch", c["id"])
    review = source.candidate_view(task, c)
    args = dict(candidate_id=c["id"], expected_digest=review["review_digest"], decision="rejected")
    session = task["state"]["session_id"]
    with pytest.raises(ValueError, match="native approval bridge"):
        service.dispatch("infra-engine-native-decision", session, "infra-lead", args)
    service.dispatch("infra-engine-native-decision", session, "native-approval", args)
    with pytest.raises(ValueError, match="final"):
        source.decide(task, {**args, "decision": "allowed-once"}, "owner-web")
    assert source.decision(task, c) == "rejected"


def test_source_edit_permission_revocation_and_active_job(tmp_path):
    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    service.submit(task, "engine_build", c["id"])
    read = source.read(task, {"path": "sglang/srt/layers/sampler.py"})
    with pytest.raises(ValueError, match="while a Job"):
        source.patch(
            service.store.get(task["id"]),
            dict(
                path=read["path"],
                expected_sha256=read["sha256"],
                old_text="return 7",
                new_text="return 8",
            ),
        )
    server = service.resources.registry.get(task["plan"]["machine_id"])
    policy = {**server["policy"], "allow_source_edit": False}
    service.resources.registry.policy(
        {
            "server_id": server["id"],
            "expected_revision": server["policy_revision"],
            "policy": policy,
        }
    )
    with pytest.raises(ValueError, match="关闭源码"):
        service.resources.check_task(task, "source_write")


def test_manual_execution_is_owner_only_and_uses_same_task(tmp_path):
    service, source, task = fixture(tmp_path)
    session = task["state"]["session_id"]
    with pytest.raises(ValueError, match="authenticated Owner"):
        service.dispatch("infra-engine-manual", session, "infra-lead", {})
    result = service.dispatch("infra-engine-manual", session, "owner", {})
    assert result["task"]["state"]["execution_mode"] == "manual"
    assert len(service.store.list()) == 1
    returned = service.dispatch("infra-engine-agent", session, "owner", {})
    assert returned["task"]["state"]["execution_mode"] == "agent"


class ReloadEngine(FakeEngine):
    loads = []
    identities = {}

    def launch(self, cancelled):
        self.loads.append(self.candidate["id"])
        self.identity = {
            "passed": True,
            "checks": {"engine_artifact": True},
            "epoch": len(self.loads),
        }
        self.identities[self.candidate["id"]] = self.identity
        return super().launch(cancelled)

    def verify_identity(self):
        return self.identities.get(self.candidate["id"], super().verify_identity())


def test_rollback_reloads_and_retests_preserving_history(tmp_path):
    service, source, task = fixture(tmp_path)
    c1 = candidate(service, task)
    artifact(service, task, c1)
    approve(service, source, task, c1)
    ReloadEngine.loads = []
    for action in ["launch", "test"]:
        job = service.submit(task, action, c1["id"])
        run(service.root, job["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {})
    c2 = candidate(service, task, c1["id"])
    artifact(service, task, c2)
    approve(service, source, task, c2)
    job = service.submit(task, "launch", c2["id"])
    run(service.root, job["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {})
    current = service.store.get(task["id"])
    job = service.rollback(current, c1["id"])
    result = run(
        service.root, job["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {}
    )
    assert ReloadEngine.loads == ["c1", "c2", "c1"]
    assert result["rollback"] == {"from": "c2", "to": "c1", "retested": True}
    assert len(service.store.get(task["id"])["state"]["experiments"]) == 2
    assert service.store.operation(job["job_id"])["action"] == "rollback"


def test_invalid_rollback_does_not_reopen_completed_task(tmp_path):
    service, source, task = fixture(tmp_path)
    service.store.update(task["id"], status="completed", outcome={"passed": True})
    with pytest.raises(ValueError, match="no prior"):
        service.rollback(service.store.get(task["id"]), "c99")
    assert service.store.get(task["id"])["state"]["status"] == "completed"
    assert service.store.get(task["id"])["state"]["outcome"] == {"passed": True}


def test_engine_tools_are_complete_and_role_scoped():
    lead = {t["name"] for t in tools("infra-lead")}
    repo = {t["name"] for t in tools("repo-expert")}
    assert {"infra_engine_build", "infra_engine_request_approval", "infra_engine_rollback"} <= lead
    assert "infra_engine_source_patch" in repo and "infra_engine_build" not in repo


def test_gpu_authorization_resolves_uuid_instead_of_assuming_minor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pilot_infra.engine_source import gpu_devices

    root = tmp_path / "driver/pci-device-fixture"
    root.mkdir(parents=True)
    (root / "information").write_text("GPU UUID: GPU-approved-seven\nDevice Minor: 1\n")
    monkeypatch.setattr(
        "pilot_infra.engine_source.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="7, GPU-approved-seven\n1, GPU-other\n"),
    )
    assert gpu_devices([7], root.parent) == [{"index": 7, "uuid": "GPU-approved-seven", "minor": 1}]
    with pytest.raises(ValueError, match="Cannot prove"):
        gpu_devices([1], root.parent)


def test_worker_claim_is_atomic_and_never_replays(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    job = service.submit(task, "engine_build", c["id"])["job_id"]
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        try:
            service.store.start_operation(job)
            return "claimed"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result() for f in futures) == ["claimed", "rejected"]
    assert service.store.operation(job)["status"] == "running"


def test_approval_distinguishes_candidate_and_review_digests(tmp_path):
    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    artifact(service, task, c)
    with pytest.raises(ValueError, match="review_digest.*not candidate.digest"):
        source.review(task, dict(candidate_id=c["id"], expected_digest=c["digest"]))
    assert source.decision(task, c) is None


def test_manual_rollback_reopens_with_new_acceptance_and_compact_finish(tmp_path):
    service, source, task = fixture(tmp_path)
    c1 = candidate(service, task)
    artifact(service, task, c1)
    approve(service, source, task, c1)
    ReloadEngine.loads = []
    for action in ["launch", "test"]:
        j = service.submit(task, action, c1["id"])
        run(service.root, j["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {})
    c2 = candidate(service, task, c1["id"])
    artifact(service, task, c2)
    approve(service, source, task, c2)
    j = service.submit(task, "launch", c2["id"])
    run(service.root, j["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {})
    prior = {"passed": True, "current_candidate": "c2"}
    service.store.update(task["id"], status="completed", outcome=prior)
    j = service.rollback(service.store.get(task["id"]), c1["id"], manual=True)
    reopened = service.store.get(task["id"])
    assert reopened["state"]["execution_mode"] == "manual"
    assert reopened["state"]["outcome"] == {} and reopened["state"]["outcome_history"] == [prior]
    run(service.root, j["job_id"], engine_factory=ReloadEngine, adapter_loader=lambda *a: {})
    service.engine_factory = ReloadEngine
    receipt = service.finish(service.store.get(task["id"]))
    assert receipt["task"]["state"]["outcome"]["current_candidate"] == "c1"
    assert receipt["task"]["state"]["outcome"]["rollback"]["retested"] is True
    assert "candidates" not in receipt["task"]["state"] and len(json.dumps(receipt)) < 4000
    assert service.store.get(task["id"])["state"]["outcome"]["outcome_history"] == [prior]


def test_service_reload_and_exit_invalidate_current_acceptance(tmp_path, monkeypatch):
    service, source, task = fixture(tmp_path)
    c = candidate(service, task)
    old = {'candidate_id': c['id'], 'identity': {'pid': 42, 'start_ticks': '10'}}
    service.store.update(task['id'], service_candidate=c['id'], experiments=[old])
    monkeypatch.setattr('pilot_infra.process_engine.process_alive', lambda _: True)
    current = service.store.get(task['id'])
    assert service.current_acceptance(current, current)['current']
    monkeypatch.setattr('pilot_infra.process_engine.process_alive', lambda _: False)
    assert not service.current_acceptance(current, current)['current']
    service.store.event(task['id'], 'job_settled', {'action': 'launch', 'status': 'completed',
        'result': {'identity': {'pid': 43, 'start_ticks': '11'}}})
    monkeypatch.setattr('pilot_infra.process_engine.process_alive', lambda _: True)
    assert not service.current_acceptance(current, current)['current']
