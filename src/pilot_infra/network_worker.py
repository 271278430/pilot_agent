"""Bounded TCP echo checks in a task-owned directory; no host/network reconfiguration."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def lease(root, task_id):
    if root.name != "pilot-network-" + task_id or root.resolve() != root or root.is_symlink():
        raise ValueError("Invalid owned network task root")
    if json.loads((root / "lease.json").read_text()) != {"task_id": task_id, "root": str(root)}:
        raise ValueError("Network task ownership mismatch")


def line(connection):
    result = bytearray()
    while not result.endswith(b"\n"):
        data = connection.recv(1)
        if not data or len(result) > 1000:
            raise ValueError("Invalid bounded network check header")
        result.extend(data)
    return json.loads(result)


def listen(root):
    config = json.loads((root / "config.json").read_text())
    record = {"status": "running", "received": 0, "node_identity": config["node_identity"]}
    try:
        with socket.socket() as server:
            server.bind((config["address"], config["port"]))
            server.listen(4)
            server.settimeout(0.5)
            record["ready"] = True
            save(root / "status.json", record)
            while time.time() < config["deadline"] and record["received"] < config["connections"]:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(5)
                    request = line(connection)
                    if (
                        request.get("nonce") != config["nonce"]
                        or request.get("bytes") != config["bytes"]
                    ):
                        raise ValueError("Network peer scope differs")
                    connection.sendall(
                        (json.dumps({"node_identity": config["node_identity"]}) + "\n").encode()
                    )
                    left = config["bytes"]
                    hasher = hashlib.sha256()
                    while left:
                        data = connection.recv(min(left, 65536))
                        if not data:
                            raise ValueError("Network payload incomplete")
                        hasher.update(data)
                        left -= len(data)
                        connection.sendall(data)
                    record["received"] += 1
                    record["last_payload_sha256"] = hasher.hexdigest()
                    save(root / "status.json", record)
            record["status"] = (
                "completed" if record["received"] == config["connections"] else "expired"
            )
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__)
    save(root / "status.json", record)


def alive(record, root):
    pid = record.get("pid")
    if not pid:
        return False
    try:
        if record.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            return False
        stat = Path("/proc/" + str(pid) + "/stat").read_text().rsplit(")", 1)[1].split()
        cmd = Path("/proc/" + str(pid) + "/cmdline").read_bytes()
        return stat[0] != "Z" and stat[19] == record["start_ticks"] and str(root).encode() in cmd
    except OSError:
        return False


def perform(root, task_id, action, args):
    lease(root, task_id)
    if action == "start":
        if (root / "config.json").exists():
            raise ValueError("Original listener exists; inspect instead of replaying")
        save(root / "config.json", args)
        process = subprocess.Popen(
            [sys.executable, "-B", __file__, "listen", str(root), task_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"PILOT_NODE_DOCKER_SUDO_PASSWORD", "PILOT_NODE_DOCKER_USE_SUDO"}
            },
        )
        record = {
            "pid": process.pid,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": Path("/proc/" + str(process.pid) + "/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19],
        }
        save(root / "process.json", record)
        return record
    if action == "inspect":
        return (
            json.loads((root / "status.json").read_text())
            if (root / "status.json").exists()
            else {"status": "starting"}
        )
    if action == "send":
        result = []
        block = bytes.fromhex(args["nonce"]) * 2048
        for _ in range(args["rounds"]):
            if time.time() >= args.get("deadline", float("inf")):
                raise TimeoutError("Network task deadline reached")
            start = time.perf_counter()
            hasher = hashlib.sha256()
            with socket.socket() as connection:
                connection.settimeout(5)
                connection.bind((args["source"], 0))
                connection.connect((args["target"], args["port"]))
                connection.sendall(
                    (json.dumps({"nonce": args["nonce"], "bytes": args["bytes"]}) + "\n").encode()
                )
                peer = line(connection)
                if peer["node_identity"] != args["peer_identity"]:
                    raise ValueError("Network endpoint identity differs")
                left = args["bytes"]
                while left:
                    data = block[: min(left, len(block))]
                    connection.sendall(data)
                    echoed = bytearray()
                    while len(echoed) < len(data):
                        chunk = connection.recv(len(data) - len(echoed))
                        if not chunk:
                            raise ValueError("Echo payload incomplete")
                        echoed.extend(chunk)
                    if bytes(echoed) != data:
                        raise ValueError("Echo payload checksum differs")
                    hasher.update(data)
                    left -= len(data)
            elapsed = time.perf_counter() - start
            result.append(
                {
                    "bytes": args["bytes"],
                    "sha256": hasher.hexdigest(),
                    "elapsed_s": elapsed,
                    "roundtrip_payload_mbps": args["bytes"] * 8 / elapsed / 1e6,
                }
            )
        return {"passed": True, "rounds": result, "measurement": "bounded_TCP_echo_not_RDMA_or_KV"}
    if action == "cleanup":
        if (root / "process.json").exists():
            record = json.loads((root / "process.json").read_text())
            if alive(record, root):
                os.kill(record["pid"], signal.SIGTERM)
                until = time.monotonic() + 5
                while alive(record, root) and time.monotonic() < until:
                    time.sleep(0.1)
                if alive(record, root):
                    raise ValueError("Original network listener has not exited")
        import shutil

        shutil.rmtree(root)
        return {"restored": not root.exists(), "listener_stopped": True}
    raise ValueError("Unknown bounded network action")


def main():
    action, root, task_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    lease(root, task_id)
    if action == "listen":
        listen(root)
        return
    try:
        value = perform(root, task_id, action, json.load(sys.stdin))
        print(json.dumps({"ok": True, "value": value}))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)[:800]}))


if __name__ == "__main__":
    main()
