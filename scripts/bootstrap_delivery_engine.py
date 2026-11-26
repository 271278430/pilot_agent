"""Start only the project-owned, read-only model engine on an idle authorized GPU."""

import argparse
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

from repopilot.storage import atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("/data/simulator/edu_graph_20261107")
PROFILES = {
    "teacher8b": {
        "model": str(
            BASE / "experiments/expanded_20261111/08_course_8b_expanded/runs/sft_b4/checkpoint"
        ),
        "adapter": str(
            BASE
            / "experiments/expanded_20261111/10_teaching_8b_rebased/runs/training_packed_b4_v3/adapter"
        ),
        "tokenizer": str(BASE / "models/Qwen3-8B"),
        "model_id": "Qwen3-8B/course-sft/teaching-lora",
    },
    "smol": {
        "model": "/data/pilot-owner/infrapilot-ml-env-20261117/models/smollm",
        "adapter": "/data/pilot-owner/infrapilot-ml-env-20261117/checkpoints/adapter",
        "tokenizer": "/data/pilot-owner/infrapilot-ml-env-20261117/models/smollm",
        "model_id": "HuggingFaceTB/SmolLM2-135M-Instruct",
    },
}


def owned_pid(pid):
    proc = Path("/proc") / str(pid)
    if not proc.exists():
        return False
    args = (proc / "cmdline").read_bytes().decode().split("\0")
    if str(ROOT / "src/pilot_delivery/engine.py") not in args or (proc / "cwd").resolve() != ROOT:
        raise ValueError("Refuse to control a process outside this engine")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, default="teacher8b")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if ROOT != Path("/data/pilot-owner/pilot"):
        raise ValueError("GPU bootstrap is limited to the authorized server project")
    ready_file = ROOT / ".local/delivery-engine-ready.json"
    config_file = ROOT / ".local/delivery-engine.json"
    ready = read_json(ready_file) if ready_file.exists() else {}
    from pilot_delivery.store import DeliveryStore

    if (args.stop or ready.get("profile") != args.profile) and any(
        release.status in {"running", "paused"} for release in DeliveryStore(ROOT).active()
    ):
        raise ValueError("Active deliveries reference this engine; refuse stop/replacement")
    if ready.get("pid") and owned_pid(ready["pid"]):
        if ready.get("profile") == args.profile and not args.stop:
            print(
                json.dumps(
                    {
                        "reused": True,
                        "profile": args.profile,
                        "model_identity": ready["model_identity"],
                    }
                )
            )
            return
        os.kill(ready["pid"], signal.SIGTERM)
        for _ in range(50):
            if not (Path("/proc") / str(ready["pid"])).exists():
                break
            time.sleep(0.1)
        else:
            raise ValueError("Owned engine did not stop")
    if args.stop:
        print("Stopped only the owned delivery engine")
        return
    probe = subprocess.run(
        [
            "nvidia-smi",
            "--id=6",
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    used, util = map(int, probe.stdout.strip().split(","))
    if used > 1024 or util > 10:
        raise ValueError("GPU 6 is occupied; do not interfere with existing work")
    socket = ROOT / ".local/engine/socket.sock"
    socket.parent.mkdir(parents=True, exist_ok=True)
    if socket.exists():
        socket.unlink()  # Only the exact registered socket, after owned engine is known stopped.
    config = {
        **PROFILES[args.profile],
        "profile": args.profile,
        "project_root": str(ROOT),
        "socket": str(socket),
        "gpu": 6,
        "token": secrets.token_urlsafe(32),
    }
    atomic_json(config_file, config)
    os.chmod(config_file, 0o600)
    if ready_file.exists():
        ready_file.unlink()
    python = "/data/pilot-owner/infrapilot-ml-env-20261117/.venv/bin/python"
    with (ROOT / ".local/delivery-engine.log").open("ab") as log:
        process = subprocess.Popen(
            [python, str(ROOT / "src/pilot_delivery/engine.py"), str(config_file)],
            cwd=ROOT,
            env={
                **os.environ,
                "CUDA_VISIBLE_DEVICES": "6",
                "HF_HOME": str(ROOT / ".local/model-cache"),
            },
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    atomic_json(
        ROOT / ".local/delivery-engine-launch.json", {"pid": process.pid, "profile": args.profile}
    )
    for _ in range(240):
        if process.poll() is not None:
            raise ValueError("Engine exited; inspect project .local/delivery-engine.log")
        if ready_file.exists():
            value = read_json(ready_file)
            print(
                json.dumps(
                    {
                        "ready": True,
                        "profile": args.profile,
                        "model_identity": value["model_identity"],
                        "gpu": 6,
                    }
                )
            )
            return
        time.sleep(1)
    raise ValueError(
        "Engine readiness not confirmed; keep original PID for inspection, do not relaunch"
    )


if __name__ == "__main__":
    main()
