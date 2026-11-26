"""Node storage and remote adapter boundaries, without GPU or real Docker."""

import base64
import hashlib
import json
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from pilot_infra import fleet_worker, node_storage, worker
from pilot_infra.fleet_contracts import ExecutorUpdate
from pilot_infra.store import AssetRegistry
from tests.test_fleet_assets import fleet
from tests.test_pilot_infra import plan


def test_mnt_data_path_is_supported_but_host_paths_and_system_paths_are_not():
    args = dict(server_id="node", expected_revision=1)
    assert ExecutorUpdate(**args, root="/mnt/nvme0/pilot-data/pilot-executor",
                          asset_root="/mnt/nvme0/pilot-data/resources")
    for path in [r"D:\pilot\runs\pilot-executor", "/etc/pilot-executor", "/mnt/../pilot-executor"]:
        with pytest.raises(ValueError):
            ExecutorUpdate(**args, root=path)


def storage_fixture(monkeypatch, *, system_path=False, symlink=False, foreign=False, docker_system=False):
    class NodePath(PurePosixPath):
        def stat(self):
            return SimpleNamespace(st_dev=2 if str(self).startswith("/mnt") else 1)

        def exists(self):
            return str(self) in {"/", "/mnt", "/mnt/nvme0", "/data", "/mnt/nvme0/docker"} or foreign and self.name == "pilot-executor"

        def is_dir(self):
            return self.exists()

        def is_symlink(self):
            return symlink and str(self) == "/mnt/nvme0"

        def is_file(self):
            return False

        def read_text(self):
            if str(self) == "/etc/machine-id":
                return "fixture-machine"
            if str(self) == "/sys/class/dmi/id/product_uuid":
                return "12345678-abcd-1234-abcd-123456789012"
            return "1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 8:2 / /mnt/nvme0 rw - ext4 /dev/data rw"

        def iterdir(self):
            return iter([self / "foreign.txt"] if foreign else [])

    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("pathlib.Path", NodePath)
    monkeypatch.setattr("os.access", lambda *a: True)
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=1024**3))
    info = {"DockerRootDir": "/data/docker" if docker_system else "/mnt/nvme0/docker", "ServerVersion": "fixture"}
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(info)))
    base = "/data" if system_path else "/mnt/nvme0"
    return {"namespace": "a" * 12, "owner_namespace": "owner", "resource_owner_namespace": "owner",
            "root": base + "/pilot-data/pilot-executor", "asset_root": base + "/pilot-data/resources"}


@pytest.mark.parametrize("case,reason", [("system_path", "系统盘"), ("symlink", "符号链接"),
                                       ("foreign", "不属于本项目"), ("docker_system", "Docker data-root")])
def test_storage_check_refuses_actual_unsafe_or_foreign_location(monkeypatch, case, reason):
    args = storage_fixture(monkeypatch, **{case: True})
    value = node_storage.inspect_node(args)
    assert not value["ready"]
    assert reason in " ".join(value["reasons"])


def test_storage_check_suggests_real_data_mount_and_does_not_create_files(monkeypatch):
    args = storage_fixture(monkeypatch)
    value = node_storage.inspect_node(args)
    assert value["ready"] and value["paths"]["root"]["device"] == 2
    assert value["suggested"]["root"].startswith("/mnt/nvme0/pilot-data/")
    assert all(p["data_disk"] for p in value["paths"].values())


def test_node_check_records_revision_and_does_not_enable_executor_or_policy(tmp_path):
    service, transport, server = fleet(tmp_path)
    before = service.registry.get(server["id"])
    result = service.inspect_executor(dict(server_id=server["id"], expected_revision=server["revision"],
                                          root="/mnt/nvme0/pilot-data/pilot-executor"))
    after = service.registry.get(server["id"])
    assert result["server_revision"] == after["revision"]
    assert before["policy"] == after["policy"]
    transport.inspect_storage = lambda *a: {"ready": False, "reasons": ["系统盘"]}
    with pytest.raises(ValueError, match="系统盘"):
        service.configure_executor(dict(server_id=server["id"], expected_revision=server["revision"],
                                        enabled=True, root="/data/pilot-other"))
    assert service.executor(server["id"])["root"] == "/data/pilot-test-executor"


def test_same_hardware_cannot_receive_two_independent_server_grants(tmp_path):
    service, transport, server = fleet(tmp_path)
    original = transport.inspect_storage
    transport.inspect_storage = lambda *a: {**original(*a), "node_identity": "d" * 64}
    request = dict(server_id=server["id"], expected_revision=server["revision"], root="/data/pilot-check")
    assert service.inspect_executor(request)["ready"]
    peer = service.registry.get(server["id"])
    peer.pop("revision")
    peer.update(id="same-node-alias", last_node_check=None)
    with service.registry.db() as db:
        db.execute("INSERT INTO servers VALUES(?,?,?)", (peer["id"], 1, json.dumps(peer)))
    duplicate = service.inspect_executor({**request, "server_id": peer["id"], "expected_revision": 1})
    assert not duplicate["ready"] and "重复" in " ".join(duplicate["reasons"])
    assert service.registry.get(server["id"])["last_node_check"]["ready"]


