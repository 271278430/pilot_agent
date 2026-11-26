"""Standalone Python 3.10 worker. Guest code never runs in the host interpreter."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXTENSIONS = {".py", ".c", ".cc", ".cpp", ".h", ".hpp", ".mu", ".cu", ".toml", ".txt", ".json"}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def safe_path(base, relative):
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or "\\" in relative or ":" in relative:
        raise ValueError("Source path escape")
    path = base / part
    for node in [path, *path.parents]:
        if node == base.parent:
            break
        if node.is_symlink():
            raise ValueError("Symlinks are forbidden")
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Source path escape")
    return path


def lease(root, task_id):
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("Invalid task identity")
    if root.name != "pilot-workbench-" + task_id or root.is_symlink():
        raise ValueError("Invalid workbench root")
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Workbench root is not canonical")
    record = json.loads((root / "lease.json").read_text())
    if record != {"task_id": task_id, "root": str(root)}:
        raise ValueError("Workbench ownership mismatch")


def manifest(base):
    rows = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Source symlink rejected")
        if path.is_file():
            rel = path.relative_to(base).as_posix()
            safe_path(base, rel)
            if path.stat().st_nlink != 1:
                raise ValueError("Source hardlink rejected")
            rows[rel] = sha(path.read_bytes())
    if len(rows) > 256 or sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) > 4000000:
        raise ValueError("Source budget exceeded")
    return rows


def container_args(root, task_id, operation_id, profile, work):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", profile["image_digest"]):
        raise ValueError("Pinned image required")
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise ValueError("Invalid operation identity")
    uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65534
    gid = os.getgid() if hasattr(os, "getgid") and os.getuid() else 65534
    args = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--runtime",
        "runc",
        "--name",
        "pilot-wb-" + operation_id,
        "--cidfile",
        str(root / "jobs" / operation_id / "container.cid"),
        "--label",
        "pilot.workbench=" + task_id,
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "1",
        "--memory",
        "2g",
        "--memory-swap",
        "2g",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--tmpfs",
        f"/workspace:rw,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=700",
        "-e",
        "HOME=/tmp",
        "-e",
        "USER=pilot",
        "-e",
        "LOGNAME=pilot",
        "-e",
        "PIP_CACHE_DIR=/tmp/pip-cache",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "MUSA_VISIBLE_DEVICES=-1",
        "-v",
        str(work) + ":/input:ro",
        "-v",
        str(root / "checks") + ":/checks:ro",
        "-w",
        "/workspace",
        "--entrypoint",
        profile["python"],
        profile["image_digest"],
        "-I",
        "/checks/runner.py",
    ]
    ids = profile.get("gpu_ids", [])
    if ids:
        args[2:2] = [
            "--tmpfs",
            f"/jit:rw,exec,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=700",
            "-e",
            "TRITON_CACHE_DIR=/jit/triton",
            "-e",
            "TMPDIR=/jit",
            "-e",
            "TORCHINDUCTOR_CACHE_DIR=/jit/inductor",
        ]
        if profile.get("platform") == "cuda":
            uuids = profile.get("gpu_uuids", [])
            if len(uuids) != len(ids) or any(
                not re.fullmatch(r"GPU-[a-fA-F0-9-]{36}", u) for u in uuids
            ):
                raise ValueError("CUDA软件任务必须绑定确切设备UUID")
            runtime = profile.get("cuda_runtime", {"mode": "nvidia"})
            args[2:2] = [
                "-e",
                "CUDA_VISIBLE_DEVICES=" + ",".join(uuids),
                "-e",
                "CUDA_DEVICE_ORDER=PCI_BUS_ID",
            ]
            if runtime["mode"] == "nvidia":
                args[args.index("--runtime") + 1] = "nvidia"
                args[2:2] = ["--gpus", "device=" + ",".join(uuids)]
            elif runtime["mode"] == "device-bind":
                mapping = {}
                for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
                    values = dict(
                        line.split(":", 1) for line in info.read_text().splitlines() if ":" in line
                    )
                    identity, minor = (
                        values.get("GPU UUID", "").strip(),
                        values.get("Device Minor", "").strip(),
                    )
                    if identity and minor.isdecimal():
                        mapping[identity] = int(minor)
                if any(u not in mapping for u in uuids):
                    raise ValueError("不能核验授权CUDA UUID到设备minor的映射")
                devices = [
                    "/dev/nvidiactl",
                    "/dev/nvidia-uvm",
                    *["/dev/nvidia" + str(mapping[u]) for u in uuids],
                ]
                for device in devices:
                    if not Path(device).exists():
                        raise ValueError("确切CUDA设备节点不存在")
                    args[2:2] = ["--device", device + ":" + device + ":rw"]
                mounts = runtime["driver_mounts"]
                if not any(m["target"] == "/pilot-driver/libcuda.so.1" for m in mounts):
                    raise ValueError("节点无可冻结的CUDA驱动库；需要所有者配置Docker GPU前置条件")
                for mount in mounts:
                    source = Path(mount["source"])
                    if (
                        source.is_symlink()
                        or not source.is_file()
                        or source.stat().st_uid != 0
                        or not source.resolve().is_relative_to(Path("/usr/lib"))
                        or sha(source.read_bytes()) != mount["sha256"]
                        or not re.fullmatch(
                            r"/pilot-driver/lib(?:cuda|nvidia-[a-z0-9-]+)\.so(?:\.[0-9]+)*",
                            mount["target"],
                        )
                    ):
                        raise ValueError("已冻结CUDA驱动库变化或路径无效")
                    args[2:2] = ["-v", str(source) + ":" + mount["target"] + ":ro"]
                    if mount["target"] == "/pilot-driver/libcuda.so.1":
                        args[2:2] = ["-v", str(source) + ":/pilot-driver/libcuda.so:ro"]
                args[2:2] = [
                    "-e",
                    "LD_LIBRARY_PATH=/pilot-driver:/usr/local/cuda/lib64",
                    "-e",
                    "CUDA_VISIBLE_DEVICES=" + ",".join(uuids),
                    "-e",
                    "TRITON_LIBCUDA_PATH=/pilot-driver",
                    "-e",
                    "CUDA_DEVICE_ORDER=PCI_BUS_ID",
                ]
            else:
                raise ValueError("未支持的CUDA设备绑定方式")
        elif profile.get("platform") == "musa":
            if any(type(n) is not int or not 0 <= n <= 127 for n in ids):
                raise ValueError("MUSA软件设备范围无效")
            for n in ids:
                device = "/dev/mtgpu." + str(n)
                if not Path(device).exists():
                    raise ValueError("所选MUSA设备节点不存在")
                args[2:2] = ["--device", device + ":" + device]
            args[args.index("MUSA_VISIBLE_DEVICES=-1")] = "MUSA_VISIBLE_DEVICES=" + ",".join(
                map(str, ids)
            )
        else:
            raise ValueError("GPU软件平台未支持")
    if profile.get("kind") == "pd":
        model = profile["model"]["prepared"]
        directory = Path(model["path"])
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("PD模型目录变化")
        for item in model["manifest"]["files"]:
            file = safe_path(directory, item["path"])
            if (
                not file.is_file()
                or file.stat().st_size != item["bytes"]
                or sha(file.read_bytes()) != item["sha256"]
            ):
                raise ValueError("PD冻结模型文件变化")
        args[args.index("--memory") + 1] = "4g"
        args[args.index("--memory-swap") + 1] = "4g"
        args[args.index("--cpus") + 1] = "2"
        args[args.index("--pids-limit") + 1] = "256"
        args[2:2] = [
            "-v",
            str(directory) + ":/model:ro",
            "--shm-size",
            "512m",
            "-e",
            "HF_HUB_OFFLINE=1",
            "-e",
            "TRANSFORMERS_OFFLINE=1",
        ]
    prefix, _ = docker_access()
    return [*prefix, *args[1:]]


def docker_access():
    password = os.environ.get("PILOT_NODE_DOCKER_SUDO_PASSWORD")
    prefix = (
        ["sudo", "-S", "-p", "", "/usr/bin/docker"]
        if password
        else ["sudo", "-n", "/usr/bin/docker"]
        if os.environ.get("PILOT_NODE_DOCKER_USE_SUDO") == "1"
        else ["docker"]
    )
    return prefix, password


def docker_run(argv, **kwargs):
    _, password = docker_access()
    result = subprocess.run(
        argv,
        input=password + "\n" if password else None,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in {"PILOT_NODE_DOCKER_SUDO_PASSWORD", "PILOT_NODE_DOCKER_USE_SUDO"}
        },
        **kwargs,
    )
    if password:
        result.stdout = result.stdout.replace(password, "[REDACTED]")
        result.stderr = result.stderr.replace(password, "[REDACTED]")
    return result


RUNNER = """import json,os,subprocess,time,shutil
from pathlib import Path
shutil.copytree('/input','/workspace',dirs_exist_ok=True)
steps=json.loads(Path('/checks/steps.json').read_text())
policy=json.loads(Path('/checks/policy.json').read_text())
deadline=time.monotonic()+policy['timeout']
results=[]
for step in steps:
 started=time.monotonic()
 try:
  remaining=min(step['timeout'],deadline-time.monotonic())
  if remaining<=0:raise subprocess.TimeoutExpired(step['argv'],0)
  p=subprocess.run(step['argv'],cwd='/workspace',capture_output=True,text=True,timeout=remaining)
  result={'stage':step['stage'],'exit_code':p.returncode,'stdout':p.stdout[-12000:],'stderr':p.stderr[-12000:],'elapsed_s':round(time.monotonic()-started,3)}
 except subprocess.TimeoutExpired:
  result={'stage':step['stage'],'exit_code':124,'error':'stage timeout'}
 results.append(result)
 if result['exit_code']: break
