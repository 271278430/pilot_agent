"""Safety and recovery behavior; real S5000 builds have separate evidence."""

import base64
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from pilot_infra import build_worker as worker
from pilot_infra.build_service import BuildService, SSHWorkbench


def owned(tmp_path):
    identity = "a" * 32
    root = tmp_path / ("pilot-workbench-" + identity)
    root.mkdir()
    (root / "lease.json").write_text(json.dumps({"task_id": identity, "root": str(root)}))
    profile = {
        "image_digest": "sha256:" + "b" * 64,
        "python": "/usr/bin/python3",
        "steps": [],
        "timeout": 20,
    }
    worker.perform(
        "prepare",
        root,
        identity,
        {"profile": profile, "files": {"engine/core.py": "value=1\n"}, "checks": {}},
    )
    return root, identity, profile


def test_remote_path_uses_posix_syntax_even_from_windows(tmp_path):
    remote = SSHWorkbench(tmp_path, {"remote_base": "/data"}, "a" * 32)
    assert remote.directory == "/data/pilot-workbench-" + "a" * 32
    assert "\\" not in remote.directory


def test_guest_cannot_mount_host_control_or_gpu_devices(tmp_path):
    root, identity, profile = owned(tmp_path)
    args = worker.container_args(root, identity, "c" * 32, profile, root / "jobs/work")
    assert "--device" not in args and "--gpus" not in args and "--privileged" not in args
    assert args[args.index("--network") + 1] == "none"
    mounts = [args[i + 1] for i, value in enumerate(args) if value == "-v"]
    assert len(mounts) == 2
    assert mounts[1].endswith(":/checks:ro")
    assert not any("lease.json" in m or ":/host" in m for m in mounts)
    assert args[args.index("--runtime") + 1] == "runc"


@pytest.mark.parametrize(
    "relative", ["../lease.json", "/etc/passwd", "C:\\data\\secret", "../checks/verify.py"]
)
def test_source_edit_cannot_escape_into_host_or_checks(tmp_path, relative):
    root, identity, _ = owned(tmp_path)
    before = (root / "lease.json").read_bytes()
    with pytest.raises(ValueError):
        worker.perform(
            "write", root, identity, {"path": relative, "content": "bad", "expected_sha256": None}
        )
    assert (root / "lease.json").read_bytes() == before


def test_stale_hash_does_not_overwrite_and_frozen_copy_survives_edit(tmp_path):
    root, identity, _ = owned(tmp_path)
    first = worker.perform("read", root, identity, {"path": "engine/core.py"})
    worker.perform("freeze", root, identity, {"candidate_id": "c1"})
    worker.perform(
        "write",
        root,
        identity,
        {"path": "engine/core.py", "content": "value=2\n", "expected_sha256": first["sha256"]},
    )
    with pytest.raises(ValueError, match="Source changed"):
        worker.perform(
            "write",
            root,
            identity,
            {"path": "engine/core.py", "content": "value=3\n", "expected_sha256": first["sha256"]},
        )
    assert (root / "candidates/c1/source/engine/core.py").read_text() == "value=1\n"
    assert (root / "source/engine/core.py").read_text() == "value=2\n"


def test_hardlink_cannot_enter_candidate(tmp_path):
    root, identity, _ = owned(tmp_path)
    external = tmp_path / "external.py"
    external.write_text("private=1")
    os.link(external, root / "source/linked.py")
    with pytest.raises(ValueError, match="hardlink"):
        worker.perform("freeze", root, identity, {"candidate_id": "c1"})


def test_cleanup_requires_ownership_and_preserves_neighbour(tmp_path):
    root, identity, _ = owned(tmp_path)
    neighbour = tmp_path / "unrelated"
    neighbour.mkdir()
    (neighbour / "original").write_text("keep")
    (root / "lease.json").write_text("{}")
    with pytest.raises(ValueError, match="ownership"):
        worker.perform("cleanup", root, identity, {})
    assert root.exists()
    (root / "lease.json").write_text(json.dumps({"task_id": identity, "root": str(root)}))
    assert worker.perform("cleanup", root, identity, {})["restored"]
    assert (neighbour / "original").read_text() == "keep"


def test_cleanup_never_stops_container_with_wrong_label(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Config": {"Labels": {"pilot.workbench": "other"}}}]),
            stderr="",
        )

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(ValueError, match="ownership"):
        worker.stop_owned("a" * 32, "b" * 32)
    assert len(calls) == 1 and calls[0][:2] == ["docker", "inspect"]


