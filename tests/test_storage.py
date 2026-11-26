import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from pilot_core.storage import ENV_PATHS, StorageLayout
from pilot_core.storage_housekeeping import archive, archive_plan, migrate_caches, tree_identity


def test_managed_environment_replaces_host_defaults_and_keeps_credentials(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = StorageLayout(root).environment({"HOME": "/system", "TMPDIR": "/tmp", "NPM_CONFIG_CACHE": "/system/npm",
                                           "REPOPILOT_API_KEY": "fixture-private"})
    for key in ENV_PATHS:
        assert Path(env[key]).is_relative_to(root) and Path(env[key]).is_dir()
    assert env["REPOPILOT_API_KEY"] == "fixture-private"
    assert "NPM_CONFIG_CACHE" not in env
    assert "fixture-private" not in (root / ".local/storage/layout.json").read_text()


def old_folder(root, name):
    path = root / ".local" / name
    path.mkdir(parents=True)
    (path / "evidence.json").write_text('{"passed":false}')
    os.utime(path, (time.time() - 7200,) * 2)
    return path


def test_archives_only_inactive_unreferenced_copies_and_preserves_bytes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    StorageLayout(root).prepare()
    retired = old_folder(root, "infra-qa-abcdef")
    registered = old_folder(root, "engine-qa-123456")
    active = old_folder(root, "domain-qa-abcdef")
    protected = old_folder(root, "model-assets")
    database_reference = old_folder(root, "role-qa-123abc")
    registry = root / ".local/resources"
    registry.mkdir()
    with sqlite3.connect(registry / "registry.sqlite3") as db:
        db.execute("CREATE TABLE assets(path TEXT)")
        db.execute("INSERT INTO assets VALUES(?)", (str(database_reference),))
    (root / ".local/registered.json").write_text(json.dumps({"source": str(registered)}))
    monkeypatch.setattr("pilot_core.storage_housekeeping.active_paths", lambda: [(active, "fixture process")])
    identity = tree_identity(retired)
    preview = archive(root)
    assert preview["applied"] is False and [r["path"] for r in preview["files"]] == [str(retired)]
    result = archive(root, apply=True)
    target = Path(result["files"][0]["archive_path"])
    assert tree_identity(target) == identity and not retired.exists()
    assert active.exists() and registered.exists() and protected.exists() and database_reference.exists()
    assert json.loads(Path(result["manifest"]).read_text())["files"][0]["moved"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink safety")
def test_managed_storage_rejects_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".cache").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symbolic"):
        StorageLayout(root).prepare()


def test_recent_test_directories_are_not_archived(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    StorageLayout(root).prepare()
    (root / ".local/t-recent").mkdir()
    monkeypatch.setattr("pilot_core.storage_housekeeping.active_paths", list)
    assert archive_plan(root) == []


def test_cache_migration_keeps_bytes_and_never_merges_or_overwrites(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    StorageLayout(root).prepare()
    source = root / ".local/tiktoken"
    source.mkdir()
    (source / "encoding").write_bytes(b"frozen encoding")
    before = tree_identity(source)
    other = root / ".local/npm-cache"
    other.mkdir()
    (other / "old").write_bytes(b"old")
    (root / ".cache/npm/new").write_bytes(b"new")
    monkeypatch.setattr("pilot_core.storage_housekeeping.active_paths", list)
    result = migrate_caches(root, apply=True)
    assert tree_identity(root / ".cache/tiktoken") == before and not source.exists()
    assert other.exists() and (root / ".cache/npm/new").read_bytes() == b"new"
    assert any(row.get("blocked") for row in result["files"])
