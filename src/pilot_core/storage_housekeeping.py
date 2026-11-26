"""Owner housekeeping, separated from runtime path and environment policy."""

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from repopilot.storage import atomic_json

from .storage import StorageLayout

ARCHIVABLE = re.compile(
    r"(?:infra|engine|domain|role)-qa-[a-f0-9]+(?:\.tar\.gz)?$"
    r"|.+-release(?:-final)?(?:-\d{8})?$"
    r"|harness-(?:host|ui)-backup-\d{8}$|t-[a-z0-9_]+$"
)


def tree_identity(path):
    """Hash bytes and link identities without traversing links to shared assets."""
    path = Path(path)
    entries = [path] if path.is_file() else sorted(path.rglob("*"))
    digest, size = hashlib.sha256(), 0
    for entry in entries:
        name = entry.relative_to(path).as_posix() if entry != path else "."
        digest.update(name.encode() + b"\0")
        if entry.is_symlink():
            digest.update(b"link:" + os.readlink(entry).encode())
        elif entry.is_file():
            size += entry.stat().st_size
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return {"sha256": digest.hexdigest(), "bytes": size}


def active_paths():
    paths = []
    proc = Path("/proc")
    if os.name == "nt" or not proc.is_dir():
        raise ValueError("Automatic archive checks require Linux process inventory")
    for item in proc.iterdir():
        if not item.name.isdecimal():
            continue
        try:
            command = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            try:
                cwd = (item / "cwd").resolve()
            except OSError:
                cwd = Path("/")
            try:
                environment = dict(p.split(b"=", 1) for p in (item / "environ").read_bytes().split(b"\0") if b"=" in p)
                for key in ("PYTHONPATH", "VIRTUAL_ENV", "DSH_HOME", "HOME", "HF_HOME", "UV_CACHE_DIR",
                            "PIP_CACHE_DIR", "TRITON_CACHE_DIR", "LD_LIBRARY_PATH"):
                    command += " " + environment.get(key.encode(), b"").decode(errors="replace")
            except OSError:
                pass
            paths.append((cwd, command))
        except (OSError, PermissionError):
            continue
    return paths


def archive_plan(project, identities=True):
    layout = StorageLayout(project)
    local = layout.path(".local")
    processes = active_paths()
    # Current private registrations can refer to old source copies. Never move those.
    references = []
    for file in local.iterdir():
        if file.is_file() and file.suffix in {".json", ".env"} and not file.is_symlink():
            references.append(file.read_text(encoding="utf-8", errors="replace"))
    for folder in (local / "harness-web/profiles", local / "model-preview", local / "workbench"):
        if folder.is_dir():
            references.extend(p.read_text(encoding="utf-8", errors="replace") for p in folder.rglob("*")
                              if p.is_file() and p.suffix in {".json", ".yml", ".env"} and not p.is_symlink())
    databases = list((local / "resources").glob("*.sqlite*"))
    databases += list((layout.root / "runs").glob("*/*.sqlite*"))
    for database in databases:
        if database.suffix not in {".sqlite", ".sqlite3"} or database.is_symlink():
            continue
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                for row in db.execute("SELECT * FROM " + quoted):
                    references.extend(value for value in row if isinstance(value, str))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    decoded = []
    for text in references:
        try:
            decoded.extend(strings(json.loads(text)))
        except ValueError:
            decoded.append(text)
    references = [text.replace("\\", "/") for text in decoded]
    result = []
    for path in sorted(local.iterdir()):
        if not ARCHIVABLE.fullmatch(path.name) or path.is_symlink():
            continue
        if any(cwd.is_relative_to(path) or str(path) in command for cwd, command in processes):
            continue
        if any(path.as_posix() in text for text in references):
            continue
        if time.time() - path.stat().st_mtime < 3600:
            continue
        result.append({"path": str(path), **(tree_identity(path) if identities else {})})
    return result


def archive(project, apply=False):
    layout = StorageLayout(project)
    plan = archive_plan(layout.root)
    result = {"version": 1, "project": str(layout.root), "applied": apply, "files": plan}
    if not apply or not plan:
        return result
    destination = layout.root.parent / "pilot-archives" / time.strftime("%Y%m%d-%H%M%S")
    if destination.parent.is_symlink() or destination.exists():
        raise ValueError("Archive destination is not private or already exists")
    destination.mkdir(parents=True, mode=0o700)
    # Journal precedes moves. Original paths remain discoverable and restorable.
    for row in plan:
        row["archive_path"] = str(destination / Path(row["path"]).name)
        row["moved"] = False
    atomic_json(destination / "manifest.json", result)
    for row in plan:
        current = {p["path"] for p in archive_plan(layout.root, identities=False)}
        if row["path"] not in current or tree_identity(row["path"]) != {
            "sha256": row["sha256"], "bytes": row["bytes"]
        }:
            raise ValueError("Archive source changed or became active")
        source, target = Path(row["path"]), Path(row["archive_path"])
        source.rename(target)
        if tree_identity(target) != {"sha256": row["sha256"], "bytes": row["bytes"]}:
            target.rename(source)
            raise ValueError("Archive identity check failed; source restored")
        row["moved"] = True
        atomic_json(destination / "manifest.json", result)
    result["manifest"] = str(destination / "manifest.json")
    atomic_json(layout.path(".local/storage/latest-archive.json"), result)
    return result


def migrate_caches(project, apply=False):
    """Move only known private caches to empty managed destinations; never merge."""
    layout = StorageLayout(project)
    layout.prepare()
    pairs = [(".local/uv-cache", ".cache/uv"), (".local/npm-cache", ".cache/npm"),
             (".local/tiktoken", ".cache/tiktoken"), (".ruff_cache", ".cache/ruff"),
             (".pytest_cache", ".cache/pytest")]
    for folder in ("src", "scripts", "tests"):
        pairs.extend((p.relative_to(layout.root).as_posix(),
                      ".cache/legacy-bytecode/" + p.relative_to(layout.root).as_posix())
                     for p in (layout.root / folder).rglob("__pycache__") if p.is_dir() and not p.is_symlink())
    result = {"version": 1, "applied": apply, "files": []}
    for source_name, target_name in pairs:
        source, target = layout.path(source_name), layout.path(target_name)
        if not source.is_dir() or source.is_symlink():
            continue
        row = {"source": str(source), "target": str(target), **tree_identity(source), "moved": False}
        if target.exists() and any(target.iterdir()):
            row["blocked"] = "Destination is not empty; preserve both caches"
        result["files"].append(row)
    if not apply:
        return result
    journal = layout.path(".local/storage/cache-migration.json")
    if journal.exists():
        # Keep every previous migration journal, including partial failures.
        previous = journal.with_name("cache-migration-" + str(time.time_ns()) + ".json")
        journal.rename(previous)
    atomic_json(journal, result)
    for row in result["files"]:
        if row.get("blocked"):
            continue
        source, target = Path(row["source"]), Path(row["target"])
        if any(cwd.is_relative_to(source) or str(source) in cmd for cwd, cmd in active_paths()):
            row["blocked"] = "Cache is referenced by an active process"
            atomic_json(journal, result)
            continue
        if tree_identity(source) != {"sha256": row["sha256"], "bytes": row["bytes"]}:
            raise ValueError("Cache changed during migration; preserve source")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()
        source.rename(target)
        if tree_identity(target) != {"sha256": row["sha256"], "bytes": row["bytes"]}:
            target.rename(source)
            raise ValueError("Cache migration identity mismatch; source restored")
        row["moved"] = True
        atomic_json(journal, result)
    return result
