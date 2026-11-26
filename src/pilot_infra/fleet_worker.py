"""Portable, standard-library-only node worker. No LLM and no arbitrary shell."""

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Metadata resolution is also used by the Windows Owner UI.
    fcntl = None

SUFFIXES = {".json", ".safetensors", ".model", ".txt"}
IMAGE_PROBE = "import importlib.metadata as m,importlib.util as u,json,platform,pathlib;names=['torch','torch_musa','torch_npu','sglang','vllm','ray','sglang_router','mooncake'];versions={d.metadata['Name'].lower().replace('-','_'):d.version for d in m.distributions()};modules={n:u.find_spec(n) is not None for n in names};spec=u.find_spec('sglang');dirs=list(spec.submodule_search_locations or []) if spec else [];modules['sglang_ray']=any((pathlib.Path(p)/'srt/ray/engine.py').is_file() for p in dirs);print(json.dumps({'architecture':platform.machine(),'packages':{n:versions.get(n) for n in names},'modules':modules}))"

# PID 1 enforces the absolute lease even if SSH/controller/node watchdog dies.
# All model code stays in the container, with no privileged host supervisor.
LEASE_SUPERVISOR = """import os,signal,subprocess,sys,time
deadline=float(sys.argv[1])
if time.time()>=deadline:sys.exit(124)
p=subprocess.Popen(sys.argv[2:],start_new_session=True)
try:
 while p.poll() is None and time.time()<deadline:time.sleep(.2)
 if p.poll() is None:
  os.killpg(p.pid,signal.SIGTERM)
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL)
finally:
 if p.poll() is None:os.killpg(p.pid,signal.SIGKILL)
sys.exit(p.wait())
"""

ADAPTER_RUNNER = """import importlib.util,json
spec=importlib.util.spec_from_file_location('adapter','/source/adapter.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
print(json.dumps(module.build_options()))
"""


def validate_adapter_options(options):
    if not isinstance(options, dict) or set(options) - {
        "max_tokens",
        "stop",
        "temperature",
        "chat_template_kwargs",
    }:
        raise ValueError("Adapter returned unsupported keys")
    if "max_tokens" in options and (
        type(options["max_tokens"]) is not int or not 8 <= options["max_tokens"] <= 1024
    ):
        raise ValueError("Unbounded output length")
    if "temperature" in options and (
        type(options["temperature"]) not in {int, float} or options["temperature"] != 0
    ):
        raise ValueError("Sampling must be deterministic")
    if "stop" in options and (
        not isinstance(options["stop"], list)
        or len(options["stop"]) > 4
        or any(not isinstance(s, str) or not 0 < len(s) <= 80 for s in options["stop"])
    ):
        raise ValueError("Invalid stop list")
    if "chat_template_kwargs" in options and options["chat_template_kwargs"] != {
        "enable_thinking": False
    }:
        raise ValueError("Unsupported template setting")
    return options


def cleanup_adapter(root, operation_id):
    row = json.loads(journal(root, operation_id).read_text())
    receipt = row.get("adapter_container")
    if not receipt:
        return
    obj = inspect(receipt["name"])
    if obj:
        if not owned_container(obj, receipt):
            raise ValueError("Foreign adapter container cleanup denied")
        docker(["rm", "--force", obj["Id"]], timeout=15)


