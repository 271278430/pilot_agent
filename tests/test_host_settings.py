"""Actual installed Host registry, blank Sessions, CLI and cold restoration."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from pilot_core.config import credentials
from pilot_host.cli import composition, prepare_profile
from pilot_host.settings_cli import main as settings_main
from pilot_host.workspace_cli import main as workspace_main
from repopilot.harness import dsh_executable
from repopilot.mock_model import ScriptedModel
from tests.intent_fixture import with_intent_fixture
from tests.test_pilot_infra import setup


def test_native_settings_shared_credentials_paths_cli_and_restart(tmp_path, capsys, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    setup(tmp_path)
    home = tmp_path / ".local/harness-web"
    home.mkdir(parents=True)
    inbox = tmp_path / "runs/harness-inbox"
    inbox.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with ScriptedModel([], responder=with_intent_fixture(lambda _: "Pilot模型连接已恢复")) as model:
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
            prepare_profile(home, home / "patch.json")
            command = [
                dsh_executable(),
                "--profile",
                "web",
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
            if channel == "api" and "args" not in payload:
                payload = {"args": payload}
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
            workspace_main(["--root", str(tmp_path), "--port", str(port), *args])
            return json.loads(capsys.readouterr().out)

        try:
            launch()

            def setting(op, payload=None):
                return rpc(op, payload or {}, "pilot-host-settings-api")

            original = cli("create", "--name", "已有工作区")
            view = setting("view")
            assert view["applied"]["workspace_root"] == str(inbox)
            new_root = tmp_path / "workspaces"
            view = setting(
                "save",
                {
                    "section": "workspace",
                    "workspace_root": str(new_root),
                    "create_missing": True,
                    "expected_revision": 0,
                },
            )
            assert view["pending"] and view["applied"]["workspace_root"] == str(inbox)
            view = setting("sync", {"expected_revision": 1})
            assert not view["pending"]
            created = cli("create", "--name", "新主目录中的工作区")
            assert Path(created["workspace"]["path"]).parent == new_root
            assert (
                cli("open", "--path", original["workspace"]["path"])["workspace"]["id"]
                == original["workspace"]["id"]
            )
            config = {
                "model_id": "scripted-settings-new",
                "base_url": model.url,
                "context_window": 16384,
                "max_output_tokens": 4096,
                "auth_required": True,
                "api_key": "native-fixture-private",
            }
            view = setting("save", {"section": "model", "model": config, "expected_revision": 1})
            assert view["pending"] and view["live_model"]["model_id"] == "scripted-workspaces"
            assert "native-fixture-private" not in json.dumps(view)
            assert (
                "native-fixture-private" not in (tmp_path / ".local/host-settings.json").read_text()
            )
            result = setting(
                "test-model", {"model": {k: v for k, v in config.items() if k != "api_key"}}
            )
            assert result["passed"] and len(model.requests) == 1
            assert model.requests[0]["max_tokens"] == 64
            view = setting("sync", {"expected_revision": 2})
            assert not view["pending"] and view["live_model"]["model_id"] == config["model_id"]
            monkeypatch.setenv("PILOT_HOST_ROOT", str(tmp_path))
            shared = credentials()
            assert shared["REPOPILOT_API_KEY"] == config["api_key"]
            assert shared["REPOPILOT_MODEL"] == config["model_id"]
            settings_main(["--root", str(tmp_path), "--port", str(port), "view"])
            assert json.loads(capsys.readouterr().out)["revision"] == 2
            stop()
            # Cold composition restores the applied provider and root, without moving history.
            cold = composition(
                tmp_path, {"REPOPILOT_MODEL": "old", "REPOPILOT_BASE_URL": model.url}
            )
            native = next(r for r in cold if r.get("id") == "llm-pi-ai")["config"]["providers"][
                "apps"
            ]
            assert (
                native["models"][0]["id"] == config["model_id"]
                and native["models"][0]["contextWindow"] == 16384
            )
            for row in patch:
                if row.get("id") == "llm-pi-ai":
                    row["config"]["providers"]["apps"] = native
                elif row.get("id") == "agent-default-model":
                    row["config"]["model"] = config["model_id"]
                elif row.get("id") == "working-directory":
                    row["config"]["defaultDirectory"] = str(new_root)
                elif row.get("id") == "sandbox-policy":
                    row["config"]["workspaceRoot"] = str(new_root)
            (home / "patch.json").write_text(json.dumps(patch))
            launch()
            view = setting("view")
            assert (
                view["revision"] == 2
                and not view["pending"]
                and view["desired"]["model"]["key_configured"]
            )
            assert len(cli("list")["workspaces"]) == 2
            assert (
                cli("open", "--path", original["workspace"]["path"])["workspace"]["id"]
                == original["workspace"]["id"]
            )
            before_calls = len(model.requests)
            rpc(
                "session/prompt",
                {
                    "request": {
                        "sessionId": original["sessionId"],
                        "requestId": uuid.uuid4().hex,
                        "mode": "queue",
                        "content": [{"type": "text", "text": "你能部署Qwen吗"}],
                    }
                },
                "api",
            )
            for _ in range(160):
                items = rpc("session/list", {"args": {"_request": {}}}, "api")["items"]
                if len(model.requests) > before_calls and not any(s.get("running") for s in items):
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("Cold restored root did not complete its scripted turn")
            assert all(
                body["model"] == config["model_id"] for body in model.requests[before_calls:]
            )
            noauth = {**config, "auth_required": False, "api_key": ""}
            view = setting("save", {"section": "model", "model": noauth, "expected_revision": 2})
            view = setting("sync", {"expected_revision": 3})
            assert (
                not view["pending"] and credentials()["REPOPILOT_API_KEY"] == "pilot-local-no-auth"
            )
            assert len(model.requests) > before_calls
        finally:
            stop()
            client.close()