def test_changed_data_mount_blocks_worker_before_docker(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    fleet_worker.atomic(tmp_path / "pilot-executor.json", {"storage_devices": {
        "root": tmp_path.stat().st_dev + 1, "asset_root": tmp_path.stat().st_dev}})
    monkeypatch.setattr(fleet_worker, "docker", lambda *a, **k: pytest.fail("Changed mount cannot access Docker"))
    with pytest.raises(ValueError, match="mount changed"):
        fleet_worker.verify_storage(tmp_path)


def test_unimplemented_remote_source_load_is_rejected_before_build_or_launch(tmp_path, monkeypatch):
    registry = AssetRegistry(tmp_path)
    values = {"machines": [{"id": "node", "allowed_gpu_ids": [7], "executor_available": True}],
              "models": [{"id": "fixture", "server_id": "node", "manifest_digest": "d" * 64}],
              "images": [{"id": "sglang-fixture", "server_id": "node", "digest": "sha256:" + "a" * 64}]}
    monkeypatch.setattr(registry, "read", lambda: values)
    with pytest.raises(ValueError, match="尚不支持引擎源码制品加载"):
        registry.validate(plan(machine_id="node", engine_source_profile="source-profile"))


def test_host_remote_adapter_never_uses_host_docker(tmp_path, monkeypatch):
    source = b"def build_options():\n    return {'max_tokens': 16}\n"
    (tmp_path / "adapter.py").write_bytes(source)
    closed = []

    class Remote:
        def __init__(self, *args):
            pass

        def check_adapter(self, data):
            assert data == source
            return {"max_tokens": 16}

        def close(self):
            closed.append(True)

    monkeypatch.setattr("pilot_infra.remote_engine.RemoteEngine", Remote)
    monkeypatch.setattr(worker, "docker", lambda *a, **k: pytest.fail("Host Docker must not run for SSH nodes"))
    service = SimpleNamespace(store=SimpleNamespace(path=lambda *a: tmp_path / "adapter.py"),
                              assets=SimpleNamespace(read=lambda: {"machines": [{"id": "node", "transport": "ssh"}]}))
    owner = {"id": "task", "plan": {"machine_id": "node"}}
    candidate = {"id": "candidate", "source_sha256": hashlib.sha256(source).hexdigest()}
    assert worker.adapter_options(service, owner, candidate) == {"max_tokens": 16}
    assert closed == [True]
    candidate["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="freeze"):
        worker.adapter_options(service, owner, candidate)


@pytest.mark.parametrize("options", [{"max_tokens": True}, {"temperature": False},
                                     {"max_tokens": 2048}, {"stop": [""]}, {"extra": 1}])
def test_adapter_result_is_revalidated_by_trusted_code(options):
    with pytest.raises(ValueError):
        fleet_worker.validate_adapter_options(options)


@pytest.mark.parametrize("fault", [None, "devices", "network", "output"])
def test_node_adapter_sandbox_is_cpu_only_and_reclaims_its_own_container(tmp_path, monkeypatch, fault):
    source = b"def build_options():\n    return {}\n"
    op = "a" * 32
    fleet_worker.atomic(fleet_worker.journal(tmp_path, op), {})
    request = {"adapter_source": base64.b64encode(source).decode(), "task_id": "task",
               "candidate": {"source_sha256": hashlib.sha256(source).hexdigest(), "digest": "b" * 64},
               "image": {"digest": "sha256:" + "c" * 64}, "deadline": 9999999999}
    obj, calls = None, []

    def docker(args, **kw):
        nonlocal obj
        calls.append(args)
        if args[0] == "create":
            labels = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
                      for i, arg in enumerate(args) if arg == "--label"}
            obj = {"Id": "d" * 64, "Config": {"Labels": labels, "User": "65534:65534"}, "Image": request["image"]["digest"],
                   "HostConfig": {"Runtime": "runc", "NetworkMode": "none", "ReadonlyRootfs": True,
                                  "Memory": 256 * 1024**2, "NanoCpus": 1000000000, "PidsLimit": 32},
                   "Mounts": [{"Destination": "/source"}],
                   "State": {"Running": False, "ExitCode": 0}}
            if fault == "devices":
                obj["HostConfig"]["Devices"] = [{"PathOnHost": "/dev/mtgpu.0"}]
            if fault == "network":
                obj["HostConfig"]["NetworkMode"] = "bridge"
            return SimpleNamespace(stdout="d" * 64)
        return SimpleNamespace(stdout="x" * 16001 if fault == "output" else '{"max_tokens":16}\n')

    monkeypatch.setattr(fleet_worker, "docker", docker)
    monkeypatch.setattr(fleet_worker, "inspect", lambda _: obj)
    monkeypatch.setattr(fleet_worker, "identity", lambda: "node")
    monkeypatch.setattr(fleet_worker, "node_identity", lambda: "node")
    if fault:
        with pytest.raises(ValueError):
            fleet_worker.check_adapter(tmp_path, request, op)
    else:
        result = fleet_worker.check_adapter(tmp_path, request, op)
        assert result["options"] == {"max_tokens": 16} and not result["gpu_access"]
    command = calls[0]
    assert command[command.index("--runtime") + 1] == "runc"
    assert command[command.index("--network") + 1] == "none"
    assert not any(a in {"--gpus", "--device", "--privileged"} for a in command)
    assert calls[-1] == ["rm", "--force", "d" * 64]


def test_docker_missing_object_is_case_insensitive_and_access_failures_still_fail(monkeypatch):
    monkeypatch.setattr(fleet_worker, "docker", lambda *a, **k: SimpleNamespace(returncode=1, stderr="error: no such object: pilot-adapter"))
    assert fleet_worker.inspect("pilot-adapter") is None
    monkeypatch.setattr(fleet_worker, "docker", lambda *a, **k: SimpleNamespace(returncode=1, stderr="permission denied"))
    with pytest.raises(ValueError, match="identity"):
        fleet_worker.inspect("pilot-adapter")
