"""Task-bound engine source and standard wheel artifacts; no Agent shell."""

import base64
import csv
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from repopilot.storage import atomic_json

from .contracts import EngineReview, EngineSourcePatch, EngineSourceRead, EngineSourceSearch
from .store import digest


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_manifest(base):
    if base.is_symlink() or not base.is_dir():
        raise ValueError("Engine source directory unavailable or symlinked")
    files = {}
    total = 0
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("Engine source/runtime symlinks forbidden")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.stat().st_nlink != 1:
            raise ValueError("Engine source/runtime hardlinks forbidden")
        total += path.stat().st_size
        if total > 128 * 1024**2 or len(files) >= 5000:
            raise ValueError("Engine source/runtime exceeds admitted package size")
        files[path.relative_to(base).as_posix()] = file_sha(path)
    if not files:
        raise ValueError("Empty engine source/runtime")
    return files


def profiles(root):
    path = Path(root) / ".local/engine-source-profiles.json"
    return json.loads(path.read_text())["profiles"] if path.is_file() else []


def profile(root, identity):
    row = next((p for p in profiles(root) if p["id"] == identity), None)
    if not row or row.get("backend") != "sglang-python-wheel":
        raise ValueError("No admitted SGLang Python source recipe")
    if not re.fullmatch(r"[0-9A-Za-z.]+", row["version"]) or not 60 <= row["build_timeout"] <= 3600:
        raise ValueError("Invalid engine version or build deadline")
    base = Path(row["source_root"])
    expected = Path(root).resolve() / ".local/engine-source-inputs" / row["id"]
    if base.resolve() != expected or base.is_symlink():
        raise ValueError("Engine input must be an Owner-registered private source directory")
    return row


def catalog(root):
    return [
        {k: p[k] for k in ("id", "label", "image_id", "version", "backend")} for p in profiles(root)
    ]


def gpu_devices(gpu_ids, driver_root=Path("/proc/driver/nvidia/gpus")):
    """nvidia-smi indices are NOT Linux device minors; bind by matched UUID."""
    observed = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    indices = {
        int(row.split(",")[0]): row.split(",")[1].strip() for row in observed.stdout.splitlines()
    }
    minors = {}
    for info in driver_root.glob("*/information"):
        values = dict(line.split(":", 1) for line in info.read_text().splitlines() if ":" in line)
        identity = values.get("GPU UUID", "").strip()
        minor = values.get("Device Minor", "").strip()
        if identity and minor.isdecimal():
            minors[identity] = int(minor)
    if any(g not in indices or indices[g] not in minors for g in gpu_ids):
        raise ValueError("Cannot prove authorized GPU UUID/device-minor mapping")
    return [{"index": g, "uuid": indices[g], "minor": minors[indices[g]]} for g in gpu_ids]


