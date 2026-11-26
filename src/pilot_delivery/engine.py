"""Trusted model worker. No repository code is executed in the GPU process."""

import hashlib
import json
import os
import socketserver
import sys
import time
from pathlib import Path


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def asset_manifest(base, adapter, tokenizer):
    """Bind inference weights/config and tokenizer assets without hashing unused weights twice."""
    manifest = {}
    for scope, directory in [("base", base), ("adapter", adapter), ("tokenizer", tokenizer)]:
        for path in sorted(Path(directory).rglob("*")):
            if not path.is_file():
                continue
            extensions = (
                {".json", ".model", ".txt", ".jinja", ".tiktoken", ".vocab"}
                if scope == "tokenizer"
                else {".json", ".safetensors", ".bin", ".model"}
            )
            if path.suffix not in extensions:
                continue
            manifest[scope + "/" + path.relative_to(directory).as_posix()] = {
                "sha256": digest_file(path),
                "bytes": path.stat().st_size,
            }
    return manifest


def main(config_file):
    if not Path("/.dockerenv").is_file():
        raise ValueError("New model workers require a Docker container; reuse existing services or use Pilot inference tasks")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    config = json.loads(Path(config_file).read_text())
    root = Path(config["project_root"]).resolve()
    socket = Path(config["socket"])
    if not socket.resolve().is_relative_to(root / ".local") or socket.exists():
        raise ValueError("Engine socket must be new and project-local")
    os.environ["HF_HOME"] = str(root / ".local/model-cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    torch.set_num_threads(4)
    torch.cuda.set_per_process_memory_fraction(0.35)
    torch.manual_seed(42)
    base, adapter = Path(config["model"]), Path(config["adapter"])
    metadata = json.loads((adapter / "adapter_config.json").read_text())
    if metadata["base_model_name_or_path"] not in {str(base), config["model_id"]}:
        raise ValueError("Adapter training base does not match registered model checkpoint")
    manifest = asset_manifest(base, adapter, Path(config["tokenizer"]))
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    tokenizer = AutoTokenizer.from_pretrained(
        config["tokenizer"], local_files_only=True, trust_remote_code=False, padding_side="left"
    )
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.bfloat16,
        attn_implementation="eager",
    ).to("cuda")
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    ready = {
        "profile": config["profile"],
        "model_identity": identity,
        "engine_code_sha256": digest_file(Path(__file__)),
        "manifest": manifest,
        "model_id": config["model_id"],
        "adapter_loaded": True,
        "dtype": "bfloat16",
        "device": config["gpu"],
        "pid": os.getpid(),
        "socket": str(socket),
    }
    ready_file = root / ".local/delivery-engine-ready.json"

    def save(value):
        tmp = ready_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        os.replace(tmp, ready_file)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(90)
            try:
                request = json.loads(self.rfile.readline(16000))
                if request.get("token") != config["token"]:
                    raise ValueError("Engine capability denied")
                if request["operation"] == "ping":
                    result = {
                        k: ready[k]
                        for k in [
                            "model_identity",
                            "engine_code_sha256",
                            "profile",
                            "model_id",
                            "adapter_loaded",
                            "dtype",
                            "device",
                        ]
                    }
                elif request["operation"] == "generate":
                    prompts = request["prompts"]
                    limit = request.get("max_new_tokens", 8)
                    if (
                        not isinstance(prompts, list)
                        or not 1 <= len(prompts) <= 4
                        or not all(isinstance(p, str) and 0 < len(p) <= 1000 for p in prompts)
                    ):
                        raise ValueError("Invalid prompts")
                    if type(limit) is not int or not 1 <= limit <= 16:
                        raise ValueError("Invalid generation budget")
                    texts = [
                        tokenizer.apply_chat_template(
                            [{"role": "user", "content": p}],
                            tokenize=False,
                            add_generation_prompt=True,
                            enable_thinking=False,
                        )
                        for p in prompts
                    ]
                    inputs = tokenizer(
                        texts, return_tensors="pt", padding=True, truncation=True, max_length=1024
                    ).to("cuda")
                    started = time.monotonic()
                    with torch.inference_mode():
                        output = model.generate(
                            **inputs,
                            max_new_tokens=limit,
                            do_sample=False,
                            pad_token_id=tokenizer.pad_token_id,
                        )
                    torch.cuda.synchronize()
                    new = output[:, inputs["input_ids"].shape[1] :]
                    counts = []
                    for row in new.tolist():
                        counts.append(
                            row.index(tokenizer.eos_token_id) + 1
                            if tokenizer.eos_token_id in row
                            else len(row)
                        )
                    result = {
                        "new_texts": tokenizer.batch_decode(new, skip_special_tokens=True),
                        "full_texts": tokenizer.batch_decode(output, skip_special_tokens=True),
                        "completion_tokens": counts,
                        "input_tokens": inputs["attention_mask"].sum(1).tolist(),
                        "model_identity": identity,
                        "inference_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                else:
                    raise ValueError("Unregistered engine operation")
                response = {"ok": True, "result": result}
            except Exception as error:
                response = {"ok": False, "error": str(error)[:500]}
            self.wfile.write((json.dumps(response) + "\n").encode())

    socket.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket.parent, 0o700)
    with socketserver.UnixStreamServer(str(socket), Handler) as server:
        os.chmod(socket, 0o666)
        save(ready)
        server.serve_forever(poll_interval=0.3)


if __name__ == "__main__":
    main(sys.argv[1])
