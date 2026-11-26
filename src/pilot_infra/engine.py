"""Trusted SGLang recipes and independent tests. No agent supplied shell code."""

import concurrent.futures
import hashlib
import json
import subprocess
import time
from pathlib import Path

import httpx

from pilot_core.docker import docker

from . import benchmark
from .inference_http import RequestGuard
from .inference_http import request as inference_request
from .metrics import percentile as percentile
from .metrics import summarize, target_assessment
from .store import digest

QUALITY = [
    ("Reply with only the capital letter A.", lambda s: s.strip() == "A"),
    ("Calculate 2+2. Only output the answer.", lambda s: s.strip() in {"4", "2 + 2 = 4", "2+2=4"}),
    (
        "Translate cat to Chinese. Reply with only the Chinese word.",
        lambda s: "猫" in s and len(s.strip()) < 20,
    ),
    ('Return only this JSON object, without markdown: {"ok":true}', lambda s: parse_ok(s)),
]


def parse_ok(text):
    try:
        return json.loads(text.strip()) == {"ok": True}
    except (ValueError, TypeError):
        return False


def model_manifest(path):
    base = Path(path).resolve()
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix in {".json", ".safetensors", ".model"}:
            if p.is_symlink() or not p.resolve().is_relative_to(base):
                raise ValueError("Model symlink outside registered asset")
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(chunk)
            files.append(
                {
                    "path": p.relative_to(base).as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": h.hexdigest(),
                }
            )
    if not any(f["path"].endswith(".safetensors") for f in files):
        raise ValueError("No full model weights in registered model asset")
    return {"files": files, "digest": digest(files)}


def machine_probe():
    def read(argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
            return {"exit_code": p.returncode, "stdout": p.stdout[:30000], "stderr": p.stderr[:500]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"exit_code": -1, "stdout": "", "stderr": str(exc)[:500]}

    return {
        "time": time.time(),
        "scope": "whole-machine",
        "gpu": read(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,driver_version,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
        ),
        "gpu_processes": read(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ]
        ),
        "cpu": read(["lscpu"]),
        "memory": read(["free", "-m"]),
        "disk": read(["df", "-h", "/data"]),
    }


