"""Read hardware availability; estimates never reserve or select shared GPUs."""

import csv
import io
import os
import re
import shutil
import subprocess
from pathlib import Path

from .contracts import Advisory, GPUResource, Hardware


def hardware(root):
    ram = None
    if Path("/proc/meminfo").exists():
        match = re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())
        if match:
            ram = round(int(match[1]) / 1024**2, 2)
    elif os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total_phys",
                    "available_phys",
                    "total_page",
                    "available_page",
                    "total_virtual",
                    "available_virtual",
                    "extended_virtual",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            ram = round(status.available_phys / 1024**3, 2)
    gpus, status = [], "absent"
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-gpu=index,name,memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            for row in csv.reader(io.StringIO(result.stdout)):
                gpus.append(
                    GPUResource(
                        index=int(row[0]),
                        name=row[1].strip(),
                        total_gb=round(float(row[2]) / 1024, 2),
                        free_gb=round(float(row[3]) / 1024, 2),
                    )
                )
            status = "available" if gpus else "absent"
        except (OSError, ValueError, subprocess.SubprocessError):
            status = "unavailable"
    from pilot_core.config import credentials

    config = credentials()
    return Hardware(
        cpu_count=os.cpu_count() or 1,
        available_ram_gb=ram,
        free_disk_gb=round(shutil.disk_usage(root).free / 1024**3, 2),
        gpus=gpus,
        gpu_probe=status,
        api_available=all(config.values()),
        api_model=config.get("REPOPILOT_MODEL") or None,
    )


def assess_model(request, machine):
    model_id = request.model_id
    params = request.parameter_billions
    basis = "用户提供参数量" if params else None
    if not params and model_id:
        match = re.search(r"(?:^|[-/_])(\d+(?:\.\d+)?)[bB](?:$|[-/_])", model_id)
        if match:
            params, basis = float(match[1]), "模型名称推测；需实际配置复核"
    if not model_id and not params:
        return {"id": None, "parameter_billions": None, "basis": "尚未选定模型"}, [], False
    notes = []
    # The supported new-model backend uses FP32 CPU weights, not arbitrary GPU placement.
    ram_need = round(params * 4 * 1.35 + 2, 2) if params else None
    vram_need = round(params * 2 * 1.35 + 2, 2) if params else None
    result = {
        "id": model_id,
        "parameter_billions": params,
        "basis": basis or "参数量未知",
        "estimated_cpu_ram_gb": ram_need,
        "estimated_fp16_vram_gb": vram_need,
        "estimate_scope": "稠密模型权重与基础余量估算；不含充分KV缓存、并发、MoE和量化差异",
    }
    if not machine.gpus:
        notes.append(
            Advisory(
                code="gpu_absent",
                severity="warning",
                title="GPU 状态未确认"
                if machine.gpu_probe == "unavailable"
                else "未检测到可用 GPU",
                explanation="新模型只能使用受支持的 CPU 小模型后端，较大模型建议调用已配置 API。",
                evidence="GPU 探测结果：" + machine.gpu_probe,
            )
        )
    elif vram_need and max(g.free_gb for g in machine.gpus) < vram_need:
        notes.append(
            Advisory(
                code="gpu_memory",
                severity="warning",
                title="当前空闲显存不足",
                explanation="单卡保守估算不足；不会自动占用共享卡或假定多卡可合并，建议 API 或更小模型。",
                evidence=f"估算 {vram_need} GB；最大单卡空闲 {max(g.free_gb for g in machine.gpus):.2f} GB",
            )
        )
    if machine.gpus:
        notes.append(
            Advisory(
                code="gpu_admission",
                severity="info",
                title="检测到 GPU 不等于获准使用",
                explanation="当前新模型部署后端为 CPU；现有 8B 服务可以复用。新 GPU 部署需另行配置配额与后端。",
                evidence=f"检测到 {len(machine.gpus)} 张卡；本次没有预留或修改任何 GPU",
            )
        )
    cpu_ok = bool(
        model_id
        and re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", model_id)
        and params
        and params <= 0.6
        and machine.available_ram_gb is not None
        and machine.available_ram_gb > ram_need + 1
        and machine.free_disk_gb > params * 2 + 2
    )
    if not cpu_ok:
        notes.append(
            Advisory(
                code="local_model_unsupported",
                severity="warning",
                title="这个模型暂不适合当前本地部署路径",
                explanation="可保留仓库、选择较小模型，或明确切换到已配置 API；不会在确认后静默换模型。",
                evidence=f"CPU RAM估算 {ram_need or '未知'} GB；可用 {machine.available_ram_gb or '未知'} GB；支持新模型≤0.6B",
            )
        )
    return result, notes, cpu_ok
