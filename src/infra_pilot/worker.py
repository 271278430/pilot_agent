"""Standalone Python 3.10+ remote worker; no controller installation needed."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

PINNED_WEIGHT_SHA = "5af571cbf074e6d21a03528d2330792e532ca608f24ac70a143f6b369968ab8c"
MODEL_FILES = {"config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.json", "merges.txt", "model.safetensors", "README.md"}


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(argv, timeout=30, capture=True, env=None):
    result = subprocess.run(argv, capture_output=capture, text=True, timeout=timeout, env=env, check=True)
    return result.stdout if capture else ""


def probe(root):
    import shutil

    data = run(["nvidia-smi", "--query-gpu=index,name,memory.free,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for row in data.splitlines():
        index, name, free, driver = [part.strip() for part in row.split(",")]
        gpus.append({"index": int(index), "name": name, "free_mib": int(free), "driver": driver})
    return {"python": platform.python_version(), "platform": platform.platform(), "disk_free_bytes": shutil.disk_usage(root).free, "gpus": gpus, "root": str(root.resolve()), "nvcc_required": False}


def provision(root, spec):
    import urllib.request

    env = dict(os.environ)
    (root / 'tmp').mkdir(exist_ok=True)
    env.update(TMPDIR=str(root / 'tmp'), PIP_CACHE_DIR=str(root / '.cache/pip'),
               VIRTUALENV_OVERRIDE_APP_DATA=str(root / '.cache/virtualenv'))
    python = root / ".venv/bin/python"
    if not python.exists():
        try:
            run([sys.executable, "-m", "venv", str(root / ".venv")], capture=False, env=env)
        except subprocess.CalledProcessError:
            bootstrap = root / "virtualenv.pyz"
            urllib.request.urlretrieve("https://bootstrap.pypa.io/virtualenv.pyz", bootstrap)
            run([sys.executable, str(bootstrap), "--app-data", str(root / ".cache/virtualenv"), str(root / ".venv")], capture=False, env=env)
    index = {'pypi': 'https://pypi.org/simple', 'tuna': 'https://pypi.tuna.tsinghua.edu.cn/simple', 'pytorch': 'https://download.pytorch.org/whl/cu124'}[spec.get('package_source', 'pytorch')]
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--retries", "3", "torch==2.6.0", "--index-url", index], timeout=1500, capture=False, env=env)
    packages = ["transformers==4.51.3", "peft==0.15.2", "fastapi==0.115.12", "uvicorn==0.34.2", "httpx==0.28.1", "huggingface-hub==0.30.2", "numpy==1.26.4"]
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--retries", "3", "--index-url", 'https://pypi.tuna.tsinghua.edu.cn/simple' if spec.get('package_source') == 'tuna' else 'https://pypi.org/simple', *packages], timeout=900, capture=False, env=env)
    run([str(python), "-m", "pip", "check"], capture=False, env=env)
    lock = run([str(python), "-m", "pip", "freeze"], env=env)
    (root / "requirements.remote.lock").write_text(lock)
    return {"python": str(python), "lock_sha256": hashlib.sha256(lock.encode()).hexdigest(), "scope": "dedicated venv only"}


def dispatch(root, spec, stage):
    if stage == "provision":
        return provision(root, spec)
    # ML stages are launched in the dedicated environment by the job wrapper.
    return ml_stage(root, spec, stage)


def retry_delay(error, attempt):
    from email.utils import parsedate_to_datetime
    from urllib.error import HTTPError

    if isinstance(error, HTTPError) and error.code == 429:
        header = (error.headers or {}).get("Retry-After")
        if header:
            try:
                delay = float(header)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(header).timestamp() - time.time()
                except (TypeError, ValueError):
                    delay = 30 * (attempt + 1)
        else:
            delay = 30 * (attempt + 1)
        return min(60, max(1, delay))
    return min(8, 2 ** (attempt + 1))


def fetch_metadata(url):
    import urllib.error
    import urllib.request

    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 2:
                raise
            delay = retry_delay(exc, attempt)
            print(f"metadata retry {attempt + 1}: {type(exc).__name__}; wait {delay}s", flush=True)
            time.sleep(delay)


def verified_receipt_metadata(root, spec):
    """Reuse immutable revision evidence, still verify every artifact's SHA."""
    receipt = root / "reports/download.json"
    try:
        data = json.loads(receipt.read_text())
        files = data["files"]
        required = {"config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"}
        if data["model_id"] != spec["model_id"] or data["revision"] != spec["revision"] or data["trust_remote_code"] is not False or not required.issubset(files):
            return None
        weights = files["model.safetensors"]
        if weights["sha256"] != PINNED_WEIGHT_SHA or weights["expected_lfs_sha256"] != PINNED_WEIGHT_SHA:
            return None
        siblings = []
        for name, row in files.items():
            if name not in MODEL_FILES:
                continue
            value = row["sha256"]
            if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                return None
            siblings.append({"rfilename": name, "verified_sha256": value, "lfs": {"sha256": row["expected_lfs_sha256"]}})
        return {"sha": data["revision"], "siblings": siblings, "cardData": {"license": data.get("license")}}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def download_model(root, spec):
    import urllib.request

    endpoint = spec["model_endpoint"]
    model, revision = spec["model_id"], spec["revision"]
    url = f"{endpoint}/api/models/{model}/revision/{revision}?blobs=true"
    metadata = verified_receipt_metadata(root, spec)
    metadata_source = "verified_download_receipt" if metadata else "approved_endpoint"
    if metadata is None:
        metadata = fetch_metadata(url)
    print(f"metadata source: {metadata_source}; all files still SHA-256 verified", flush=True)
    if metadata["sha"] != revision:
        raise ValueError("Model revision mismatch")
    wanted = MODEL_FILES
    target = root / "models/smollm"
    target.mkdir(exist_ok=True)
    files = {}
    for entry in metadata["siblings"]:
        name = entry["rfilename"]
        if name not in wanted:
            continue
        path = target / name
        expected = entry.get("lfs", {}).get("sha256") or entry.get("verified_sha256")
        if name == "model.safetensors":
            if expected is not None and expected != PINNED_WEIGHT_SHA:
                raise ValueError("Pinned model weight SHA-256 mismatch")
            expected = PINNED_WEIGHT_SHA
        if not path.exists() or (expected and file_hash(path) != expected):
            last_error = None
            for attempt in range(3):
                try:
                    request = urllib.request.Request(f"{endpoint}/{model}/resolve/{revision}/{name}?download=true", headers={"User-Agent": "InfraPilot/0.2"})
                    size = 0
                    with urllib.request.urlopen(request, timeout=60) as response, path.with_suffix(path.suffix + ".partial").open("wb") as stream:
                        while chunk := response.read(1024 * 1024):
                            stream.write(chunk)
                            size += len(chunk)
                            if size % (16 * 1024 * 1024) == 0:
                                print(f"download {name}: {size // 1024**2} MiB", flush=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary = path.with_suffix(path.suffix + ".partial")
                    if expected and file_hash(temporary) != expected:
                        raise ValueError("Downloaded model SHA-256 does not match pinned LFS metadata")
                    os.replace(temporary, path)
                    break
                except Exception as exc:
                    last_error = exc
                    print(f"download retry {attempt + 1}: {type(exc).__name__}", flush=True)
                    if attempt < 2:
                        delay = retry_delay(exc, attempt)
                        print(f"retry delay: {delay}s", flush=True)
                        time.sleep(delay)
            else:
                raise last_error
        files[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size, "expected_lfs_sha256": entry.get("lfs", {}).get("sha256") or (PINNED_WEIGHT_SHA if name == "model.safetensors" else None)}
    if "model.safetensors" not in files:
        raise ValueError("Pinned model has no approved safetensors weights")
    return {"model_id": model, "revision": revision, "endpoint": endpoint, "metadata_source": metadata_source, "license": metadata.get("cardData", {}).get("license"), "files": files, "trust_remote_code": False}


def file_hash(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def valid_job_id(job):
    return bool(job) and len(job) <= 80 and all(ch.isalnum() or ch in "-_" for ch in job)


def load_model(root, adapter=False):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(4)
    torch.cuda.set_per_process_memory_fraction(0.10)
    tokenizer = AutoTokenizer.from_pretrained(root / "models/smollm", local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(root / "models/smollm", local_files_only=True, trust_remote_code=False, torch_dtype=torch.float32, attn_implementation="eager").to("cuda")
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, root / "checkpoints/adapter", is_trainable=False)
    return model, tokenizer


def train_model(root, spec):
    import gc

    import torch
    from peft import LoraConfig, get_peft_model

    torch.manual_seed(42)
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    model, tokenizer = load_model(root)
    config = LoraConfig(r=8, lora_alpha=16, lora_dropout=0, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM")
    model = get_peft_model(model, config)
    texts = [
        "Question: What is NCCL? Answer: NCCL provides GPU collective communication.",
        "Question: What does CUDA do? Answer: CUDA runs tensor computation on NVIDIA GPUs.",
        "Question: Why isolate dependencies? Answer: A virtual environment keeps project versions separate.",
        "Question: How should a service be verified? Answer: Check health, send requests, and record latency.",
        "Question: What happens when a port is occupied? Answer: Select an available port and restart the owned service.",
        "Question: Why save checkpoints? Answer: Checkpoints let training resume with optimizer and random state.",
        "Question: What does TTFT measure? Answer: TTFT measures the time until the first output token.",
        "Question: What is a deployment manifest? Answer: It records model and dependency versions for reproduction.",
    ]
    tokenizer.pad_token = tokenizer.eos_token
    encoded = tokenizer(texts, return_tensors="pt", padding="max_length", truncation=True, max_length=96)
    batches = []
    for i in range(0, len(texts), 2):
        inputs = {key: value[i:i+2].to("cuda") for key, value in encoded.items()}
        labels = inputs["input_ids"].clone()
        labels[inputs["attention_mask"] == 0] = -100
        batches.append({**inputs, "labels": labels})
    named = {name: parameter for name, parameter in model.named_parameters() if parameter.requires_grad}
    initial = {name: parameter.detach().cpu().clone() for name, parameter in named.items()}
    trainable = sum(parameter.numel() for parameter in named.values())
    total = sum(parameter.numel() for parameter in model.parameters())
    optimizer = torch.optim.AdamW(named.values(), lr=5e-4)
    model.train()
    torch.cuda.reset_peak_memory_stats()
    losses = []
    gradients = []
    checkpoint = root / "checkpoints/midpoint.pt"
    started = time.monotonic()
    for step in range(spec["train_steps"]):
        optimizer.zero_grad(set_to_none=True)
        loss = model(**batches[step % len(batches)]).loss
        if not torch.isfinite(loss):
            raise ValueError("nonfinite_loss")
        loss.backward()
        grad = torch.nn.utils.clip_grad_norm_(named.values(), 1.0)
        if not torch.isfinite(grad):
            raise ValueError("nonfinite_gradient")
        optimizer.step()
        losses.append(loss.item())
        gradients.append(float(grad))
        print(f"train step {step + 1}: loss={loss.item():.6f}", flush=True)
        if step + 1 == spec["train_steps"] // 2:
            torch.save({"step": step + 1, "parameters": {name: p.detach().cpu() for name, p in named.items()}, "optimizer": optimizer.state_dict(), "rng": torch.get_rng_state(), "cuda_rng": torch.cuda.get_rng_state_all()}, checkpoint)
    expected = {name: parameter.detach().cpu().clone() for name, parameter in named.items()}
    changed = sum(float((expected[name] - initial[name]).square().sum()) for name in named) ** 0.5
    peak = torch.cuda.max_memory_allocated() / 1024**2
    del model, optimizer, named
    gc.collect()
    torch.cuda.empty_cache()
    model, _ = load_model(root)
    model = get_peft_model(model, config)
    named = {name: parameter for name, parameter in model.named_parameters() if parameter.requires_grad}
    saved = torch.load(checkpoint, map_location="cuda", weights_only=True)
    with torch.no_grad():
        for name, value in saved["parameters"].items():
            named[name].copy_(value)
    optimizer = torch.optim.AdamW(named.values(), lr=5e-4)
    optimizer.load_state_dict(saved["optimizer"])
    torch.set_rng_state(saved["rng"].cpu())
    torch.cuda.set_rng_state_all([value.cpu() for value in saved["cuda_rng"]])
    model.train()
    resumed_losses = []
    for step in range(saved["step"], spec["train_steps"]):
        optimizer.zero_grad(set_to_none=True)
        loss = model(**batches[step % len(batches)]).loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(named.values(), 1.0)
        optimizer.step()
        resumed_losses.append(loss.item())
    max_delta = max(float((parameter.detach().cpu() - expected[name]).abs().max()) for name, parameter in named.items())
    if changed == 0 or max_delta > 1e-6:
        raise ValueError(f"checkpoint_resume_mismatch: max_delta={max_delta}")
    model.save_pretrained(root / "checkpoints/adapter", safe_serialization=True)
    return {"dataset": "8 synthetic AI Infra QA texts; engineering smoke test only", "seed": 42, "steps": spec["train_steps"], "batch_size": 2, "sequence_length": 96, "precision": "float32", "total_parameters": total, "trainable_parameters": trainable, "losses": losses, "first_four_mean_loss": sum(losses[:4])/4, "last_four_mean_loss": sum(losses[-4:])/4, "finite_gradients": all(value < float("inf") for value in gradients), "adapter_parameter_delta_l2": changed, "peak_allocated_mib": round(peak, 2), "resume_from_step": saved["step"], "resumed_losses": resumed_losses, "resume_max_parameter_delta": max_delta, "checkpoint_sha256": file_hash(checkpoint), "duration_seconds": round(time.monotonic()-started, 3), "general_accuracy_measured": False}


def nccl_rank(root):
    from datetime import timedelta

    import torch
    import torch.distributed as dist

    rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(rank)
    dist.init_process_group("nccl", timeout=timedelta(seconds=60))
    try:
        value = torch.tensor([rank + 1.0], device=f"cuda:{rank}")
        dist.all_reduce(value)
        expected = dist.get_world_size() * (dist.get_world_size() + 1) / 2
        assert value.item() == expected
        atomic(root / "reports" / f"nccl-rank-{rank}.json", {"rank": rank, "world_size": dist.get_world_size(), "result": value.item(), "expected": expected, "backend": dist.get_backend()})
    finally:
        dist.destroy_process_group()


def ml_stage(root, spec, stage):
    if stage == "download":
        return download_model(root, spec)
    import torch

    torch.set_num_threads(4)
    torch.cuda.set_per_process_memory_fraction(0.10)
    if stage == "cuda_smoke":
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.manual_seed(42)
        x = torch.randn(128, 128)
        expected = x @ x
        actual = (x.cuda() @ x.cuda()).cpu()
        assert torch.allclose(actual, expected, rtol=1e-4, atol=1e-4)
        return {"torch": torch.__version__, "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(), "max_abs_error": float((actual-expected).abs().max()), "passed": True}
    if stage == "nccl_smoke":
        if len(spec["gpu_ids"]) != 2:
            return {"verified": False, "reason": "two GPUs required"}
        run([sys.executable, "-m", "torch.distributed.run", "--standalone", "--nproc_per_node=2", str(root / "worker.py"), str(root), "nccl_rank"], timeout=120, capture=False)
        return {"verified": True, "scope": "single-host two-GPU collective, not multi-node RDMA", "ranks": [json.loads((root / "reports" / f"nccl-rank-{rank}.json").read_text()) for rank in range(2)]}
    if stage == "inference":
        model, tokenizer = load_model(root)
        model.eval()
        inputs = tokenizer.apply_chat_template([{"role": "user", "content": "Explain what a GPU does in one sentence."}], return_tensors="pt", add_generation_prompt=True).to("cuda")
        started = time.monotonic()
        with torch.inference_mode():
            output = model.generate(inputs, max_new_tokens=32, do_sample=False, pad_token_id=tokenizer.eos_token_id)
        torch.cuda.synchronize()
        text = tokenizer.decode(output[0, inputs.shape[1]:], skip_special_tokens=True)
        assert text and output.shape[1] > inputs.shape[1]
        return {"model_id": spec["model_id"], "model_parameters": sum(p.numel() for p in model.parameters()), "generated_text": text, "output_tokens": output.shape[1]-inputs.shape[1], "seconds": round(time.monotonic()-started, 3), "peak_allocated_mib": round(torch.cuda.max_memory_allocated()/1024**2, 2)}
    if stage == "train":
        return train_model(root, spec)
    if stage == "serve_benchmark":
        return service_benchmark(root, spec)
    raise ValueError("Unknown ML stage")


def serve(root, spec):
    import queue
    import threading

    import torch
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from transformers.generation.streamers import BaseStreamer

    model, tokenizer = load_model(root, adapter=True)
    model.eval()
    lock = threading.Lock()
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok", "model": spec["model_id"], "adapter": True}

    @app.post("/v1/chat/completions")
    def completion(body: dict):
        if not body.get("stream", False):
            raise ValueError("Smoke service supports streaming requests")
        maximum = max(1, min(int(body.get("max_tokens", 24)), 64))
        inputs = tokenizer.apply_chat_template(body["messages"], return_tensors="pt", add_generation_prompt=True).to("cuda")
        def chunks():
            with lock:
                events = queue.Queue()
                class Streamer(BaseStreamer):
                    def __init__(self):
                        self.prompt = True
                    def put(self, value):
                        if self.prompt:
                            self.prompt = False
                            return
                        for token in value.flatten().tolist():
                            events.put({"token_id": token, "text": tokenizer.decode([token], skip_special_tokens=True)})
                    def end(self):
                        events.put(None)
                def generate():
                    try:
                        with torch.inference_mode():
                            model.generate(inputs, streamer=Streamer(), max_new_tokens=maximum, min_new_tokens=maximum, do_sample=False, pad_token_id=tokenizer.eos_token_id)
                    except Exception as exc:
                        events.put({"error": str(exc)})
                        events.put(None)
                thread = threading.Thread(target=generate, daemon=True)
                thread.start()
                count = 0
                while (event := events.get(timeout=60)) is not None:
                    if "error" in event:
                        yield "data: " + json.dumps(event) + "\n\n"
                        break
                    count += 1
                    yield "data: " + json.dumps({"choices": [{"delta": {"content": event["text"]}}], "token_id": event["token_id"]}) + "\n\n"
                thread.join(timeout=10)
                yield "data: " + json.dumps({"usage": {"prompt_tokens": inputs.shape[1], "completion_tokens": count}}) + "\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")
    uvicorn.run(app, host="127.0.0.1", port=spec["service_port"], log_level="info")


def service_benchmark(root, spec):
    import concurrent.futures
    import socket
    import statistics

    import httpx

    occupied = None
    if spec["inject_port_conflict"]:
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", spec["service_port"]))
        occupied.listen()
    port = spec["service_port"]
    with (root / "logs/server.log").open("w") as logfile:
        process = subprocess.Popen([sys.executable, "-u", str(root / "worker.py"), str(root), "server"], stdout=logfile, stderr=subprocess.STDOUT)
        try:
            url = f"http://127.0.0.1:{port}"
            ready = False
            for _ in range(120):
                if process.poll() is not None:
                    break
                try:
                    with httpx.Client(timeout=2) as client:
                        ready = client.get(url + "/health").status_code == 200
                    if ready:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            if not ready:
                details = (root / "logs/server.log").read_text()[-3000:]
                raise RuntimeError("port_conflict: Address already in use" if "address already in use" in details.lower() else "service_start_failed: " + details)
            def request(index):
                timings, text, usage = [], [], {}
                start = time.perf_counter()
                with httpx.Client(timeout=60) as client, client.stream("POST", url + "/v1/chat/completions", json={"model": spec["model_id"], "stream": True, "messages": [{"role": "user", "content": "Explain why GPU communication matters."}], "max_tokens": 24}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        data = json.loads(line[6:])
                        if "error" in data:
                            raise ValueError(data["error"])
                        if "token_id" in data:
                            timings.append(time.perf_counter())
                            text.append(data["choices"][0]["delta"]["content"])
                        if "usage" in data:
                            usage = data["usage"]
                assert len(timings) == usage["completion_tokens"] == 24
                return {"request": index, "input_tokens": usage["prompt_tokens"], "output_tokens": len(timings), "ttft_ms": (timings[0]-start)*1000, "tpot_ms": (timings[-1]-timings[0])*1000/(len(timings)-1), "duration_seconds": time.perf_counter()-start, "text": "".join(text)}
            request(-1)  # warmup excluded from reported samples
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                samples = list(pool.map(request, range(6)))
            wall = time.perf_counter()-started
            return {"backend": "Transformers + PEFT + FastAPI; serialized model execution", "model": spec["model_id"], "port": port, "binding": "127.0.0.1", "requests": 6, "concurrency": 2, "failed_requests": 0, "warmup_requests_excluded": 1, "ttft_p50_ms": statistics.median(row["ttft_ms"] for row in samples), "tpot_p50_ms": statistics.median(row["tpot_ms"] for row in samples), "aggregate_output_tokens_per_second": sum(row["output_tokens"] for row in samples)/wall, "wall_seconds": wall, "samples": samples, "metric_scope": "client end-to-end, including queueing; one SSE event per generated token; single-host smoke, not a vLLM/SGLang benchmark", "service_cleanup": "owned process stopped after benchmark"}
        finally:
            if occupied:
                occupied.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    root = Path(sys.argv[1]).resolve()
    if root.parent == Path("/") or not (root / "spec.json").is_file():
        raise ValueError("Worker requires an initialized dedicated workspace")
    spec = json.loads((root / "spec.json").read_text())
    if root != Path(spec["root"]) / spec["project"]:
        raise ValueError("Workspace does not match authorized specification")
    action = sys.argv[2]
    if action == "probe":
        print(json.dumps(probe(root)))
    elif action == "start":
        stage = sys.argv[3]
        if stage not in {"provision", "download", "cuda_smoke", "nccl_smoke", "inference", "train", "serve_benchmark"}:
            raise ValueError("Unknown recipe stage")
        job = sys.argv[4] if len(sys.argv) > 4 else stage + "-" + uuid.uuid4().hex[:12]
        if not job.startswith(stage + "-") or not valid_job_id(job):
            raise ValueError("Invalid job ID")
        if (root / "jobs" / (job + ".json")).exists():
            print((root / "jobs" / (job + ".json")).read_text())
            return
        if stage in {'cuda_smoke', 'nccl_smoke', 'inference', 'train', 'serve_benchmark'}:
            # Fail closed on occupied GPUs; never reclaim another user's card.
            usage = run(['nvidia-smi', '--query-gpu=index,memory.used', '--format=csv,noheader,nounits'])
            busy = {int(row.split(',')[0]): int(row.split(',')[1]) for row in usage.splitlines()}
            selected = spec['gpu_ids'] if stage == 'nccl_smoke' else spec['gpu_ids'][:1]
            if any(busy[gpu] > 0 for gpu in selected):
                raise ValueError('gpu_unavailable: selected GPU is occupied; no shared job is stopped')
        interpreter = sys.executable if stage == "provision" else str(root / ".venv/bin/python")
        env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ["KEY", "PASSWORD", "SECRET", "TOKEN"])}
        (root / "tmp").mkdir(exist_ok=True)
        env.update(CUDA_VISIBLE_DEVICES=",".join(map(str, spec["gpu_ids"] if stage == "nccl_smoke" else spec["gpu_ids"][:1])), HF_HOME=str(root / ".cache/hf"), HF_ENDPOINT=spec["model_endpoint"], HF_HUB_DISABLE_XET="1", OMP_NUM_THREADS="4", TOKENIZERS_PARALLELISM="false", CUBLAS_WORKSPACE_CONFIG=":4096:8", PIP_CACHE_DIR=str(root / ".cache/pip"), VIRTUALENV_OVERRIDE_APP_DATA=str(root / ".cache/virtualenv"), TMPDIR=str(root / "tmp"), CUDA_CACHE_PATH=str(root / ".cache/cuda"))
        atomic(root / "jobs" / (job + ".json"), {"job": job, "stage": stage, "status": "running", "started": time.time()})
        with (root / "logs" / (job + ".log")).open("w") as log:
            proc = subprocess.Popen([interpreter, "-u", str(root / "worker.py"), str(root), "job", job, stage], stdout=log, stderr=subprocess.STDOUT, cwd=root, env=env, start_new_session=True)
        state = json.loads((root / "jobs" / (job + ".json")).read_text())
        state["pid"] = proc.pid
        atomic(root / "jobs" / (job + ".json"), state)
        print(json.dumps(state))
    elif action == "inspect":
        job = sys.argv[3]
        if not valid_job_id(job):
            raise ValueError("Invalid job ID")
        state = json.loads((root / "jobs" / (job + ".json")).read_text())
        log = root / "logs" / (job + ".log")
        state["log_tail"] = log.read_text(errors="replace")[-1800:] if log.exists() else ""
        if state["status"] == "running":
            pid = state.get("pid")
            if pid and not Path(f"/proc/{pid}").exists():
                state.update(status="unknown", error="process exited without a final result; not automatically replayed")
        print(json.dumps(state))
    elif action == 'cancel':
        job = sys.argv[3]
        if not valid_job_id(job):
            raise ValueError('Invalid job ID')
        state_path = root / 'jobs' / (job + '.json')
        state = json.loads(state_path.read_text())
        pid = state.get('pid')
        if state['status'] == 'running' and pid:
            command = Path(f'/proc/{pid}/cmdline')
            if command.exists():
                args = command.read_bytes().decode().split('\0')
                if str(root / 'worker.py') not in args or job not in args or os.getpgid(pid) != pid:
                    raise ValueError('Owned job identity mismatch; process not stopped')
                os.killpg(pid, signal.SIGTERM)
                state.update(status='failed', error='operator_cancelled_owned_job')
                atomic(state_path, state)
        print(json.dumps(state))
    elif action == "job":
        job, stage = sys.argv[3:5]
        started = time.monotonic()
        state_path = root / "jobs" / (job + ".json")
        state = json.loads(state_path.read_text())
        # Stage timeout also terminates owned child processes if SSH disconnects.
        def expired(*_):
            state.update(status="failed", error="stage_timeout")
            atomic(state_path, state)
            os.killpg(os.getpgrp(), signal.SIGTERM)
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(spec["stage_timeout"])
        try:
            output = dispatch(root, spec, stage)
            state.update(status="completed", result=output)
            atomic(root / "reports" / (stage + ".json"), output)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            signal.alarm(0)
            state.update(pid=os.getpid(), duration_seconds=round(time.monotonic() - started, 3), finished=time.time())
            atomic(state_path, state)
        print(json.dumps(state))
    elif action == "nccl_rank":
        nccl_rank(root)
    elif action == "server":
        serve(root, spec)


if __name__ == "__main__":
    main()
