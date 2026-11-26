"""Owner-controlled access configuration; never includes plaintext secrets in views."""

import base64
import ctypes
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from repopilot.storage import atomic_json

from .resource_contracts import (
    Authority,
    AuthorityUpdate,
    DockerAccess,
    SecretCreate,
    ServerSave,
    TrustHost,
)
from .store import digest


def private_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Private resource path escapes Pilot")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def dpapi(data, decrypt=False):
    """Windows user-bound key storage; Linux uses an owner-only key file."""

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("User-bound resource key unavailable")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class ResourceVault:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def cipher(self):
        path = private_path(self.root, ".local/resources/vault.key")
        # Serialize first creation across bridge processes. An exclusive file
        # alone could be observed before its creator finished writing the key.
        lock = private_path(self.root, ".local/resources/vault-lock.sqlite3")
        with sqlite3.connect(lock, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not path.exists():
                key = Fernet.generate_key()
                stored = dpapi(key) if os.name == "nt" else key
                with path.open("xb") as stream:
                    stream.write(stored)
                path.chmod(0o600)
            stored = path.read_bytes()
        lock.chmod(0o600)
        return Fernet(dpapi(stored, True) if os.name == "nt" else stored)

    def put(self, request):
        request = SecretCreate.model_validate(request)
        reference = "secret-" + uuid.uuid4().hex
        path = private_path(self.root, ".local/resources/secrets/" + reference + ".json")
        atomic_json(
            path,
            {
                "label": request.label,
                "kind": request.kind,
                "encrypted": self.cipher().encrypt(request.value.encode()).decode(),
            },
        )
        path.chmod(0o600)
        return {"credential_ref": reference, "label": request.label, "kind": request.kind}

    def get(self, reference, kind):
        if not reference or not re.fullmatch(r"secret-[a-f0-9]{32}", reference):
            raise ValueError("A registered private credential is required")
        path = private_path(self.root, ".local/resources/secrets/" + reference + ".json")
        if not path.is_file():
            raise ValueError("Credential reference is unavailable")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["kind"] != kind:
            raise ValueError("Credential type differs from the connection")
        return self.cipher().decrypt(value["encrypted"].encode()).decode()

    def catalog(self):
        base = self.root / ".local/resources/secrets"
        if not base.exists():
            return []
        result = []
        for path in sorted(base.glob("secret-*.json")):
            if path.is_symlink():
                raise ValueError("Credential symlink denied")
            value = json.loads(path.read_text(encoding="utf-8"))
            result.append(
                {"credential_ref": path.stem, "label": value["label"], "kind": value["kind"]}
            )
        return result


class ResourceRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = private_path(root, ".local/resources/registry.sqlite3")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS servers(id TEXT PRIMARY KEY, revision INTEGER, data TEXT);
                CREATE TABLE IF NOT EXISTS configuration_requests(id TEXT PRIMARY KEY, payload TEXT);
            """)
            self.path.chmod(0o600)
            if not db.execute("SELECT 1 FROM servers LIMIT 1").fetchone():
                source = self.root / ".local/infra-assets.json"
                legacy = json.loads(source.read_text(encoding="utf-8")) if source.is_file() else {}
                for machine in legacy.get("machines", [])[:1]:
                    devices = machine.get("allowed_gpu_ids", [])
                    item = {
                        "id": machine["id"],
                        "label": machine.get("label", machine["id"]),
                        "connection": {
                            "transport": "local",
                            "origin": "pilot_host",
                            "jumps": [],
                            "target": None,
                            "prerequisite": "",
                        },
                        "enabled": True,
                        "baseline_gpu_ids": devices,
                        "policy": Authority(mode="review", approvable_gpu_ids=devices).model_dump(),
                        "policy_revision": 1,
                        "last_connection_test": None,
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO servers VALUES(?,?,?)",
                        (item["id"], 1, json.dumps(item)),
                    )

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    @staticmethod
    def decode(row):
        value = {**json.loads(row["data"]), "revision": row["revision"]}
        value.setdefault("docker", DockerAccess().model_dump())
        return value

    def catalog(self):
        with self.db() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM servers ORDER BY id")]

    def get(self, server_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            if not row:
                raise ValueError("Server is not registered")
            return self.decode(row)

    def node_check_result(self, server_id, expected_revision, result):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            if not row or row["revision"] != expected_revision:
                raise ValueError("服务器配置在核验期间变化，请重新读取")
            if result.get("node_identity"):
                for other in db.execute("SELECT * FROM servers WHERE id!=?", (server_id,)):
                    peer = json.loads(other["data"])
                    check = peer.get("last_node_check") or {}
                    if (
                        peer["enabled"]
                        and check.get("ready")
                        and check.get("server_revision") == other["revision"]
                        and check.get("node_identity") == result["node_identity"]
                    ):
                        result["ready"] = False
                        result["reasons"].append(
                            "节点身份与已登记服务器" + other["id"] + "重复，请合并登记后再分配配额"
                        )
            value = json.loads(row["data"])
            value["last_node_check"] = result
            db.execute("UPDATE servers SET data=? WHERE id=?", (json.dumps(value), server_id))

    def save(self, args):
        request = ServerSave.model_validate(args)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = digest(request.model_dump())
            previous = db.execute(
                "SELECT payload FROM configuration_requests WHERE id=?", (request.request_id,)
            ).fetchone()
            if previous:
                if previous[0] != payload:
                    raise ValueError("Server request ID conflict")
                return self.decode(
                    db.execute("SELECT * FROM servers WHERE id=?", (request.server_id,)).fetchone()
                )
            row = db.execute("SELECT * FROM servers WHERE id=?", (request.server_id,)).fetchone()
            if request.expected_revision != (row["revision"] if row else 0):
                raise ValueError("Server revision conflict; reload before saving")
            old = self.decode(row) if row else None
            if request.connection.transport == "local":
                for existing in db.execute(
                    "SELECT * FROM servers WHERE id!=?", (request.server_id,)
                ):
                    if json.loads(existing["data"])["connection"]["transport"] == "local":
                        raise ValueError(
                            "The Pilot Host already has a stable local server identity"
                        )
            access = (
                request.docker.model_dump()
                if request.docker
                else old["docker"]
                if old
                else DockerAccess().model_dump()
            )
            if access["credential_ref"]:
                available = ResourceVault(self.root).catalog()
                if not any(
                    v["credential_ref"] == access["credential_ref"] and v["kind"] == "password"
                    for v in available
                ):
                    raise ValueError("Docker sudo密码凭据不存在或类型错误")
            connection_changed = old and old["connection"] != request.connection.model_dump()
            changed = old and (connection_changed or old["docker"] != access)
            # Existing local identity cannot be redirected to another machine.
            if (
                old
                and old["connection"]["transport"] == "local"
                and request.connection.transport != "local"
            ):
                raise ValueError("Keep the local server identity; add a new remote resource")
            item = {
                "id": request.server_id,
                "label": request.label,
                "connection": request.connection.model_dump(),
                "docker": access,
                "enabled": request.enabled,
                "baseline_gpu_ids": old["baseline_gpu_ids"] if old else [],
                "policy": old["policy"] if old else Authority().model_dump(),
                "policy_revision": (old["policy_revision"] + int(bool(changed))) if old else 1,
                "last_connection_test": None
                if connection_changed
                else old.get("last_connection_test")
                if old
                else None,
            }
            revision = request.expected_revision + 1
            db.execute(
                "INSERT OR REPLACE INTO servers VALUES(?,?,?)",
                (request.server_id, revision, json.dumps(item)),
            )
            db.execute(
                "INSERT INTO configuration_requests VALUES(?,?)", (request.request_id, payload)
            )
        return self.get(request.server_id)

    def policy(self, args):
        request = AuthorityUpdate.model_validate(args)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM servers WHERE id=?", (request.server_id,)).fetchone()
            if not row or row["revision"] != request.expected_revision:
                raise ValueError("Server revision conflict; reload before changing authority")
            item = self.decode(row)
            item["policy"] = request.policy.model_dump()
            item["policy_revision"] += 1
            item["baseline_gpu_ids"] = sorted(
                set(item["baseline_gpu_ids"]) & set(request.policy.approvable_gpu_ids)
            )
            item.pop("revision")
            db.execute(
                "UPDATE servers SET revision=revision+1,data=? WHERE id=?",
                (json.dumps(item), request.server_id),
            )
        return self.get(request.server_id)

    def connection_result(self, server_id, revision, value):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            if not row or row["revision"] != revision:
                raise ValueError("Connection configuration changed during testing")
            item = json.loads(row["data"])
            item["last_connection_test"] = value
            db.execute("UPDATE servers SET data=? WHERE id=?", (json.dumps(item), server_id))
        return value

    def trust(self, args):
        request = TrustHost.model_validate(args)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM servers WHERE id=?", (request.server_id,)).fetchone()
            if not row or row["revision"] != request.expected_revision:
                raise ValueError("Server revision conflict; repeat the connection check")
            item = json.loads(row["data"])
            result = item.get("last_connection_test") or {}
            if (
                result.get("stage") != "host_identity"
                or result.get("hop") != request.hop
                or result.get("fingerprint") != request.fingerprint
            ):
                raise ValueError(
                    "Confirm only the exact fingerprint returned by the current handshake"
                )
            endpoints = [*item["connection"]["jumps"], item["connection"]["target"]]
            endpoints[request.hop]["fingerprint"] = request.fingerprint
            item["last_connection_test"] = None
            item["policy_revision"] += 1
            db.execute(
                "UPDATE servers SET revision=revision+1,data=? WHERE id=?",
                (json.dumps(item), request.server_id),
            )
        return self.get(request.server_id)

    def asset_machines(self, legacy):
        from .fleet_transport import worker_current
        from .node_authority import allowed_operations

        result = {m["id"]: dict(m) for m in legacy}
        for server in self.catalog():
            policy = server["policy"]
            item = result.get(server["id"], {"id": server["id"], "observed_gpu_ids": []})
            item.update(
                label=server["label"],
                enabled=server["enabled"],
                allowed_gpu_ids=server["baseline_gpu_ids"]
                if policy["mode"] == "review" and server["enabled"]
                else [],
                approvable_gpu_ids=policy["approvable_gpu_ids"],
                policy_revision=server["policy_revision"],
                executor_available=server["connection"]["transport"] == "local",
                transport=server["connection"]["transport"],
                permission_mode=policy["mode"],
                permitted_operations=sorted(allowed_operations(policy)),
                worker_version_current=server["connection"]["transport"] == "local",
            )
            if not item["executor_available"]:
                with self.db() as db:
                    exists = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_executors'"
                    ).fetchone()
                    executor = (
                        db.execute(
                            "SELECT data FROM fleet_executors WHERE server_id=?", (server["id"],)
                        ).fetchone()
                        if exists
                        else None
                    )
                if executor:
                    cfg = json.loads(executor[0])
                    item["executor_available"] = (
                        cfg["enabled"] and cfg["server_revision"] == server["revision"]
                    )
                    item["worker_version_current"] = worker_current(cfg)
            result[server["id"]] = item
        return list(result.values())


def fingerprint(key):
    import hashlib

    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
