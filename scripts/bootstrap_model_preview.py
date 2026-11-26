"""Launch v2 on an idle GPU; never replace or stop the accepted v1 engine."""

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from pilot_delivery.engine import digest_file
from pilot_delivery.engine_client import call_engine
from repopilot.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    if ROOT != Path("/data/pilot-owner/pilot"):
        raise ValueError("Preview GPU bootstrap is restricted to the authorized project")
    folder = ROOT / ".local/model-preview"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    source = ROOT / "src/pilot_delivery/preview_engine.py"
    if (folder / "config.json").exists():
        config = json.loads((folder / "config.json").read_text())
        try:
            ready = call_engine(config, "ping", timeout=3)
        except (OSError, ValueError):
            raise ValueError(
                "Existing preview configuration needs inspection; refuse duplicate launch"
            ) from None
        if ready["code_sha256"] != digest_file(source):
            raise ValueError("Existing preview source version changed; inspect before replacing")
        print(json.dumps({"reused": True, "protocol": ready["protocol"], "gpu": ready["gpu"]}))
        return
    probe = subprocess.run(
        [
            "nvidia-smi",
            "--id=7",
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    used, util = map(int, probe.stdout.strip().split(","))
    if used > 1024 or util > 10:
        raise ValueError("GPU7 is occupied; refuse to interfere with shared work")
    v1 = json.loads((ROOT / ".local/delivery-engine.json").read_text())
    ready = call_engine(v1, "ping", timeout=3)
    if ready["profile"] != "teacher8b":
        raise ValueError("Expected accepted teacher8b model")
    config = {k: v1[k] for k in ("project_root", "model", "adapter", "tokenizer", "model_id")}
    config.update(
        socket=str(folder / "socket.sock"),
        token=secrets.token_urlsafe(32),
        gpu=7,
        expected_identity=ready["model_identity"],
    )
    atomic_json(folder / "config.json", config)
    os.chmod(folder / "config.json", 0o600)
    python = "/data/pilot-owner/infrapilot-ml-env-20261117/.venv/bin/python"
    with (folder / "worker.log").open("ab") as log:
        worker = subprocess.Popen(
            [python, "-m", "pilot_delivery.preview_engine", str(folder / "config.json")],
            cwd=ROOT,
            env={**os.environ, "CUDA_VISIBLE_DEVICES": "7", "PYTHONPATH": str(ROOT/'src')},
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    atomic_json(folder / "launch.json", {"pid": worker.pid, "gpu": 7})
    for _ in range(240):
        if worker.poll() is not None:
            raise ValueError("Preview exited; inspect its own worker.log, do not relaunch blindly")
        if (folder / "ready.json").exists():
            current = call_engine(config, "ping", timeout=3)
            print(
                json.dumps(
                    {
                        "ready": True,
                        "protocol": current["protocol"],
                        "model_identity": current["model_identity"],
                        "gpu": 7,
                    }
                )
            )
            return
        time.sleep(1)
    raise ValueError("Preview readiness not yet confirmed; inspect original launch PID")


if __name__ == "__main__":
    main()
