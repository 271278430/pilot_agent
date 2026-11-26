"""Task-owned GPU process backend for a host without NVIDIA Docker runtime."""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from repopilot.storage import atomic_json

from .engine import SGLangEngine, model_manifest
from .store import InfraStore, digest

VERSION_PROBE = "import importlib.metadata as m,json,platform;print(json.dumps({'python':platform.python_version(),'packages':sorted([(d.metadata['Name'],d.version) for d in m.distributions()])}))"
LIMITED_LAUNCH = "import os,resource,sys; cores=int(sys.argv[1]); size=int(sys.argv[2]); os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:cores]); resource.setrlimit(resource.RLIMIT_FSIZE,(size,size)); os.execv(sys.argv[3],sys.argv[3:])"


def start_recorded_process(command_builder, *, cwd, env, stream, info_fd, record, receipt, gated, authorization=None):
    """Release sandbox execution only after its durable PID receipt exists."""
    gate_read, gate_write = os.pipe() if gated else (None, None)
    process = None
    try:
        command = command_builder(gate_read)
        process = subprocess.Popen(
            command, cwd=cwd, env=env, start_new_session=True,
            stdout=stream, stderr=stream,
            pass_fds=(info_fd, gate_read) if gated else (),
        )
        ticks = Path(f"/proc/{process.pid}/stat").read_text().rsplit(')', 1)[1].split()[19]
        saved = {**receipt, 'pid': process.pid, 'start_ticks': ticks,
                 'info_fd': info_fd, 'gate_fd': gate_read}
        atomic_json(record, saved)
        if authorization:
            atomic_json(authorization['path'], {**authorization['expected'],
                        'pid': process.pid, 'start_ticks': ticks})
        if gate_write is not None:
            os.write(gate_write, b'1')
        return process
    except BaseException:
        # This group was created here and has not been handed to recovery.
        # Keep any saved receipt as evidence; never release an unrecorded run.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        raise
    finally:
        for fd in (gate_read, gate_write):
            if fd is not None:
                os.close(fd)


def process_alive(receipt):
    try:
        fields = Path(f"/proc/{receipt['pid']}/stat").read_text().rsplit(')', 1)[1].split()
        return fields[0] != 'Z' and fields[19] == receipt['start_ticks']
    except FileNotFoundError:
        return False


