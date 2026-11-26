"""Frozen OCI runtime contract. Never install host software or fall back to a process."""

import hashlib
import json
import os
import re
from pathlib import Path

from pilot_core.docker import docker


def admit_backend(image, backend):
    allowed = (
        {"auto", "flash_attn", "torch_native"}
        if image.get("engine") == "vllm"
        else {"triton", "torch_native"}
    )
    if image.get("stack_recipe"):
        allowed = set(image["stack_recipe"]["attention_backends"])
    if backend not in allowed:
        raise ValueError(
            f"Attention backend {backend} is not supported by {image.get('engine', 'sglang')}; choose {', '.join(sorted(allowed))}"
        )


def runtime_digest(image):
    from .store import digest

    contract = {
        "contract_version": 1,
        "engine": image.get("engine", "sglang"),
        "backend": image.get("backend", "docker"),
        "gpu_mode": image.get("gpu_mode", "nvidia"),
        "driver_mounts": image.get("driver_mounts", []),
    }
    if image.get("server_id"):
        contract.update(
            server_id=image["server_id"],
            stack_recipe=image["stack_recipe"],
            asset_revision=image.get("fleet_asset_revision"),
        )
    return digest(contract)


def gpu_spec(image, gpu_ids):
    from .engine_source import gpu_devices

    mapped = gpu_devices(gpu_ids)
    uuids = ",".join(g["uuid"] for g in mapped)
    visible = uuids
    visibility = {
        "CUDA_VISIBLE_DEVICES": visible,
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "PILOT_GPU_UUIDS": uuids,
    }
    mode = image.get("gpu_mode", "nvidia")
    if mode == "nvidia":
        return ["--gpus", '"device=' + uuids + '"'], [], visibility
    if mode != "device-bind":
        raise ValueError("Unregistered Docker GPU runtime mode")
    devices = [
        "/dev/nvidiactl",
        "/dev/nvidia-uvm",
        *["/dev/nvidia" + str(g["minor"]) for g in mapped],
    ]
    if Path("/dev/nvidia-uvm-tools").exists():
        devices.append("/dev/nvidia-uvm-tools")
    args = []
    for path in devices:
        if not Path(path).exists():
            raise ValueError("Authorized GPU device missing")
        args += ["--device", path + ":" + path + ":rw"]
    mounts = []
    for item in image.get("driver_mounts", []):
        source = Path(item["source"])
        target = item["target"]
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_uid != 0
            or not source.resolve().is_relative_to(Path("/usr/lib"))
            or not re.fullmatch(
                r"/pilot-driver/lib(?:cuda|nvidia-[a-z0-9-]+)\.so(?:\.[0-9]+)*", target
            )
            or not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", ""))
            or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ValueError("Driver injection must use frozen read-only system libraries")
        mounts.append((str(source), target, True))
    if not any(t == "/pilot-driver/libcuda.so.1" for _, t, _ in mounts):
        raise ValueError("CUDA driver library is not registered")
    if not any(t == "/pilot-driver/libcuda.so" for _, t, _ in mounts):
        source = next(s for s, t, _ in mounts if t == "/pilot-driver/libcuda.so.1")
        # Triton's image-local linker needs -lcuda; the identical frozen system
        # driver is mounted read-only under its unversioned link name as well.
        mounts.append((source, "/pilot-driver/libcuda.so", True))
    env = {
        **visibility,
        "LD_LIBRARY_PATH": "/pilot-driver:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
    }
    return args, mounts, env


def inspect(name):
    result = docker(["inspect", name], check=False)
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise ValueError("Cannot inspect Docker identity: " + result.stderr[-500:])
    return json.loads(result.stdout)[0]


def image_admitted(image):
    if image.get("backend", "docker") != "docker":
        raise ValueError(
            "Model serving requires a Docker container; host-process fallback is disabled"
        )
    if image.get("engine", "sglang") not in {"sglang", "vllm"}:
        raise ValueError("Engine has no admitted container adapter")
    result = docker(["image", "inspect", image["digest"]], check=False)
    if result.returncode or json.loads(result.stdout)[0]["Id"] != image["digest"]:
        raise ValueError("Frozen engine image unavailable; register/pull it before starting")


def base_args(
    name, image, labels, limits, mounts, command, env=None, network="none", serving=False
):
    image_admitted(image)
    args = [
        "create",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        str(limits.get("memory_gib", 20)) + "g",
        "--memory-swap",
        str(limits.get("memory_gib", 20)) + "g",
        "--cpus",
        str(limits.get("cpu_cores", 8)),
        "--network",
        network,
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=512m",
        "--shm-size",
        "2g",
        "--entrypoint",
        command[0],
    ]
    for key, value in sorted(labels.items()):
        args += ["--label", key + "=" + value]
    for source, target, readonly in mounts:
        if "," in source or "," in target:
            raise ValueError("Mount path contains ambiguous delimiter")
        args += [
            "--mount",
            "type=bind,src=" + source + ",dst=" + target + (",readonly" if readonly else ""),
        ]
    for key, value in sorted((env or {}).items()):
        args += ["--env", key + "=" + str(value)]
    if serving:
        args += ["--publish", "127.0.0.1::30000"]
    return args + [image["digest"], *command[1:]]


def contract_checks(obj, image, labels, limits, mounts, command, env, network):
    host = obj.get("HostConfig") or {}
    config = obj.get("Config") or {}
    actual_mounts = {
        m["Destination"]: (m["Source"], not m["RW"])
        for m in obj.get("Mounts", [])
        if m.get("Type") == "bind"
    }
    actual_env = dict(v.split("=", 1) for v in config.get("Env", []) if "=" in v)
    return {
        "image": obj.get("Image") == image["digest"],
        "labels": all((config.get("Labels") or {}).get(k) == v for k, v in labels.items()),
        "command": config.get("Entrypoint") == command[:1] and config.get("Cmd") == command[1:],
        "mounts": actual_mounts == {t: (s, ro) for s, t, ro in mounts},
        "environment": all(actual_env.get(k) == str(v) for k, v in env.items()),
        "isolation": host.get("ReadonlyRootfs") is True
        and not host.get("Privileged")
        and not host.get("CapAdd")
        and host.get("PidMode", "") != "host"
        and host.get("IpcMode", "") != "host"
        and set(host.get("CapDrop") or []) == {"ALL"}
        and set(host.get("SecurityOpt") or [])
        in ({"no-new-privileges"}, {"no-new-privileges:true"}),
        "user": config.get("User") == f"{os.getuid()}:{os.getgid()}",
        "limits": host.get("Memory") == int(limits.get("memory_gib", 20) * 1024**3)
        and host.get("MemorySwap") == host.get("Memory")
        and host.get("NanoCpus") == int(limits.get("cpu_cores", 8) * 1e9)
        and host.get("PidsLimit") == 512,
        "network": host.get("NetworkMode") == network,
    }


def stop_owned(name, labels, expected_id=None):
    obj = inspect(name)
    if obj is None:
        return
    if (expected_id and obj["Id"] != expected_id) or any(
        (obj.get("Config", {}).get("Labels") or {}).get(k) != v for k, v in labels.items()
    ):
        raise ValueError("Refusing to remove a replaced or unowned container")
    docker(["rm", "-f", obj["Id"]], timeout=30)
