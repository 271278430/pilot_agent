"""Real Streamable HTTP discovery through the same official CLI client."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def test_streamable_http_native_cli_discovers_and_closes_without_calling_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "http.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, str(root / "tests/mcp_fixture.py"), "--http", str(port)], stdout=log, stderr=log)
    cli = [sys.executable, "-m", "pilot_host.cli", "tools", "--root", str(tmp_path)]
    definition = {"name": "http", "transport": "streamable-http", "enabled": False, "roles": ["infra-lead"],
                  "access": "reference", "url": f"http://127.0.0.1:{port}/mcp"}
    source = tmp_path / "server.json"
    source.write_text(json.dumps(definition), encoding="utf-8")
    try:
        for _ in range(60):
            assert process.poll() is None
            try:
                if httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=1).status_code == 405:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        subprocess.run([*cli, "put", "--file", str(source), "--revision", "0"], check=True, capture_output=True, timeout=30)
        result = subprocess.run([*cli, "test", "http"], check=True, capture_output=True, text=True, timeout=45)
        value = json.loads(result.stdout)
        assert value["status"] == "connection_verified"
        assert any(t["name"] == "mcp__http__echo" for t in value["registered_tools"])
        assert "CallToolRequest" not in (tmp_path / "http.log").read_text(errors="replace")
        assert not json.loads(subprocess.run([*cli, "list"], check=True, capture_output=True, text=True).stdout)["servers"][0]["enabled"]
    finally:
        process.terminate()
        process.wait(timeout=15)