class SGLangEngine:
    def __init__(self, registry, task, candidate):
        self.registry, self.task, self.candidate = registry, task, candidate
        self.machine, self.model, self.image = registry.validate(
            task["plan"], cleanup=task.get("_cleanup", False)
        )
        self.name = f"pilot-infra-{task['id'][:16]}-{candidate['id']}"

    def registered_identity(self):
        from .container_runtime import runtime_digest

        return (
            self.candidate.get("image_digest") == self.image["digest"]
            and self.candidate.get("model_manifest_digest") == self.model["manifest_digest"]
            and self.candidate.get("runtime_digest") == runtime_digest(self.image)
        )

    def command(self):
        s = self.candidate["serving"]
        args = [
            "python3",
            "-m",
            "sglang.launch_server",
            "--model-path",
            "/model",
            "--host",
            "0.0.0.0",
            "--port",
            "30000",
            "--dtype",
            "bfloat16",
            "--tp-size",
            str(len(self.task["plan"]["gpu_ids"])),
            "--context-length",
            str(s["context_length"]),
            "--mem-fraction-static",
            str(s["mem_fraction_static"]),
            "--attention-backend",
            s["attention_backend"],
            "--served-model-name",
            self.model["id"],
        ]
        if s["disable_cuda_graph"]:
            args.append("--disable-cuda-graph")
        return args

    def labels(self):
        return {
            "pilot.infra.task": self.task["id"],
            "pilot.infra.candidate": self.candidate["digest"],
        }

    def limits(self):
        return self.task.get("_resource_limits", {})

    def paths(self):
        from .store import InfraStore

        base = InfraStore(self.registry.root).path(
            self.task["id"], f"candidates/{self.candidate['id']}"
        )
        state, auth = base / "container-state", base / "container-authorization"
        for path in (state, auth):
            if path.is_symlink():
                raise ValueError("Container state path is symlinked")
            path.mkdir(exist_ok=True)
        return base, state, auth

    def artifact(self):
        from .engine_source import EngineSource
        from .service import InfraService

        return EngineSource(InfraService(self.registry.root, spawn_jobs=False)).admitted(
            self.task, self.candidate
        )

    def layout(self, nonce):
        from .container_runtime import gpu_spec
        from .engine_source import file_sha

        base, state, auth = self.paths()
        devices, drivers, env = gpu_spec(self.image, self.task["plan"]["gpu_ids"])
        env.update(
            HOME="/state",
            TMPDIR="/tmp",
            HF_HOME="/state/hf",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            TORCHINDUCTOR_CACHE_DIR="/state/inductor",
            TRITON_CACHE_DIR="/state/triton",
            PILOT_GPU_IDS=",".join(map(str, self.task["plan"]["gpu_ids"])),
        )
        mounts = [
            (str(Path(self.model["path"]).resolve()), "/model", True),
            (str(state), "/state", False),
            *drivers,
        ]
        command = self.command()
        if (
            self.image.get("engine") == "vllm"
            and self.candidate["serving"]["attention_backend"] == "torch_native"
        ):
            env["VLLM_ATTENTION_BACKEND"] = "TORCH_SDPA"
        elif (
            self.image.get("engine") == "vllm"
            and self.candidate["serving"]["attention_backend"] == "flash_attn"
        ):
            env["VLLM_ATTENTION_BACKEND"] = "FLASH_ATTN"
        if self.candidate.get("engine_source"):
            artifact = self.artifact()
            launcher = Path(__file__).with_name("engine_launch.py")
            admission = launcher.with_name("engine_admission.py")
            expected = {
                "version": artifact["version"],
                "candidate_digest": self.candidate["digest"],
                "source_digest": artifact["source_digest"],
                "wheel_sha256": artifact["wheel_sha256"],
                "launcher_sha256": file_sha(launcher),
                "admission_sha256": file_sha(admission),
                "launch_nonce": nonce,
                "launch_authorization": "/auth/allow.json",
            }
            mounts += [
                (str(base / artifact["runtime"]), "/engine", True),
                (str(launcher), "/pilot/engine_launch.py", True),
                (str(admission), "/pilot/engine_admission.py", True),
                (str(auth), "/auth", True),
            ]
            command = [
                "python3",
                "-s",
                "/pilot/engine_launch.py",
                "/engine",
                "/state/engine-loaded.json",
                json.dumps(expected, sort_keys=True),
                *command[3:],
            ]
        return devices, mounts, command, env

    def network_name(self):
        return "pilot-infra-net-" + self.task["id"][:16]

    def network(self, create=False):
        result = docker(["network", "inspect", self.network_name()], check=False)
        if result.returncode:
            if not create or not any(v in result.stderr for v in ("No such", "not found")):
                raise ValueError("Cannot verify task private network")
            docker(
                [
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    "pilot.infra.task=" + self.task["id"],
                    self.network_name(),
                ]
            )
            return self.network()
        obj = json.loads(result.stdout)[0]
        if (
            not obj["Internal"]
            or (obj.get("Labels") or {}).get("pilot.infra.task") != self.task["id"]
        ):
            raise ValueError("Task network identity differs")
        return obj

    def inspect(self):
        from .container_runtime import inspect

        return inspect(self.name)

    def receipt(self, obj):
        from .store import InfraStore

        path = InfraStore(self.registry.root).path(
            self.task["id"], f"candidates/{self.candidate['id']}/container.json"
        )
        if not path.is_file() or path.is_symlink():
            raise ValueError("Container has no durable task launch receipt")
        receipt = json.loads(path.read_text())
        labels = obj.get("Config", {}).get("Labels") or {}
        if (
            (receipt.get("container_id") and receipt["container_id"] != obj["Id"])
            or not receipt.get("launch_nonce")
            or labels.get("pilot.infra.launch") != receipt["launch_nonce"]
            or receipt.get("candidate_digest") != self.candidate["digest"]
        ):
            raise ValueError("Container launch receipt differs; do not replay")
        return receipt

    def verify_identity(self, ready=True):
        try:
            return self._verify_identity(ready)
        except (ValueError, OSError, KeyError, TypeError) as error:
            return {"passed": False, "error": str(error)}

    def _verify_identity(self, ready=True):
        from .container_runtime import contract_checks

        obj = self.inspect()
        if not obj:
            return {"passed": False, "error": "Task container absent"}
        nonce = self.receipt(obj)["launch_nonce"]
        devices, mounts, command, env = self.layout(nonce)
        checks = contract_checks(
            obj,
            self.image,
            {**self.labels(), "pilot.infra.launch": nonce},
            self.limits(),
            mounts,
            command,
            env,
            self.network_name(),
        )
        checks.update(
            frozen_assets=self.registered_identity(),
            running=obj["State"]["Running"],
            private_network=bool(self.network()),
        )
        if self.image.get("gpu_mode", "nvidia") == "nvidia":
            requests = obj["HostConfig"].get("DeviceRequests") or []
            ids = [i for req in requests for i in req.get("DeviceIDs") or []]
            checks["devices"] = (
                sorted(ids)
                == sorted(env.get("PILOT_GPU_UUIDS", env["CUDA_VISIBLE_DEVICES"]).split(","))
                and len(requests) == 1
                and requests[0].get("Count") != -1
                and not obj["HostConfig"].get("Devices")
            )
        else:
            expected = [devices[i + 1].split(":")[0] for i in range(0, len(devices), 2)]
            actual = obj["HostConfig"].get("Devices") or []
            checks["devices"] = (
                sorted(d["PathOnHost"] for d in actual) == sorted(expected)
                and all(
                    d["PathOnHost"] == d["PathInContainer"] and d["CgroupPermissions"] == "rw"
                    for d in actual
                )
                and not obj["HostConfig"].get("DeviceRequests")
            )
        checks["weights"] = (
            model_manifest(self.model["path"])["digest"] == self.model["manifest_digest"]
            if ready
            else True
        )
        result = {
            "passed": False,
            "checks": checks,
            "container_id": obj["Id"],
            "started_at": obj["State"]["StartedAt"],
            "image": obj["Image"],
            "model_manifest": self.model["manifest_digest"],
            "config_digest": self.candidate["digest"],
            "runtime": "docker",
            "engine": self.image.get("engine", "sglang"),
        }
        if ready and self.candidate.get("engine_source"):
            artifact = self.artifact()
            loaded = json.loads((self.paths()[1] / "engine-loaded.json").read_text())
            expected = json.loads(command[5])
            checks["engine_loaded"] = (
                all(loaded.get(k) == v for k, v in expected.items())
                and loaded["module"] == "/engine/sglang/__init__.py"
                and loaded["module_sha256"] == artifact["runtime_files"]["sglang/__init__.py"]
                and loaded["distribution_version"] == artifact["version"]
                and loaded["cuda_visible_devices"] == env["CUDA_VISIBLE_DEVICES"]
                and loaded["gpu_ids"] == env["PILOT_GPU_IDS"]
            )
            result["engine_loaded"] = loaded
            checks["engine_artifact"] = checks["engine_loaded"]
        result["passed"] = all(checks.values())
        return result

    def endpoint(self):
        import ipaddress

        obj = self.inspect()
        if not obj:
            raise ValueError("Container unavailable")
        self.receipt(obj)
        ports = obj["NetworkSettings"]["Ports"].get("30000/tcp") or []
        if ports:
            if len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
                raise ValueError("Endpoint must be localhost-only")
            return "http://127.0.0.1:" + ports[0]["HostPort"]
        # Docker disables port publishing on an internal network. The Host
        # reaches the private bridge directly, with no external egress/port.
        network = self.network()
        binding = obj["NetworkSettings"].get("Networks", {}).get(self.network_name(), {})
        address = ipaddress.ip_address(binding.get("IPAddress", ""))
        if (
            binding.get("NetworkID") != network["Id"]
            or not address.is_private
            or address.version != 4
        ):
            raise ValueError("Endpoint must be localhost-only")
        return f"http://{address}:30000"

    def live_acceptance(self, accepted):
        current = self.verify_identity(ready=False)
        return current.get("passed", False) and all(
            current.get(k) == accepted.get(k)
            for k in ("container_id", "started_at", "image", "config_digest")
        )

    def launch(self, cancelled=lambda: False):
        import uuid

        from repopilot.storage import atomic_json

        from .container_runtime import base_args, image_admitted

        if cancelled():
            raise InterruptedError("Cancelled before container create")
        image_admitted(self.image)
        if not self.registered_identity():
            raise ValueError("Frozen runtime/model/image changed; freeze a new candidate")
        obj = self.inspect()
        if obj:
            if not self.receipt(obj).get("container_id"):
                raise ValueError(
                    "Unsettled container creation; reconcile original job before another launch"
                )
            if not self.verify_identity()["passed"]:
                raise ValueError("Existing container identity differs; do not replay")
        else:
            from .resource_assessment import check_gpu_occupancy
            from .resource_probe import probe_machine
            from .resource_registry import ResourceRegistry

            server = ResourceRegistry(self.registry.root).get(self.task["plan"]["machine_id"])
            check_gpu_occupancy(
                probe_machine(), self.task["plan"]["gpu_ids"],
                allow_shared=server["policy"].get("gpu_sharing") == "shared",
            )
            nonce = uuid.uuid4().hex
            self.network(create=True)
            devices, mounts, command, env = self.layout(nonce)
            base, state, auth = self.paths()
            (auth / "allow.json").unlink(missing_ok=True)
            record = base / "container.json"
            atomic_json(
                record,
                {
                    "name": self.name,
                    "launch_nonce": nonce,
                    "candidate_digest": self.candidate["digest"],
                },
            )
            args = base_args(
                self.name,
                self.image,
                {**self.labels(), "pilot.infra.launch": nonce},
                self.limits(),
                mounts,
                command,
                env,
                self.network_name(),
                True,
            )
            # Device flags must precede the immutable image and command.
            args[-len(command) :] = [*devices, *args[-len(command) :]]
            created = docker(args, timeout=60).stdout.strip()
            atomic_json(
                record,
                {
                    "name": self.name,
                    "container_id": created,
                    "launch_nonce": nonce,
                    "candidate_digest": self.candidate["digest"],
                },
            )
            if cancelled():
                raise InterruptedError("Cancelled before container start")
            docker(["start", created], timeout=30)
            obj = self.inspect()
            if not obj or obj["Id"] != created or not self.verify_identity(ready=False)["passed"]:
                raise ValueError("Created container contract differs")
            if self.candidate.get("engine_source"):
                expected = json.loads(command[5])
                pid = obj["State"]["Pid"]
                expected.update(
                    pid=pid,
                    start_ticks=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                    container_id=created,
                )
                if cancelled():
                    raise InterruptedError("Cancelled before editable import authorization")
                atomic_json(auth / "allow.json", expected)
        until = time.monotonic() + 600
        while time.monotonic() < until:
            if cancelled():
                raise InterruptedError("Task stopped during container launch")
            obj = self.inspect()
            if not obj or not obj["State"]["Running"]:
                raise RuntimeError("Container exited before readiness: " + self.logs()[-2000:])
            try:
                if httpx.get(self.endpoint() + "/health", timeout=3).status_code == 200:
                    identity = self.verify_identity()
                    if not identity["passed"]:
                        raise ValueError("Container readiness identity failed")
                    return {"endpoint": self.endpoint(), "identity": identity}
            except httpx.HTTPError:
                pass
            except ValueError:
                current = self.inspect()
                if not current or not current["State"]["Running"]:
                    raise RuntimeError(
                        "Container exited during readiness: " + self.logs()[-3000:]
                    ) from None
                raise
            time.sleep(2)
        raise TimeoutError("Container not ready in 600 seconds")

    def stop(self):
        from .container_runtime import stop_owned

        base, _, _ = self.paths()
        record = base / "container.json"
        expected = json.loads(record.read_text()).get("container_id") if record.is_file() else None
        obj = self.inspect()
        if obj:
            receipt = self.receipt(obj)
            stop_owned(
                self.name,
                {**self.labels(), "pilot.infra.launch": receipt["launch_nonce"]},
                expected or obj["Id"],
            )
        net = docker(["network", "inspect", self.network_name()], check=False)
        if not net.returncode:
            obj = self.network()
            if not obj.get("Containers"):
                docker(["network", "rm", obj["Id"]])

    def logs(self):
        obj = self.inspect()
        if not obj:
            return "Container absent"
        self.receipt(obj)
        if any((obj["Config"].get("Labels") or {}).get(k) != v for k, v in self.labels().items()):
            raise ValueError("Unowned container logs")
        result = docker(["logs", "--tail", "1000", obj["Id"]], check=False)
        return result.stdout[-150000:] + result.stderr[-150000:]

    def performance_payload(self, length, workload):
        return {
            "input_ids": [1024] * length,
            "sampling_params": {
                "max_new_tokens": workload["max_new_tokens"],
                "temperature": 0,
                "ignore_eos": True,
            },
        }

    def request(self, payload, *, native=False, timeout=90, guard=None):
        path = (("/v1/completions" if self.image.get("engine") == "vllm" else "/generate")
                if native else "/v1/chat/completions")
        return inference_request(self.endpoint() + path, payload,
                                 sglang_native=native and self.image.get("engine") != "vllm",
                                 timeout=timeout, guard=guard)

    def test(self, adapter, cancelled=lambda: False, plan=None):
        deadline = self.task.get("_execution_deadline_epoch", self.task.get("state", {}).get("deadline_epoch"))
        with RequestGuard(cancelled, deadline) as guard:
            return self._test(adapter, guard, plan or self.task["plan"])

    def _test(self, adapter, guard, plan):
        cancelled = guard.interrupted
        before = self.verify_identity()
        if not before["passed"]:
            raise ValueError("Cannot test wrong or stopped model identity")
        quality = []
        for prompt, check in QUALITY:
            if cancelled():
                raise InterruptedError("Quality test stopped")
            p = {
                "model": self.model["id"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self.candidate["serving"]["max_new_tokens"],
                "stop": self.candidate["serving"]["stop"],
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
            }
            p.update(adapter)
            guard.check()
            row = self.request(p, guard=guard)
            row.update(prompt=prompt, passed=bool(row["ok"] and check(row.get("output", ""))))
            quality.append(row)
        cases, workload = [], plan["workload"]
        for length in workload["input_lengths"]:
            for concurrency in workload["concurrency"]:
                if cancelled():
                    raise InterruptedError("Performance test stopped")
                base = self.performance_payload(length, workload)
                upper = 1024
                config = Path(self.model.get("path", "")) / "config.json"
                if config.is_file():
                    upper = min(upper, json.loads(config.read_text()).get("vocab_size", upper))
                def prepared(sample, base=base, length=length, concurrency=concurrency, upper=upper):
                    return benchmark.payload(base, length, workload, f"{concurrency}:{sample}", upper)
                def send(payload):
                    guard.check()
                    return self.request(payload, native=True, timeout=workload["timeout_seconds"], guard=guard)
                warmup = [send(prepared(f"warmup-{n}")) for n in range(2)]
                payloads = [prepared(f"sample-{n}") for n in range(workload["requests_per_case"])]
                begin = time.perf_counter()
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
                try:
                    rows = list(executor.map(send, payloads))
                except BaseException:
                    guard.cancel()
                    raise
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                elapsed = time.perf_counter() - begin
                for row in rows:
                    row["ok"] = bool(
                        row["ok"]
                        and row.get("input_tokens") == length
                        and row.get("output_tokens") == workload["max_new_tokens"]
                    )
                cases.append(
                    {
                        "input_length": length,
                        "concurrency": concurrency,
                        "cache_mode": workload.get("cache_mode", "legacy_repeated"),
                        "input_payload_digests": [digest(p) for p in payloads],
                        "cached_input_tokens": [r.get("cached_input_tokens") for r in rows],
                        "warmup": warmup,
                        "rows": rows,
                        **summarize(rows, elapsed),
                    }
                )
        after = self.verify_identity()
        identity = before["passed"] and after["passed"] and before == after
        complete = all(
            c["successful"] == c["attempted"] and all(w["ok"] for w in c["warmup"]) for c in cases
        )
        slo = plan.get("ttft_slo_ms")
        target = target_assessment(cases, complete, slo)
        perf = complete and (slo is None or target["status"] == "passed")
        return {
            "protocol": "pilot-quality-smoke-v2+"
            + (
                "vllm-completions-matrix-v2"
                if self.image.get("engine") == "vllm"
                else "sglang-native-matrix-v2"
            ),
            "candidate_id": self.candidate["id"],
            "candidate_digest": self.candidate["digest"],
            "protocol_digest": benchmark.identity(workload),
            "cache_policy": {"mode": workload.get("cache_mode", "legacy_repeated"),
                             "seed": workload.get("seed"), "server_prefix_cache": "not_queried"},
            "adapter_digest": digest(adapter),
            "identity": after,
            "quality": quality,
            "cases": cases,
            "metrics_protocol": "pilot-client-inference-metrics-v1",
            "performance_target": target,
            "gates": {
                "identity": identity,
                "quality": all(q["passed"] for q in quality),
                "test_complete": complete,
                "performance": perf,
            },
            "passed": identity
            and all(q["passed"] for q in quality)
            and (perf if plan["performance_required"] else complete),
            "limits": [
                "Four protocol smoke probes, not a general accuracy benchmark",
                "Shared machine load is observable but not controlled",
                "Short performance samples; no significance claim",
            ],
        }


class VLLMEngine(SGLangEngine):
    def command(self):
        s = self.candidate["serving"]
        args = [
            "python3",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            "/model",
            "--host",
            "0.0.0.0",
            "--port",
            "30000",
            "--dtype",
            "bfloat16",
            "--tensor-parallel-size",
            str(len(self.task["plan"]["gpu_ids"])),
            "--max-model-len",
            str(s["context_length"]),
            "--gpu-memory-utilization",
            str(s["mem_fraction_static"]),
            "--served-model-name",
            self.model["id"],
        ]
        if s["disable_cuda_graph"]:
            args.append("--enforce-eager")
        return args

    def performance_payload(self, length, workload):
        return {
            "model": self.model["id"],
            "prompt": [1024] * length,
            "max_tokens": workload["max_new_tokens"],
            "temperature": 0,
            "ignore_eos": True,
            "stream_options": {"include_usage": True},
        }
