"""Trusted Docker broker. Workload code receives only a private container and inputs."""

import contextlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from .docker import docker

IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
ID = re.compile(r"[a-f0-9]{32}")
LEASE = """exec 3<&0
"$@" <&3 3<&- & child=$!
exec 3<&-
(sleep "$PILOT_LEASE_SECONDS"; kill -TERM "$child" 2>/dev/null; sleep 2; kill -KILL "$child" 2>/dev/null) & guard=$!
wait "$child"; code=$?; kill "$guard" 2>/dev/null; exit "$code"
"""
CONTROL_ENV = {
    "PILOT_MCP_SANDBOX_SPEC",
    "PILOT_MCP_CONTAINER_ENV",
    "PATH",
    "HOME",
    "SHELL",
    "ENV",
    "BASH_ENV",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "NODE_OPTIONS",
    "NODE_PATH",
    "SUDO_ASKPASS",
    "PILOT_LEASE_SECONDS",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
}


def atomic(path, value):
    if path.is_symlink():
        raise ValueError("Sandbox metadata cannot be a symbolic link")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def base(root):
    root = Path(root).resolve()
    local = root / ".local"
    local.mkdir(exist_ok=True, mode=0o700)
    if local.is_symlink():
        raise ValueError("Invalid sandbox control directory")
    directory = local / "execution-sandbox"
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Invalid sandbox control directory")
    return directory


def policy(root):
    path = base(root) / "policy.json"
    value = (
        json.loads(path.read_text())
        if path.exists()
        else {"version": 1, "revision": 0, "probe_image": None}
    )
    if path.is_symlink() or value.get("version") != 1 or type(value.get("revision")) is not int:
        raise ValueError("Sandbox policy invalid")
    if value.get("probe_image") is not None and not IMAGE.fullmatch(value["probe_image"]):
        raise ValueError("Sandbox probe image must be a full local image ID")
    return value


def configure(root, arguments):
    value = policy(root)
    if arguments.get("expected_revision") != value["revision"]:
        raise ValueError("Sandbox policy revision conflict; refresh first")
    image = arguments.get("probe_image")
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("Use a full sha256 image ID already present on the Pilot server")
    obj = json.loads(docker(["image", "inspect", image]).stdout)[0]
    if obj["Id"] != image:
        raise ValueError("Sandbox image identity mismatch")
    value.update(probe_image=image, revision=value["revision"] + 1)
    atomic(base(root) / "policy.json", value)
    return view(root)