def sandbox_command(recipe, python, writable, readonly=(), gpu_ids=(), tmpdir=None):
    """Nonprivileged mount/PID isolation; never fall back to source-on-host."""
    if os.name != "posix":
        raise ValueError("This engine recipe needs Linux user namespaces")
    binary = Path(recipe["sandbox_binary"])
    if not binary.is_file() or file_sha(binary) != recipe["sandbox_sha256"]:
        raise ValueError("Pinned project bubblewrap is unavailable")
    python_root = Path(python).parent.parent
    base_prefix = Path(recipe["python_base_prefix"])
    command = [
        str(binary),
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    if not gpu_ids:
        command += ["--die-with-parent", "--unshare-net"]
    for path in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm", "--tmpfs", "/tmp"]
    for path in ("/etc/ld.so.cache", "/etc/hosts", "/etc/resolv.conf", "/sys"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    for path in dict.fromkeys([python_root, base_prefix]):
        command += ["--ro-bind", str(path), str(path)]
    for name in recipe.get("dependency_roots", []):
        path = Path(name)
        if path.name != "site-packages" or not path.is_dir() or path.is_symlink():
            raise ValueError("Only Owner registered site-packages dependency roots are mountable")
        command += ["--ro-bind", str(path), str(path)]
    command += ["--bind", str(writable), str(writable), "--chdir", str(writable)]
    for path in dict.fromkeys(map(Path, readonly)):
        command += ["--ro-bind", str(path), str(path)]
    if tmpdir:
        command += ["--bind", str(tmpdir), str(tmpdir), "--setenv", "TMPDIR", str(tmpdir)]
    devices = gpu_devices(gpu_ids) if gpu_ids else []
    for device in (
        [
            "nvidiactl",
            "nvidia-uvm",
            "nvidia-uvm-tools",
            *["nvidia" + str(g["minor"]) for g in devices],
        ]
        if gpu_ids
        else []
    ):
        path = Path("/dev") / device
        if path.exists():
            command += ["--dev-bind", str(path), str(path)]
        elif device != "nvidia-uvm-tools":
            raise ValueError("Authorized GPU device missing: " + device)
    command += [
        "--setenv",
        "PATH",
        str(Path(python).parent) + ":/usr/bin:/bin",
        "--setenv",
        "HOME",
        str(writable),
        "--setenv",
        "USER",
        "pilot-task",
        "--setenv",
        "TORCHINDUCTOR_CACHE_DIR",
        str(Path(writable) / "cache/inductor"),
        "--setenv",
        "TRITON_CACHE_DIR",
        str(Path(writable) / "cache/triton"),
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "HF_HUB_OFFLINE",
        "1",
        "--setenv",
        "PYTHONUNBUFFERED",
        "1",
    ]
    if gpu_ids:
        command += [
            "--setenv",
            "CUDA_VISIBLE_DEVICES",
            ",".join(g["uuid"] for g in devices),
            "--setenv",
            "PILOT_GPU_IDS",
            ",".join(map(str, gpu_ids)),
        ]
    return command


class EngineSource:
    def __init__(self, infra):
        self.infra, self.root, self.store = infra, infra.root, infra.store
        with self.store.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS engine_reviews(task_id TEXT,candidate_id TEXT,digest TEXT,decision TEXT,source TEXT,PRIMARY KEY(task_id,candidate_id))"
            )

    def recipe(self, task, validate=True):
        row = profile(self.root, task["plan"].get("engine_source_profile"))
        image = next(
            i for i in self.infra.assets.read()["images"] if i["id"] == task["plan"]["image_asset"]
        )
        if (
            row["image_id"] != image["id"]
            or image.get("backend", "docker") != "docker"
            or image.get("engine", "sglang") != "sglang"
        ):
            raise ValueError("Engine source recipe and registered runtime differ")
        if validate and digest(file_manifest(Path(row["source_root"]))) != row["source_digest"]:
            raise ValueError("Owner engine inputs changed; create a newly reviewed recipe")
        return row

    def working(self, task):
        return self.store.path(task["id"], "engine-source/working")

    def prepare(self, task):
        task = self.store.get(task["id"])
        self.infra.running(task)
        self.infra.resources.check_task(task, "source_write")
        if task["state"].get("active_job"):
            raise ValueError("Settle current Job before preparing source")
        recipe = self.recipe(task)
        destination = self.working(task)
        receipt = self.store.path(task["id"], "engine-source/prepared.json")
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if previous["profile_digest"] != digest(recipe):
                raise ValueError("Prepared recipe changed")
            return self.status(task)
        if destination.exists():
            raise ValueError("Unsettled source preparation; inspect original directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(recipe["source_root"], destination)
        actual = file_manifest(destination)
        if digest(actual) != recipe["source_digest"]:
            raise ValueError("Engine source copy identity mismatch")
        atomic_json(receipt, {"profile_digest": digest(recipe), "baseline": actual})
        self.store.event(
            task["id"],
            "engine_source_prepared",
            {"profile": recipe["id"], "source_digest": recipe["source_digest"]},
        )
        return self.status(task)

    def status(self, task):
        working = self.working(task)
        error = None
        try:
            count = len(file_manifest(working)) if working.is_dir() else 0
        except (ValueError, OSError) as exc:
            count, error = 0, str(exc)
        return {
            "profile": task["plan"].get("engine_source_profile"),
            "prepared": working.is_dir(),
            "file_count": count,
            "error": error,
            "editable": "sglang/srt/**/*.py",
            "candidates": [
                self.candidate_view(task, c)
                for c in task["state"]["candidates"]
                if c.get("engine_source")
            ],
        }

    def source_path(self, task, name):
        path = self.store.path(task["id"], "engine-source/working/" + name)
        if not name.startswith("sglang/") or "\\" in name or ":" in name:
            raise ValueError("Only registered engine package source is readable")
        if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 400000:
            raise ValueError("Source file missing or outside read limit")
        return path

    def read(self, task, args):
        request = EngineSourceRead.model_validate(args)
        path = self.source_path(task, request.path)
        lines = path.read_text().splitlines(keepends=True)
        return {
            "path": request.path,
            "sha256": file_sha(path),
            "start_line": request.start_line,
            "total_lines": len(lines),
            "content": "".join(
                lines[request.start_line - 1 : request.start_line - 1 + request.max_lines]
            )[:24000],
        }

    def search(self, task, args):
        request = EngineSourceSearch.model_validate(args)
        rows = []
        for name in file_manifest(self.working(task)):
            if not name.startswith("sglang/") or not name.endswith(".py"):
                continue
            for number, line in enumerate(self.source_path(task, name).read_text().splitlines(), 1):
                if request.text in line:
                    rows.append({"path": name, "line": number, "text": line[:320]})
                    if len(rows) >= request.limit:
                        return {"matches": rows, "truncated": True}
        return {"matches": rows, "truncated": False}

    def patch(self, task, args):
        task = self.store.get(task["id"])
        request = EngineSourcePatch.model_validate(args)
        self.infra.running(task)
        self.infra.resources.check_task(task, "source_write")
        if task["state"].get("active_job") or task["plan"]["kind"] in {"test", "compare"}:
            raise ValueError(
                "Source editing unavailable while a Job runs or in readonly experiments"
            )
        self.recipe(task)
        if not fnmatch.fnmatchcase(request.path, "sglang/srt/*.py"):
            raise ValueError("Recipe admits only existing sglang/srt Python engine files")
        path = self.source_path(task, request.path)
        if file_sha(path) != request.expected_sha256:
            raise ValueError("Engine source version conflict")
        original = path.read_text()
        if original.count(request.old_text) != 1:
            raise ValueError("Engine patch must match exactly one fragment")
        changed = original.replace(request.old_text, request.new_text, 1)
        if len(changed.encode()) > 400000:
            raise ValueError("Engine patch exceeds file budget")
        path.write_text(changed)
        result = {"path": request.path, "before": request.expected_sha256, "sha256": file_sha(path)}
        self.store.event(task["id"], "engine_source_patch", result)
        return result

    def freeze(self, task, candidate_id=None):
        recipe = self.recipe(task)
        working = self.working(task)
        prepared = self.store.path(task["id"], "engine-source/prepared.json")
        if not prepared.exists() or json.loads(prepared.read_text())["profile_digest"] != digest(
            recipe
        ):
            raise ValueError("Prepare the approved engine recipe before freezing")
        actual = file_manifest(working)
        baseline = json.loads(prepared.read_text())["baseline"]
        if set(actual) != set(baseline) or any(
            actual[k] != baseline[k] and not fnmatch.fnmatchcase(k, "sglang/srt/*.py")
            for k in actual
        ):
            raise ValueError("Engine source changed outside admitted repair files")
        snapshot = {
            "profile": recipe,
            "profile_digest": digest(recipe),
            "files": actual,
            "source_digest": digest(actual),
            "changed_files": [k for k in actual if actual[k] != baseline[k]],
        }
        if candidate_id:
            target = self.store.path(task["id"], f"candidates/{candidate_id}/engine-source")
            shutil.copytree(working, target)
            if file_manifest(target) != actual:
                raise ValueError(
                    "Source changed during freeze; keep unsettled directory for inspection"
                )
            atomic_json(target.parent / "engine-source.json", snapshot)
        return snapshot

    def candidate_view(self, task, candidate):
        error = None
        try:
            artifact = self.artifact(task, candidate, required=False)
        except (ValueError, OSError, KeyError) as exc:
            artifact, error = None, str(exc)
        jobs = [
            j
            for j in self.store.operations(task["id"])
            if j["action"] == "engine_build" and j["data"]["candidate_id"] == candidate["id"]
        ]
        return {
            "candidate_id": candidate["id"],
            "source_digest": candidate["engine_source"]["source_digest"],
            "changed_files": candidate["engine_source"]["changed_files"],
            "build": "unverified"
            if error
            else "passed"
            if artifact
            else jobs[-1]["status"]
            if jobs
            else "not_built",
            "error": error,
            "artifact": {k: v for k, v in artifact.items() if k != "runtime_files"}
            if artifact
            else None,
            "review_digest": digest(
                {
                    "task_id": task["id"],
                    "candidate_digest": candidate["digest"],
                    "artifact": artifact,
                }
            )
            if artifact
            else None,
            "decision": self.decision(task, candidate),
            "active": task["state"].get("service_candidate") == candidate["id"],
        }

    def artifact(self, task, candidate, required=True):
        receipt = self.store.path(task["id"], f"candidates/{candidate['id']}/engine-artifact.json")
        if not receipt.exists():
            if required:
                raise ValueError("Build a frozen engine wheel before approval or launch")
            return None
        value = json.loads(receipt.read_text())
        if (
            value["candidate_digest"] != candidate["digest"]
            or value["source_digest"] != candidate["engine_source"]["source_digest"]
        ):
            raise ValueError("Engine artifact belongs to another source candidate")
        base = self.store.path(task["id"], f"candidates/{candidate['id']}")
        for key in ("wheel", "runtime"):
            target = base / value[key]
            if target.is_symlink() or not target.resolve().is_relative_to(base.resolve()):
                raise ValueError("Artifact path escapes candidate")
        if (
            file_manifest(base / "engine-source") != candidate["engine_source"]["files"]
            or file_sha(base / value["wheel"]) != value["wheel_sha256"]
        ):
            raise ValueError("Frozen source or wheel changed")
        if file_manifest(base / value["runtime"]) != value["runtime_files"]:
            raise ValueError("Installed engine artifact changed")
        job = self.store.operation(value["job_id"])
        if job["status"] != "completed" and not required:
            return None
        if (
            job["status"] != "completed"
            or job["action"] != "engine_build"
            or job["task_id"] != task["id"]
        ):
            raise ValueError("Engine artifact has no settled build Job")
        return value

    def review(self, task, args):
        task = self.store.get(task["id"])
        request = EngineReview.model_validate(args)
        candidate = next(
            (c for c in task["state"]["candidates"] if c["id"] == request.candidate_id), None
        )
        if not candidate or not candidate.get("engine_source"):
            raise ValueError("Unknown engine source candidate")
        artifact = self.artifact(task, candidate)
        expected = digest(
            {"task_id": task["id"], "candidate_digest": candidate["digest"], "artifact": artifact}
        )
        if expected != request.expected_digest:
            raise ValueError(
                "Engine review digest differs; use this candidate's review_digest from engine_source_status, not candidate.digest or source_digest"
            )
        self.infra.resources.check_task(task, "source_write", candidate)
        return {
            "candidate_id": candidate["id"],
            "digest": expected,
            "artifact": {k: v for k, v in artifact.items() if k != "runtime_files"},
            "changed_files": candidate["engine_source"]["changed_files"],
            "decision": self.decision(task, candidate),
        }

    def decision(self, task, candidate):
        with self.store.db() as db:
            row = db.execute(
                "SELECT decision FROM engine_reviews WHERE task_id=? AND candidate_id=?",
                (task["id"], candidate["id"]),
            ).fetchone()
        return row[0] if row else None

    def decide(self, task, args, source):
        decision = args["decision"]
        if decision not in {"allowed-once", "rejected", "cancelled", "unavailable"}:
            raise ValueError("Unsupported native engine approval outcome")
        review = self.review(task, {k: v for k, v in args.items() if k != "decision"})
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT digest,decision FROM engine_reviews WHERE task_id=? AND candidate_id=?",
                (task["id"], review["candidate_id"]),
            ).fetchone()
            if previous and (
                previous["digest"] != review["digest"] or previous["decision"] != decision
            ):
                raise ValueError("Engine approval is final; freeze a new candidate")
            db.execute(
                "INSERT OR IGNORE INTO engine_reviews VALUES(?,?,?,?,?)",
                (task["id"], review["candidate_id"], review["digest"], decision, source),
            )
        self.store.event(
            task["id"],
            "engine_artifact_reviewed",
            {
                "candidate_id": review["candidate_id"],
                "digest": review["digest"],
                "decision": decision,
                "source": source,
            },
        )
        return {**review, "decision": decision}

    def admitted(self, task, candidate):
        artifact = self.artifact(task, candidate)
        expected = digest(
            {"task_id": task["id"], "candidate_digest": candidate["digest"], "artifact": artifact}
        )
        with self.store.db() as db:
            row = db.execute(
                "SELECT digest,decision FROM engine_reviews WHERE task_id=? AND candidate_id=?",
                (task["id"], candidate["id"]),
            ).fetchone()
        if not row or row["digest"] != expected or row["decision"] != "allowed-once":
            raise ValueError("Exact engine artifact needs Owner approval before execution")
        return artifact


def build_artifact(infra, task, candidate, operation_id, cancelled):
    source = EngineSource(infra)
    recipe = source.recipe(task)
    frozen = candidate["engine_source"]
    base = infra.store.path(task["id"], f"candidates/{candidate['id']}")
    if (
        file_manifest(base / "engine-source") != frozen["files"]
        or digest(recipe) != frozen["profile_digest"]
    ):
        raise ValueError("Frozen engine source/recipe changed")
    destination = base / "artifacts" / operation_id
    destination.mkdir(parents=True)
    if cancelled():
        raise InterruptedError("Build cancelled before wheel creation")
    wheel_name = f"sglang-{recipe['version']}-py3-none-any.whl"
    wheel = destination / wheel_name
    metadata = (base / "engine-source" / "METADATA").read_bytes()
    info = f"sglang-{recipe['version']}.dist-info"
    records = []
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        payloads = {
            k: (base / "engine-source" / k).read_bytes()
            for k in frozen["files"]
            if k.startswith("sglang/")
        }
        for name, data in payloads.items():
            if name.endswith(".py"):
                compile(data, name, "exec")
        payloads[info + "/METADATA"] = metadata
        payloads[info + "/WHEEL"] = (
            b"Wheel-Version: 1.0\nGenerator: Pilot\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        for name, data in sorted(payloads.items()):
            archive.writestr(name, data)
            records.append(
                [
                    name,
                    "sha256="
                    + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="),
                    str(len(data)),
                ]
            )
        output = io.StringIO()
        csv.writer(output).writerows([*records, [info + "/RECORD", "", ""]])
        archive.writestr(info + "/RECORD", output.getvalue())
    from pilot_core.docker import docker

    from .container_runtime import base_args, contract_checks, inspect, stop_owned

    image = next(i for i in infra.assets.read()["images"] if i["id"] == recipe["image_id"])
    name = "pilot-engine-build-" + operation_id
    labels = {
        "pilot.infra.task": task["id"],
        "pilot.infra.candidate": candidate["digest"],
        "pilot.infra.build": operation_id,
    }
    limits = task.get("_resource_limits", {})
    mounts = [(str(destination), "/output", False)]
    command = [
        "python3",
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--no-compile",
        "--disable-pip-version-check",
        "--target",
        "/output/runtime",
        "/output/" + wheel_name,
    ]
    env = {"HOME": "/output", "TMPDIR": "/tmp", "PYTHONNOUSERSITE": "1"}
    # Persist intent before create. A timeout is reconciled by exact labels;
    # output of an unsettled build is never admitted or automatically rebuilt.
    infra.store.operation(operation_id, "running", build_container_name=name, build_labels=labels)
    identity = docker(
        base_args(name, image, labels, limits, mounts, command, env), timeout=60
    ).stdout.strip()
    infra.store.operation(
        operation_id, "running", build_container_id=identity, build_image=image["digest"]
    )
    log = infra.store.path(task["id"], f"jobs/{operation_id}/engine.log")
    try:
        obj = inspect(identity)
        if not obj or not all(
            contract_checks(obj, image, labels, limits, mounts, command, env, "none").values()
        ):
            raise ValueError("CPU build container contract differs")
        if cancelled():
            raise InterruptedError("Build cancelled before container start")
        docker(["start", identity], timeout=30)
        deadline = time.monotonic() + recipe["build_timeout"]
        while True:
            obj = inspect(identity)
            if not obj or obj["Id"] != identity:
                raise ValueError("Build container identity lost")
            if not obj["State"]["Running"]:
                if obj["State"]["ExitCode"]:
                    raise ValueError("Offline wheel installation failed in Docker")
                break
            if cancelled() or time.monotonic() >= deadline:
                raise InterruptedError("Engine build cancelled or deadline reached")
            time.sleep(0.2)
    finally:
        result = docker(["logs", "--tail", "150", identity], check=False)
        log.write_text(result.stdout + result.stderr)
        stop_owned(name, labels, identity)
    if cancelled():
        raise InterruptedError("Build cancelled before artifact commit")
    value = {
        "candidate_digest": candidate["digest"],
        "source_digest": frozen["source_digest"],
        "job_id": operation_id,
        "wheel": wheel.relative_to(base).as_posix(),
        "wheel_sha256": file_sha(wheel),
        "runtime": (destination / "runtime").relative_to(base).as_posix(),
        "runtime_files": file_manifest(destination / "runtime"),
        "version": recipe["version"],
        "build_runtime": "docker",
        "build_image": image["digest"],
        "build_container_id": identity,
    }
    atomic_json(base / "engine-artifact.json", value)
    return {
        "passed": True,
        "artifact": value,
        "stage": "standard_wheel_built_and_installed",
        "inference_verified": False,
    }


def recover_build(infra, task, candidate, job):
    """Inspect and stop the original build group, without rebuilding or admitting output."""
    if job["data"].get("build_container_name"):
        from .container_runtime import stop_owned

        stop_owned(
            job["data"]["build_container_name"],
            job["data"]["build_labels"],
            job["data"].get("build_container_id"),
        )
        infra.store.operation(
            job["id"],
            "failed",
            recovered=True,
            result={
                "passed": False,
                "error": "Original Docker build reconciled; artifact not admitted",
                "failure_layer": "engine_build",
            },
        )
        return
    from .process_engine import process_alive

    receipt = {
        "pid": job["data"].get("build_pid"),
        "start_ticks": job["data"].get("build_start_ticks"),
    }
    if receipt["pid"] and process_alive(receipt):
        proc = Path("/proc") / str(receipt["pid"])
        command = (proc / "cmdline").read_bytes().decode().split("\0")[:-1]
        if (
            command not in [job["data"]["build_command"], job["data"].get("build_launch")]
            or (proc / "cwd").resolve() != Path(job["data"]["build_cwd"])
            or proc.stat().st_uid != os.getuid()
            or os.getpgid(receipt["pid"]) != receipt["pid"]
        ):
            raise ValueError("Build process ownership differs; keep unknown operation")
        os.killpg(receipt["pid"], 15)
        until = time.monotonic() + 5
        while process_alive(receipt) and time.monotonic() < until:
            time.sleep(0.1)
        if process_alive(receipt):
            os.killpg(receipt["pid"], 9)
    if receipt["pid"]:
        from .process_engine import reconcile_owned_group

        reconcile_owned_group(receipt, Path(job["data"]["build_cwd"]))
    # Unsettled output is deliberately not a completed build. New explicit
    # build writes a new operation directory, preserving all prior evidence.
    infra.store.operation(
        job["id"],
        "failed",
        recovered=True,
        result={
            "passed": False,
            "error": "Original build reconciled; artifact not admitted",
            "failure_layer": "engine_build",
        },
    )
