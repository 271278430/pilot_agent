"""Run only on the designated project server; record setup without host installs."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if ROOT != Path("/data/pilot-owner/pilot"):
    raise RuntimeError("Bootstrap is limited to /data/pilot-owner/pilot")


def run(argv, env=None, secret=None):
    print("STEP", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=ROOT, env=env, input=(secret + "\n") if secret else None,
                            text=True, capture_output=True, timeout=600)
    output = result.stdout[-4000:] + result.stderr[-2000:]
    if secret:
        output = output.replace(secret, "[REDACTED]")
    print(output, flush=True)
    if result.returncode:
        raise RuntimeError(f"Bootstrap command failed exit={result.returncode}")


def main():
    env = dict(os.environ)
    env.update(UV_CACHE_DIR=str(ROOT / ".local/uv-cache"), UV_PYTHON_INSTALL_DIR=str(ROOT / ".local/python"))
    boot = ROOT / ".local/bootstrap"
    run([sys.executable, "-m", "pip", "install", "--target", str(boot), "uv==0.9.8", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], env)
    uv = str(boot / "bin/uv")
    run([uv, "python", "install", "3.11"], env)
    run([uv, "venv", "--python", "3.11", str(ROOT / ".venv")], env)
    run([uv, "pip", "install", "--python", str(ROOT / ".venv/bin/python"), "-e", ".[dev]", "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"], env)
    run(["npm", "ci", "--prefix", "harness/runtime", "--cache", str(ROOT / ".local/npm-cache")], env)
    password = dict(line.split("=", 1) for line in (ROOT / ".local/docker.env").read_text().splitlines())["PILOT_DOCKER_SUDO_PASSWORD"]
    run(["sudo", "-S", "-p", "", "docker", "pull", "python:3.11-slim"], env, password)
    docker = ROOT / ".local/docker"
    docker.mkdir(exist_ok=True)
    (docker / "Dockerfile").write_text("FROM python:3.11-slim\nRUN pip install --no-cache-dir pytest==8.3.5\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nWORKDIR /workspace\nUSER 65534:65534\n")
    run(["sudo", "-S", "-p", "", "docker", "build", "-t", "pilot-python:0.5.0", str(docker)], env, password)
    (ROOT / ".local/bootstrap-ready.json").write_text(json.dumps({"ready": True, "root": str(ROOT), "image": "pilot-python:0.5.0"}))


if __name__ == "__main__":
    main()
