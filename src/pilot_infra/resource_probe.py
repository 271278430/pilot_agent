"""A fixed read-only probe, runnable locally or through an admitted SSH channel."""

import inspect


def probe_machine():
    import csv
    import hashlib
    import io
    import json
    import os
    import platform
    import re
    import shutil
    import socket
    import subprocess
    import time
    from pathlib import Path

    def command(argv):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=12, check=False)
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout[:150000],
                "stderr": result.stderr[:800],
                "truncated": len(result.stdout) > 150000,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": type(error).__name__,
                "truncated": False,
            }

    def number(value):
        try:
            return float(value.strip())
        except (ValueError, TypeError):
            return None

    def csv_rows(value):
        try:
            return list(csv.reader(io.StringIO(value), skipinitialspace=True, strict=True))
        except csv.Error:
            return None

    raw = {}
    raw["gpu"] = command(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,driver_version,memory.total,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    raw["gpu_processes"] = command(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus, gpu_processes = [], []
    gpu_processes_known = False
    cuda_inventory_complete = raw["gpu"]["exit_code"] == 0 and not raw["gpu"]["truncated"]
    if raw["gpu"]["exit_code"] == 0 and not raw["gpu"]["truncated"]:
        rows = csv_rows(raw["gpu"]["stdout"])
        if rows is None:
            cuda_inventory_complete = False
        for row in rows or []:
            if len(row) != 7 or not row[0].strip().isdigit():
                cuda_inventory_complete = False
                continue
            gpus.append(
                {
                    "index": int(row[0]),
                    "uuid": row[1].strip(),
                    "name": row[2].strip(),
                    "driver": row[3].strip(),
                    "total_mib": number(row[4]),
                    "free_mib": number(row[5]),
                    "utilization_percent": number(row[6]),
                    "platform": "cuda",
                }
            )
    if gpus and raw["gpu_processes"]["exit_code"] == 0 and not raw["gpu_processes"]["truncated"]:
        cuda_processes_complete = not raw["gpu_processes"]["truncated"]
        known_uuids = {gpu["uuid"] for gpu in gpus}
        if len(known_uuids) != len(gpus) or "" in known_uuids or len({gpu["index"] for gpu in gpus}) != len(gpus):
            cuda_inventory_complete = False
        rows = csv_rows(raw["gpu_processes"]["stdout"])
        if rows is None:
            cuda_processes_complete = False
        for row in rows or []:
            if len(row) != 4 or not row[1].strip().isdigit() or row[0].strip() not in known_uuids:
                cuda_processes_complete = False
                continue
            gpu_processes.append(
                {
                    "gpu_uuid": row[0].strip(),
                    "pid": int(row[1]),
                    "name": row[2].strip(),
                    "used_mib": number(row[3]),
                }
            )
        gpu_processes_known = cuda_inventory_complete and cuda_processes_complete
    accelerators = "cuda" if gpus else "unknown"
    if not gpus:
        if shutil.which("mthreads-gmi"):
            raw["musa"] = command(["mthreads-gmi"])
            accelerators = "musa"
            text = raw["musa"]["stdout"]
            driver = re.search(r"Driver Version:\s*(\S+)", text)
            for row in re.finditer(
                r"^\s*(\d+)\s+(.+?)\s*\|([^|]+)\|\s*(\d+)%\s+(\d+)MiB\((\d+)MiB\)", text, re.M
            ):
                index, name, pci, util, used, total = row.groups()
                gpus.append(
                    {
                        "index": int(index),
                        "uuid": "musa-" + pci.strip(),
                        "name": name.strip(),
                        "driver": driver.group(1) if driver else None,
                        "total_mib": float(total),
                        "free_mib": float(total) - float(used),
                        "utilization_percent": float(util),
                        "platform": "musa",
                    }
                )
            mapped = {g["index"]: g["uuid"] for g in gpus}
            section = text.split("Processes:", 1)[-1] if "Processes:" in text else ""
            inventory_section = text.split("Processes:", 1)[0]
            device_rows = [line for line in inventory_section.splitlines() if re.match(r"^\s*\d+\s+.*\|", line)]
            gpu_processes_known = bool(
                raw["musa"]["exit_code"] == 0 and not raw["musa"]["truncated"]
                and "Processes:" in text and gpus and len(mapped) == len(gpus)
                and len(device_rows) == len(gpus)
            )
            saw_process = saw_empty = False
            for line in section.splitlines():
                row = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(.+?)\s+(\d+)MiB\s*", line)
                if row:
                    index, pid, name, used = row.groups()
                    if int(index) not in mapped:
                        gpu_processes_known = False
                        continue
                    saw_process = True
                    gpu_processes.append(
                        {"gpu_uuid": mapped[int(index)], "pid": int(pid),
                         "name": name.strip(), "used_mib": float(used)}
                    )
                elif line.strip().lower() == "no running processes found":
                    saw_empty = True
                elif not (not line.strip() or re.fullmatch(r"[\s+|=\-]+", line)
                          or re.fullmatch(r"\s*ID\s+PID\s+Process name\s+GPU Memory\s*", line)
                          or line.strip() == "Usage"):
                    gpu_processes_known = False
            gpu_processes_known = gpu_processes_known and saw_process != saw_empty
        if shutil.which("npu-smi"):
            raw["ascend"] = command(["npu-smi", "info"])
            accelerators = "ascend"
    memory = {"total_bytes": None, "available_bytes": None}
    if Path("/proc/meminfo").exists():
        values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        memory = {
            "total_bytes": int(values["MemTotal"].split()[0]) * 1024,
            "available_bytes": int(values.get("MemAvailable", values["MemFree"]).split()[0]) * 1024,
        }
    elif os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "available",
                    "page_total",
                    "page_available",
                    "virtual_total",
                    "virtual_available",
                    "extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory = {"total_bytes": status.total, "available_bytes": status.available}
    disks = []
    raw["disk"] = command(["df", "-P", "-B1"])
    if raw["disk"]["exit_code"] == 0:
        for line in raw["disk"]["stdout"].splitlines()[1:]:
            row = line.split(maxsplit=5)
            if len(row) == 6 and row[1].isdigit() and row[3].isdigit():
                disks.append(
                    {
                        "filesystem": row[0],
                        "total_bytes": int(row[1]),
                        "available_bytes": int(row[3]),
                        "mount": row[5],
                    }
                )
    else:
        total, _, free = shutil.disk_usage(Path.cwd().anchor)
        disks = [
            {
                "filesystem": "host-volume",
                "total_bytes": total,
                "available_bytes": free,
                "mount": Path.cwd().anchor,
            }
        ]
    raw["cpu"] = command(["lscpu", "-J"])
    cpu = {
        "logical_cores": os.cpu_count(),
        "affinity_cores": len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }
    if raw["cpu"]["exit_code"] == 0:
        try:
            cpu["details"] = {
                v["field"].rstrip(":"): v["data"] for v in json.loads(raw["cpu"]["stdout"])["lscpu"]
            }
        except (ValueError, KeyError):
            cpu["details"] = {"parse_status": "unknown"}
    raw["processes"] = command(["ps", "-eo", "pid,ppid,user:32,pcpu,pmem,rss,comm", "--sort=-rss"])
    processes = []
    for line in raw["processes"]["stdout"].splitlines()[1:]:
        row = line.split(maxsplit=6)
        if len(row) == 7 and row[0].isdigit():
            processes.append(
                {
                    "pid": int(row[0]),
                    "parent_pid": int(row[1]),
                    "owner": row[2],
                    "cpu_percent": number(row[3]),
                    "memory_percent": number(row[4]),
                    "rss_bytes": int(row[5]) * 1024,
                    "name": row[6],
                }
            )
    owners = {p["pid"]: p["owner"] for p in processes}
    for process in gpu_processes:
        process["owner"] = owners.get(process["pid"], "unknown")
        if os.name != "nt":
            try:
                import pwd

                status = (Path("/proc") / str(process["pid"]) / "status").read_text()
                uid = next(
                    line.split()[1] for line in status.splitlines() if line.startswith("Uid:")
                )
                process["owner"] = pwd.getpwuid(int(uid)).pw_name
            except (OSError, KeyError, StopIteration, ValueError):
                pass
    machine_id = (
        Path("/etc/machine-id").read_text().strip()
        if Path("/etc/machine-id").is_file()
        else socket.gethostname()
    )
    return {
        "schema_version": 1,
        "observed_at": time.time(),
        "scope": "whole-machine",
        "identity": {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "architecture": platform.machine(),
            "machine_fingerprint": hashlib.sha256(machine_id.encode()).hexdigest(),
        },
        "accelerator_platform": accelerators,
        "gpus": gpus,
        "gpu_processes": gpu_processes,
        "gpu_processes_known": gpu_processes_known,
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "processes": processes[:256],
        "process_count": len([p for p in Path("/proc").iterdir() if p.name.isdigit()])
        if Path("/proc").is_dir()
        else len(processes),
        "parsed_process_count": len(processes),
        "process_summary_limit": 256,
        "raw": raw,
        "limitations": [
            "观测不授予执行权限",
            "非CUDA设备当前保留原始诊断，不能据此宣称完整执行支持",
            "进程摘要按RSS取前256项，命令参数和环境变量不读取",
        ],
    }


def probe_program():
    return (
        inspect.getsource(probe_machine)
        + "\nimport json\nprint(json.dumps(probe_machine(),ensure_ascii=False))\n"
    )
