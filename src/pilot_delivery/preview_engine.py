"""Trusted v2 interactive worker; old accepted v1 engine remains untouched."""

import hashlib
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from pathlib import Path

from pilot_delivery.engine import asset_manifest, digest_file
from pilot_delivery.preview_contract import PreviewRequest, completion_stats


def main(config_file):
    if not Path("/.dockerenv").is_file():
        raise ValueError("New model workers require a Docker container; reuse existing services or use Pilot inference tasks")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    config = json.loads(Path(config_file).read_text())
    root = Path(config["project_root"]).resolve()
    folder = Path(config.get("worker_dir", str(root / ".local/model-preview")))
    socket = Path(config["socket"])
    if (
        root != Path("/data/pilot-owner/pilot")
        or not folder.resolve().is_relative_to(root)
        or folder.is_symlink()
        or socket != folder / "socket.sock"
        or socket.exists()
    ):
        raise ValueError("New preview socket must be project-local")
    os.environ["HF_HUB_OFFLINE"] = "1"
    torch.set_num_threads(4)
    device = "cpu" if config.get("runtime") == "cpu" else "cuda"
    if device == "cuda":
        torch.cuda.set_per_process_memory_fraction(0.4)
    torch.manual_seed(42)
    base = Path(config["model"])
    adapter = Path(config["adapter"]) if config.get("adapter") else None
    if adapter and json.loads((adapter / "adapter_config.json").read_text())[
        "base_model_name_or_path"
    ] not in {str(base), config["model_id"]}:
        raise ValueError("Adapter base mismatch")
    manifest = asset_manifest(base, adapter or base, Path(config["tokenizer"]))
    if not adapter:
        manifest = {k: v for k, v in manifest.items() if not k.startswith("adapter/")}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if config.get("expected_identity") and identity != config["expected_identity"]:
        raise ValueError("Preview model assets differ from accepted model")
    tokenizer = AutoTokenizer.from_pretrained(
        config["tokenizer"], local_files_only=True, trust_remote_code=False
    )
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
        attn_implementation="eager",
    ).to(device)
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    ready = {
        "protocol": "model-preview-v2",
        "model_identity": identity,
        "code_sha256": digest_file(Path(__file__)),
        "contract_sha256": digest_file(Path(__file__).with_name("preview_contract.py")),
        "model_id": config["model_id"],
        "gpu": config.get("gpu"),
        "runtime": device,
        "pid": os.getpid(),
        "max_output_tokens": 1024,
        "max_input_tokens": 4096,
    }
    lock = threading.Lock()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(120)
            try:
                wire = json.loads(self.rfile.readline(131072))
                if not secrets.compare_digest(str(wire.get("token", "")), config["token"]):
                    raise ValueError("Preview capability denied")
                if wire["operation"] == "ping":
                    result = ready
                elif wire["operation"] == "chat":
                    request = PreviewRequest.model_validate(wire["request"])
                    if not lock.acquire(blocking=False):
                        raise ValueError("模型正在处理另一个试用请求，请稍后再试。")
                    try:
                        text = tokenizer.apply_chat_template(
                            [m.model_dump() for m in request.messages],
                            tokenize=False,
                            add_generation_prompt=True,
                            enable_thinking=False,
                        )
                        inputs = tokenizer(text, return_tensors="pt", truncation=False).to(device)
                        if inputs["input_ids"].shape[1] > 4096:
                            raise ValueError("输入超过模型试用的上下文预算，请精简输入或新建对话。")
                        started = time.monotonic()
                        with torch.inference_mode():
                            output = model.generate(
                                **inputs,
                                max_new_tokens=request.max_new_tokens,
                                do_sample=False,
                                pad_token_id=tokenizer.pad_token_id,
                            )
                        if device == "cuda":
                            torch.cuda.synchronize()
                        new = output[:, inputs["input_ids"].shape[1] :]
                        eos = model.generation_config.eos_token_id or tokenizer.eos_token_id
                        counts, reasons = completion_stats(
                            new.tolist(), set(eos if isinstance(eos, list) else [eos])
                        )
                        result = {
                            "text": tokenizer.decode(new[0][: counts[0]], skip_special_tokens=True),
                            "completion_tokens": counts[0],
                            "input_tokens": inputs["input_ids"].shape[1],
                            "finish_reason": reasons[0],
                            "model_identity": identity,
                            "protocol": "model-preview-v2",
                            "model_id": config["model_id"],
                            "inference_ms": round((time.monotonic() - started) * 1000, 2),
                        }
                        result["receipt_id"] = secrets.token_hex(16)
                        result["model_input"] = request.messages[-1].content
                        with (folder / "inference-receipts.jsonl").open("a") as receipts:
                            receipts.write(
                                json.dumps(
                                    {
                                        "receipt_id": result["receipt_id"],
                                        "model_identity": identity,
                                        "input_sha256": hashlib.sha256(
                                            result["model_input"].encode()
                                        ).hexdigest(),
                                        "answer_sha256": hashlib.sha256(
                                            result["text"].encode()
                                        ).hexdigest(),
                                    }
                                )
                                + "\n"
                            )
                    finally:
                        lock.release()
                else:
                    raise ValueError("Unregistered preview operation")
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)[:500]}
            self.wfile.write((json.dumps(response) + "\n").encode())

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    with Server(str(socket), Handler) as server:
        # Parent remains owner-only; selected CPU containers receive only this
        # exact socket, without access to config, GLM keys or the receipt ledger.
        os.chmod(socket, 0o666)
        tmp = folder / "ready.tmp"
        tmp.write_text(json.dumps(ready))
        os.replace(tmp, folder / "ready.json")
        server.serve_forever(poll_interval=0.3)


if __name__ == "__main__":
    main(sys.argv[1])
