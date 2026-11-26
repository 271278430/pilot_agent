"""Evidence-based capacity analysis, never a grant or an installation claim."""

import json
import math
from pathlib import Path

GIB = 1024**3


def gpu_occupancy_known(observation):
    """Only a complete probe may classify an empty process list as idle."""
    gpus = observation.get("gpus")
    processes = observation.get("gpu_processes")
    if observation.get("gpu_processes_known") is not True or not isinstance(gpus, list) or not isinstance(processes, list):
        return False
    if not gpus or not all(
        isinstance(gpu, dict) and isinstance(gpu.get("uuid"), str) and gpu["uuid"]
        and type(gpu.get("index")) is int and gpu["index"] >= 0 for gpu in gpus
    ):
        return False
    uuids = {gpu["uuid"] for gpu in gpus}
    indices = {gpu["index"] for gpu in gpus}
    return len(uuids) == len(gpus) and len(indices) == len(gpus) and all(
        isinstance(process, dict) and isinstance(process.get("gpu_uuid"), str)
        and process["gpu_uuid"] in uuids for process in processes
    )


def check_gpu_occupancy(observation, gpu_ids, *, allow_shared=False):
    if not gpu_ids:
        return
    if not gpu_occupancy_known(observation):
        raise ValueError("GPU进程占用未知，请检查设备查询并重新观测")
    devices = {g.get("index"): g for g in observation["gpus"]}
    if any(index not in devices for index in gpu_ids):
        raise ValueError("GPU设备未在完整观测中发现")
    busy = {p["gpu_uuid"] for p in observation["gpu_processes"]}
    if not allow_shared and any(devices[index]["uuid"] in busy for index in gpu_ids):
        raise ValueError("设备已有业务占用；未批准共享负载")


def workspace_disk(disks, root):
    directory = str(root).replace("\\", "/").rstrip("/")
    matching = [
        d
        for d in disks
        if directory == d["mount"].replace("\\", "/").rstrip("/")
        or directory.startswith(d["mount"].replace("\\", "/").rstrip("/") + "/")
    ]
    return max(matching, key=lambda d: len(d["mount"]), default=None)