def check_adapter(root, request, operation_id):
    source = base64.b64decode(request["adapter_source"], validate=True)
    if (
        len(source) > 65536
        or hashlib.sha256(source).hexdigest() != request["candidate"]["source_sha256"]
    ):
        raise ValueError("Frozen adapter source identity/size differs")
    image = request["image"]["digest"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Fixed node adapter image required")
    base = path_in(root, "adapters/" + operation_id)
    base.mkdir(parents=True, exist_ok=False, mode=0o755)
    path = base / "adapter.py"
    path.write_bytes(source)
    path.chmod(0o444)
    receipt = {
        "name": "pilot-adapter-" + operation_id[:24],
        "labels": {
            "pilot.adapter.operation": operation_id,
            "pilot.adapter.task": request["task_id"],
            "pilot.adapter.candidate": request["candidate"]["digest"],
        },
    }
    row = json.loads(journal(root, operation_id).read_text())
    row["adapter_container"] = receipt
    atomic(journal(root, operation_id), row)
    deadline = min(request["deadline"], time.time() + 20)
    try:
        if inspect(receipt["name"]):
            raise ValueError("Adapter container name already exists; reconcile original operation")
        args = [
            "create",
            "--name",
            receipt["name"],
            "--runtime",
            "runc",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=8m",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--mount",
            "type=bind,src=" + str(base) + ",dst=/source,readonly",
            "--workdir",
            "/source",
            "--entrypoint",
            "python3",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "NVIDIA_VISIBLE_DEVICES=void",
            "-e",
            "CUDA_VISIBLE_DEVICES=",
            "-e",
            "MUSA_VISIBLE_DEVICES=",
        ]
        for key, val in receipt["labels"].items():
            args += ["--label", key + "=" + val]
        args += [
            image,
            "-I",
            "-c",
            LEASE_SUPERVISOR,
            str(deadline),
            "python3",
            "-I",
            "-c",
            ADAPTER_RUNNER,
        ]
        receipt["container_id"] = docker(args).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", receipt["container_id"]):
            raise ValueError("Docker did not return a container identity")
        row = json.loads(journal(root, operation_id).read_text())
        row["adapter_container"] = receipt
        atomic(journal(root, operation_id), row)
        obj = inspect(receipt["container_id"])
        if not owned_container(obj, receipt) or obj["Image"] != image:
            raise ValueError("Node adapter container identity differs")
        if (
            obj["HostConfig"].get("Devices")
            or obj["HostConfig"].get("DeviceRequests")
            or obj["HostConfig"].get("Privileged")
        ):
            raise ValueError("Adapter CPU sandbox unexpectedly received device access")
        policy = obj["HostConfig"]
        if (
            policy.get("Runtime") != "runc"
            or policy.get("NetworkMode") != "none"
            or not policy.get("ReadonlyRootfs")
            or obj["Config"].get("User") != "65534:65534"
        ):
            raise ValueError("Adapter CPU sandbox isolation differs")
        if (
            policy.get("Memory") != 256 * 1024**2
            or policy.get("NanoCpus") != 1000000000
            or policy.get("PidsLimit") != 32
        ):
            raise ValueError("Adapter CPU sandbox limits differ")
        if {m["Destination"] for m in obj["Mounts"]} != {"/source"}:
            raise ValueError("Adapter image introduced unexpected mounts")
        docker(["start", receipt["container_id"]])
        while True:
            obj = inspect(receipt["container_id"])
            if not owned_container(obj, receipt):
                raise ValueError("Adapter container identity changed")
            if not obj["State"]["Running"]:
                if obj["State"]["ExitCode"] != 0:
                    raise ValueError("Node adapter candidate failed or exceeded its lease")
                break
            if cancelled(root, operation_id, deadline + 5):
                raise InterruptedError("Adapter check cancelled/expired")
            time.sleep(0.1)
        output = docker(["logs", receipt["container_id"]]).stdout
        if len(output.encode()) > 16000:
            raise ValueError("Adapter output exceeded budget")
        options = validate_adapter_options(json.loads(output))
        return {
            "options": options,
            "source_sha256": request["candidate"]["source_sha256"],
            "image": image,
            "execution_node": node_identity(),
            "gpu_access": False,
        }
    finally:
        cleanup_adapter(root, operation_id)


def network_info(name):
    value = docker(["network", "inspect", name], check=False)
    if value.returncode:
        if "no such" in value.stderr.casefold() or "not found" in value.stderr.casefold():
            return None
        raise ValueError("Network identity unavailable")
    return json.loads(value.stdout)[0]


def owned_container(obj, receipt):
    return bool(
        obj
        and all(obj["Config"].get("Labels", {}).get(k) == v for k, v in receipt["labels"].items())
        and (not receipt.get("container_id") or obj["Id"] == receipt["container_id"])
    )


def watch_service(root, task_id):
    while state_path(root, task_id).is_file():
        record = json.loads(state_path(root, task_id).read_text())
        if record.get("stopped"):
            return
        if cancelled(root, record["labels"]["pilot.infra.launch"], record["deadline"]):
            with path_in(root, "node-mutation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                stop_service(root, task_id)
            return
        time.sleep(0.2)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def identity():
    return hashlib.sha256(Path("/etc/machine-id").read_text().strip().encode()).hexdigest()


def node_identity():
    machine_id = Path("/etc/machine-id").read_text().strip()
    try:
        hardware = Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower()
    except OSError:
        hardware = ""
    if not hardware or set(hardware.replace("-", "")) <= {"0", "f"}:
        hardware = ""
    return hashlib.sha256((machine_id + "\0" + hardware).encode()).hexdigest()


def boot():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def path_in(root, relative):
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Node path escape")
    path = root / part
    if not path.resolve().is_relative_to(root) or any(
        p.is_symlink() for p in [path, *path.parents] if p != root.parent
    ):
        raise ValueError("Node symlink escape")
    return path


def command(args, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise ValueError(
            "Node command failed: " + Path(args[0]).name + " (exit " + str(result.returncode) + ")"
        )
    return result


def cuda_busy_devices(text, gpu_map):
    """Reject incomplete or unrecognized process records before GPU allocation."""
    busy = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 2 or fields[0] not in gpu_map or not fields[1].isdigit():
            raise ValueError("GPU process occupancy could not be verified")
        busy.add(gpu_map[fields[0]])
    return busy


def musa_busy_devices(text, gpu_indices):
    if "Processes:" not in text:
        raise ValueError("GPU process occupancy could not be verified")
    busy = set()
    saw_process = saw_empty = False
    for line in text.split("Processes:", 1)[1].splitlines():
        row = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(.+?)\s+(\d+)MiB\s*", line)
        if row:
            index = int(row.group(1))
            if index not in gpu_indices:
                raise ValueError("GPU process occupancy could not be verified")
            saw_process = True
            busy.add(index)
        elif line.strip().lower() == "no running processes found":
            saw_empty = True
        elif not (not line.strip() or re.fullmatch(r"[\s+|=\-]+", line)
                  or re.fullmatch(r"\s*ID\s+PID\s+Process name\s+GPU Memory\s*", line)
                  or line.strip() == "Usage"):
            raise ValueError("GPU process occupancy could not be verified")
    if saw_process == saw_empty:
        raise ValueError("GPU process occupancy could not be verified")
    return busy


def docker(args, **kwargs):
    prefix, password = docker_access()
    check = kwargs.pop("check", True)
    result = subprocess.run(
        [*prefix, *args],
        input=password + "\n" if password else None,
        capture_output=True,
        text=True,
        env=docker_environment(),
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )
    if password:
        result.stdout = result.stdout.replace(password, "[REDACTED]")
        result.stderr = result.stderr.replace(password, "[REDACTED]")
    if check and result.returncode:
        raise ValueError(
            "Node Docker operation failed (exit "
            + str(result.returncode)
            + "): "
            + result.stderr[-800:].strip()
        )
    return result


def docker_environment():
    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"PILOT_NODE_DOCKER_SUDO_PASSWORD", "PILOT_NODE_DOCKER_USE_SUDO"}
    }


def docker_access():
    # The Host may reuse its already configured local Docker credential.
    # It is never journalled, uploaded to a remote node, or injected in containers.
    password = os.environ.get("PILOT_NODE_DOCKER_SUDO_PASSWORD")
    return (
        ["sudo", "-S", "-p", "", "/usr/bin/docker"]
        if password
        else ["sudo", "-n", "/usr/bin/docker"]
        if os.environ.get("PILOT_NODE_DOCKER_USE_SUDO") == "1"
        else ["docker"],
        password,
    )


def inspect(name):
    result = docker(["inspect", name], check=False)
    if result.returncode:
        if "no such" in result.stderr.casefold():
            return None
        raise ValueError("Docker identity unavailable")
    return json.loads(result.stdout)[0]


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def model_manifest(directory):
    base = Path(directory)
    if base.is_symlink() or not base.is_dir():
        raise ValueError("Model directory unavailable")
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix in {".json", ".safetensors", ".model", ".txt"}:
            if p.is_symlink() or not p.resolve().is_relative_to(base.resolve()):
                raise ValueError("Model symlink denied")
            files.append(
                {
                    "path": p.relative_to(base).as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": sha(p),
                }
            )
    if not any(f["path"].endswith(".safetensors") for f in files):
        raise ValueError("Full safetensors weights missing")
    config = json.loads((base / "config.json").read_text())
    index = base / "model.safetensors.index.json"
    if index.is_file():
        shards = set(json.loads(index.read_text()).get("weight_map", {}).values())
        available = {f["path"] for f in files}
        if not shards or not shards.issubset(available):
            raise ValueError("Indexed model shards are missing")
    if not (base / "tokenizer.json").is_file() and not any(base.glob("*.model")):
        raise ValueError("Tokenizer missing")
    return {
        "files": files,
        "digest": digest(files),
        "config": config,
        "bytes": sum(f["bytes"] for f in files),
    }


def https_bytes(url):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("HTTPS source required")
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "Pilot/0.8"}), timeout=30
    ) as response:
        data = response.read(16 * 1024**2 + 1)
    if len(data) > 16 * 1024**2:
        raise ValueError("Source metadata exceeds limit")
    return data