@contextlib.contextmanager
def lock(root):
    import fcntl

    with (base(root) / "writer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def receipt(root, identity):
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        raise ValueError("Invalid sandbox identity")
    path = base(root) / identity
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Sandbox lease unavailable")
    p = path / "receipt.json"
    if p.is_symlink():
        raise ValueError("Sandbox receipt changed")
    value = json.loads(p.read_text())
    if value["id"] != identity or value["root"] != str(Path(root).resolve()):
        raise ValueError("Sandbox ownership mismatch")
    return path, value


def labels(value):
    return {"pilot.sandbox": value["id"], "pilot.sandbox.owner": value["owner_token"]}


def inspect_owned(value):
    result = docker(["inspect", value.get("container_id") or value["name"]], check=False)
    if result.returncode:
        if "no such" in result.stderr.lower():
            return None
        raise ValueError("Sandbox inspection unavailable; no cleanup or new allocation")
    obj = json.loads(result.stdout)[0]
    if not all(obj["Config"].get("Labels", {}).get(k) == v for k, v in labels(value).items()):
        raise ValueError("Foreign/replaced container; cleanup refused")
    return obj


def stop(root, identity, final=True, owner_token=None):
    with lock(root):
        return _stop(root, identity, final, owner_token)


def _stop(root, identity, final=True, owner_token=None):
    directory, value = receipt(root, identity)
    # A retired transport must never close a newer negotiation generation.
    if owner_token is not None and value["owner_token"] != owner_token:
        return public(value)
    if value["status"] != "closed":
        obj = inspect_owned(value)
        if obj:
            docker(["rm", "--force", obj["Id"]], timeout=20)
    negotiating = (
        not final
        and not value.get("finalized")
        and value["status"] not in {"closed", "failed"}
        and not value.get("execution_started")
        and value.get("attempt", 0) == 0
        and time.time() < value["deadline"]
    )
    value.update(
        status="negotiating" if negotiating else "closed",
        finalized=final or value.get("finalized", False),
        closed_at=time.time(),
    )
    atomic(directory / "receipt.json", value)
    inputs = directory / "inputs"
    if not negotiating and inputs.exists():
        if inputs.is_symlink() or inputs.parent != directory:
            raise ValueError("Sandbox input ownership changed")
        shutil.rmtree(inputs)
    return public(value)


def public(value):
    return {
        k: value.get(k)
        for k in (
            "id",
            "server_name",
            "root_session",
            "status",
            "image",
            "deadline",
            "created_at",
            "closed_at",
            "container_id",
            "verified",
            "scope",
            "last_error",
        )
    }


def view(root):
    setting = policy(root)
    rows = []
    for path in base(root).glob("*/receipt.json"):
        _, value = receipt(root, path.parent.name)
        row = public(value)
        if value["status"] not in {"closed", "failed"} and time.time() >= value["deadline"]:
            row["status"] = "expired_needs_cleanup"
        rows.append(row)
    report_path = base(root) / "self-test.json"
    return {
        "policy": setting,
        "platform": sys.platform,
        "supported": sys.platform == "linux",
        "max_active": 4,
        "limits": {
            "cpu_cores": 1,
            "memory_mib": 512,
            "pids": 64,
            "workspace_mib": 128,
            "temp_mib": 64,
            "network": "none",
            "gpu": "none",
            "user": "65534:65534",
        },
        "leases": sorted(rows, key=lambda x: (x["status"] not in {"closed", "failed"}, x["created_at"]), reverse=True)[:100],
        "active_count": sum(r["status"] not in {"closed", "failed"} for r in rows),
        "last_self_test": self_test_view(report_path, setting),
        "boundary": "Host broker is trusted; MCP/code containers have no host credentials/socket or writable host mount. HTTP MCP remains an external service.",
    }


def self_test_view(path, setting):
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    report["current"] = (
        report.get("image") == setting["probe_image"]
        and report.get("policy_revision") == setting["revision"]
    )
    return report


def inputs_snapshot(root, source, destination):
    source = Path(source)
    control = Path(root).resolve() / ".local"
    if not source.is_absolute() or not source.is_dir() or source.resolve() != source:
        raise ValueError("Workspace must be a real absolute directory")
    if source == control or source.is_relative_to(control) or control.is_relative_to(source):
        raise ValueError("Sandbox cannot snapshot the Pilot control directory")
    total, count = 0, 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Workspace contains a symbolic link; snapshot refused")
        rel = path.relative_to(source)
        if any(x.startswith(".") for x in rel.parts) or path.suffix.lower() in {
            ".pem",
            ".key",
            ".env",
            ".p12",
        }:
            continue
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("Workspace contains a special file or hardlink")
        count += 1
        total += path.stat().st_size
        if count > 128 or total > 4 * 1024**2 or path.stat().st_size > 1024**2:
            raise ValueError(
                "Workspace snapshot exceeds 128 files / 4 MiB; select a smaller workspace"
            )
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(path.read_bytes())
        target.chmod(0o644)
    destination.chmod(0o755)
    return {"files": count, "bytes": total}


def prepare(root, args):
    if sys.platform != "linux":
        raise ValueError("Local MCP containers require the Linux Docker executor; no host fallback")
    required = {
        "server_name",
        "root_session",
        "image",
        "command",
        "args",
        "env_names",
        "cwd",
        "workspace_read",
        "lease_seconds",
    }
    if set(args) != required:
        raise ValueError("Invalid sandbox launch contract")
    image, command = args["image"], args["command"]
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("Local MCP requires a pinned, already available container image")
    if (
        not isinstance(command, str)
        or not command.startswith("/")
        or len(command) > 2048
        or any(c in command for c in "\n\r\0")
    ):
        raise ValueError("MCP command must be an absolute path inside its container")
    if (
        not isinstance(args["args"], list)
        or len(args["args"]) > 100
        or any(not isinstance(x, str) or len(x) > 4096 or "\0" in x for x in args["args"])
    ):
        raise ValueError("Invalid MCP argv")
    if (
        not isinstance(args["root_session"], str)
        or len(args["root_session"]) > 200
        or not isinstance(args["server_name"], str)
    ):
        raise ValueError("Invalid MCP scope")
    if type(args["lease_seconds"]) is not int or not 30 <= args["lease_seconds"] <= 1800:
        raise ValueError("MCP lease must be 30–1800 seconds")
    if not isinstance(args["env_names"], list) or any(
        not isinstance(k, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or k in CONTROL_ENV
        or k.startswith(("LD_", "DYLD_", "REPOPILOT_", "DSH_", "PILOT_DOCKER_", "PILOT_MODEL_"))
        for k in args["env_names"]
    ):
        raise ValueError(
            "MCP environment cannot override the broker or inherit control credentials"
        )
    image_obj = json.loads(docker(["image", "inspect", image]).stdout)[0]
    if image_obj["Id"] != image:
        raise ValueError("MCP image identity changed")
    with lock(root):
        # Expired leases are reconciled by exact container labels before counting slots.
        for row in view(root)["leases"]:
            if row["status"] == "expired_needs_cleanup":
                _stop(root, row["id"])
        if view(root)["active_count"] >= 4:
            raise ValueError("Sandbox concurrency limit reached; close an idle lease first")
        identity = uuid.uuid4().hex
        directory = base(root) / identity
        directory.mkdir(mode=0o700)
        inputs = directory / "inputs"
        inputs.mkdir(mode=0o755)
        try:
            scope = (
                inputs_snapshot(root, args["cwd"], inputs)
                if args["workspace_read"]
                else {"files": 0, "bytes": 0}
            )
        except Exception:
            shutil.rmtree(directory)
            raise
        value = {
            "version": 1,
            "id": identity,
            "root": str(Path(root).resolve()),
            "name": "pilot-sandbox-" + identity,
            "owner_token": uuid.uuid4().hex,
            "status": "prepared",
            "created_at": time.time(),
            "deadline": time.time() + args["lease_seconds"],
            "scope": {
                "input": "readonly snapshot" if args["workspace_read"] else "empty",
                "output": "private tmpfs; no automatic host writeback",
                **scope,
            },
            **args,
            "verified": False,
        }
        atomic(directory / "receipt.json", value)
        return {
            "id": identity,
            "spec_path": str(directory / "receipt.json"),
            "deadline": value["deadline"],
        }


def container_args(directory, value):
    result = [
        "create",
        "--interactive",
        "--name",
        value["name"],
        "--runtime",
        "runc",
        "--user",
        "65534:65534",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--network",
        "none",
        "--cpus",
        "1",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,uid=65534,gid=65534,mode=700",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=128m,uid=65534,gid=65534,mode=700",
        "--mount",
        "type=bind,src=" + str(directory / "inputs") + ",dst=/input,readonly",
        "--workdir",
        "/workspace",
        "--env",
        "PILOT_LEASE_SECONDS=" + str(max(1, int(value["deadline"] - time.time()))),
        "--env",
        "CUDA_VISIBLE_DEVICES=-1",
        "--env",
        "NVIDIA_VISIBLE_DEVICES=void",
        "--env",
        "HOME=/workspace",
        "--entrypoint",
        "/bin/sh",
    ]
    for k, v in labels(value).items():
        result += ["--label", k + "=" + v]
    if value["env_names"]:
        result += ["--env-file", str(directory / "runtime.env")]
    return result + [value["image"], "-c", LEASE, "pilot-sandbox", value["command"], *value["args"]]


def attest(obj, value, directory):
    host, config = obj["HostConfig"], obj["Config"]
    checks = {
        "non_root": config["User"] == "65534:65534",
        "read_only_root": host["ReadonlyRootfs"] is True,
        "capabilities": host.get("CapDrop") == ["ALL"] and not host.get("Privileged"),
        "no_new_privileges": any(
            s.startswith("no-new-privileges") for s in host.get("SecurityOpt", [])
        ),
        "network": host["NetworkMode"] == "none",
        "devices": not host.get("Devices") and not host.get("DeviceRequests"),
        "limits": host["Memory"] == 512 * 1024**2
        and host["MemorySwap"] == 512 * 1024**2
        and host["PidsLimit"] == 64
        and host["NanoCpus"] == 1000000000,
        "image": obj["Image"] == value["image"],
        "runtime": host["Runtime"] == "runc",
        "mounts": all(
            m["Type"] == "tmpfs"
            or m["Type"] == "bind"
            and m["Source"] == str(directory / "inputs")
            and m["Destination"] == "/input"
            and not m["RW"]
            for m in obj["Mounts"]
        ),
        "input_mount": len([m for m in obj["Mounts"] if m["Type"] == "bind"]) == 1,
        "scratch": set(host.get("Tmpfs", {})) == {"/workspace", "/tmp"}
        and "size=128m" in host["Tmpfs"]["/workspace"]
        and "size=64m" in host["Tmpfs"]["/tmp"],
        "labels": all(config.get("Labels", {}).get(k) == v for k, v in labels(value).items()),
    }
    if not all(checks.values()):
        raise ValueError("Sandbox container contract was not enforced")
    return checks


def launch(root, identity, environment):
    with lock(root):
        try:
            return _launch(root, identity, environment)
        except Exception:
            # Failed creation/attestation owns no reusable admission. Exact label
            # verification prevents this rollback from touching foreign objects.
            _, state = receipt(root, identity)
            if state["status"] == "prepared":
                _stop(root, identity)
            raise


def _launch(root, identity, environment):
    directory, value = receipt(root, identity)
    if (
        value["status"] == "negotiating"
        and not value.get("finalized")
        and not value.get("execution_started")
        and value.get("attempt", 0) < 1
        and time.time() < value["deadline"]
    ):
        # Native MCP era negotiation may rebuild its stdio transport once.
        # No tool has executed: start a fresh container under the same absolute lease.
        value.update(
            status="prepared",
            attempt=1,
            owner_token=uuid.uuid4().hex,
            name="pilot-sandbox-" + identity + "-negotiation",
            container_id=None,
        )
        atomic(directory / "receipt.json", value)
    if value["status"] != "prepared" or time.time() >= value["deadline"]:
        raise ValueError("Sandbox already started/expired; no replay")
    if set(environment) != set(value["env_names"]) or any(
        not isinstance(v, str) or len(v) > 8192 or "\0" in v for v in environment.values()
    ):
        raise ValueError("Sandbox environment scope mismatch")
    if inspect_owned(value):
        raise ValueError("Existing container cannot be adopted or replayed")
    # A private temporary env file survives sudo's environment scrubbing. It is
    # never mounted or recorded and values never become command-line arguments.
    env_file = directory / "runtime.env"
    try:
        if environment:
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for key, value_text in environment.items():
                    if any(c in value_text for c in "\r\n\0"):
                        raise ValueError("MCP environment contains a line break")
                    stream.write(key + "=" + value_text + "\n")
        outcome = docker(container_args(directory, value), timeout=25, check=False)
    finally:
        env_file.unlink(missing_ok=True)
    if outcome.returncode:
        raise ValueError("Sandbox container creation failed")
    value["container_id"] = outcome.stdout.strip()
    atomic(directory / "receipt.json", value)
    obj = inspect_owned(value)
    checks = attest(obj, value, directory)
    value.update(status="running", verified=True, checks=checks)
    atomic(directory / "receipt.json", value)
    return value


def self_test(root):
    setting = policy(root)
    image = setting["probe_image"]
    if not image:
        raise ValueError("Configure an already available Python container image before self-test")
    parent = Path(root).resolve() / "runs/sandbox-probes"
    parent.mkdir(parents=True, exist_ok=True)
    source = parent / uuid.uuid4().hex
    source.mkdir(mode=0o700)
    (source / "public.txt").write_text("public sandbox canary")
    outside = parent / (source.name + "-outside.txt")
    outside.write_text("private test canary")
    probe = """import os,json,socket
from pathlib import Path
checks={'non_root':os.getuid()!=0,'no_gpu':not Path('/dev/nvidia0').exists(),'no_docker_socket':not Path('/var/run/docker.sock').exists(),'no_control_key':'REPOPILOT_API_KEY' not in os.environ,'host_file_hidden':not Path(OUTSIDE).exists(),'input_read':Path('/input/public.txt').read_text()=='public sandbox canary'}
for name,path in [('root_write_denied','/host-write-probe'),('input_write_denied','/input/public.txt')]:
 try: Path(path).write_text('bad');checks[name]=False
 except OSError: checks[name]=True
try:
 s=socket.create_connection(('1.1.1.1',443),timeout=.3);s.close();checks['network_denied']=False
except OSError: checks['network_denied']=True
Path('/workspace/private.txt').write_text('private output');checks['private_write']=True
print(json.dumps({'passed':all(checks.values()),'checks':checks}))
""".replace("OUTSIDE", repr(str(outside)))
    prepared = None
    try:
        prepared = prepare(
            root,
            {
                "server_name": "self-test",
                "root_session": "self-test",
                "image": image,
                "command": "/usr/local/bin/python3",
                "args": ["-c", probe],
                "env_names": [],
                "cwd": str(source),
                "workspace_read": True,
                "lease_seconds": 30,
            },
        )
        value = launch(root, prepared["id"], {})
        result = docker(["start", "--attach", value["container_id"]], timeout=20)
        data = json.loads(result.stdout)
        data.update(
            at=time.time(),
            image=image,
            container_id=value["container_id"],
            contract_verified=value["verified"],
            policy_revision=setting["revision"],
        )
        atomic(base(root) / "self-test.json", data)
        return data
    except Exception as error:
        atomic(base(root) / "self-test.json", {
            "at": time.time(), "image": image, "policy_revision": setting["revision"],
            "passed": False, "checks": {}, "error": type(error).__name__,
        })
        raise
    finally:
        if prepared:
            try:
                stop(root, prepared["id"])
            except Exception as error:
                atomic(base(root) / "self-test.json", {
                    "at": time.time(), "image": image, "policy_revision": setting["revision"],
                    "passed": False, "checks": {}, "error": "cleanup: " + type(error).__name__,
                })
                raise
        shutil.rmtree(source)
        outside.unlink(missing_ok=True)


def dispatch(root, operation, args):
    if operation == "view":
        return view(root)
    if operation == "prepare":
        return prepare(root, args)
    if operation == "stop":
        return stop(root, args["id"])
    if operation == "seal":
        with lock(root):
            directory, value = receipt(root, args["id"])
            if value["status"] != "running" or time.time() >= value["deadline"]:
                raise ValueError("Sandbox has closed/expired before tool admission")
            value["execution_started"] = True
            atomic(directory / "receipt.json", value)
            return {"sealed": True}
    if operation == "configure":
        return configure(root, args)
    if operation == "self-test":
        return self_test(root)
    raise ValueError("Unknown sandbox operation")
