"""Bounded transport shared by delivery execution and read-only inventory."""

import json
import socket


def call_engine(config, operation, *, timeout=90, **arguments):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(config["socket"])
        stream.sendall((json.dumps({"operation": operation, "token": config["token"], **arguments}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            part = stream.recv(65536)
            if not part:
                break
            data += part
            if len(data) > 262144:
                raise ValueError("Oversize model output")
        value = json.loads(data)
        if not value["ok"]:
            raise ValueError(value["error"])
        return value["result"]