def test_daemon_failure_is_not_treated_as_container_absence(monkeypatch):
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="Cannot connect to Docker daemon"
        ),
    )
    with pytest.raises(ValueError, match="inspection unavailable"):
        worker.stop_owned("a" * 32, "b" * 32)


class FakeRemote:
    attempts = 0
    fail = False

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bootstrap(self):
        pass

    def call(self, action, args):
        if action == "prepare":
            return {"files": {"core.py": hashlib.sha256(b"original").hexdigest()}}
        if action == "freeze":
            return {
                "candidate_id": args["candidate_id"],
                "files": {"core.py": hashlib.sha256(b"original").hexdigest()},
            }
        if action == "read_candidate":
            return {
                "sha256": hashlib.sha256(b"original").hexdigest(),
                "bytes_total": 8,
                "content_base64": base64.b64encode(b"original"[args["offset"] :]).decode(),
            }
        if action == "run":
            type(self).attempts += 1
            if type(self).fail:
                raise TimeoutError("lost reply")
        if action == "cleanup":
            return {"restored": True}
        return {
            "status": "completed",
            "passed": True,
            "gpu_execution": False,
            "operation_id": args["operation_id"],
        }


def service(tmp_path):
    from pilot_infra.asset_service import AssetService
    from pilot_infra.resource_contracts import Authority
    from pilot_infra.resource_registry import ResourceRegistry

    local = tmp_path / ".local"
    local.mkdir(exist_ok=True)
    (local / "workbench-seeds").mkdir(exist_ok=True)
    (local / "workbench-seeds/seed.py").write_text("original")
    registry = ResourceRegistry(tmp_path)
    with registry.db() as db:
        db.execute(
            "INSERT INTO servers VALUES(?,?,?)",
            (
                "fixture-server",
                1,
                json.dumps(
                    {
                        "id": "fixture-server",
                        "label": "fixture",
                        "enabled": True,
                        "connection": {"transport": "local"},
                        "policy": Authority().model_dump(),
                        "policy_revision": 1,
                        "baseline_gpu_ids": [],
                    }
                ),
            ),
        )
    AssetService(tmp_path)
    with registry.db() as db:
        db.execute(
            "INSERT INTO fleet_executors VALUES(?,?)",
            (
                "fixture-server",
                json.dumps(
                    {
                        "server_id": "fixture-server",
                        "server_revision": 1,
                        "enabled": True,
                        "root": "/data/pilot-fixture",
                        "owner_namespace": "fixture",
                        "probe": {"worker_sha256": "f" * 64},
                    }
                ),
            ),
        )
    profile = {
        "id": "fixture",
        "label": "fixture",
        "server_id": "fixture-server",
        "image_digest": "sha256:" + "d" * 64,
        "python": "/usr/bin/python3",
        "timeout": 30,
        "steps": [{"stage": "test", "argv": ["python", "-V"], "timeout": 10}],
        "files": {"core.py": ".local/workbench-seeds/seed.py"},
        "checks": {},
    }
    (local / "workbench-profiles.json").write_text(json.dumps({"profiles": [profile]}))
    FakeRemote.attempts, FakeRemote.fail = 0, False
    return BuildService(tmp_path, remote_factory=FakeRemote)


def plan():
    return {
        "request_id": "reviewed-build-fixture",
        "title": "环境源码验证",
        "goal": "在隔离环境验证源码和构建",
        "profile_id": "fixture",
    }


def test_draft_cannot_build_and_wrong_root_cannot_edit(tmp_path):
    s = service(tmp_path)
    t = s.create(plan(), "root-one")
    with pytest.raises(ValueError, match="not prepared"):
        s.freeze(t["id"])
    with pytest.raises(ValueError, match="another root"):
        s.tool(
            "workbench_write",
            "root-two",
            "repo-expert",
            {"task_id": t["id"], "path": "core.py", "content": "bad"},
        )
    assert FakeRemote.attempts == 0


