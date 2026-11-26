"""Real Linux CPU process/sandbox lifecycle; these are not GPU accuracy tests."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from pilot_infra.process_engine import process_alive, reconcile_owned_group, start_recorded_process

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux process/sandbox lifecycle")


def test_receipt_failure_never_releases_execution_and_closes_fds(tmp_path, monkeypatch):
    captured = []

    def builder(fd):
        captured.append(fd)
        return [
            sys.executable,
            "-c",
            'import os,sys; data=os.read(int(sys.argv[1]),1); data and open(sys.argv[2],"w").write("ran")',
            str(fd),
            str(tmp_path / "ran"),
        ]

    def fail(*a):
        raise OSError("controlled receipt failure")

    monkeypatch.setattr("pilot_infra.process_engine.atomic_json", fail)
    with (tmp_path / "log").open("wb") as stream:
        with pytest.raises(OSError, match="receipt failure"):
            start_recorded_process(
                builder,
                cwd=tmp_path,
                env=os.environ,
                stream=stream,
                info_fd=stream.fileno(),
                record=tmp_path / "receipt.json",
                receipt={},
                gated=True,
            )
    assert not (tmp_path / "ran").exists()
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_dead_leader_descendants_are_reconciled_without_replaying(tmp_path):
    command = [
        sys.executable,
        "-c",
        'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); print(p.pid,flush=True)',
    ]
    p = subprocess.Popen(
        command, cwd=tmp_path, start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    ticks = Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    child = int(p.stdout.readline())
    p.wait(timeout=5)
    try:
        assert Path(f"/proc/{child}").exists()
        assert reconcile_owned_group({"pid": p.pid, "start_ticks": ticks}, tmp_path) >= 1
        assert not process_alive({"pid": p.pid, "start_ticks": ticks})
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def bwrap():
    path = os.environ.get("PILOT_TEST_BWRAP")
    if not path:
        pytest.skip("Explicit registered bubblewrap test binary required")
    return path


def test_real_bwrap_eof_requires_durable_authorization_before_execution(tmp_path):
    binary = bwrap()
    read, write = os.pipe()
    try:
        p = subprocess.Popen(
            [
                binary,
                "--unshare-pid",
                "--ro-bind",
                "/",
                "/",
                '--dev', '/dev', '--proc', '/proc',
                "--bind",
                str(tmp_path),
                str(tmp_path),
                "--block-fd",
                str(read),
                "--",
                sys.executable, '-I', '-c',
                'import runpy,sys; runpy.run_path(sys.argv[1])["wait_for_authorization"](sys.argv[2],{"launch_nonce":"expected"},timeout=.2); open(sys.argv[3],"w").write("ran")',
                str(Path(__file__).parents[1]/'src/pilot_infra/engine_admission.py'),
                str(tmp_path/'absent-auth.json'), str(tmp_path/'ran'),
            ],
            pass_fds=(read,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        os.close(write)
        write = None
        p.communicate(timeout=5)
        assert p.returncode != 0 and not (tmp_path / "ran").exists()
    finally:
        os.close(read)
        if write is not None:
            os.close(write)


def test_real_bwrap_service_survives_worker_exit_and_is_owned(tmp_path):
    binary = bwrap()
    code = """import json,os,sys
from pathlib import Path
from pilot_infra.process_engine import start_recorded_process
root=Path(sys.argv[1]); binary=sys.argv[2]
auth=root/'auth-readonly'; auth.mkdir(); path=auth/'allowed.json'
admission=Path(sys.argv[3])
expected={'launch_nonce':'unique-test'}
with (root/'log').open('wb') as log,(root/'namespace.json').open('w') as info:
 def builder(fd):
  return [binary,'--unshare-pid','--ro-bind','/','/','--dev','/dev','--proc','/proc','--bind',str(root),str(root),'--ro-bind',str(auth),str(auth),'--chdir',str(root),'--info-fd',str(info.fileno()),'--block-fd',str(fd),'--',sys.executable,'-I','-c','import runpy,sys,time; runpy.run_path(sys.argv[1])["wait_for_authorization"](sys.argv[2],{"launch_nonce":"unique-test"}); open("ready","w").write("ready"); time.sleep(120)',str(admission),str(path)]
 start_recorded_process(builder,cwd=root,env=os.environ,stream=log,info_fd=info.fileno(),record=root/'receipt.json',receipt={},gated=True,authorization={'path':path,'expected':expected})
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path), binary,
                    str(Path(__file__).parents[1]/'src/pilot_infra/engine_admission.py')], check=True, timeout=10)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    try:
        assert process_alive(receipt), (tmp_path/'log').read_text(errors='replace')
        # Readiness is bounded; the worker above has already exited.
        import time

        until = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < until:
            time.sleep(0.05)
        assert (tmp_path / "ready").exists()
        assert reconcile_owned_group(receipt, tmp_path) >= 1
        assert not process_alive(receipt)
    finally:
        try:
            os.killpg(receipt["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_authorization_identity_is_exact_and_requires_recorded_pid(tmp_path):
    from pilot_infra.engine_admission import wait_for_authorization
    path=tmp_path/'auth.json'
    path.write_text(json.dumps({'launch_nonce':'old', 'pid':123,'start_ticks':'1'}))
    with pytest.raises(ValueError, match='identity differs'):
        wait_for_authorization(path,{'launch_nonce':'new'})
    path.write_text(json.dumps({'launch_nonce':'new'}))
    with pytest.raises(ValueError, match='durable process identity'):
        wait_for_authorization(path,{'launch_nonce':'new'})
