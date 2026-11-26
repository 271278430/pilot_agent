import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from pilot_core.docker import docker
from pilot_delivery.engine_client import call_engine
from repopilot.environment import Environment
from repopilot.storage import atomic_json

from .acceptance import read_receipts, validate_inference
from .contracts import AppFile
from .fixtures import CLIENT, COPY_GUIDE, GUIDE, TEST
from .store import source_hash
from .ui_contract import INTERACTION_GUIDE, check_preview_controls
from .workspace import checked_path, source_files


class AppBackend:
    def __init__(self, store, id):
        self.store, self.id = store, id
        self.directory = store.directory(id)

    @property
    def workspace(self):
        return self.store.path(self.id, "workspace")

    def source_path(self, relative):
        return self.store.path(self.id, "workspace/" + relative)

    def candidate_path(self, candidate):
        if candidate.get("id") not in {"c1", "c2", "c3"}:
            raise ValueError("Unregistered application candidate")
        return self.store.path(self.id, "candidates/" + candidate["id"])

    def status(self):
        row = self.store.get(self.id)
        return {
            "plan": row.plan.model_dump(),
            "status": row.status,
            "state": row.state,
            "plan_version": row.state.get("plan_version", 1),
            "actions": self.store.actions(row),
            "development_contract": GUIDE.replace(
                "Only app.py and index.html are editable.",
                "Entrypoints are app.py and index.html. Use tree/read/write to edit admitted modules/assets and the repository working copy in the actual delivery cwd.",
            )
            + "\n"
            + COPY_GUIDE
            + "\n"
            + INTERACTION_GUIDE,
        }

    def connection(self):
        return json.loads(self.store.path(self.id, "model-connection.json").read_text())

    def initialize_workspace(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.source_path("tests").mkdir(exist_ok=True)
        for name, content in {
            "model_client.py": CLIENT,
            "tests/test_contract.py": TEST,
            "README.md": GUIDE,
        }.items():
            target = self.source_path(name)
            if target.is_symlink() or (target.exists() and target.read_text() != content):
                raise ValueError("Protected workspace file changed; inspect before retry")
            if not target.exists():
                target.write_text(content, encoding="utf-8")
        row = self.store.get(self.id)
        if row.plan.project_id and not row.state.get("project_working_copy"):
            from pilot_projects.store import ProjectStore

            project = ProjectStore(self.store.root).get(row.plan.project_id)
            if project.status != "ready":
                raise ValueError("Managed project is not ready")
            origin = Path(project.path)
            destination = self.source_path("repository")
            if destination.exists():
                raise ValueError("Working-copy import has unknown outcome; inspect before retry")
            files = source_files(origin, evidence=True)
            destination.mkdir()
            # Copy data only. Repository README/tests retain their own names;
            # root model client/acceptance fixtures are never overwritten.
            for relative in files:
                source = checked_path(origin, relative)
                target = checked_path(destination, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            self.store.update(
                self.id,
                project_working_copy={
                    "project_id": project.id,
                    "path": "repository",
                    "files": len(files),
                },
            )

    def prepare_model(self):
        plan = self.store.get(self.id).plan
        if (self.directory / "model-connection.json").exists():
            info = call_engine(self.connection(), "ping", timeout=3)
            if info["model_id"] != plan.model_id:
                raise ValueError("Registered model does not match approved source")
            self.store.update(
                self.id,
                model={
                    "model_id": info["model_id"],
                    "identity": info["model_identity"],
                    "protocol": info["protocol"],
                },
            )
            self.initialize_workspace()
            return {
                "reused": True,
                "model_id": info["model_id"],
                "model_identity": info["model_identity"],
            }
        if plan.model_source == "existing_teacher8b":
            folder = self.store.root / ".local/model-preview"
            config = json.loads((folder / "config.json").read_text())
        else:
            from .model_prepare import download

            legacy_model = self.directory / "model/assets"
            location = self.directory / "model/resource-location.json"
            if location.is_file():
                model_dir = Path(json.loads(location.read_text())["path"])
            elif legacy_model.exists():
                model_dir = legacy_model
            else:
                from pilot_core.resource_paths import ensure_resource_root

                model_dir = ensure_resource_root(self.store.root) / "models/applications" / self.id
                location.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(
                    location,
                    {"path": str(model_dir), "model_id": plan.model_id, "revision": plan.revision},
                )
            if plan.model_source == "configured_api":
                from pilot_core.config import credentials

                if credentials()["REPOPILOT_MODEL"] != plan.model_id:
                    raise ValueError("API配置与批准的模型不匹配")
            else:
                report = download(plan, model_dir)
                self.store.event(self.id, "infra", "model_download", report)
            folder = self.directory / "model/worker"
            folder.mkdir(parents=True, exist_ok=True)
            if (folder / "launch.json").exists():
                config = json.loads((folder / "config.json").read_text())
                try:
                    info = call_engine(config, "ping", timeout=3)
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "Previous model launch has no confirmed result; inspect its PID/log before retry"
                    ) from exc
                atomic_json(self.directory / "model-connection.json", config)
                os.chmod(self.directory / "model-connection.json", 0o600)
                self.store.update(
                    self.id,
                    model={
                        "model_id": info["model_id"],
                        "identity": info["model_identity"],
                        "protocol": info["protocol"],
                    },
                )
                self.initialize_workspace()
                return {
                    "reused": True,
                    "model_id": info["model_id"],
                    "model_identity": info["model_identity"],
                }
            config = {
                "project_root": str(self.store.root),
                "worker_dir": str(folder),
                "model": str(model_dir),
                "tokenizer": str(model_dir),
                "adapter": None,
                "model_id": plan.model_id,
                "socket": str(folder / "socket.sock"),
                "token": secrets.token_urlsafe(32),
                "runtime": "api" if plan.model_source == "configured_api" else "cpu",
            }
            atomic_json(folder / "config.json", config)
            os.chmod(folder / "config.json", 0o600)
            python = (
                os.sys.executable
                if plan.model_source == "configured_api"
                else "/data/pilot-owner/infrapilot-ml-env-20261117/.venv/bin/python"
            )
            with (folder / "worker.log").open("ab") as log:
                process = subprocess.Popen(
                    [
                        python,
                        "-m",
                        "pilot_projects.api_engine"
                        if plan.model_source == "configured_api"
                        else "pilot_delivery.preview_engine",
                        str(folder / "config.json"),
                    ],
                    cwd=self.store.root,
                    env={
                        **os.environ,
                        "CUDA_VISIBLE_DEVICES": "",
                        "PYTHONPATH": str(self.store.root / "src"),
                    },
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            atomic_json(folder / "launch.json", {"pid": process.pid})
            for _ in range(180):
                if process.poll() is not None:
                    raise ValueError("Model worker exited; inspect its task-local log")
                if (folder / "ready.json").exists():
                    break
                time.sleep(1)
            else:
                raise ValueError("Model startup not confirmed; inspect before repeating")
        info = call_engine(config, "ping", timeout=3)
        atomic_json(self.directory / "model-connection.json", config)
        os.chmod(self.directory / "model-connection.json", 0o600)
        self.store.update(
            self.id,
            model={
                "model_id": info["model_id"],
                "identity": info["model_identity"],
                "protocol": info["protocol"],
            },
        )
        self.initialize_workspace()
        return {
            "model_id": info["model_id"],
            "model_identity": info["model_identity"],
            "runtime": info.get("runtime", "cuda"),
        }

    def read(self, path):
        target = checked_path(self.workspace, path)
        if target.is_symlink():
            raise ValueError("Symlink is not allowed")
        if target.exists() and target.stat().st_size > 512000:
            raise ValueError("Source read exceeds byte budget")
        return {
            "path": path,
            "content": target.read_text(encoding="utf-8")[:25000]
            if target.exists()
            else "(not yet created)",
        }

    def write(self, path, content):
        request = AppFile(path=path, content=content)
        target = checked_path(self.workspace, request.path, write=True)
        if target.is_symlink():
            raise ValueError("Source symlink is not allowed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(request.content, encoding="utf-8")
        self.store.update(self.id, source_test=None, verification=None)
        return {"path": path, "bytes": target.stat().st_size}

    def tree(self):
        return {
            "cwd": str(self.workspace),
            "files": source_files(self.workspace),
            "protected": ["model_client.py", "README.md", "tests/**", "**/tests/**"],
        }

    def source_test(self):
        source_hash(self.workspace)
        check_preview_controls(self.source_path("index.html").read_text())
        if (
            self.source_path("model_client.py").read_text() != CLIENT
            or self.source_path("tests/test_contract.py").read_text() != TEST
        ):
            raise ValueError("Protected test/client changed")
        result = Environment(self.workspace, "docker", "pilot-python:0.5.0").execute(
            ["python", "-m", "pytest", "tests/test_contract.py", "-q", "-p", "no:cacheprovider"], 60
        )
        result.update(passed=result["exit_code"] == 0, source_sha256=source_hash(self.workspace))
        self.store.update(self.id, source_test=result)
        return result

    def candidate(self):
        state = self.store.get(self.id).state
        tested = state.get("source_test") or {}
        if len(state.get("candidates", [])) >= 3:
            raise ValueError("Candidate budget exhausted; revise goal instead of unlimited retries")
        digest = source_hash(self.workspace)
        if not tested.get("passed") or tested.get("source_sha256") != digest:
            raise ValueError("Current application must pass protected source tests")
        name = "c" + str(len(state.get("candidates", [])) + 1)
        stage = self.candidate_path({"id": name})
        stage.mkdir(parents=True)
        for filename in source_files(self.workspace):
            target = checked_path(stage, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked_path(self.workspace, filename), target)
        candidate = {
            "id": name,
            "source_sha256": source_hash(stage),
            "model_identity": state["model"]["identity"],
        }
        self.store.update(
            self.id,
            candidate=candidate,
            candidates=[*state.get("candidates", []), candidate],
            verification=None,
        )
        return candidate

    def container_name(self):
        return "pilot-app-" + self.id[:24]

    def close(self, *, stop_model=False):
        name = self.container_name()
        check = docker(["inspect", name], check=False)
        if check.returncode == 0:
            row = json.loads(check.stdout)[0]
            if row["Config"]["Labels"].get("pilot.application") != self.id:
                raise ValueError("Container ownership differs")
            docker(["rm", "-f", name])
        if stop_model and self.store.get(self.id).plan.model_source == "configured_api":
            from pilot_projects.api_engine import stop_worker

            stop_worker(self.directory / "model/worker")

    def deploy(self):
        state = self.store.get(self.id).state
        candidate = state.get("candidate")
        if not candidate:
            raise ValueError("Source-tested candidate required")
        stage = self.candidate_path(candidate)
        if source_hash(stage) != candidate["source_sha256"]:
            raise ValueError("Candidate files changed")
        config = self.connection()
        info = call_engine(config, "ping", timeout=3)
        if info["model_identity"] != candidate["model_identity"]:
            raise ValueError("Model assets changed")
        self.close()
        docker(
            [
                "run",
                "-d",
                "--name",
                self.container_name(),
                "--label",
                "pilot.application=" + self.id,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65534:65534",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,nosuid,size=128m",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "MODEL_SOCKET=/model.sock",
                "-e",
                "MODEL_TOKEN=" + config["token"],
                "--mount",
                f"type=bind,source={stage},target=/workspace,readonly",
                "--mount",
                f"type=bind,source={config['socket']},target=/model.sock,readonly",
                "-w",
                "/workspace",
                "pilot-python:0.5.0",
                "python",
                "app.py",
                "--port",
                "18080",
            ]
        )
        for _ in range(30):
            result = self.http("/health")
            if result.get("status") == 200 and result.get("body", {}).get("ok"):
                break
            time.sleep(0.2)
        else:
            raise ValueError("Application health check did not pass")
        self.store.update(self.id, deployment={"candidate": candidate["id"], "healthy": True})
        return {"healthy": True, "candidate": candidate["id"]}

    def http(self, path, message=None, *, payload=None):
        if path not in {"/", "/health", "/chat"}:
            raise ValueError("Unregistered app endpoint")
        # Keep trusted HTTP runner independent of the generated app code.
        source = "import json,urllib.request\nreq=urllib.request.Request(" + repr(
            "http://127.0.0.1:18080" + path
        )
        if message is not None:
            source += (
                ",data="
                + repr(
                    json.dumps(payload if payload is not None else {"message": message}).encode()
                )
                + ",headers={'Content-Type':'application/json'}"
            )
        source += (
            ')\ntry:\n r=urllib.request.urlopen(req,timeout=120);data=r.read(65536).decode();print(json.dumps({"status":r.status,"body":'
            + ("data" if path == "/" else "json.loads(data)")
            + '}))\nexcept Exception as e:print(json.dumps({"status":500,"body":{"error":str(e)}}))'
        )
        result = docker(
            ["exec", self.container_name(), "python", "-c", source], timeout=130, check=False
        )
        if result.returncode:
            return {"status": 500, "body": {"error": "Application process is unavailable"}}
        return json.loads(result.stdout)

    def ensure_current_acceptance(self, *, completing=False):
        row = self.store.get(self.id)
        state = row.state
        candidate = state.get("candidate") or {}
        verification = state.get("verification") or {}
        deployment = state.get("deployment") or {}
        model = state.get("model") or {}
        if (
            row.status != ("running" if completing else "completed")
            or not verification.get("passed")
            or verification.get("candidate") != candidate.get("id")
            or deployment.get("candidate") != candidate.get("id")
            or not deployment.get("healthy")
            or candidate.get("model_identity") != model.get("identity")
            or source_hash(self.candidate_path(candidate)) != candidate.get("source_sha256")
        ):
            raise ValueError("Application acceptance is missing or outdated")
        info = call_engine(self.connection(), "ping", timeout=3)
        if info.get("model_identity") != model.get("identity") or info.get("model_id") != model.get(
            "model_id"
        ):
            raise ValueError("Accepted model identity changed")
        return candidate, model, self.check_container(candidate)

    def check_container(self, candidate):
        result = docker(["inspect", self.container_name()], check=False)
        if result.returncode:
            raise ValueError("Accepted application container unavailable")
        value = json.loads(result.stdout)[0]
        mounts = value.get("Mounts", [])
        source = self.candidate_path(candidate)
        workspace = next((m for m in mounts if m.get("Destination") == "/workspace"), {})
        if (
            not value.get("State", {}).get("Running")
            or (value.get("Config", {}).get("Labels") or {}).get("pilot.application") != self.id
            or workspace.get("Source") != str(source)
            or workspace.get("RW") is not False
        ):
            raise ValueError("Running container does not match accepted application version")
        return value["Id"]

    def preview(self):
        binding = self.ensure_current_acceptance()
        result = self.http("/")
        if result.get("status") != 200 or not isinstance(result.get("body"), str):
            raise ValueError("Application preview unavailable")
        if self.ensure_current_acceptance() != binding:
            raise ValueError("Application changed during preview")
        candidate = binding[0]
        return {
            "html": result["body"],
            "binding": {
                "candidate": candidate["id"],
                "source_sha256": candidate["source_sha256"],
                "model_identity": candidate["model_identity"],
            },
        }

    def chat(self, message, binding=None, *, profile=None, history=None):
        candidate, model, container = self.ensure_current_acceptance()
        if binding is not None and binding != {
            "candidate": candidate["id"],
            "source_sha256": candidate["source_sha256"],
            "model_identity": candidate["model_identity"],
        }:
            raise ValueError("Preview version is outdated; reopen the application")
        config = self.connection()
        previous = {r.get("receipt_id") for r in read_receipts(self.store.root, config)}
        extras = {
            k: v for k, v in {"profile": profile, "history": history}.items() if v is not None
        }
        response = (
            self.http("/chat", message, payload={"message": message, **extras})
            if extras
            else self.http("/chat", message)
        )
        if response.get("status") != 200:
            raise ValueError("Application response unavailable")
        value = validate_inference(
            response.get("body"),
            read_receipts(self.store.root, config),
            message=message,
            model=model,
            previous_ids=previous,
        )
        if self.ensure_current_acceptance() != (candidate, model, container):
            raise ValueError("Application changed during inference")
        return {k: getattr(value, k) for k in ("answer", "model_id", "finish_reason", "receipt_id")}

    def verify(self):
        state = self.store.get(self.id).state
        candidate = state.get("candidate") or {}
        if not candidate or (state.get("deployment") or {}).get("candidate") != candidate["id"]:
            raise ValueError("Deploy current candidate before acceptance")
        if source_hash(self.candidate_path(candidate)) != candidate["source_sha256"]:
            raise ValueError("Candidate changed after deployment")
        config = self.connection()
        folder = Path(config.get("worker_dir", str(self.store.root / ".local/model-preview")))
        cases = []
        for example in self.store.get(self.id).plan.acceptance_examples:
            nonce = secrets.token_hex(8)
            input = example + " [verification:" + nonce + "]"
            response = self.http("/chat", input)
            body = response.get("body") or {}
            receipts = [
                json.loads(line)
                for line in (folder / "inference-receipts.jsonl").read_text().splitlines()[-200:]
            ]
            receipt = next((r for r in receipts if r["receipt_id"] == body.get("receipt_id")), None)
            passed = (
                response["status"] == 200
                and isinstance(body.get("answer"), str)
                and bool(body["answer"].strip())
                and receipt
                and body.get("model_identity") == state["model"]["identity"]
                and receipt["model_identity"] == body["model_identity"]
                and nonce in body.get("model_input", "")
                and receipt["input_sha256"]
                == hashlib.sha256(body.get("model_input", "").encode()).hexdigest()
                and receipt["answer_sha256"]
                == hashlib.sha256(body.get("model_answer", "").encode()).hexdigest()
            )
            cases.append(
                {
                    "input": example,
                    "passed": bool(passed),
                    "status": response["status"],
                    "genuine_model_receipt": bool(receipt),
                }
            )
        result = {
            "passed": all(c["passed"] for c in cases),
            "cases": cases,
            "candidate": state["candidate"]["id"],
        }
        self.store.update(self.id, verification=result)
        atomic_json(self.directory / "acceptance.json", result)
        return result