def test_lost_reply_is_recovered_without_relaunch_and_receipt_is_idempotent(tmp_path):
    s = service(tmp_path)
    t = s.create(plan(), "root-one")
    s.prepare(t["id"])
    s.freeze(t["id"])
    request = {"task_id": t["id"], "candidate_id": "c1", "request_id": "same-operation"}
    FakeRemote.fail = True
    with pytest.raises(TimeoutError):
        s.run(request)
    assert s.task(t["id"])["status"] == "unknown"
    with pytest.raises(ValueError, match="unsettled"):
        s.run(request)
    assert FakeRemote.attempts == 1
    s.recover(t["id"])
    assert s.run(request)["passed"] and FakeRemote.attempts == 1
    assert s.cleanup(t["id"])["restored"]
    assert s.task(t["id"])["results"]


def test_same_root_cannot_silently_acquire_a_second_build(tmp_path):
    s = service(tmp_path)
    s.create(plan(), "root-one")
    with pytest.raises(ValueError, match="already has"):
        s.create({**plan(), "request_id": "another-request"}, "root-one")


def test_partial_read_and_unique_patch_preserve_other_engine_text(tmp_path):
    root, identity, _ = owned(tmp_path)
    source = root / "source/engine/core.py"
    source.write_bytes(b"header=0\nvalue=1\nfooter=9\n")
    read = worker.perform(
        "read", root, identity, {"path": "engine/core.py", "start_line": 2, "max_lines": 1}
    )
    assert read["content"] == "value=1\n" and read["total_lines"] == 3
    request = {
        "path": "engine/core.py",
        "expected_sha256": read["sha256"],
        "old_text": "value=1",
        "new_text": "value=2",
    }
    worker.perform("patch", root, identity, request)
    assert source.read_text() == "header=0\nvalue=2\nfooter=9\n"
    with pytest.raises(ValueError, match="Source changed"):
        worker.perform("patch", root, identity, request)
    request.update(
        expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        old_text="=",
        new_text="bad",
    )
    with pytest.raises(ValueError, match="exactly one"):
        worker.perform("patch", root, identity, request)
    assert "bad" not in source.read_text()


def test_reviewed_executor_and_materials_survive_registry_changes(tmp_path):
    s = service(tmp_path)
    t = s.create(plan(), "root-one")
    (tmp_path / ".local/workbench-profiles.json").write_text('{"profiles":[]}')
    (tmp_path / ".local/workbench-seeds/seed.py").write_text("changed after review")
    snapshot = json.loads((tmp_path / ".local/build-executors" / (t["id"] + ".json")).read_text())
    assert snapshot["materials"]["files"]["core.py"] == "original"
    scope = s.task(t["id"])["execution_scope"]
    assert scope["directory"] == "/data/pilot-fixture/workspaces/pilot-workbench-" + t["id"]
    assert scope["gpu_ids"] == [] and scope["platform"] == "cpu"
    assert scope["cpu_cores"] == 1 and scope["memory_gib"] == 2
    assert scope["image_digest"] == snapshot["profile"]["image_digest"]
    assert not {"credential_ref", "server", "executor"}.intersection(scope)
    assert s.remote(t) is not None
    s.prepare(t["id"])
    assert s.cleanup(t["id"])["restored"]


def test_old_tasks_derive_scope_from_original_snapshot_without_rewriting_history(tmp_path):
    s = service(tmp_path)
    t = s.create(plan(), "root-one")
    with s.store.db() as db:
        stored = json.loads(
            db.execute("SELECT data FROM build_tasks WHERE id=?", (t["id"],)).fetchone()[0]
        )
        stored.pop("pipeline_stages")
        db.execute("UPDATE build_tasks SET data=? WHERE id=?", (json.dumps(stored), t["id"]))
    assert s.task(t["id"])["validation"]["expected_stages"] == ["test"]
    with s.store.db() as db:
        assert "pipeline_stages" not in json.loads(
            db.execute("SELECT data FROM build_tasks WHERE id=?", (t["id"],)).fetchone()[0]
        )


def test_late_build_receipt_cannot_reopen_a_cleaned_workspace(tmp_path):
    s = service(tmp_path)
    t = s.create(plan(), "root-one")
    s.prepare(t["id"])
    s.freeze(t["id"])
    result = s.run({"task_id": t["id"], "candidate_id": "c1", "request_id": "late-build-receipt"})
    s.cleanup(t["id"])
    s.settle(t["id"], result["operation_id"], result)
    assert s.task(t["id"])["status"] == "restored"
    with pytest.raises(ValueError, match="not prepared"):
        s.freeze(t["id"])