artifacts={}
for name in ['kernel.o']:
 p=Path('/workspace')/name
 if p.is_file():
  import hashlib
  artifacts[name]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
print(json.dumps({'passed':len(results)==len(steps) and all(s['exit_code']==0 for s in results),'steps':results,'artifacts':artifacts,'gpu_execution':policy['gpu_execution']}))
"""


def stop_owned(task_id, operation_id, cidfile=None):
    name = "pilot-wb-" + operation_id
    prefix, _ = docker_access()
    p = docker_run([*prefix, "inspect", name], capture_output=True, text=True, timeout=10)
    if p.returncode:
        if "no such object" in p.stderr.lower() or "no such container" in p.stderr.lower():
            return
        raise ValueError("Container inspection unavailable; cleanup blocked")
    data = json.loads(p.stdout)[0]
    if data["Config"].get("Labels", {}).get("pilot.workbench") != task_id:
        raise ValueError("Container ownership mismatch; cleanup blocked")
    if cidfile is not None and (not cidfile.is_file() or data["Id"] != cidfile.read_text().strip()):
        raise ValueError("Original container ID cannot be confirmed; cleanup blocked")
    docker_run([*prefix, "rm", "-f", name], capture_output=True, check=True, text=True, timeout=15)


def perform(action, root, task_id, args):
    root = Path(root)
    lease(root, task_id)
    if action == "prepare":
        if (root / "profile.json").exists():
            raise ValueError("Already prepared; inspect original workspace")
        profile = args["profile"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", profile["image_digest"]):
            raise ValueError("Pinned image required")
        (root / "source").mkdir()
        (root / "checks").mkdir()
        (root / "jobs").mkdir()
        (root / "candidates").mkdir()
        for name, content in args["files"].items():
            path = safe_path(root / "source", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for name, content in args["checks"].items():
            path = safe_path(root / "checks", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (root / "checks/runner.py").write_text(RUNNER, encoding="utf-8")
        save(root / "checks/steps.json", profile["steps"])
        save(
            root / "checks/policy.json",
            {"timeout": profile["timeout"], "gpu_execution": bool(profile.get("gpu_ids"))},
        )
        save(root / "profile.json", profile)
        return {
            "prepared": True,
            "files": manifest(root / "source"),
            "checks_manifest": manifest(root / "checks"),
        }
    if action == "read":
        path = safe_path(root / "source", args["path"])
        data = path.read_bytes()
        if len(data) > 400000:
            raise ValueError("Source read budget exceeded")
        if path.stat().st_nlink != 1:
            raise ValueError("Source hardlink rejected")
        result = {"path": args["path"], "content": data.decode(), "sha256": sha(data)}
        if args.get("start_line") is not None:
            start, limit = args["start_line"], args.get("max_lines", 120)
            if start < 1 or not 1 <= limit <= 200:
                raise ValueError("Invalid source line range")
            lines = result["content"].splitlines(keepends=True)
            result.update(
                content="".join(lines[start - 1 : start - 1 + limit]),
                start_line=start,
                total_lines=len(lines),
                truncated=start - 1 + limit < len(lines),
            )
            if len(result["content"]) > 24000:
                result.update(content=result["content"][:24000], truncated=True)
        return result
    if action == "read_candidate":
        candidate_id = args["candidate_id"]
        if not re.fullmatch(r"c[1-9][0-9]*", candidate_id):
            raise ValueError("Invalid source candidate")
        base = root / "candidates" / candidate_id
        expected = json.loads((base / "manifest.json").read_text())
        if manifest(base / "source") != expected:
            raise ValueError("Frozen source changed")
        file = safe_path(base / "source", args["path"])
        offset, limit = args.get("offset", 0), args.get("limit", 300000)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 300000
        ):
            raise ValueError("Invalid source archive chunk")
        data = file.read_bytes()
        return {
            "bytes_total": len(data),
            "sha256": sha(data),
            "content_base64": base64.b64encode(data[offset : offset + limit]).decode(),
        }
    if action in {"write", "patch"}:
        path = safe_path(root / "source", args["path"])
        if path.suffix not in EXTENSIONS and path.name != "CMakeLists.txt":
            raise ValueError("Unsupported source file type")
        before = sha(path.read_bytes()) if path.exists() else None
        if path.exists() and path.stat().st_nlink != 1:
            raise ValueError("Source hardlink rejected")
        if before != args.get("expected_sha256"):
            raise ValueError("Source changed; read current hash before editing")
        if action == "patch":
            original = path.read_bytes().decode("utf-8")
            old = args["old_text"]
            if not old or original.count(old) != 1:
                raise ValueError("Patch must match exactly one source fragment")
            content = original.replace(old, args["new_text"], 1).encode()
        else:
            content = args["content"].encode()
        if len(content) > 400000:
            raise ValueError("Source write budget exceeded")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": args["path"], "before": before, "sha256": sha(content)}
    if action == "freeze":
        candidate_id = args["candidate_id"]
        if not re.fullmatch(r"c[1-9][0-9]*", candidate_id):
            raise ValueError("Invalid candidate")
        data = manifest(root / "source")
        destination = root / "candidates" / candidate_id
        destination.mkdir()
        shutil.copytree(root / "source", destination / "source")
        save(destination / "manifest.json", data)
        return {"candidate_id": candidate_id, "files": data}
    if action in {"run", "inspect"}:
        operation_id = args["operation_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise ValueError("Invalid operation")
        journal = root / "jobs" / operation_id
        if action == "inspect" or journal.exists():
            return json.loads((journal / "result.json").read_text())
        candidate_id = args["candidate_id"]
        if not re.fullmatch(r"c[1-9][0-9]*", candidate_id):
            raise ValueError("Invalid candidate")
        candidate = root / "candidates" / candidate_id
        expected = json.loads((candidate / "manifest.json").read_text())
        if manifest(candidate / "source") != expected:
            raise ValueError("Frozen source changed")
        checks = manifest(root / "checks")
        if args.get("expected_checks") is not None and checks != args["expected_checks"]:
            raise ValueError("Trusted checks changed after preparation")
        journal.mkdir()
        save(journal / "result.json", {"status": "running", "operation_id": operation_id})
        work = journal / "workspace"
        shutil.copytree(candidate / "source", work)
        profile = json.loads((root / "profile.json").read_text())
        result = {
            "operation_id": operation_id,
            "candidate_id": candidate_id,
            "source_manifest": expected,
        }
        try:
            p = docker_run(
                container_args(root, task_id, operation_id, profile, work),
                capture_output=True,
                text=True,
                timeout=profile["timeout"],
            )
            (journal / "container.log").write_text(p.stdout + p.stderr, encoding="utf-8")
            if p.returncode:
                result.update(status="failed", passed=False, error=p.stderr[-3000:])
            else:
                result.update(json.loads(p.stdout), status="completed")
                if not result["passed"]:
                    result["status"] = "failed"
        except subprocess.TimeoutExpired:
            stop_owned(task_id, operation_id, journal / "container.cid")
            result.update(status="failed", passed=False, error="bounded build timeout")
        except Exception as exc:
            result.update(status="unknown", passed=False, error=type(exc).__name__)
        result["frozen_source_unchanged"] = manifest(candidate / "source") == expected
        result["checks_manifest"] = checks
        result["checks_unchanged"] = manifest(root / "checks") == checks
        if result.get("status") in {"completed", "failed"}:
            for step in result.get("steps", []):
                try:
                    evidence = json.loads(step.get("stdout", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(evidence, dict) and evidence.get("protocol") in {
                    "pilot-http-metrics-contract-v1",
                    "pilot-rmsnorm-cuda-v1",
                    "pilot-cpu-profile-v1",
                    "pilot-pd-shared-storage-v1",
                }:
                    result["business_evidence"] = evidence
        save(journal / "result.json", result)
        return result
    if action == "cleanup":
        for path in (root / "jobs").iterdir() if (root / "jobs").exists() else []:
            if not re.fullmatch(r"[a-f0-9]{32}", path.name):
                raise ValueError("Unknown job directory; cleanup blocked")
            stop_owned(task_id, path.name, path / "container.cid")
        lease(root, task_id)
        shutil.rmtree(root)
        return {"restored": not root.exists(), "scope": "owned workspace and labelled containers"}
    raise ValueError("Unknown workbench action")


def main():
    try:
        result = perform(sys.argv[1], sys.argv[2], sys.argv[3], json.load(sys.stdin))
        print(json.dumps({"ok": True, "value": result}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:1500]}))


if __name__ == "__main__":
    main()