def https_json(url):
    return json.loads(https_bytes(url))


def resolve_model(endpoint, repo, revision):
    if endpoint not in {"https://huggingface.co", "https://hf-mirror.com"} or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise ValueError("Invalid model hub")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", revision):
        raise ValueError("Invalid revision")
    data = https_json(endpoint + "/api/models/" + repo + "/revision/" + revision + "?blobs=true")
    if not re.fullmatch(r"[a-f0-9]{40}", data.get("sha", "")):
        raise ValueError("Model revision unavailable")
    files = []
    for f in data.get("siblings", []):
        name = f["rfilename"]
        if Path(name).suffix not in SUFFIXES:
            continue
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Remote model path escape")
        size = f.get("size", f.get("lfs", {}).get("size"))
        expected = f.get("lfs", {}).get("sha256") or f.get("blobId")
        if (
            type(size) is not int
            or size < 0
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", expected or "")
        ):
            raise ValueError("File size/hash must be supplied by the fixed model revision")
        files.append({"path": name, "bytes": size, "hash": expected})
    config = data.get("config", {})
    config_file = next((f for f in files if f["path"] == "config.json"), None)
    if config_file:
        raw = https_bytes(endpoint + "/" + repo + "/resolve/" + data["sha"] + "/config.json")
        hashed = (
            hashlib.sha256(raw).hexdigest()
            if len(config_file["hash"]) == 64
            else hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        )
        if hashed != config_file["hash"] or len(raw) != config_file["bytes"]:
            raise ValueError("Fixed model configuration hash mismatch")
        config = json.loads(raw)
    return {
        "revision": data["sha"],
        "files": files,
        "bytes": sum(f["bytes"] for f in files),
        "config": config,
    }


def alive(record):
    try:
        parts = Path("/proc/" + str(record["pid"]) + "/stat").read_text().rsplit(")", 1)[1].split()
        return (
            parts[0] != "Z" and parts[19] == record["start_ticks"] and record["boot_id"] == boot()
        )
    except (OSError, KeyError):
        return False


def journal(root, operation_id):
    if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
        raise ValueError("Invalid operation")
    return path_in(root, "operations/" + operation_id + ".json")


def cancelled(root, operation_id, deadline):
    return (
        time.time() >= deadline or path_in(root, "operations/" + operation_id + ".cancel").exists()
    )


def checked_asset_root(root, request):
    marker = root / "pilot-executor.json"
    config = json.loads(marker.read_text()) if marker.is_file() and not marker.is_symlink() else {}
    base = Path(config.get("asset_root", str(root / "assets")))
    if request.get("asset_root", str(base)) != str(base):
        raise ValueError("Resource root changed after operation approval")
    if not base.is_absolute() or ".." in base.parts or base == root or root.is_relative_to(base):
        raise ValueError("Invalid node resource root")
    for p in [base, *base.parents]:
        if p.is_symlink():
            raise ValueError("Node resource root cannot contain symbolic links")
    if base != root / "assets":
        owned = base / ".pilot-node-assets.json"
        if (
            owned.is_symlink()
            or not owned.is_file()
            or json.loads(owned.read_text()).get("owner_namespace") != config.get("owner_namespace")
        ):
            raise ValueError("Node resource root ownership differs")
    return base


