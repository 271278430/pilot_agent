"""Managed download roots. Changing defaults never relocates existing artifacts."""

import hashlib
import json
from pathlib import Path


def resource_root(project):
    project = Path(project).resolve()
    from .config import host_settings

    saved = host_settings(project)
    return (
        Path(saved["applied"].get("resource_root", str(project / "resources")))
        if saved
        else project / "resources"
    )


def layout(project):
    base = resource_root(project)
    return {
        "root": str(base),
        "models": str(base / "models"),
        "sources": str(base / "sources"),
        "cache": str(base / "cache"),
        "exports": str(base / "exports"),
        "local_executor": str(Path(project).resolve() / "runs/nodes/pilot-executor"),
        "remote_executor": None,
        "remote_storage_requires_probe": True,
        "docker_storage": "Docker daemon-managed; this setting does not move DockerRootDir",
    }


def ensure_resource_root(project):
    base = resource_root(project)
    namespace = hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()
    for path in [base, *base.parents]:
        if path.is_symlink():
            raise ValueError("Resource root cannot contain symbolic links")
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    marker = base / ".pilot-resources.json"
    if marker.is_symlink():
        raise ValueError("Invalid resource ownership marker")
    if marker.exists():
        if json.loads(marker.read_text())["owner_namespace"] != namespace:
            raise ValueError("Resource root belongs to another Pilot project")
    elif any(base.iterdir()):
        raise ValueError("Use an empty private resource root; import existing assets separately")
    else:
        # Exclusive creation, never claim or overwrite someone else's directory.
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"version": 1, "owner_namespace": namespace}, stream)
        marker.chmod(0o600)
    return base