def reconcile_owned_group(receipt, cwd):
    """Do not settle a dead leader while its owned descendants still run."""
    def members():
        values = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != receipt['pid'] or fields[0] == 'Z':
                    continue
                if (proc.stat().st_uid != os.getuid() or (proc/'cwd').resolve() != cwd
                        or int(fields[19]) < int(receipt['start_ticks'])
                        or (int(proc.name) == receipt['pid'] and fields[19] != receipt['start_ticks'])):
                    raise ValueError('Surviving process group ownership differs; keep unknown operation')
                values.append(int(proc.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        return values
    count = len(members())
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not members():
            return count
        try:
            os.killpg(receipt['pid'], sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while members() and time.monotonic() < deadline:
            time.sleep(0.1)
    if members():
        raise ValueError('Owned group has not exited; keep unknown operation')
    return count


def group_usage(pid, start_ticks):
    if not process_alive({'pid':pid, 'start_ticks':start_ticks}):
        return {'rss_bytes':0}
    rss=0
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
            if int(fields[2]) == pid:
                rss += int(fields[21])*os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            continue
    return {'rss_bytes':rss}


def environment_manifest(python):
    result = subprocess.run(
        [str(python), "-I", "-c", VERSION_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    values = json.loads(result.stdout)
    return {**values, "digest": "sha256:" + digest(values)}


class SGLangProcessEngine(SGLangEngine):
    def __init__(self, registry, task, candidate):
        super().__init__(registry, task, candidate)
        self.store = InfraStore(registry.path.parent.parent)
        self.record = self.store.path(task["id"], f"candidates/{candidate['id']}/process.json")
        self.log = self.store.path(task["id"], f"candidates/{candidate['id']}/service.log")
        self.service_dir = self.record.parent / 'service-state' if candidate.get('engine_source') else self.record.parent
        self.loaded = self.service_dir / 'engine-loaded.json'
        self.namespace = self.record.parent / 'sandbox-info.json'

    def engine_artifact(self):
        from .engine_source import EngineSource
        from .service import InfraService
        return EngineSource(InfraService(self.store.root, spawn_jobs=False)).admitted(self.task, self.candidate)

    def sandboxed_command(self, command, ipc_tmpdir, info_fd, gate_fd=None, authorization=None):
        from .engine_source import file_sha, sandbox_command
        artifact=self.engine_artifact()
        recipe=self.candidate['engine_source']['profile']
        runtime=self.record.parent/artifact['runtime']
        launcher=Path(__file__).with_name('engine_launch.py')
        admission=launcher.with_name('engine_admission.py')
        expected={'version':artifact['version'], 'candidate_digest':self.candidate['digest'],
                  'source_digest':artifact['source_digest'], 'wheel_sha256':artifact['wheel_sha256'],
                  'launcher_sha256':file_sha(launcher)}
        readonly=[runtime, self.model['path'],launcher]
        if authorization:
            auth_path=Path(authorization['path'])
            expected.update(authorization['expected'], launch_authorization=str(auth_path))
            readonly += [admission, auth_path.parent]
        args=sandbox_command(recipe,self.image['python'],self.service_dir,
            readonly=readonly,gpu_ids=self.task['plan']['gpu_ids'],tmpdir=ipc_tmpdir)
        args += ['--info-fd',str(info_fd),'--setenv','OMP_NUM_THREADS',str(self.task.get('_resource_limits',{}).get('cpu_cores',4)),
                 '--',self.image['python'],'-s',str(launcher),str(runtime),str(self.loaded),json.dumps(expected,sort_keys=True),*command[3:]]
        if gate_fd is not None:
            args[1:1]=['--block-fd',str(gate_fd)]
        return args

    def expected_command(self, obj):
        command=self.command()
        command[command.index('--port')+1]=str(obj['port'])
        if self.candidate.get('engine_source'):
            command=self.sandboxed_command(command,Path(obj['ipc_tmpdir']),obj['info_fd'],obj.get('gate_fd'),obj.get('authorization'))
        return command

    def loaded_identity(self, obj):
        from .engine_source import file_sha, gpu_devices
        artifact=self.engine_artifact()
        runtime=self.record.parent/artifact['runtime']
        loaded=json.loads(self.loaded.read_text())
        ns=json.loads(self.namespace.read_text())
        init=Path('/proc')/str(ns['child-pid'])
        checks=(loaded['candidate_digest']==self.candidate['digest']
            and loaded['source_digest']==artifact['source_digest']
            and loaded['wheel_sha256']==artifact['wheel_sha256']
            and loaded['distribution_version']==artifact['version']
            and loaded['module']==str(runtime/'sglang/__init__.py')
            and loaded['module_sha256']==artifact['runtime_files']['sglang/__init__.py']
            and loaded['launcher_sha256']==file_sha(Path(__file__).with_name('engine_launch.py'))
            and loaded['admission_sha256']==file_sha(Path(__file__).with_name('engine_admission.py'))
            and loaded['launch_nonce']==obj['authorization']['expected']['launch_nonce']
            and loaded['launch_authorization']==str(obj['authorization']['path'])
            and loaded['gpu_ids']==','.join(map(str,self.task['plan']['gpu_ids']))
            and loaded['cuda_visible_devices']==','.join(g['uuid'] for g in gpu_devices(self.task['plan']['gpu_ids']))
            and init.exists() and os.getpgid(ns['child-pid'])==obj['pid']
            and str(ns['mnt-namespace']) in os.readlink(init/'ns/mnt')
            and str(ns['pid-namespace']) in os.readlink(init/'ns/pid')
            and os.readlink(init/'ns/mnt')!=os.readlink('/proc/self/ns/mnt'))
        return checks, loaded

    def command(self):
        args = super().command()
        args[0] = self.image["python"]
        args[args.index("--model-path") + 1] = str(Path(self.model["path"]).resolve())
        args[args.index("--host") + 1] = "127.0.0.1"
        return args

    def ipc_tempdir(self):
        # Python may fall back to cwd when system temp storage is unavailable.
        # Candidate paths can exceed Linux's 107-byte Unix-socket limit.
        base = self.store.root / ".ipc"
        base.mkdir(mode=0o700, exist_ok=True)
        if base.is_symlink() or not base.resolve().is_relative_to(self.store.root):
            raise ValueError("IPC directory escapes project")
        # Reserve space for mkdtemp's 8-byte name and SGLang's socket basename.
        if len(os.fsencode(str(base))) + 1 + 8 + 1 + 16 > 107:
            raise ValueError("Project path is too long for SGLang IPC; use a shorter project root")
        return Path(tempfile.mkdtemp(prefix="", dir=base))

    def inspect(self):
        if not self.record.is_file():
            return None
        data = json.loads(self.record.read_text())
        proc = Path("/proc") / str(data["pid"])
        if not proc.is_dir():
            return None
        try:
            stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z" or stat[19] != data["start_ticks"]:
                return None
            cmd = (proc / "cmdline").read_bytes().decode().split("\0")[:-1]
            env = dict(
                v.split("=", 1)
                for v in (proc / "environ").read_bytes().decode().split("\0")
                if "=" in v
            )
            return {
                **data,
                "command": cmd,
                "gpu_ids": env.get("CUDA_VISIBLE_DEVICES"),
                "cwd": str((proc / "cwd").resolve()),
            }
        except (OSError, ValueError):
            if proc.exists():
                raise ValueError("Live process ownership could not be inspected") from None
            return None

    def verify_identity(self):
        obj = self.inspect()
        if not obj:
            return {"passed": False, "error": "Task-owned process absent"}
        expected = self.expected_command(obj)
        checks = {
            "frozen_assets": self.registered_identity(),
            "owned": obj["task_id"] == self.task["id"]
            and obj["candidate_digest"] == self.candidate["digest"],
            "command": obj["command"] == expected,
            "gpu_ids": obj["gpu_ids"] == ",".join(map(str, self.task["plan"]["gpu_ids"])),
            "cwd": obj["cwd"] == str(self.service_dir),
            "environment": environment_manifest(self.image["python"])["digest"]
            == self.image["digest"],
            "weights": model_manifest(self.model["path"])["digest"]
            == self.model["manifest_digest"],
        }
        loaded=None
        if self.candidate.get('engine_source'):
            try:
                checks['engine_artifact'],loaded=self.loaded_identity(obj)
            except (OSError, ValueError, KeyError):
                checks['engine_artifact']=False
        value = {
            "passed": all(checks.values()),
            "checks": checks,
            "pid": obj["pid"],
            "start_ticks": obj["start_ticks"],
            "image": self.image["digest"],
            "model_manifest": self.model["manifest_digest"],
            "config_digest": self.candidate["digest"],
        }
        if loaded:
            value['engine_loaded']=loaded
        return value

    def endpoint(self):
        obj = self.inspect()
        if not obj:
            raise ValueError("Task service process absent")
        return "http://127.0.0.1:" + str(obj["port"])

    def launch(self, cancelled=lambda: False):
        raise ValueError('Host-process model deployment is retired; use a container')

    def stop(self):
        obj = self.inspect()
        if obj:
            expected = obj.get('launched_command') or self.expected_command(obj)
            if (
                obj["task_id"] != self.task["id"]
                or obj["command"] != expected
                or os.getpgid(obj["pid"]) != obj["pid"]
            ):
                raise ValueError("Refusing to stop an unowned or changed process group")
            os.killpg(obj["pid"], signal.SIGTERM)
            until = time.monotonic() + 8
            # Once ownership was proved and SIGTERM sent, /proc/environ may
            # become unreadable during teardown. Poll the frozen PID/start
            # identity instead of treating that shutdown race as lost ownership.
            while self.process_alive(obj) and time.monotonic() < until:
                time.sleep(0.2)
            if self.process_alive(obj):
                os.killpg(obj["pid"], signal.SIGKILL)
        if self.record.exists() and (not obj or not self.process_alive(obj)):
            # The launcher may have exited before its scheduler children.
            # A surviving group is still a side effect; prove every member's
            # unique task cwd, user and GPU environment before signalling it.
            data = json.loads(self.record.read_text())
            members = []
            for proc in Path("/proc").iterdir():
                if not proc.name.isdecimal():
                    continue
                try:
                    stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(stat[2]) != data["pid"] or stat[0] == "Z":
                        continue
                    env = dict(
                        v.split("=", 1)
                        for v in (proc / "environ").read_bytes().decode().split("\0")
                        if "=" in v
                    )
                    if (
                        proc.stat().st_uid != os.getuid()
                        or (proc / "cwd").resolve() != self.service_dir
                        or env.get('PILOT_GPU_IDS',env.get("CUDA_VISIBLE_DEVICES"))
                        != ",".join(map(str, self.task["plan"]["gpu_ids"]))
                    ):
                        raise ValueError(
                            "Surviving process group ownership differs; keep unknown operation"
                        )
                    members.append(int(proc.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
            if members:
                os.killpg(data["pid"], signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.killpg(data["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.record.exists():
            reconcile_owned_group(json.loads(self.record.read_text()), self.service_dir)
            # Preserve the receipt; a new launch gets a new receipt after this
            # explicit stop, never before unknown effects have been settled.
            saved = self.record.with_name("process-stopped-" + str(time.time_ns()) + ".json")
            self.record.rename(saved)

    def process_alive(self, receipt):
        proc = Path("/proc") / str(receipt["pid"])
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            return fields[0] != "Z" and fields[19] == receipt["start_ticks"]
        except FileNotFoundError:
            return False

    def logs(self):
        if not self.log.is_file():
            return ""
        with self.log.open("rb") as stream:
            stream.seek(max(0, self.log.stat().st_size - 60000))
            return stream.read().decode(errors="replace")


def engine_for(registry, task, candidate):
    _, _, asset = registry.validate(task["plan"], cleanup=task.get("_cleanup", False))
    if asset.get('server_id'):
        from .remote_engine import RemoteEngine
        return RemoteEngine(registry,task,candidate)
    from .engine import VLLMEngine
    cls = SGLangProcessEngine if task.get('_cleanup') and asset.get('backend') == 'process' else VLLMEngine if asset.get('engine') == 'vllm' else SGLangEngine
    return cls(registry, task, candidate)
