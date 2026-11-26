"""Read-only Docker inventory; secrets and arbitrary daemon commands are excluded."""

import inspect


def inventory(request):
    import hashlib
    import json
    import os
    import re
    import subprocess
    from pathlib import Path

    password = os.environ.get("PILOT_NODE_DOCKER_SUDO_PASSWORD")
    prefix = (
        ["sudo", "-S", "-p", "", "/usr/bin/docker"]
        if password
        else ["sudo", "-n", "/usr/bin/docker"]
        if os.environ.get("PILOT_NODE_DOCKER_USE_SUDO") == "1"
        else ["docker"]
    )

    def docker(args):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PILOT_NODE_DOCKER_SUDO_PASSWORD", "PILOT_NODE_DOCKER_USE_SUDO"}
        }
        result = subprocess.run(
            [*prefix, *args],
            input=password + "\n" if password else None,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        if result.returncode:
            raise ValueError("节点Docker访问失败或所选对象已变化")
        return result.stdout

    info = json.loads(docker(["info", "--format", "{{json .}}"]))
    owned = {}
    root = Path(request["root"]) if request.get("root") else None
    if root and root.is_dir() and not root.is_symlink():
        marker = root / "pilot-executor.json"
        if (
            marker.is_file()
            and json.loads(marker.read_text()).get("owner_namespace") == request["owner_namespace"]
        ):
            for path in (root / "services").glob("*.json"):
                if path.is_symlink():
                    continue
                record = json.loads(path.read_text())
                if record.get("container_id"):
                    owned[record["container_id"]] = {
                        "task_id": path.stem,
                        "kind": "inference",
                        "labels": record["labels"],
                    }
            for path in (root / "operations").glob("*.json"):
                if path.is_symlink():
                    continue
                record = json.loads(path.read_text()).get("adapter_container") or {}
                if record.get("container_id"):
                    owned[record["container_id"]] = {
                        "task_id": record["labels"].get("pilot.adapter.task"),
                        "kind": "adapter",
                        "labels": record["labels"],
                    }
            for workspace in list((root / "workspaces").glob("pilot-workbench-*"))[:256]:
                if workspace.is_symlink() or not re.fullmatch(
                    r"pilot-workbench-[a-f0-9]{32}", workspace.name
                ):
                    continue
                task_id = workspace.name.removeprefix("pilot-workbench-")
                lease = workspace / "lease.json"
                if (
                    not lease.is_file()
                    or lease.is_symlink()
                    or json.loads(lease.read_text()) != {"task_id": task_id, "root": str(workspace)}
                ):
                    continue
                for cid in list((workspace / "jobs").glob("*/container.cid"))[:256]:
                    if cid.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", cid.parent.name):
                        continue
                    identity = cid.read_text().strip()
                    if re.fullmatch(r"[a-f0-9]{64}", identity):
                        owned[identity] = {
                            "task_id": task_id,
                            "kind": "software",
                            "labels": {"pilot.workbench": task_id},
                        }
    ids = list(dict.fromkeys(docker(["image", "ls", "-q", "--no-trunc"]).splitlines()))
    images = []
    for start in range(0, min(len(ids), 256), 32):
        for image in json.loads(docker(["image", "inspect", *ids[start : start + 32]])):
            images.append(
                {k: image.get(k) for k in ("Id", "RepoTags", "RepoDigests", "Size", "Created")}
            )
    ids = docker(["ps", "-aq", "--no-trunc"]).splitlines()
    containers = []
    for start in range(0, min(len(ids), 512), 32):
        for container in json.loads(docker(["inspect", *ids[start : start + 32]])):
            expected = owned.get(container["Id"])
            ours = bool(
                expected
                and all(
                    container["Config"].get("Labels", {}).get(k) == v
                    for k, v in expected["labels"].items()
                )
            )
            containers.append(
                {
                    "id": container["Id"],
                    "name": container["Name"].lstrip("/"),
                    "image": container["Image"],
                    "state": container["State"]["Status"],
                    "started_at": container["State"].get("StartedAt"),
                    "owned": ours,
                    "task_id": expected["task_id"] if ours else None,
                    "kind": expected["kind"] if ours else "external",
                    "ports": container.get("NetworkSettings", {}).get("Ports", {}),
                }
            )
    storage = Path(info["DockerRootDir"])
    return {
        "daemon": {
            "version": info["ServerVersion"],
            "root": str(storage),
            "data_disk": storage.stat().st_dev != Path("/").stat().st_dev,
            "driver": info.get("Driver"),
            "runtimes": sorted(info.get("Runtimes", {})),
        },
        "images": images,
        "containers": containers,
        "machine_identity": hashlib.sha256(
            Path("/etc/machine-id").read_text().strip().encode()
        ).hexdigest(),
        "truncated": len(ids) > 512,
        "management_boundary": "外部容器仅查看；Pilot容器通过所属任务停止/恢复，不开放任意Docker命令",
    }


def program():
    return (
        inspect.getsource(inventory)
        + "\nimport json,sys\ntry:\n value=inventory(json.load(sys.stdin));print(json.dumps({'ok':True,'value':value}))\nexcept Exception as e:\n print(json.dumps({'ok':False,'error':str(e)[:500]}))"
    )