def model_requirements(model, context, concurrency):
    if not model:
        return {"known": False, "reason": "尚未选择已登记模型"}
    path = Path(model["path"])
    try:
        if model.get("server_id"):
            config = model["manifest"]["config"]
            weights = sum(
                f["bytes"] for f in model["manifest"]["files"] if f["path"].endswith(".safetensors")
            )
        else:
            config = json.loads((path / "config.json").read_text(encoding="utf-8"))
            weights = sum(p.stat().st_size for p in path.glob("*.safetensors"))
    except (OSError, ValueError, KeyError):
        return {"known": False, "reason": "模型配置或权重大小无法核验"}
    fields = [
        config.get(k)
        for k in ("num_hidden_layers", "num_key_value_heads", "hidden_size", "num_attention_heads")
    ]
    known = all(type(v) is int and v > 0 for v in fields) and weights > 0
    kv = None
    if known:
        layers, heads, hidden, attention = fields
        head_dim = config.get("head_dim", hidden // attention)
        if type(head_dim) is int and head_dim > 0:
            kv = 2 * layers * heads * head_dim * 2 * context * concurrency
    return {
        "known": bool(known and kv is not None),
        "weights_bytes": weights,
        "kv_bytes": kv,
        "dtype_assumption": "KV按BF16/FP16两字节估算",
        "context_length": context,
        "concurrency": concurrency,
        "minimum_gpu_gib": (weights + kv + 2 * GIB) / GIB if kv is not None else None,
        "reason": "权重 + KV + 2 GiB工作区的规划估算，启动时重新核验"
        if kv is not None
        else "KV配置缺失，容量不能判定通过",
    }


def assess(server, observation, assets, model_id=None, context=4096, concurrency=2, root=None):
    from .node_authority import allowed_operations

    remote = server["connection"]["transport"] != "local"
    model = next(
        (m for m in assets["models"] if m["id"] == model_id
         and (m.get("server_id") == server["id"] if remote else m.get("server_id") in (None, server["id"]))),
        None,
    )
    requirements = model_requirements(model, context, concurrency)
    policy = server["policy"]
    occupancy_known = gpu_occupancy_known(observation)
    devices = []
    for gpu in observation.get("gpus", []):
        free, total = gpu.get("free_mib"), gpu.get("total_mib")
        known = (
            all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (free, total))
            and total > 0
            and free <= total
        )
        required = requirements.get("minimum_gpu_gib")
        devices.append(
            {
                **gpu,
                "approvable": gpu["index"] in policy["approvable_gpu_ids"],
                "capacity": "unknown"
                if not known or required is None
                else "fits_estimate"
                if free / 1024 >= required
                else "insufficient",
                "processes": [
                    p for p in (observation.get("gpu_processes") or [])
                    if isinstance(p, dict) and p.get("gpu_uuid") == gpu["uuid"]
                ],
            }
        )
    engines = []
    for image in assets["images"]:
        if image.get("server_id") and image["server_id"] != server["id"]:
            continue
        if not image.get("server_id") and server["connection"]["transport"] != "local":
            continue
        platform = image.get("platform", "cuda" if "sglang" in image["id"].lower() else "unknown")
        runtime = image.get("backend", "docker") == "docker" and image.get("engine", "sglang") in {
            "sglang",
            "vllm",
        }
        compatible = platform == observation.get("accelerator_platform") and runtime
        engines.append(
            {
                "id": image["id"],
                "label": image.get("label", image["id"]),
                "platform": platform,
                "compatible": compatible,
                "registered": True,
                "executor": image.get("backend", "docker"),
                "engine": image.get("engine", "sglang"),
                "reason": "匹配当前设备平台的登记配方"
                if compatible
                else "平台不匹配或未知；不能直接执行",
            }
        )
    engines.sort(key=lambda e: not e["compatible"])
    disks = observation.get("disks", [])
    directory = (
        (str(Path(root).resolve()) if server["connection"]["transport"] == "local" else str(root))
        if root
        else None
    )
    selected_disk = workspace_disk(disks, directory) if directory else None
    memory = observation.get("memory", {})
    feasible = sorted(
        (d for d in devices if occupancy_known and d["approvable"] and d["capacity"] == "fits_estimate" and not d["processes"]),
        key=lambda d: (
            d.get("utilization_percent") if d.get("utilization_percent") is not None else 100,
            -(d.get("free_mib") or 0),
        ),
    )
    findings = []
    for disk in disks:
        total, available = disk.get("total_bytes"), disk.get("available_bytes")
        if total and available is not None and available / total < 0.05:
            findings.append(
                {
                    "kind": "disk_pressure",
                    "scope": disk["mount"],
                    "reason": "挂载盘空闲不足5%；先核对任务目录，不自动删除其他用户文件",
                }
            )
    cpu = observation.get("cpu", {})
    load = cpu.get("load_average")
    if load and cpu.get("logical_cores") and load[0] >= cpu["logical_cores"]:
        findings.append(
            {
                "kind": "cpu_pressure",
                "scope": "whole-machine",
                "reason": "整机负载达到逻辑CPU数量；建议减少并发或等待",
            }
        )
    limits = None
    if (
        cpu.get("affinity_cores")
        and memory.get("available_bytes")
        and selected_disk
        and selected_disk.get("available_bytes") is not None
    ):
        limits = {
            "cpu_cores": min(4, policy["max_cpu_cores"], cpu["affinity_cores"]),
            "memory_gib": min(16, policy["max_memory_gib"], memory["available_bytes"] / GIB * 0.8),
            "disk_gib": min(
                5, policy["max_disk_gib"], selected_disk.get("available_bytes", 0) / GIB * 0.8
            ),
            "duration_seconds": min(2400, policy["max_duration_seconds"]),
        }
        if (
            limits["memory_gib"] < 0.5
            or limits["disk_gib"] < 0.1
            or limits["duration_seconds"] < 120
        ):
            limits = None
    serving = None
    if feasible and requirements["known"]:
        gpu = feasible[0]
        weights_mib = requirements["weights_bytes"] / 1024**2
        fraction = min(0.18, (gpu["free_mib"] - weights_mib - 2048) / gpu["total_mib"] * 0.8)
        kv_fraction = (requirements["kv_bytes"] / 1024**2) / gpu["total_mib"]
        if fraction >= max(0.05, kv_fraction):
            serving = {"context_length": context, "mem_fraction_static": round(fraction, 3)}
    limitations = [
        "容量估算不是实测吞吐或性能保证",
        "其他用户占用保持原状；不停止或抢占服务",
        "仅登记配方参与执行推荐，未知栈需先准备并验收",
    ]
    if root is None:
        limitations.append("节点持久执行器未启用或已失效")
    if model_id and model is None:
        limitations.append("所选模型未在当前节点核验")
    if not occupancy_known:
        limitations.append("GPU进程占用未知，请刷新整机观测并检查设备查询是否完整")
    if policy["mode"] != "review":
        limitations.append("当前节点仅允许观察")
    elif not {"inference", "containers"}.issubset(allowed_operations(policy)):
        limitations.append("制定推理方案需同时允许推理和本任务容器")
    if not feasible and any(d["approvable"] and d["processes"] for d in devices):
        limitations.append("可审批设备已有进程，不作为自动推荐")
    return {
        "server_id": server["id"],
        "scope": "whole-machine",
        "observation_id": observation["id"],
        "requirements": requirements,
        "devices": devices,
        "cpu": observation.get("cpu", {}),
        "memory": memory,
        "disks": disks,
        "workspace_disk": selected_disk,
        "engines": engines,
        "policy": policy,
        "suggested_gpu_ids": [d["index"] for d in feasible][:1],
        "suggested_limits": limits,
        "suggested_serving": serving,
        "health_findings": findings,
        "recommendation_ready": bool(
            limits
            and serving
            and any(e["compatible"] for e in engines)
            and root is not None
            and server["enabled"]
            and policy["mode"] == "review"
            and {"inference", "containers"}.issubset(allowed_operations(policy))
        ),
        "executor_available": root is not None,
        "limitations": limitations,
    }


