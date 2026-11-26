"""Bounded multi-file delivery workspaces; never run repository hooks."""

import os
from pathlib import Path

DENIED = {".git", ".local", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
EXTENSIONS = {".py", ".html", ".css", ".js", ".mjs", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".svg"}
PROTECTED = {"model_client.py", "README.md"}


def admitted(relative, *, write=False):
    path = Path(relative)
    if (not relative or path.is_absolute() or ".." in path.parts
            or any(p.startswith(".") or p in DENIED for p in path.parts)
            or "\\" in relative or ":" in relative
            or path.suffix.lower() not in EXTENSIONS
            or any(word in path.name.lower() for word in ("secret", "credential", ".env"))):
        raise ValueError("File outside admitted delivery source")
    if write and (relative in PROTECTED or any(p.lower() in {"tests", "test"} for p in path.parts)
                  or path.name.startswith("test_") or path.name == "conftest.py"
                  or path.stem.endswith("_test") or ".test." in path.name or ".spec." in path.name):
        raise ValueError("Contract, client and repository tests are protected")
    return path


def source_files(directory, *, evidence=False):
    """Stable file catalogue; skipped metadata never enters deploy snapshots."""
    directory = Path(directory)
    result = []
    total = 0
    if directory.is_symlink():
        raise ValueError("Source directory symlink")
    paths = []
    for current, directories, filenames in os.walk(directory, followlinks=False):
        for name in [*directories, *filenames]:
            if (Path(current) / name).is_symlink():
                raise ValueError("Source symlink is not allowed")
        directories[:] = [name for name in directories if not name.startswith(".") and name not in DENIED]
        paths.extend(Path(current) / name for name in filenames)
    for path in sorted(paths):
        relative = path.relative_to(directory).as_posix()
        # Resolve and inspect ancestors at every use, including excluded paths.
        if path.is_symlink():
            raise ValueError("Source symlink is not allowed")
        if not path.is_file():
            continue
        try:
            admitted(relative)
        except ValueError:
            continue
        if not evidence and any(p.lower() in {"tests", "test"} for p in path.relative_to(directory).parts):
            continue
        if not evidence and relative == "README.md":
            continue
        size = path.stat().st_size
        total += size
        if size > 512000 or total > 8000000 or len(result) >= 500:
            raise ValueError("Delivery source exceeds admitted file/byte budget")
        result.append(relative)
    return result


def checked_path(directory, relative, *, write=False):
    path = Path(directory) / admitted(relative, write=write)
    current = path
    while current != Path(directory):
        if current.is_symlink():
            raise ValueError("Source symlink is not allowed")
        current = current.parent
    if not path.resolve().is_relative_to(Path(directory).resolve()):
        raise ValueError("Source path escape")
    return path
