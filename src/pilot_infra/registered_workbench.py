"""Source work uses the registered node connection, roots and frozen scope."""

import base64
import hashlib
import math
from pathlib import Path, PurePosixPath

from . import build_worker
from .fleet_transport import FleetTransport
from .node_authority import require_operation
from .resource_assessment import check_gpu_occupancy
from .resource_registry import ResourceRegistry


class RegisteredWorkbench:
    worker_file = build_worker.__file__
    subdir = "workspaces"
    prefix = "pilot-workbench-"

    def __init__(self, project, profile, task_id):
        self.project, self.profile, self.task_id = Path(project), profile, task_id
        self.scope = profile["node_scope"]
        self.server, self.executor = self.scope["server"], self.scope["executor"]
        self.transport = FleetTransport(project)
        self.directory = str(
            PurePosixPath(self.executor["root"]) / self.subdir / (self.prefix + task_id)
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def admit(self, edit=False):
        current = ResourceRegistry(self.project).get(self.server["id"])
        if current["revision"] != self.server["revision"]:
            raise ValueError("节点连接或权限已变化，请重新审阅软件任务；恢复/清理仍使用原快照")
        require_operation(current, "software", source_edit=edit)
        require_operation(current, "containers")
        if current["policy"]["max_cpu_cores"] < 1 or current["policy"]["max_memory_gib"] < 2:
            raise ValueError("节点软件任务需至少1CPU/2GiB授权限额")
        if self.profile["timeout"] > current["policy"]["max_duration_seconds"]:
            raise ValueError("软件任务时限超过节点授权")
        if self.profile.get("kind") == "profile":
            require_operation(current, "profiling")
        if self.profile.get("kind") == "pd":
            require_operation(current, "inference")
            if current["policy"]["max_memory_gib"] < 4 or current["policy"]["max_cpu_cores"] < 2:
                raise ValueError("PD检查需至少2CPU/4GiB授权")
        ids = self.scope["gpu_ids"]
        if not set(ids).issubset(current["policy"]["approvable_gpu_ids"]):
            raise ValueError("软件GPU任务超出节点设备配额")
        gpu_uuids = []
        if ids:
            observed = self.transport.transport.observe(current)
            check_gpu_occupancy(
                observed, ids, allow_shared=current["policy"].get("gpu_sharing") == "shared"
            )
            devices = {g["index"]: g for g in observed["gpus"]}
            for gpu in ids:
                free = devices.get(gpu, {}).get("free_mib")
                if type(free) not in {int, float} or not math.isfinite(free) or free < self.profile.get("minimum_gpu_mib", 512):
                    raise ValueError("软件GPU任务设备不可用或显存不足")
                gpu_uuids.append(devices[gpu]["uuid"])
        return gpu_uuids

    def bootstrap(self):
        self.admit()
        source = Path(self.worker_file).read_bytes()
        if hashlib.sha256(source).hexdigest() != self.scope["worker_sha256"]:
            raise ValueError("软件Worker代码已变化，请重新审阅任务")
        code = """import base64,hashlib,json,os,sys
from pathlib import Path
v=json.load(sys.stdin);base=Path(v['root']);marker=json.loads((base/'pilot-executor.json').read_text())
assert marker['owner_namespace']==v['owner_namespace']
assert hashlib.sha256((base/'worker.py').read_bytes()).hexdigest()==v['executor_worker_sha256']
p=base/v['subdir']/(v['prefix']+v['task_id'])
for x in [p,*p.parents]:assert not x.is_symlink()
assert not p.exists();p.mkdir(parents=True,mode=0o700)
(p/'lease.json').write_text(json.dumps({'task_id':v['task_id'],'root':str(p)}))
source=base64.b64decode(v['source']);assert hashlib.sha256(source).hexdigest()==v['source_sha256']
(p/'worker.py').write_bytes(source);os.chmod(p/'worker.py',0o600)
print(json.dumps({'ok':True,'value':{'prepared':True,'root':str(p)}}))
"""
        return self.transport.exchange(
            self.server,
            ["python3", "-B", "-c", code],
            {
                "root": self.executor["root"],
                "task_id": self.task_id,
                "subdir": self.subdir,
                "prefix": self.prefix,
                "owner_namespace": self.executor["owner_namespace"],
                "executor_worker_sha256": self.executor["probe"]["worker_sha256"],
                "source": base64.b64encode(source).decode(),
                "source_sha256": self.scope["worker_sha256"],
            },
        )["value"]

    def call(self, action, args):
        uuids = (
            self.admit(edit=action in {"write", "patch"})
            if action not in {"cleanup", "inspect", "read", "read_candidate"}
            else []
        )
        payload = dict(args)
        if action == "prepare":
            check = self.transport.inspect_storage(
                self.server,
                {
                    "namespace": self.executor["namespace"],
                    "owner_namespace": self.executor["owner_namespace"],
                    "root": self.executor["root"],
                    "asset_root": self.executor["asset_root"],
                },
            )
            if not check["ready"] or check["node_identity"] != self.executor["node_identity"]:
                raise ValueError("节点目录、数据挂载或身份已变化")
            payload["profile"] = {
                **payload["profile"],
                "gpu_ids": self.scope["gpu_ids"],
                "gpu_uuids": uuids,
                "cuda_runtime": check.get("cuda_runtime", {"mode": "nvidia"}),
            }
        code = """import hashlib,json,runpy,sys
from pathlib import Path
v=json.load(sys.stdin);p=Path(v['directory']);action=v['action']
if action=='cleanup' and not p.exists():print(json.dumps({'ok':True,'value':{'restored':True,'scope':'owned workspace absent'}}));sys.exit()
if action=='cleanup' and not (p/'worker.py').exists():
 assert not p.is_symlink() and p.resolve()==p
 assert json.loads((p/'lease.json').read_text())=={'task_id':v['task_id'],'root':str(p)}
 assert {x.name for x in p.iterdir()}=={'lease.json'}
 (p/'lease.json').unlink();p.rmdir();print(json.dumps({'ok':True,'value':{'restored':True,'scope':'partial bootstrap'}}));sys.exit()
assert not p.is_symlink() and hashlib.sha256((p/'worker.py').read_bytes()).hexdigest()==v['worker_sha256']
sys.argv=[str(p/'worker.py'),action,str(p),v['task_id']]
import io
sys.stdin=io.StringIO(json.dumps(v['args']));runpy.run_path(str(p/'worker.py'),run_name='__main__')
"""
        result = self.transport.exchange(
            self.server,
            ["python3", "-B", "-c", code],
            {
                "directory": self.directory,
                "task_id": self.task_id,
                "action": action,
                "worker_sha256": self.scope["worker_sha256"],
                "args": payload,
            },
            timeout=self.profile["timeout"] + 20,
        )
        if not result["ok"]:
            raise ValueError(result.get("error", "节点软件任务失败"))
        return result["value"]