def check_capacity(observation, plan, limits, model, root):
    requirements = model_requirements(
        model, plan["serving"]["context_length"], max(plan["workload"]["concurrency"])
    )
    if not requirements["known"]:
        raise ValueError("模型KV配置缺失，不能批准容量未知的执行方案")
    gpus = {g["index"]: g for g in observation.get("gpus", [])}
    for device in plan["gpu_ids"]:
        gpu = gpus.get(device)
        if not gpu or gpu.get("platform") not in {"cuda", "musa"}:
            raise ValueError("设备容量未知或当前平台尚无可执行数值观测")
        total, free = gpu.get("total_mib"), gpu.get("free_mib")
        if not all(
            isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (total, free)
        ):
            raise ValueError("显存读数未知，不能批准")
        required = max(
            requirements["minimum_gpu_gib"] * 1024 / len(plan["gpu_ids"]),
            total * plan["serving"]["mem_fraction_static"]
            + requirements["weights_bytes"] / 1024**2 / len(plan["gpu_ids"])
            + 2048,
        )
        if free < required:
            raise ValueError(
                f"GPU {device} 可用显存不足；需要约 {required:.0f} MiB，当前 {free:.0f} MiB"
            )
    cpu = observation.get("cpu", {}).get("affinity_cores")
    available = observation.get("memory", {}).get("available_bytes")
    disk = workspace_disk(observation.get("disks", []), root)
    if not cpu or limits["cpu_cores"] > cpu:
        raise ValueError("CPU配额超过当前可用范围或观测未知")
    if available is None or limits["memory_gib"] * GIB > available:
        raise ValueError("宿主可用内存不足或未知")
    if (
        not disk
        or disk.get("available_bytes") is None
        or limits["disk_gib"] * GIB > disk["available_bytes"]
    ):
        raise ValueError("Pilot工作区磁盘可用空间不足或未知")
    return requirements
