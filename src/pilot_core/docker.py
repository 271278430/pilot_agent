"""Trusted Docker access; privileged credentials never enter a workload container."""

import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def prefix():
    secret_file = ROOT / ".local/docker.env"
    password = dotenv_values(secret_file).get("PILOT_DOCKER_SUDO_PASSWORD") if secret_file.exists() else None
    return (["sudo", "-S", "-p", "", "/usr/bin/docker"] if password else ["docker"]), password


def docker(argv: list[str], timeout=30, check=True):
    command, password = prefix()
    result = subprocess.run([*command, *argv], input=password + "\n" if password else None,
                            capture_output=True, text=True, timeout=timeout)
    if password:
        result.stdout = result.stdout.replace(password, "[REDACTED]")
        result.stderr = result.stderr.replace(password, "[REDACTED]")
    if check and result.returncode:
        raise RuntimeError(f"Docker operation failed exit={result.returncode}: {result.stderr[-1000:]}")
    return result
