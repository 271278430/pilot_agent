"""Per-node Docker credentials stay outside persistent job payloads."""

from .resource_contracts import DockerAccess
from .resource_registry import ResourceVault

PRIVATE_ENV = {"PILOT_NODE_DOCKER_SUDO_PASSWORD", "PILOT_NODE_DOCKER_USE_SUDO"}

REMOTE_WRAPPER = """import json,os,subprocess,sys
v=json.load(sys.stdin);password=v.get('docker_password');env=dict(os.environ)
for k in ['PILOT_NODE_DOCKER_SUDO_PASSWORD','PILOT_NODE_DOCKER_USE_SUDO']:env.pop(k,None)
if v['docker_mode']=='sudo_password':env['PILOT_NODE_DOCKER_SUDO_PASSWORD']=password
elif v['docker_mode']=='sudo_nopasswd':env['PILOT_NODE_DOCKER_USE_SUDO']='1'
p=subprocess.run(v['command'],input=json.dumps(v['payload']) if v['payload'] is not None else None,capture_output=True,text=True,env=env,timeout=v['timeout'])
sys.stdout.write(p.stdout.replace(password,'[REDACTED]') if password else p.stdout)
sys.stderr.write(p.stderr.replace(password,'[REDACTED]') if password else p.stderr)
sys.exit(p.returncode)
"""


def access(root, server):
    config = DockerAccess.model_validate(server.get("docker") or {})
    password = (
        ResourceVault(root).get(config.credential_ref, "password")
        if config.credential_ref
        else None
    )
    return config.mode, password


def envelope(root, server, command, payload, timeout):
    mode, password = access(root, server)
    return {
        "command": command,
        "payload": payload,
        "timeout": timeout,
        "docker_mode": mode,
        "docker_password": password,
    }
