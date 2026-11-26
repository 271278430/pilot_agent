"""Actual installed Host registry, blank Sessions, CLI and cold restoration."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from pilot_host.cli import composition
from pilot_host.workspace_cli import main
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.test_pilot_infra import setup


def test_native_workspace_creation_cli_and_restart(tmp_path, capsys):
    project = Path(__file__).resolve().parents[1]
    setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with ScriptedModel([], responder=lambda _: "unexpected model invocation") as model:
        patch = composition(
            project,
            {
                "REPOPILOT_MODEL": "scripted-workspaces",
                "REPOPILOT_BASE_URL": model.url,
                "PILOT_CLIENT_URL": f"http://127.0.0.1:{port}",
            },
        settings_root=tmp_path,
        )
        for row in patch[-1]["insert"]:
            if "root" in row.get("config", {}):
                row["config"]["root"] = str(tmp_path)
        for row in patch:
            if row.get("id") == "working-directory":
                row["config"]["defaultDirectory"] = str(inbox)
            if row.get("id") == "sandbox-policy":
                row["config"]["workspaceRoot"] = str(inbox)
        (home / "patch.json").write_text(json.dumps(patch), encoding="utf-8")
        process = None
        client = httpx.Client(follow_redirects=True, timeout=30, trust_env=False)

        def stop():
            nonlocal process
            if process is None:
                return
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True
                )
            else:
                process.terminate()
            process.wait(timeout=20)
            process = None

        def launch():
            nonlocal process
            command = [
                dsh_executable(),
                "--profile",
                "web",
                "--patch",
                str(home / "patch.json"),
                "--no-open",
                "--port",
                str(port),
            ]
            if os.name == "nt":
                command = ["cmd.exe", "/c", *command]
            with (home / "host.log").open("ab") as log:
                process = subprocess.Popen(
                    command,
                    cwd=inbox,
                    stdout=log,
                    stderr=log,
                    env={**os.environ, "DSH_HOME": str(home), "REPOPILOT_API_KEY": "fixture-only"},
                )
            for _ in range(200):
                assert process.poll() is None, (home / "host.log").read_text(errors="replace")
                if (home / "entry.json").is_file():
                    try:
                        if (
                            client.get(
                                json.loads((home / "entry.json").read_text())["url"]
                            ).status_code
                            == 200
                        ):
                            return
                    except httpx.TransportError:
                        pass
                time.sleep(0.2)
            raise AssertionError("Native workspace Host startup timed out")

        def rpc(method, payload, channel="pilot-infra-api"):
            value = client.post(
                f"http://127.0.0.1:{port}/{channel}/{method}",
                json={
                    "type": "client-request",
                    "rpcId": str(uuid.uuid4()),
                    "method": method,
                    "payload": payload,
                },
            ).json()["result"]
            assert value["ok"], value
            return value["value"]

        def cli(*args):
            main(["--root", str(tmp_path), "--port", str(port), *args])
            return json.loads(capsys.readouterr().out)

        try:
            launch()
            args = {"request_id": str(uuid.uuid4()), "title": "环境验证工作区"}
            created = rpc("workspace-create", args)
            assert Path(created["workspace"]["path"]).is_dir()
            assert Path(created["workspace"]["path"]).parent == inbox
            assert rpc("workspace-create", args) == created
            assert cli("list")["workspaces"][0]["id"] == created["workspace"]["id"]
            second = cli("session", created["workspace"]["id"], "--request-id", str(uuid.uuid4()))
            assert second["sessionId"] != created["sessionId"]
            other = cli("create", "--name", "另一个工作区", "--request-id", str(uuid.uuid4()))
            assert other["workspace"]["path"] != created["workspace"]["path"]
            roots = rpc("session/list", {"args": {"_request": {}}}, "api")["items"]
            assert len(roots) == 3 and not any(s.get("running") for s in roots)
            stop()
            launch()
            assert rpc("workspace-create", args) == created
            rows = cli("list")["workspaces"]
            assert len(rows) == 2
            assert next(w for w in rows if w["id"] == created["workspace"]["id"])["sessions"] == 2
            assert len(model.requests) == 0
            assert len(list(inbox.iterdir())) == 2
            native = cli("create", "--name", "原生工作区")
            assert Path(native["workspace"]["path"]).name == "原生工作区"
            assert (inbox / "原生工作区").is_dir()
            with pytest.raises(ValueError, match="选择已有文件夹"):
                cli("create", "--name", "原生工作区")
            adopted = cli("open", "--path", native["workspace"]["path"])
            assert adopted["workspace"]["id"] == native["workspace"]["id"]
            first = rpc("new-session", {"session_id": native["sessionId"]})
            second = rpc("new-session", {"session_id": first["sessionId"]})
            assert first["sessionId"] != second["sessionId"]
            rows = cli("list")["workspaces"]
            assert len(rows) == 3
            assert next(w for w in rows if w["id"] == native["workspace"]["id"])["sessions"] == 4
            with pytest.raises(ValueError, match="工作区"):
                cli("create", "--name", "不能越界", "--parent", str(tmp_path))
            assert not (tmp_path / "不能越界").exists()
            assert len(model.requests) == 0
        finally:
            stop()
            client.close()


def test_cli_transport_error_never_exposes_owner_login(tmp_path, monkeypatch):
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    secret = "owner-private-secret"
    (home / "entry.json").write_text(
        json.dumps({"url": "http://127.0.0.1:18889/login?token=" + secret})
    )

    def unavailable(self, url):
        raise httpx.ConnectError("private url " + url)

    monkeypatch.setattr(httpx.Client, "get", unavailable)
    with pytest.raises(ValueError, match="unavailable") as error:
        main(["--root", str(tmp_path), "list"])
    assert secret not in str(error.value) and error.value.__suppress_context__