def download_model(root, request, operation_id):
    source = request["source"]
    if source["kind"] == "existing":
        return {
            "path": source["location"],
            "manifest": model_manifest(source["location"]),
            "owned": False,
        }
    manifest = resolve_model(source["endpoint"], source["location"], source["revision"])
    if manifest["revision"] != source["revision"] or manifest["bytes"] > request["max_bytes"]:
        raise ValueError("Model identity/size exceeds the reviewed download")
    asset_root = checked_asset_root(root, request)
    base = path_in(asset_root, "models/" + request["asset_id"] + "/" + source["revision"])
    base.mkdir(parents=True, exist_ok=True, mode=0o755)
    base.chmod(0o755)
    if shutil.disk_usage(base).free < manifest["bytes"] + 512 * 1024**2:
        raise ValueError("Node disk insufficient")
    for f in manifest["files"]:
        p = path_in(base, f["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        if cancelled(root, operation_id, request["deadline"]):
            raise InterruptedError("Download cancelled/expired")
        url = (
            source["endpoint"]
            + "/"
            + source["location"]
            + "/resolve/"
            + source["revision"]
            + "/"
            + urllib.parse.quote(f["path"], safe="/")
        )
        temporary = p.with_name(p.name + ".part")
        if p.is_file() and p.stat().st_size == f["bytes"]:
            cached = (
                hashlib.sha256()
                if len(f["hash"]) == 64
                else hashlib.sha1(b"blob " + str(f["bytes"]).encode() + b"\0")
            )
            with p.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    cached.update(chunk)
            if cached.hexdigest() == f["hash"]:
                continue
        total = 0
        h = (
            hashlib.sha256()
            if len(f["hash"]) == 64
            else hashlib.sha1(b"blob " + str(f["bytes"]).encode() + b"\0")
        )
        with (
            urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Pilot/0.8"}), timeout=30
            ) as response,
            temporary.open("wb") as stream,
        ):
            for chunk in iter(lambda: response.read(1024**2), b""):
                if cancelled(root, operation_id, request["deadline"]):
                    raise InterruptedError("Download cancelled/expired")
                if shutil.disk_usage(base).free < 512 * 1024**2 + len(chunk):
                    raise ValueError("Node disk reserve exhausted; partial file retained")
                total += len(chunk)
                if total > f["bytes"]:
                    raise ValueError("Download exceeds frozen file size")
                stream.write(chunk)
                h.update(chunk)
        if total != f["bytes"] or h.hexdigest() != f["hash"]:
            raise ValueError("Downloaded file hash mismatch")
        temporary.replace(p)
        p.chmod(0o444)
        row = json.loads(journal(root, operation_id).read_text())
        row.update(
            progress={
                "file": f["path"],
                "downloaded_bytes": sum(
                    x.stat().st_size
                    for x in base.rglob("*")
                    if x.is_file() and not x.name.endswith(".part")
                ),
                "expected_bytes": manifest["bytes"],
            }
        )
        atomic(journal(root, operation_id), row)
    return {
        "path": str(base),
        "manifest": model_manifest(base),
        "owned": True,
        "source_revision": source["revision"],
    }


def image_asset(root, request, operation_id):
    source = request["source"]
    existing = source["kind"] == "existing"
    prior = docker(["image", "inspect", source["location"]], check=False)
    if not existing:
        manifest = json.loads(
            docker(["manifest", "inspect", source["location"]], timeout=45).stdout
        )
        if manifest.get("manifests"):
            import platform

            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
            descriptor = next(
                (
                    d
                    for d in manifest["manifests"]
                    if d.get("platform", {}).get("architecture") == architecture
                    and d.get("platform", {}).get("os") == "linux"
                ),
                None,
            )
            if not descriptor:
                raise ValueError("OCI image has no matching Linux architecture")
            manifest = json.loads(
                docker(
                    [
                        "manifest",
                        "inspect",
                        source["location"].split("@")[0] + "@" + descriptor["digest"],
                    ],
                    timeout=45,
                ).stdout
            )
        layers = manifest.get("layers", [])
        if (
            not layers
            or sum(layer.get("size", request["max_bytes"] + 1) for layer in layers)
            > request["max_bytes"]
        ):
            raise ValueError("OCI transfer size unknown or exceeds reviewed budget")
        storage = Path(docker(["info", "--format", "{{.DockerRootDir}}"]).stdout.strip())
        available = shutil.disk_usage(storage).free
        if available < request["max_bytes"] + 1024**3:
            raise ValueError("Docker storage lacks reviewed image budget plus reserve")
        log = path_in(root, "operations/" + operation_id + ".pull.log")
        row = json.loads(journal(root, operation_id).read_text())
        row["pull_started"] = True
        atomic(journal(root, operation_id), row)
        with log.open("wb") as stream:
            prefix, password = docker_access()
            process = subprocess.Popen(
                [*prefix, "pull", source["location"]],
                stdout=stream,
                stderr=stream,
                stdin=subprocess.PIPE if password else subprocess.DEVNULL,
                env=docker_environment(),
            )
            if password:
                process.stdin.write((password + "\n").encode())
                process.stdin.close()
            while process.poll() is None:
                free = shutil.disk_usage(storage).free
                if (
                    cancelled(root, operation_id, request["deadline"])
                    or free < 1024**3
                    or available - free > request["max_bytes"]
                ):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise ValueError(
                        "Pull interrupted; reconcile Docker daemon effects before another download"
                    )
                time.sleep(0.2)
            if process.returncode:
                raise ValueError("Fixed OCI pull failed")
        row = json.loads(journal(root, operation_id).read_text())
        row["pull_completed"] = True
        atomic(journal(root, operation_id), row)
    image = json.loads(docker(["image", "inspect", source["location"]]).stdout)[0]
    if image["Size"] > request["max_bytes"]:
        raise ValueError(
            "Image exceeds reviewed storage limit; inspect retained pull before cleanup"
        )
    if existing and image["Id"] != source["location"]:
        raise ValueError("Existing image ID changed")

    def normalized(value):
        value = value.removeprefix("docker.io/").removeprefix("registry-1.docker.io/")
        return value if "/" in value else "library/" + value

    if not existing and normalized(source["location"]) not in [
        normalized(v) for v in image.get("RepoDigests", [])
    ]:
        raise ValueError("Pulled OCI manifest not confirmed")
    probe_name = "pilot-image-check-" + operation_id[:16]
    row = json.loads(journal(root, operation_id).read_text())
    row["probe_name"] = probe_name
    atomic(journal(root, operation_id), row)
    code = IMAGE_PROBE
    environment = docker(
        [
            "run",
            "--rm",
            "--name",
            probe_name,
            "--label",
            "pilot.asset.operation=" + operation_id,
            "--runtime",
            "runc",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "32",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m",
            "--entrypoint",
            "python3",
            image["Id"],
            "-c",
            code,
        ],
        timeout=30,
    )
    facts = json.loads(environment.stdout)
    return {
        "digest": image["Id"],
        "repository_digests": image.get("RepoDigests", []),
        "architecture": image["Architecture"],
        "bytes": image["Size"],
        "owned": not existing and prior.returncode != 0,
        "environment": facts,
    }


def state_path(root, task_id):
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("Invalid task")
    return path_in(root, "services/" + task_id + ".json")


def service_identity(root, task_id):
    p = state_path(root, task_id)
    if not p.is_file():
        return {"passed": False, "error": "No node launch intent"}
    receipt = json.loads(p.read_text())
    obj = inspect(receipt["name"])
    if not obj:
        return {"passed": False, "error": "Node container absent", "receipt": receipt}
    expected = receipt["labels"]
    cfg = obj["Config"]
    host = obj["HostConfig"]
    checks = {
        "labels": all(cfg.get("Labels", {}).get(k) == v for k, v in expected.items()),
        "image": obj["Image"] == receipt["image"],
        "node": receipt["machine_identity"] == identity() and receipt["boot_id"] == boot(),
        "running": obj["State"]["Running"],
        "container": not receipt.get("container_id") or obj["Id"] == receipt["container_id"],
        "isolation": host["ReadonlyRootfs"]
        and not host["Privileged"]
        and not host.get("CapAdd")
        and set(host.get("CapDrop") or []) == {"ALL"}
        and set(host.get("SecurityOpt") or [])
        in ({"no-new-privileges"}, {"no-new-privileges:true"})
        and host.get("PidMode", "") != "host"
        and host.get("IpcMode", "") != "host"
        and host.get("PidsLimit") == 512,
        "user": cfg["User"] == "65534:65534",
        "command": cfg.get("Entrypoint") == ["python3"]
        and cfg.get("Cmd") == receipt["command"][1:],
        "memory": host["Memory"] == receipt["memory_bytes"]
        and host["MemorySwap"] == host["Memory"],
        "cpu": host["NanoCpus"] == receipt["cpu_nanos"],
        "lease": time.time() < receipt["deadline"] and alive(receipt.get("watchdog", {})),
        "weights": {
            m["Destination"]: (m["Source"], m["RW"])
            for m in obj.get("Mounts", [])
            if m.get("Type") == "bind"
        }
        == {
            "/model": (receipt["model_path"], False),
            **{m["target"]: (m["source"], False) for m in receipt.get("driver_mounts", [])},
        },
        "environment": all(
            dict(e.split("=", 1) for e in cfg.get("Env", []) if "=" in e).get(k) == v
            for k, v in receipt["environment"].items()
        ),
    }
    if receipt["platform"] == "cuda" and receipt.get("cuda_mode", "nvidia") == "nvidia":
        requests = host.get("DeviceRequests") or []
        checks["devices"] = (
            len(requests) == 1
            and requests[0].get("Count") != -1
            and sorted(requests[0].get("DeviceIDs") or []) == sorted(receipt["devices"])
        )
    else:
        checks["devices"] = (
            sorted(d["PathOnHost"] for d in host.get("Devices") or []) == sorted(receipt["devices"])
            and all(
                d.get("PathInContainer") == d["PathOnHost"] and d.get("CgroupPermissions") == "rw"
                for d in host.get("Devices") or []
            )
            and not host.get("DeviceRequests")
        )
    network = network_info(receipt["network"])
    checks["network"] = bool(
        network
        and network["Id"] == receipt.get("network_id")
        and network.get("Internal")
        and host.get("NetworkMode") == receipt["network"]
        and all(network.get("Labels", {}).get(k) == v for k, v in expected.items())
        and set(network.get("Containers") or {}).issubset({obj["Id"]})
        and set(obj.get("NetworkSettings", {}).get("Networks") or {}) == {receipt["network"]}
    )
    ports = obj.get("NetworkSettings", {}).get("Ports", {}).get("30000/tcp") or []
    binding = obj.get("NetworkSettings", {}).get("Networks", {}).get(receipt["network"], {})
    try:
        address = ipaddress.ip_address(binding.get("IPAddress", ""))
        bridge_endpoint = (
            address.version == 4
            and address.is_private
            and binding.get("NetworkID") == receipt.get("network_id")
        )
    except ValueError:
        bridge_endpoint = False
    checks["private_endpoint"] = (len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1") or (
        not ports and bridge_endpoint
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "container_id": obj["Id"],
        "started_at": obj["State"]["StartedAt"],
        "machine_identity": identity(),
        "boot_id": boot(),
        "config_digest": receipt["candidate_digest"],
        "image": obj["Image"],
        "model_manifest": receipt["model_manifest"],
        "runtime": "remote-docker",
        "engine": receipt["engine"],
        "gpu_ids": receipt["gpu_ids"],
        "port": int(ports[0]["HostPort"]) if ports else 30000,
        "address": "127.0.0.1" if ports else binding.get("IPAddress"),
        "deadline": receipt["deadline"],
        "launch_operation": receipt["labels"]["pilot.infra.launch"],
    }


def stop_service(root, task_id):
    p = state_path(root, task_id)
    if not p.is_file():
        return {"passed": True, "absent": True}
    record = json.loads(p.read_text())
    obj = inspect(record["name"])
    if obj:
        if not owned_container(obj, record):
            raise ValueError("Foreign/replaced container; stop denied")
        docker(["stop", "--time", "10", obj["Id"]], timeout=25)
        docker(["rm", obj["Id"]])
    if inspect(record["name"]):
        raise ValueError("Owned container still present")
    if record.get("network"):
        network = network_info(record["network"])
        if network:
            if (record.get("network_id") and network["Id"] != record["network_id"]) or not all(
                network.get("Labels", {}).get(k) == v for k, v in record["labels"].items()
            ):
                raise ValueError("Foreign/replaced network; removal denied")
            if network.get("Containers"):
                raise ValueError("Network contains another container; removal denied")
            docker(["network", "rm", network["Id"]])
    for resource in record["resources"]:
        lock = path_in(root, "locks/" + resource + ".json")
        if lock.is_file() and json.loads(lock.read_text()).get("task_id") == task_id:
            lock.unlink()
    record.update(stopped=True)
    atomic(p, record)
    return {"passed": True, "stopped": True, "task_id": task_id}


def launch(root, request, operation_id):
    task_id = request["task_id"]
    recipe = request["recipe"]
    candidate = request["candidate"]
    s = candidate["serving"]
    if candidate.get("engine_source"):
        raise ValueError("Remote engine-source loading needs a separately admitted recipe")
    image = json.loads(docker(["image", "inspect", request["image"]["digest"]]).stdout)[0]
    if image["Id"] != candidate["image_digest"]:
        raise ValueError("Frozen node image changed")
    manifest = model_manifest(request["model"]["path"])
    if manifest["digest"] != candidate["model_manifest_digest"]:
        raise ValueError("Frozen node model changed")
    if s["attention_backend"] not in recipe["attention_backends"]:
        raise ValueError("Backend outside stack recipe")
    # Re-observe before allocation. Initially remote jobs use empty devices
    # only, preserving all existing shared services.
    if recipe["platform"] == "cuda":
        gpu_rows = command(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.free", "--format=csv,noheader,nounits"]
        ).stdout.splitlines()
        free = {int(r.split(",")[0]): float(r.split(",")[2]) for r in gpu_rows}
        gpu_map = {r.split(",")[1].strip(): int(r.split(",")[0]) for r in gpu_rows}
        processes = command(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"]
        ).stdout.splitlines()
        busy = cuda_busy_devices("\n".join(processes), gpu_map)
    elif recipe["platform"] == "musa":
        text = command(["mthreads-gmi"]).stdout
        free = {
            int(r.group(1)): float(r.group(3)) - float(r.group(2))
            for r in re.finditer(
                r"^\s*(\d+)\s+[^|]+\|[^|]+\|\s*\d+%\s+(\d+)MiB\((\d+)MiB\)", text, re.M
            )
        }
        busy = musa_busy_devices(text, free)
    else:
        raise ValueError("This platform still requires a verified capacity/occupancy adapter")
    needed = manifest["bytes"] / 1024**2 / len(request["gpu_ids"]) + 2048
    if any(g in busy or free.get(g, 0) < needed for g in request["gpu_ids"]):
        raise ValueError(
            "Authorized device is occupied or lacks memory; never stop the existing service"
        )
    labels = {
        "pilot.node.root": digest(str(root)),
        "pilot.infra.task": task_id,
        "pilot.infra.candidate": candidate["digest"],
        "pilot.infra.launch": operation_id,
    }
    name = "pilot-node-" + task_id[:16] + "-" + candidate["id"]
    network_name = name + "-net"
    if inspect(name):
        raise ValueError("Existing launch must be reconciled, never adopted/replayed")
    if network_info(network_name):
        raise ValueError("Existing network must be reconciled, never adopted/replayed")
    devices = []
    args = []
    platform = recipe["platform"]
    gpu_ids = request["gpu_ids"]
    driver_mounts = []
    cuda_mode = (request.get("cuda_runtime") or {}).get("mode", "nvidia")
    if platform == "cuda":
        rows = command(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"]
        ).stdout.splitlines()
        mapped = {int(r.split(",")[0]): r.split(",")[1].strip() for r in rows}
        devices = [mapped[g] for g in gpu_ids]
        args = ["--gpus", '"device=' + ",".join(devices) + '"']
        visible = "CUDA_VISIBLE_DEVICES"
        visible_ids = devices
        if cuda_mode == "device-bind":
            mapping = {}
            for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
                values = dict(
                    line.split(":", 1) for line in info.read_text().splitlines() if ":" in line
                )
                gpu_identity, minor = (
                    values.get("GPU UUID", "").strip(),
                    values.get("Device Minor", "").strip(),
                )
                if gpu_identity and minor.isdecimal():
                    mapping[gpu_identity] = int(minor)
            if any(u not in mapping for u in visible_ids):
                raise ValueError("Cannot prove CUDA UUID/device-minor binding")
            devices = [
                "/dev/nvidiactl",
                "/dev/nvidia-uvm",
                *["/dev/nvidia" + str(mapping[u]) for u in visible_ids],
            ]
            args = []
            for device in devices:
                if not Path(device).exists():
                    raise ValueError("Authorized CUDA device missing")
                args += ["--device", device + ":" + device + ":rw"]
            for mount in request["cuda_runtime"]["driver_mounts"]:
                library = Path(mount["source"])
                if (
                    library.is_symlink()
                    or not library.is_file()
                    or library.stat().st_uid != 0
                    or not library.resolve().is_relative_to(Path("/usr/lib"))
                    or sha(library) != mount["sha256"]
                    or not re.fullmatch(
                        r"/pilot-driver/lib(?:cuda|nvidia-[a-z0-9-]+)\.so(?:\.[0-9]+)*",
                        mount["target"],
                    )
                ):
                    raise ValueError("Frozen CUDA driver library changed")
                driver_mounts.append(mount)
                if mount["target"] == "/pilot-driver/libcuda.so.1":
                    driver_mounts.append({**mount, "target": "/pilot-driver/libcuda.so"})
            if not any(m["target"] == "/pilot-driver/libcuda.so.1" for m in driver_mounts):
                raise ValueError("No frozen CUDA driver; configure GPU container prerequisites")
            for mount in driver_mounts:
                args += [
                    "--mount",
                    "type=bind,src=" + mount["source"] + ",dst=" + mount["target"] + ",readonly",
                ]
        elif cuda_mode != "nvidia":
            raise ValueError("Unsupported CUDA container binding")
    else:
        devices = [t.format(index=g) for g in gpu_ids for t in recipe["device_templates"]] + recipe[
            "shared_devices"
        ]
        for d in devices:
            if not Path(d).exists():
                raise ValueError("Declared accelerator device missing")
            args += ["--device", d + ":" + d + ":rw"]
        visible = "MUSA_VISIBLE_DEVICES" if platform == "musa" else "ASCEND_RT_VISIBLE_DEVICES"
        visible_ids = [str(g) for g in gpu_ids]
    resources = [platform + "-" + str(g) for g in gpu_ids]
    for resource in resources:
        lock = path_in(root, "locks/" + resource + ".json")
        lock.parent.mkdir(exist_ok=True)
        if lock.exists():
            raise ValueError("Node device reserved; reconcile original task")
    module = (
        "sglang.launch_server"
        if recipe["engine"] == "sglang"
        else "vllm.entrypoints.openai.api_server"
    )
    cmd = [
        "python3",
        "-m",
        module,
        "--model-path" if recipe["engine"] == "sglang" else "--model",
        "/model",
        "--host",
        "0.0.0.0",
        "--port",
        "30000",
        "--dtype",
        "bfloat16",
        "--served-model-name",
        request["model"]["id"],
    ]
    if recipe["engine"] == "sglang":
        cmd += [
            "--tp-size",
            str(len(gpu_ids)),
            "--context-length",
            str(s["context_length"]),
            "--mem-fraction-static",
            str(s["mem_fraction_static"]),
            "--attention-backend",
            s["attention_backend"],
        ]
        if s["disable_cuda_graph"]:
            cmd += ["--disable-cuda-graph"]
    else:
        cmd += [
            "--tensor-parallel-size",
            str(len(gpu_ids)),
            "--max-model-len",
            str(s["context_length"]),
            "--gpu-memory-utilization",
            str(s["mem_fraction_static"]),
        ]
    limits = request["limits"]
    if recipe["engine"] == "vllm" and s["disable_cuda_graph"]:
        cmd.append("--enforce-eager")
    memory = int(limits["memory_gib"] * 1024**3)
    cpu = int(limits["cpu_cores"] * 1e9)
    environment = {
        visible: ",".join(visible_ids),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HOME": "/tmp",
        "USER": "pilot",
        "LOGNAME": "pilot",
        "TORCHINDUCTOR_CACHE_DIR": "/jit/inductor",
        "TRITON_CACHE_DIR": "/jit/triton",
        "TMPDIR": "/jit",
    }
    if platform == "cuda" and cuda_mode == "device-bind":
        environment.update(
            LD_LIBRARY_PATH="/pilot-driver:/usr/local/cuda/lib64",
            TRITON_LIBCUDA_PATH="/pilot-driver",
        )
    if recipe["engine"] == "vllm" and s["attention_backend"] in {"torch_native", "flash_attn"}:
        environment["VLLM_ATTENTION_BACKEND"] = (
            "TORCH_SDPA" if s["attention_backend"] == "torch_native" else "FLASH_ATTN"
        )
    cmd = ["python3", "-c", LEASE_SUPERVISOR, str(request["deadline"]), *cmd]
    record = {
        "name": name,
        "labels": labels,
        "resources": resources,
        "image": image["Id"],
        "model_path": request["model"]["path"],
        "model_manifest": manifest["digest"],
        "candidate_digest": candidate["digest"],
        "machine_identity": identity(),
        "boot_id": boot(),
        "platform": platform,
        "engine": recipe["engine"],
        "devices": devices,
        "cuda_mode": cuda_mode,
        "driver_mounts": driver_mounts,
        "gpu_ids": gpu_ids,
        "command": cmd,
        "memory_bytes": memory,
        "cpu_nanos": cpu,
        "deadline": request["deadline"],
        "environment": environment,
        "network": network_name,
    }
    atomic(state_path(root, task_id), record)
    for resource in resources:
        atomic(
            path_in(root, "locks/" + resource + ".json"),
            {"task_id": task_id, "operation_id": operation_id},
        )
    network_args = ["network", "create", "--internal"]
    for k, v in labels.items():
        network_args += ["--label", k + "=" + v]
    record["network_id"] = docker([*network_args, network_name]).stdout.strip()
    atomic(state_path(root, task_id), record)
    watcher = subprocess.Popen(
        [sys.executable, __file__, "watch", str(root), task_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    record["watchdog"] = {
        "pid": watcher.pid,
        "boot_id": boot(),
        "start_ticks": Path("/proc/" + str(watcher.pid) + "/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
    }
    atomic(state_path(root, task_id), record)
    docker_args = [
        "create",
        "--name",
        name,
        "--user",
        "65534:65534",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        str(memory),
        "--memory-swap",
        str(memory),
        "--cpus",
        str(limits["cpu_cores"]),
        "--shm-size",
        "2g",
        "--network",
        network_name,
        "--runtime",
        "runc",
        "-p",
        "127.0.0.1::30000",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=1g",
        "--tmpfs",
        "/jit:rw,exec,nosuid,nodev,size=1g,mode=1777",
        "--mount",
        "type=bind,src=" + record["model_path"] + ",dst=/model,readonly",
        "--entrypoint",
        "python3",
        *args,
    ]
    for k, v in labels.items():
        docker_args += ["--label", k + "=" + v]
    for k, v in environment.items():
        docker_args += ["-e", k + "=" + v]
    record["container_id"] = docker([*docker_args, image["Id"], *cmd[1:]]).stdout.strip()
    atomic(state_path(root, task_id), record)
    docker(["start", record["container_id"]])
    while not cancelled(root, operation_id, request["deadline"]):
        result = service_identity(root, task_id)
        if not result["passed"]:
            raise ValueError("Node launch contract failed")
        try:
            conn = http.client.HTTPConnection(result["address"], result["port"], timeout=3)
            conn.request("GET", "/v1/models")
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            if response.status == 200 and any(
                m.get("id") == request["model"]["id"] for m in data.get("data", [])
            ):
                return {"passed": True, "identity": result}
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise InterruptedError("Launch lease expired/cancelled")


def perform(root, request, operation_id):
    action = request["action"]
    if action == "asset_prepare":
        return (
            download_model(root, request, operation_id)
            if request["kind"] == "model"
            else image_asset(root, request, operation_id)
        )
    if action == "launch":
        return launch(root, request, operation_id)
    if action == "stop":
        return stop_service(root, request["task_id"])
    if action == "adapter_check":
        return check_adapter(root, request, operation_id)
    raise ValueError("Unsupported persistent node action")


def run_operation(root, operation_id):
    p = journal(root, operation_id)
    row = json.loads(p.read_text())
    request = row["request"]
    try:
        while "pid" not in row:
            time.sleep(0.02)
            row = json.loads(p.read_text())
        if row["status"] != "submitted" or not alive(row):
            raise ValueError("Operation claim unavailable")
        row.update(status="running")
        atomic(p, row)
        if request["machine_identity"] != identity() or request["boot_id"] != boot():
            raise ValueError("Node identity/boot changed")
        mutex = path_in(root, "node-mutation.lock")
        # Transfers have their own mutex so simultaneous downloads cannot
        # consume the same space estimate or block a GPU service cancellation.
        if request["action"] == "asset_prepare":
            mutex = path_in(root, "asset-preparation.lock")
        elif request["action"] == "adapter_check":
            mutex = path_in(root, "adapter-check.lock")
        with mutex.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if cancelled(root, operation_id, request["deadline"]):
                raise InterruptedError("Operation cancelled/expired")
            result = perform(root, request, operation_id)
        row = json.loads(p.read_text())
        row.update(status="completed", result=result)
    except Exception as exc:
        row = json.loads(p.read_text())
        row.update(
            status="cancelled" if isinstance(exc, InterruptedError) else "failed",
            error_type=type(exc).__name__,
            error=str(exc)[:800],
        )
        if request["action"] == "launch":
            try:
                intent = state_path(root, request["task_id"])
                if intent.is_file():
                    receipt = json.loads(intent.read_text())
                    if receipt["labels"]["pilot.infra.launch"] != operation_id:
                        raise ValueError("Original launch has different ownership")
                    obj = inspect(receipt["name"])
                    if obj:
                        if not owned_container(obj, receipt):
                            raise ValueError("Foreign launch logs/cleanup denied")
                        row["engine_log"] = docker(
                            ["logs", "--tail", "200", obj["Id"]], check=False
                        ).stdout[-20000:]
                    stop_service(root, request["task_id"])
            except Exception as cleanup:
                row.update(status="unknown", cleanup_error=type(cleanup).__name__)
        elif request["action"] == "adapter_check":
            try:
                cleanup_adapter(root, operation_id)
            except Exception as cleanup:
                row.update(status="unknown", cleanup_error=type(cleanup).__name__)
        elif request["action"] == "asset_prepare" and request["kind"] == "image":
            if row.get("pull_started") and not row.get("pull_completed"):
                row["status"] = "unknown"
            if row.get("probe_name"):
                try:
                    obj = inspect(row["probe_name"])
                    if obj:
                        if (
                            obj["Config"].get("Labels", {}).get("pilot.asset.operation")
                            != operation_id
                        ):
                            raise ValueError("Image check ownership differs")
                        docker(["stop", "--time", "5", obj["Id"]], timeout=15)
                        docker(["rm", obj["Id"]], check=False)
                except Exception:
                    row["status"] = "unknown"
    row.update(ended_at=time.time())
    atomic(p, row)


def dispatch(root, action, request):
    if action == "lookup":
        p = journal(root, request["operation_id"])
        return json.loads(p.read_text()) if p.is_file() else None
    if action == "reconcile":
        p = journal(root, request["operation_id"])
        row = json.loads(p.read_text())
        if alive(row):
            raise ValueError("Original worker is still alive; cancel and wait")
        payload = row["request"]
        if payload["action"] == "adapter_check":
            cleanup_adapter(root, request["operation_id"])
            row.update(
                status="failed",
                reconciled_at=time.time(),
                error="Original CPU adapter worker exited; owned sandbox reclaimed",
            )
            atomic(p, row)
            return row
        if payload["action"] != "asset_prepare" or payload["kind"] != "model":
            raise ValueError(
                "Image daemon effects need separate evidence; automatic reconciliation denied"
            )
        row.update(
            status="failed",
            reconciled_at=time.time(),
            error="Owner confirmed original model worker exited; partial cache retained, never published as ready",
        )
        atomic(p, row)
        return row
    if action == "submit":
        op = request["operation_id"]
        p = journal(root, op)
        payload = request["request"]
        with path_in(root, "submission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if p.exists():
                previous = json.loads(p.read_text())
                if previous["request_digest"] != digest(payload):
                    raise ValueError("Operation ID reused for different effects")
                return previous
            atomic(
                p,
                {
                    "operation_id": op,
                    "request": payload,
                    "request_digest": digest(payload),
                    "status": "submitted",
                    "boot_id": boot(),
                    "created_at": time.time(),
                },
            )
            process = subprocess.Popen(
                [sys.executable, __file__, "run", str(root), op],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            row = json.loads(p.read_text())
            row.update(
                pid=process.pid,
                start_ticks=Path("/proc/" + str(process.pid) + "/stat")
                .read_text()
                .rsplit(")", 1)[1]
                .split()[19],
            )
            atomic(p, row)
            return row
    if action in {"status", "cancel"}:
        p = journal(root, request["operation_id"])
        row = json.loads(p.read_text())
        if action == "cancel":
            path_in(root, "operations/" + request["operation_id"] + ".cancel").touch()
        if row["status"] in {"running", "submitted"} and not alive(row):
            row.update(
                status="unknown",
                error="Original node worker absent; effects require reconciliation",
            )
            atomic(p, row)
        return row
    if action == "identity":
        return service_identity(root, request["task_id"])
    if action == "manifest":
        return model_manifest(request["path"])
    if action == "logs":
        p = state_path(root, request["task_id"])
        if not p.exists():
            return {"text": ""}
        r = json.loads(p.read_text())
        obj = inspect(r["name"])
        if not obj:
            return {"text": "Container absent"}
        if not all(obj["Config"].get("Labels", {}).get(k) == v for k, v in r["labels"].items()):
            raise ValueError("Foreign container log denied")
        result = docker(["logs", "--tail", "500", obj["Id"]], check=False)
        return {"text": (result.stdout + result.stderr)[-100000:]}
    if action == "probe":
        verify_storage(root)
        return {
            "machine_identity": identity(),
            "node_identity": node_identity(),
            "boot_id": boot(),
            "docker": docker(["version", "--format", "{{.Server.Version}}"]).stdout.strip(),
            "root": str(root),
            "asset_root": str(checked_asset_root(root, {})),
            "disk_free": shutil.disk_usage(root).free,
            "worker_sha256": sha(Path(__file__)),
        }
    if action == "export":
        return {
            "operations": [
                json.loads(p.read_text()) for p in sorted((root / "operations").glob("*.json"))
            ]
            if (root / "operations").exists()
            else [],
            "services": [
                json.loads(p.read_text()) for p in sorted((root / "services").glob("*.json"))
            ]
            if (root / "services").exists()
            else [],
            "lease": json.loads((root / "pilot-executor.json").read_text()),
        }
    if action == "cleanup_executor":
        marker = json.loads((root / "pilot-executor.json").read_text())
        if request["owner_namespace"] != marker["owner_namespace"] or not root.name.startswith(
            "pilot-"
        ):
            raise ValueError("Executor cleanup namespace mismatch")
        for p in (root / "operations").glob("*.json") if (root / "operations").exists() else []:
            row = json.loads(p.read_text())
            if row["status"] in {"submitted", "running", "unknown"} or alive(row):
                raise ValueError("Original node operation still unsettled")
            if inspect("pilot-image-check-" + row["operation_id"][:16]):
                raise ValueError("Original CPU check container still present")
        for p in (root / "services").glob("*.json") if (root / "services").exists() else []:
            row = json.loads(p.read_text())
            if not row.get("stopped") or inspect(row["name"]):
                raise ValueError("Node service still present")
        target = root.resolve()
        if target != root or not target.name.startswith("pilot-") or len(target.parts) < 3:
            raise ValueError("Unsafe executor cleanup target")
        shutil.rmtree(target)
        return {
            "restored": True,
            "root_removed": True,
            "shared_images_preserved": True,
            "external_asset_roots_preserved": True,
        }
    if action == "resolve_image":
        reference = request["reference"]
        if not re.fullmatch(r"[A-Za-z0-9./:_@-]{3,400}", reference) or reference.startswith("-"):
            raise ValueError("Invalid registry reference")
        value = json.loads(
            docker(["manifest", "inspect", "--verbose", reference], timeout=45).stdout
        )
        values = value if isinstance(value, list) else [value]
        import platform

        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
        candidates = []
        for image in values:
            d = image.get("Descriptor", {})
            if (
                re.fullmatch(r"sha256:[a-f0-9]{64}", d.get("digest", ""))
                and d.get("platform", {}).get("architecture", arch) == arch
            ):
                repository = reference.split("@")[0]
                if ":" in repository.rsplit("/", 1)[-1]:
                    repository = repository.rsplit(":", 1)[0]
                candidates.append(
                    {
                        "location": repository + "@" + d["digest"],
                        "architecture": arch,
                        "digest": d["digest"],
                    }
                )
        if not candidates:
            raise ValueError("Registry did not provide a verifiable matching image digest")
        return {"candidates": candidates}
    raise ValueError("Unsupported node request")


def verify_storage(root):
    config = json.loads((root / "pilot-executor.json").read_text())
    if config.get("storage_devices"):
        for name, path in [("root", root), ("asset_root", checked_asset_root(root, {}))]:
            if path.stat().st_dev != config["storage_devices"][name]:
                raise ValueError("Node data mount changed; reconfigure executor")


def main():
    action = sys.argv[1]
    root = Path(sys.argv[2])
    if (
        root != root.resolve()
        or any(p.is_symlink() for p in [root, *root.parents])
        or not (root / "pilot-executor.json").is_file()
    ):
        raise ValueError("Unregistered executor root")
    lease = json.loads((root / "pilot-executor.json").read_text())
    if lease["machine_identity"] != identity():
        raise ValueError("Executor node identity changed")
    if lease.get("node_identity") and lease["node_identity"] != node_identity():
        raise ValueError("Executor node hardware identity changed")
    verify_storage(root)
    if action == "run":
        run_operation(root, sys.argv[3])
        return
    if action == "watch":
        watch_service(root, sys.argv[3])
        return
    request = json.load(sys.stdin)
    try:
        result = {"ok": True, "value": dispatch(root, action, request)}
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:800]}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
