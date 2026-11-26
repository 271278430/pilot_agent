"""Fixed node protocol over the existing strict SSH access chain."""

import base64
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from .node_docker import PRIVATE_ENV, REMOTE_WRAPPER, access, envelope
from .resource_transport import ResourceTransport


def worker_current(executor):
    """Compare the registered worker identity without modifying the node."""
    probe = executor.get("probe")
    if not isinstance(probe, dict):
        return False
    pinned = probe.get("worker_sha256")
    current = hashlib.sha256(Path(__file__).with_name("fleet_worker.py").read_bytes()).hexdigest()
    return isinstance(pinned, str) and pinned == current


class FleetTransport:
    def __init__(self, root):
        self.root = Path(root)
        self.transport = ResourceTransport(root)

    def exchange(self, server, command, payload=None, timeout=60):
        if server["connection"]["transport"] == "local":
            from dotenv import dotenv_values

            secret_file = self.root / ".local/docker.env"
            mode, password = access(self.root, server)
            password = password or (
                dotenv_values(secret_file).get("PILOT_DOCKER_SUDO_PASSWORD")
                if secret_file.is_file()
                else None
            )
            env = {k: v for k, v in os.environ.items() if k not in PRIVATE_ENV}
            if password:
                env["PILOT_NODE_DOCKER_SUDO_PASSWORD"] = password
            elif mode == "sudo_nopasswd":
                env["PILOT_NODE_DOCKER_USE_SUDO"] = "1"
            result = subprocess.run(
                command,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode or len(result.stdout) > 4 * 1024**2:
                raise ValueError("Local node protocol failed")
            return json.loads(result.stdout)
        with self.transport.connect(server) as transport:
            channel = transport.open_session(timeout=10)
            try:
                channel.exec_command(shlex.join(["python3", "-B", "-c", REMOTE_WRAPPER]))
                channel.sendall(json.dumps(envelope(self.root, server, command, payload, timeout)).encode())
                channel.shutdown_write()
                output = bytearray()
                error = bytearray()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if channel.recv_ready():
                        output.extend(channel.recv(65536))
                    if channel.recv_stderr_ready():
                        error.extend(channel.recv_stderr(16384))
                    if len(output) > 4 * 1024**2 or len(error) > 65536:
                        raise ValueError("Node response exceeds budget")
                    if (
                        (channel.eof_received or channel.closed)
                        and channel.exit_status_ready()
                        and not channel.recv_ready()
                        and not channel.recv_stderr_ready()
                    ):
                        break
                    time.sleep(0.02)
                else:
                    raise TimeoutError("Node response timed out; reconcile original operation")
                if channel.recv_exit_status() != 0:
                    detail = bytes(error).decode("utf-8", errors="replace")[-1500:]
                    raise ValueError("Node protocol process failed; inspect original operation. " + detail)
                return json.loads(output)
            finally:
                channel.close()

    def bootstrap(self, server, executor):
        source = Path(__file__).with_name("fleet_worker.py").read_bytes()
        from .node_storage import source as storage_source

        code = """import base64,hashlib,json,os,sys
from pathlib import Path
v=json.load(sys.stdin);p=Path(v['root'])
scope={};exec(v['storage_source'],scope)
check=scope['inspect_node'](v)
assert check['ready'],'Node storage/capabilities changed: '+ '; '.join(check['reasons'])
assert check['node_identity']==v['node_identity'],'Node hardware identity changed'
assert {k:check['paths'][k]['device'] for k in ['root','asset_root']}==v['storage_devices'],'Node data mount changed'
assert p.is_absolute() and p.name.startswith('pilot-') and len(p.parts)>=3 and '..' not in p.parts
for x in [p,*p.parents]:assert not x.is_symlink()
assert hashlib.sha256(Path('/etc/machine-id').read_text().strip().encode()).hexdigest()==v['machine_identity']
marker=p/'pilot-executor.json'
if p.exists():
 assert marker.is_file() and json.loads(marker.read_text())['owner_namespace']==v['owner_namespace']
else:p.mkdir(parents=True,mode=0o700)
a=Path(v.get('asset_root',str(p/'assets')))
assert a.is_absolute() and '..' not in a.parts and len(a.parts)>=3 and a!=p and not p.is_relative_to(a)
for x in [a,*a.parents]:assert not x.is_symlink()
am=a/'.pilot-node-assets.json'
if a.exists():
 if am.exists():assert not am.is_symlink() and json.loads(am.read_text())['owner_namespace']==v['owner_namespace']
 else:
  hm=a/'.pilot-resources.json'
  local_owned=hm.is_file() and not hm.is_symlink() and json.loads(hm.read_text()).get('owner_namespace')==v.get('resource_owner_namespace')
  assert a==p/'assets' or local_owned or not any(a.iterdir()),'Use an empty private asset root'
else:a.mkdir(parents=True,mode=0o700)
if not am.exists():
 with am.open('x') as f:json.dump({'owner_namespace':v['owner_namespace']},f)
 os.chmod(am,0o600)
data=base64.b64decode(v['source']);assert hashlib.sha256(data).hexdigest()==v['source_sha256']
# Never replace an executor while it owns a live operation or service.
if (p/'worker.py').exists() and hashlib.sha256((p/'worker.py').read_bytes()).hexdigest()!=v['source_sha256']:
 for f in (p/'operations').glob('*.json') if (p/'operations').exists() else []:assert json.loads(f.read_text())['status'] not in {'submitted','running','unknown'}
 for f in (p/'services').glob('*.json') if (p/'services').exists() else []:assert json.loads(f.read_text()).get('stopped')
temp=p/'worker.py.new';temp.write_bytes(data);os.chmod(temp,0o600);temp.replace(p/'worker.py')
marker.write_text(json.dumps({**{k:v[k] for k in ['owner_namespace','machine_identity','source_sha256','storage_devices','node_identity']},'asset_root':str(a)}));os.chmod(marker,0o600)
print(json.dumps({'ok':True,'value':{'installed':True,'worker_sha256':v['source_sha256']}}))
"""
        result = self.exchange(
            server,
            ["python3", "-c", code],
            {
                **executor,
                "source": base64.b64encode(source).decode(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "storage_source": storage_source(),
            },
        )
        if not result["ok"]:
            raise ValueError("Executor preparation failed")
        return self.call(server, executor, "probe", {})

    def inspect_storage(self, server, request):
        from .node_storage import program

        return self.exchange(server, ["python3", "-c", program()], request)["value"]

    def call(self, server, executor, action, payload):
        if action not in {
            "submit",
            "lookup",
            "reconcile",
            "status",
            "cancel",
            "identity",
            "manifest",
            "logs",
            "probe",
            "resolve_image",
            "export",
            "cleanup_executor",
        }:
            raise ValueError("Invalid node protocol action")
        pinned = executor.get("probe", {}).get("worker_sha256")
        code = """import hashlib,runpy,sys
from pathlib import Path
p=Path(sys.argv[1]);expected=sys.argv[4]
assert not p.is_symlink() and (not expected or hashlib.sha256(p.read_bytes()).hexdigest()==expected),'Executor code changed; reconfigure while idle'
sys.argv=[str(p),sys.argv[2],sys.argv[3]]
runpy.run_path(str(p),run_name='__main__')
"""
        result = self.exchange(
            server,
            [
                "python3",
                "-c",
                code,
                executor["root"] + "/worker.py",
                action,
                executor["root"],
                pinned or "",
            ],
            payload,
        )
        if not result["ok"]:
            raise ValueError(result.get("error", "Node request rejected"))
        return result["value"]
