from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path

import paramiko
from dotenv import dotenv_values

from .spec import InfraSpec


class Remote:
    def __init__(self, spec: InfraSpec, ssh_env: Path, known_hosts: Path):
        self.spec = spec
        self._password = dotenv_values(ssh_env).get(spec.password_env) or os.getenv(spec.password_env)
        self._known_hosts = known_hosts
        self.client, self.sftp = None, None
        if not self._password:
            raise ValueError(f"Missing local credential variable {spec.password_env}")
        self._connect()

    def _connect(self):
        self.close()
        self.client = paramiko.SSHClient()
        self.client.load_host_keys(str(self._known_hosts))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self.client.connect(self.spec.host, port=self.spec.port, username=self.spec.username, password=self._password, timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
            self.sftp = self.client.open_sftp()
        except Exception:
            self.close()
            raise

    def _ensure_connection(self):
        transport = self.client.get_transport() if self.client else None
        if transport is None or not transport.is_active() or not transport.is_authenticated():
            self._connect()

    def close(self):
        for handle in (self.sftp, self.client):
            if handle is not None:
                try:
                    handle.close()
                except (OSError, paramiko.SSHException):
                    pass

    def command(self, argv: list[str], timeout: int = 30) -> dict:
        self._ensure_connection()
        try:
            _, output, error = self.client.exec_command(shlex.join(argv), timeout=timeout)
            # Commands return small responses; long jobs redirect logs.
            stdout, stderr = output.read().decode(), error.read().decode()
            code = output.channel.recv_exit_status()
        except (EOFError, OSError, paramiko.SSHException):
            # Outcome may be unknown after dispatch. Close the broken channel,
            # never resend argv. The journal decides the next inspect action.
            self.close()
            raise
        if code:
            raise RuntimeError(f"remote_exit_{code}: {stderr[-1500:]} {stdout[-1500:]}")
        return json.loads(stdout)

    def initialize(self):
        script = """import pathlib,json,sys
base=pathlib.Path(sys.argv[1]); target=base/sys.argv[2]
base.mkdir(parents=True,exist_ok=True)
if base.is_symlink() or base.resolve()!=base: raise ValueError('authorized root resolves elsewhere')
target.mkdir(exist_ok=True)
if target.is_symlink() or target.resolve().parent!=base.resolve(): raise ValueError('workspace escapes root')
for name in ['jobs','logs','reports','models','checkpoints']: (target/name).mkdir(exist_ok=True)
print(json.dumps({'remote':str(target)}))"""
        self.command(["python3", "-c", script, self.spec.root, self.spec.project])
        worker = Path(__file__).with_name("worker.py")
        self.sftp.put(str(worker), self.spec.remote + "/worker.py.tmp")
        self.sftp.posix_rename(self.spec.remote + "/worker.py.tmp", self.spec.remote + "/worker.py")
        self.write("spec.json", self.spec.model_dump())

    def write(self, relative: str, value: dict):
        self._ensure_connection()
        with self.sftp.open(self.spec.remote + "/" + relative + ".tmp", "w") as stream:
            stream.write(json.dumps(value))
        self.sftp.posix_rename(self.spec.remote + "/" + relative + ".tmp", self.spec.remote + "/" + relative)

    def worker(self, *args: str):
        return self.command(["python3", self.spec.remote + "/worker.py", self.spec.remote, *args], timeout=45)

    def read(self, relative: str):
        self._ensure_connection()
        with self.sftp.open(self.spec.remote + "/" + relative) as stream:
            return json.loads(stream.read())

    def fetch(self, relative: str, local: Path):
        self._ensure_connection()
        local.parent.mkdir(parents=True, exist_ok=True)
        self.sftp.get(self.spec.remote + "/" + relative, str(local))

    def wait(self, job: str, seconds: int = 20):
        end = time.monotonic() + seconds
        while True:
            state = self.worker("inspect", job)
            if state["status"] != "running" or time.monotonic() >= end:
                return state
            time.sleep(2)
