"""Read-only Linux node preflight, also embedded in the fixed SSH protocol."""

import inspect


def inspect_node(request):
    import hashlib
    import json
    import os
    import platform
    import shutil
    import subprocess
    from pathlib import Path

    value = {
        "supported": platform.system() == "Linux",
        "platform": platform.system(),
        "data_mounts": [],
        "paths": {},
        "docker": {},
        "reasons": [],
    }
    if not value["supported"]:
        value.update(
            ready=False, reasons=["节点Worker需要Linux；Windows可作为主控，通过SSH使用Linux节点"]
        )
        return value
    system_device = Path("/").stat().st_dev
    machine_id = Path("/etc/machine-id").read_text().strip()
    product = Path("/sys/class/dmi/id/product_uuid")
    try:
        hardware = product.read_text().strip().lower()
    except OSError:
        hardware = ""
    if not hardware or set(hardware.replace("-", "")) <= {"0", "f"}:
        hardware = ""
    value["node_identity"] = hashlib.sha256((machine_id + "\0" + hardware).encode()).hexdigest()
    value["identity_source"] = "machine-id+hardware" if hardware else "machine-id"
    value["network_interfaces"] = []
    try:
        data = json.loads(
            subprocess.run(
                ["ip", "-j", "address"], capture_output=True, text=True, timeout=5
            ).stdout
        )
        if isinstance(data, list):
            for interface in data:
                name = interface["ifname"]
                if name.startswith(("lo", "docker", "veth", "br-", "cni")):
                    continue
                try:
                    speed = int((Path("/sys/class/net") / name / "speed").read_text())
                except (OSError, ValueError):
                    speed = None
                for address in interface.get("addr_info", []):
                    if address.get("scope") == "global" and address.get("family") == "inet":
                        value["network_interfaces"].append(
                            {
                                "interface": name,
                                "address": address["local"],
                                "prefixlen": address["prefixlen"],
                                "speed_mbps": speed,
                            }
                        )
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        pass
    # Inventory only: an ACTIVE HCA is not evidence of peer reachability,
    # permission to use it, or a working KV-transfer path.
    value["rdma_hcas"] = []
    rdma_root = Path("/sys/class/infiniband")
    value["rdma_inventory_known"] = rdma_root.is_dir()
    if value["rdma_inventory_known"]:
        try:
            for device in sorted(rdma_root.iterdir()):
                interfaces = sorted(p.name for p in (device / "device/net").glob("*"))
                for port in sorted((device / "ports").glob("*")):
                    try:
                        state = (port / "state").read_text().strip()
                        link = (port / "link_layer").read_text().strip()
                    except OSError:
                        state, link = None, None
                    value["rdma_hcas"].append({"device": device.name, "port": port.name,
                                                "state": state, "link_layer": link,
                                                "interfaces": interfaces})
        except OSError:
            value["rdma_inventory_known"] = False
    pseudo = {
        "tmpfs",
        "devtmpfs",
        "proc",
        "sysfs",
        "overlay",
        "ramfs",
        "squashfs",
        "cgroup",
        "cgroup2",
        "autofs",
        "debugfs",
        "tracefs",
    }
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mount = Path(fields[4].replace("\\040", " ").replace("\\134", "\\"))
        kind = fields[fields.index("-") + 1]
        if kind in pseudo or any(
            mount.is_relative_to(Path(p))
            for p in ("/proc", "/sys", "/dev", "/run", "/boot", "/usr", "/etc", "/var/lib")
        ):
            continue
        try:
            if mount.stat().st_dev == system_device or mount.is_symlink():
                continue
            usage = shutil.disk_usage(mount)
            mounts.append(
                {
                    "path": str(mount),
                    "filesystem": kind,
                    "device": mount.stat().st_dev,
                    "free_bytes": usage.free,
                    "writable": os.access(mount, os.W_OK | os.X_OK),
                }
            )
        except OSError:
            continue
    value["data_mounts"] = sorted(mounts, key=lambda m: (not m["writable"], -m["free_bytes"]))
    namespace = request.get("namespace", "")
    if not namespace or any(c not in "0123456789abcdef" for c in namespace):
        raise ValueError("Invalid project namespace")
    if value["data_mounts"]:
        parent = Path(value["data_mounts"][0]["path"]) / "pilot-data" / ("pilot-" + namespace)
        value["suggested"] = {
            "root": str(parent / "pilot-executor"),
            "asset_root": str(parent / "resources"),
        }
    else:
        value["suggested"] = {}
        value["reasons"].append("未发现独立数据盘，不能自动准备节点目录")

    def location(name, text):
        path = Path(text)
        item = {"path": text, "ready": False}
        value["paths"][name] = item
        if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
            item["reason"] = "必须是数据盘上的专属绝对目录"
            return
        for part in [path, *path.parents]:
            if part.is_symlink():
                item["reason"] = "目录或父目录含符号链接"
                return
        ancestor = path
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            item["reason"] = "现有父路径不是目录"
            return
        device = ancestor.stat().st_dev
        item.update(
            exists=path.exists(),
            device=device,
            data_disk=device != system_device,
            free_bytes=shutil.disk_usage(ancestor).free,
        )
        candidates = [
            m for m in mounts if path.is_relative_to(Path(m["path"])) and m["device"] == device
        ]
        if not item["data_disk"] or not candidates:
            item["reason"] = "目录位于系统盘或非持久文件系统"
            return
        item["mount"] = max(candidates, key=lambda m: len(m["path"]))["path"]
        if not os.access(ancestor, os.W_OK | os.X_OK):
            item["reason"] = "SSH用户对目录或最近现有父目录没有写入权限"
            return
        if item["free_bytes"] < 512 * 1024**2:
            item["reason"] = "数据盘可用空间不足512MiB预留"
            return
        if path.exists():
            marker = path / ("pilot-executor.json" if name == "root" else ".pilot-node-assets.json")
            host_marker = path / ".pilot-resources.json"
            if marker.is_symlink() or host_marker.is_symlink():
                item["reason"] = "目录所有权标记无效"
                return
            owned = marker.is_file() and json.loads(marker.read_text()).get(
                "owner_namespace"
            ) == request.get("owner_namespace")
            host_owned = (
                name == "asset_root"
                and host_marker.is_file()
                and json.loads(host_marker.read_text()).get("owner_namespace")
                == request.get("resource_owner_namespace")
            )
            execution_marker = Path(roots["root"]) / "pilot-executor.json"
            legacy_owned = (
                name == "asset_root"
                and path == Path(roots["root"]) / "assets"
                and not execution_marker.is_symlink()
                and execution_marker.is_file()
                and json.loads(execution_marker.read_text()).get("owner_namespace")
                == request.get("owner_namespace")
            )
            # Existing roots cannot be adopted just because they look private.
            if (name == "root" and not owned) or (
                name == "asset_root"
                and not (owned or host_owned or legacy_owned)
                and any(path.iterdir())
            ):
                item["reason"] = "目录已存在且不属于本项目；使用新的专属目录或导入已有资产"
                return
        item["ready"] = True

    roots = {
        name: request.get(name) or value["suggested"].get(name) for name in ("root", "asset_root")
    }
    for name, text in roots.items():
        if text:
            location(name, text)
    if all(roots.values()):
        execution, assets = map(Path, (roots["root"], roots["asset_root"]))
        if execution == assets or execution.is_relative_to(assets):
            value["reasons"].append("资源目录不能覆盖执行根")
    for name, item in value["paths"].items():
        if not item["ready"]:
            value["reasons"].append(name + ": " + item["reason"])
    try:
        password = os.environ.get("PILOT_NODE_DOCKER_SUDO_PASSWORD")
        argv = (
            ["sudo", "-S", "-p", "", "/usr/bin/docker"]
            if password
            else ["sudo", "-n", "/usr/bin/docker"]
            if os.environ.get("PILOT_NODE_DOCKER_USE_SUDO") == "1"
            else ["docker"]
        )
        result = subprocess.run(
            [*argv, "info", "--format", "{{json .}}"],
            input=password + "\n" if password else None,
            capture_output=True,
            text=True,
            timeout=12,
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        storage = Path(info["DockerRootDir"])
        value["docker"] = {
            "ready": True,
            "version": info["ServerVersion"],
            "root": str(storage),
            "data_disk": storage.stat().st_dev != system_device,
        }
        value["docker"]["runtimes"] = sorted(info.get("Runtimes", {}))
        value["cuda_runtime"] = {
            "mode": "nvidia" if "nvidia" in info.get("Runtimes", {}) else "device-bind",
            "driver_mounts": [],
        }
        if value["cuda_runtime"]["mode"] == "device-bind" and Path("/dev/nvidiactl").exists():
            for name in [
                "libcuda.so.1",
                "libnvidia-ml.so.1",
                "libnvidia-ptxjitcompiler.so.1",
                "libnvidia-gpucomp.so.1",
            ]:
                library = Path("/usr/lib/x86_64-linux-gnu") / name
                if not library.exists():
                    continue
                canonical = library.resolve()
                if (
                    canonical.is_file()
                    and canonical.is_relative_to(Path("/usr/lib"))
                    and canonical.stat().st_uid == 0
                ):
                    value["cuda_runtime"]["driver_mounts"].append(
                        {
                            "source": str(canonical),
                            "target": "/pilot-driver/" + name,
                            "sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
                        }
                    )
        if not value["docker"]["data_disk"]:
            value["reasons"].append(
                "Docker data-root位于系统盘，需由所有者单独调整；Pilot不自动迁移daemon"
            )
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        value["docker"] = {"ready": False}
        value["reasons"].append("SSH用户无法访问可用Docker daemon，先完成节点前置条件")
    value["ready"] = bool(all(roots.values()) and not value["reasons"])
    return value


def source():
    return inspect.getsource(inspect_node)


def program():
    return (
        source()
        + "\nimport json,sys\nprint(json.dumps({'ok':True,'value':inspect_node(json.load(sys.stdin))}))"
    )
