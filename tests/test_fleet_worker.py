"""Portable journal/manifest behavior, no actual GPU claims."""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pilot_infra import fleet_worker as worker


def model(directory):
    directory.mkdir()
    (directory / "model.safetensors").write_bytes(b"fixture not a runnable model")
    (directory / "config.json").write_text(json.dumps({"model_type": "qwen3"}))
    (directory / "tokenizer.json").write_text("{}")
    return directory


def test_manifest_rejects_partial_shards_and_outside_symlinks(tmp_path):
    directory = model(tmp_path / "model")
    manifest = worker.model_manifest(directory)
    assert manifest["digest"] and len(manifest["files"]) == 3
    (directory / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"weight": "missing.safetensors"}})
    )
    with pytest.raises(ValueError, match="shards"):
        worker.model_manifest(directory)
    (directory / "model.safetensors.index.json").unlink()
    if os.name != "nt":
        (directory / "foreign.safetensors").symlink_to(tmp_path / "outside")
        (tmp_path / "outside").write_bytes(b"outside")
        with pytest.raises(ValueError, match="symlink"):
            worker.model_manifest(directory)


@pytest.mark.skipif(os.name == "nt", reason="Node journal uses Linux fcntl and /proc")
def test_native_detached_import_survives_submit_exit_and_rejects_replay(tmp_path):
    root = tmp_path / "pilot-executor"
    root.mkdir()
    source = model(tmp_path / "model")
    (root / "pilot-executor.json").write_text(
        json.dumps({"machine_identity": worker.identity(), "owner_namespace": "fixture"})
    )
    operation_id = uuid.uuid4().hex
    request = {
        "action": "asset_prepare",
        "kind": "model",
        "asset_id": "model",
        "source": {"kind": "existing", "location": str(source)},
        "machine_identity": worker.identity(),
        "boot_id": worker.boot(),
        "deadline": time.time() + 30,
    }
    command = [sys.executable, str(Path(worker.__file__).resolve()), "submit", str(root)]
    submitted = subprocess.run(
        command,
        input=json.dumps({"operation_id": operation_id, "request": request}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(submitted.stdout)["ok"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = worker.dispatch(root, "status", {"operation_id": operation_id})
        if row["status"] not in {"submitted", "running"}:
            break
        time.sleep(0.02)
    assert (
        row["status"] == "completed"
        and row["result"]["manifest"]["digest"] == worker.model_manifest(source)["digest"]
    )
    first_pid = row["pid"]
    same = worker.dispatch(root, "submit", {"operation_id": operation_id, "request": request})
    assert same["pid"] == first_pid
    with pytest.raises(ValueError, match="reused"):
        worker.dispatch(
            root,
            "submit",
            {"operation_id": operation_id, "request": {**request, "asset_id": "changed"}},
        )
    assert source.exists()


def test_foreign_container_is_never_stopped(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    task_id = uuid.uuid4().hex
    worker.atomic(
        worker.state_path(root, task_id),
        {
            "name": "owned",
            "labels": {"pilot.infra.task": task_id},
            "container_id": "expected",
            "resources": [],
        },
    )
    monkeypatch.setattr(
        worker,
        "inspect",
        lambda _: {"Id": "foreign", "Config": {"Labels": {"pilot.infra.task": "another"}}},
    )
    calls = []
    monkeypatch.setattr(worker, "docker", lambda args, **_: calls.append(args))
    with pytest.raises(ValueError, match="Foreign"):
        worker.stop_service(root, task_id)
    assert calls == []


@pytest.mark.parametrize("corrupt", [False, True, "low_disk"])
def test_fixed_download_checks_bytes_hash_and_reuses_verified_cache(tmp_path, monkeypatch, corrupt):
    contents = {
        "config.json": b'{"model_type":"llama"}',
        "tokenizer.json": b"{}",
        "model.safetensors": b"fixture weights",
    }
    files = [
        {"path": name, "bytes": len(data), "hash": hashlib.sha256(data).hexdigest()}
        for name, data in contents.items()
    ]
    manifest = {
        "revision": "a" * 40,
        "bytes": sum(len(b) for b in contents.values()),
        "files": files,
    }
    monkeypatch.setattr(worker, "resolve_model", lambda *_: manifest)
    calls = []
    if corrupt == "low_disk":
        from types import SimpleNamespace

        readings = iter([1024**3, 64])
        monkeypatch.setattr(
            worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(readings))
        )

    def fetch(request, **_):
        name = request.full_url.rsplit("/", 1)[-1]
        calls.append(name)
        data = contents[name]
        return io.BytesIO(b"x" * len(data) if corrupt is True else data)

    monkeypatch.setattr(worker.urllib.request, "urlopen", fetch)
    op = uuid.uuid4().hex
    worker.atomic(worker.journal(tmp_path, op), {"operation_id": op})
    request = {
        "source": {
            "kind": "huggingface",
            "endpoint": "https://huggingface.co",
            "location": "owner/model",
            "revision": "a" * 40,
        },
        "asset_id": "model",
        "max_bytes": 1024,
        "deadline": time.time() + 30,
    }
    if corrupt:
        with pytest.raises(
            ValueError, match="reserve exhausted" if corrupt == "low_disk" else "hash mismatch"
        ):
            worker.download_model(tmp_path, request, op)
        assert not list((tmp_path / "assets").rglob("*.safetensors"))
    else:
        first = worker.download_model(tmp_path, request, op)
        second = worker.download_model(tmp_path, request, op)
        assert first == second and len(calls) == len(files)
        assert first["manifest"]["bytes"] == manifest["bytes"]
