from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath

from .environment import git_environment
from .storage import digest

EXCLUDED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "node_modules"}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=check,
        env=git_environment(),
    )


def clone(repo: Path, destination: Path, commit: str) -> str:
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    baseline = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").stdout.strip()
    subprocess.run(
        ["git", "-c", "core.longpaths=true", "-c", "core.autocrlf=false", "clone", "--no-hardlinks", "--", str(repo.resolve()), str(destination.resolve())],
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=90,
        env=git_environment(),
    )
    git(destination, "checkout", "--detach", baseline)
    return baseline


def files(repo: Path):
    for current, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(current) / d).is_symlink())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink():
                continue
            yield path


def snapshot(repo: Path) -> dict[str, str]:
    if os.name == "nt":
        # Git can create paths beyond MAX_PATH; Python's verifier must read
        # those same files through the extended namespace as well.
        absolute = str(repo.absolute())
        if not absolute.startswith("\\\\?\\"):
            repo = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
    result = {p.relative_to(repo).as_posix(): digest(p.read_bytes()) for p in files(repo)}
    for current, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED]
        for name in dirs + names:
            path = Path(current) / name
            if path.is_symlink():
                result[path.relative_to(repo).as_posix()] = "symlink:" + digest(os.readlink(path).encode())
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
    return result


def safe_path(repo: Path, relative: str) -> Path:
    if "\\" in relative or ":" in relative or "\x00" in relative:
        raise ValueError("Use a relative POSIX path without drive or backslash")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError("Path traversal or absolute path rejected")
    if any(part in EXCLUDED or part.startswith(".") for part in pure.parts):
        raise ValueError("Hidden/internal paths are protected")
    current = repo
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlink paths are forbidden")
    current.resolve().relative_to(repo.resolve())
    return current


def allowed_source(relative: str, roots: list[str] | None = None) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        return False
    if path.suffix != ".py" or any(p.startswith(".") or p.lower() in {"test", "tests", "testing", "fixtures"} for p in path.parts):
        return False
    if path.name in {"conftest.py", "setup.py", "noxfile.py"} or path.name.startswith("test_") or path.name.endswith("_test.py"):
        return False
    roots = roots or ["."]
    return any(root == "." or relative == root or relative.startswith(root.rstrip("/") + "/") for root in roots)


def patch(repo: Path) -> str:
    # Include new source files in the diff without staging their contents.
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\x00")
    for name in filter(None, untracked):
        if allowed_source(name):
            git(repo, "add", "--intent-to-add", "--", name)
    return git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD").stdout


def changes(before: dict, after: dict) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
