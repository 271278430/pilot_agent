"""Private stdio broker used only by the native MCP client. Never executes workload code on host."""

import json
import os
import selectors
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pilot_core import execution_sandbox as sandbox  # noqa: E402
from pilot_core.docker import prefix  # noqa: E402


def attach_command(directory):
    command, password = prefix()
    environment = dict(os.environ)
    if password:
        # Askpass is an independent private pipe even with no timestamp cache
        # or NOPASSWD. No password byte can become workload protocol input.
        askpass = directory / "askpass"
        content = (
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(str(Path(__file__).with_name("sandbox_askpass.py")))
            + "\n"
        )
        if askpass.exists():
            if askpass.is_symlink() or askpass.read_text() != content:
                raise ValueError("Sandbox authentication helper changed")
        else:
            fd = os.open(askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
        command = ["sudo", "-A", "-p", "", "/usr/bin/docker"]
        environment["SUDO_ASKPASS"] = str(askpass)
    return command, environment


def main():
    path = Path(os.environ.pop("PILOT_MCP_SANDBOX_SPEC"))
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or path.name != "receipt.json"
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("Sandbox spec unavailable")
    value = json.loads(path.read_text())
    root, identity = value["root"], value["id"]
    if path != sandbox.base(root) / identity / "receipt.json":
        raise ValueError("Sandbox spec ownership mismatch")
    environment = json.loads(os.environ.pop("PILOT_MCP_CONTAINER_ENV", "{}"))
    process = None
    stopped = threading.Event()
    phase = "create"
    received = 0
    sent = 0
    stop_reason = ""
    launched = False
    try:
        value = sandbox.launch(root, identity, environment)
        launched = True
        phase = "attach"
        command, attach_env = attach_command(path.parent)
        process = subprocess.Popen(
            [*command, "start", "--attach", "--interactive", value["container_id"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=attach_env,
        )

        def feed():
            nonlocal received, stop_reason
            try:
                while not stopped.is_set():
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        stop_reason = "stdin-eof"
                        break
                    received += len(data)
                    process.stdin.write(data)
                    process.stdin.flush()
            except (OSError, ValueError):
                pass
            stopped.set()

        threading.Thread(target=feed, daemon=True).start()

        def end(*_):
            nonlocal stop_reason
            stop_reason = "signal"
            stopped.set()

        signal.signal(signal.SIGTERM, end)
        signal.signal(signal.SIGINT, end)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            total = 0
            while not stopped.is_set() and time.time() < value["deadline"]:
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        stop_reason = "attach-eof"
                        stopped.set()
                        break
                    total += len(data)
                    sent += len(data)
                    if total > 8 * 1024**2:
                        raise ValueError("Sandbox protocol output exceeded its budget")
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                if process.poll() is not None:
                    stop_reason = "attach-exit"
                    break
    except Exception as error:
        with sandbox.lock(root):
            directory, state = sandbox.receipt(root, identity)
            if state["owner_token"] == value["owner_token"]:
                state["last_error"] = {
                    "phase": phase,
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                }
                sandbox.atomic(directory / "receipt.json", state)
        raise
    finally:
        stopped.set()
        try:
            if launched:
                with sandbox.lock(root):
                    directory, state = sandbox.receipt(root, identity)
                    if state["owner_token"] == value["owner_token"]:
                        state["transport"] = {
                            "pid": os.getpid(),
                            "received_bytes": received,
                            "sent_bytes": sent,
                            "stop_reason": stop_reason,
                            "attach_exit": process.poll() if process else None,
                        }
                        sandbox.atomic(directory / "receipt.json", state)
                        sandbox._stop(root, identity, final=False, owner_token=value["owner_token"])
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Docker stderr, env values and model credentials never cross MCP stdout/stderr.
        sys.stderr.write("Pilot execution sandbox unavailable; inspect its receipt\n")
        raise SystemExit(1) from None
