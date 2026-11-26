from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def git_environment(environment=None):
    """Remove inherited inline Git configuration as one complete group.

    Secret scrubbing can remove GIT_CONFIG_KEY_n while leaving COUNT/VALUE_n,
    which makes every Git command fail. Local repository operations also do
    not need the caller's injected credential helper configuration.
    """
    source = os.environ if environment is None else environment
    return {key: value for key, value in source.items()
            if key not in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
            and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}


def process_running(pid: int) -> bool:
    """Probe a lock owner without sending a signal on Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            # ERROR_INVALID_PARAMETER means the PID does not exist. Access
            # denied/other errors must conservatively retain the lock.
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Environment:
    def __init__(self, repo: Path, mode: str, image: str = "repopilot-python:0.1.0"):
        self.repo, self.mode, self.image = repo, mode, image
        self.image_id = None

    def execute(self, argv: list[str], timeout: int) -> dict:
        if not argv or any("\x00" in v for v in argv):
            raise ValueError("Non-empty argv is required")
        started = time.monotonic()
        command = list(argv)
        working_directory = str(self.repo)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(t in k.upper() for t in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
        }
        env = git_environment(env)
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(self.repo), PYTHONUTF8="1")
        if os.name == "nt" and self.mode == "local":
            # Deep acceptance copies can exceed MAX_PATH. Use a process-local
            # namespace instead of changing Windows registry or host settings.
            absolute = str(self.repo.resolve())
            working_directory = (
                absolute
                if absolute.startswith("\\\\?\\")
                else "\\\\?\\UNC\\" + absolute[2:]
                if absolute.startswith("\\\\")
                else "\\\\?\\" + absolute
            )
            env["PYTHONPATH"] = working_directory
        container_name = None
        if self.mode == "docker":
            import uuid

            from pilot_core.docker import docker, prefix

            inspect = docker(["image", "inspect", self.image, "--format", "{{.Id}}"], timeout=15)
            self.image_id = inspect.stdout.strip()
            container_name = "repopilot-" + uuid.uuid4().hex[:16]
            command = [
                *prefix()[0],
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                "none",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--pids-limit",
                "256",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,size=512m",
                "--user",
                "65534:65534",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "PYTHONPATH=/workspace",
                "--mount",
                f"type=bind,source={self.repo.resolve()},target=/workspace,readonly",
                "-w",
                "/workspace",
                self.image,
                *argv,
            ]
        elif command[0] in {"python", "python3"}:
            command[0] = sys.executable
        elif command[0] == "pytest":
            command = [sys.executable, "-m", "pytest", *command[1:]]
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        with tempfile.TemporaryFile() as out:
            password = prefix()[1] if self.mode == "docker" else None
            proc = subprocess.Popen(
                command,
                cwd=working_directory,
                env=env,
                stdin=subprocess.PIPE if password else subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
            if password:
                proc.stdin.write((password + "\n").encode())
                proc.stdin.close()
            error = None
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                error, code = "timeout", 124
                if container_name:
                    docker(["kill", container_name], timeout=15, check=False)
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            out.seek(0, 2)
            length = out.tell()
            out.seek(max(0, length - 24000))
            output = out.read().decode("utf-8", errors="replace")
        return {
            "exit_code": code,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "output": output,
            "output_truncated": length > 24000,
            "error": error,
            "image_id": self.image_id,
        }
