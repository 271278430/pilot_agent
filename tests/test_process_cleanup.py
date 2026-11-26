import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pilot_infra.process_engine import SGLangProcessEngine


def test_stop_does_not_reinspect_environment_during_verified_shutdown(tmp_path, monkeypatch):
    engine = object.__new__(SGLangProcessEngine)
    engine.task = {"id": "owned-task"}
    engine.candidate = {}
    engine.record = tmp_path / 'absent-receipt.json'
    inspected = []

    def inspect():
        inspected.append(True)
        assert len(inspected) == 1, 'teardown must not reread environ'
        return {"pid": 12345, "task_id": "owned-task", "port": 99, "command": ["python", "--port", "99"]}

    engine.inspect = inspect
    engine.command = lambda: ["python", "--port", "0"]
    engine.process_alive = lambda _: False
    signals = []
    monkeypatch.setattr('pilot_infra.process_engine.os.getpgid', lambda _: 12345, raising=False)
    monkeypatch.setattr('pilot_infra.process_engine.os.killpg', lambda pid, signal: signals.append((pid, signal)), raising=False)
    engine.stop()
    assert len(signals) == 1 and signals[0][0] == 12345


def test_liveness_rejects_reused_pid_and_zombie(tmp_path, monkeypatch):
    engine = object.__new__(SGLangProcessEngine)
    fields = ['S'] + ['0'] * 18 + ['new-ticks']
    proc = tmp_path / '123'
    proc.mkdir()
    status = proc / 'stat'
    status.write_text('123 (test) ' + ' '.join(fields))
    monkeypatch.setattr('pilot_infra.process_engine.Path', lambda p: tmp_path if p == '/proc' else Path(p))
    assert not engine.process_alive({'pid': 123, 'start_ticks': 'old-ticks'})
    assert engine.process_alive({'pid': 123, 'start_ticks': 'new-ticks'})
    fields[0] = 'Z'
    status.write_text('123 (test) ' + ' '.join(fields))
    assert not engine.process_alive({'pid': 123, 'start_ticks': 'new-ticks'})


@pytest.mark.skipif(os.name == 'nt', reason='Linux /proc process backend')
def test_real_owned_process_shutdown_keeps_receipt_and_leaves_no_service(tmp_path):
    command = [sys.executable, '-c', 'import time; time.sleep(120)', '--port', '99']
    process = subprocess.Popen(command, cwd=tmp_path, env={**os.environ, 'CUDA_VISIBLE_DEVICES': '7'}, start_new_session=True)
    try:
        engine = object.__new__(SGLangProcessEngine)
        engine.task = {'id': 'owned-cpu-cleanup', 'plan': {'gpu_ids': [7]}}
        engine.candidate = {}
        engine.service_dir = tmp_path
        engine.record = tmp_path / 'process.json'
        engine.command = lambda: command
        ticks = (Path('/proc') / str(process.pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        engine.record.write_text(json.dumps({'pid': process.pid, 'start_ticks': ticks, 'port': 99, 'task_id': engine.task['id']}))
        engine.stop()
        process.wait(timeout=10)
        assert not engine.record.exists()
        assert len(list(tmp_path.glob('process-stopped-*.json'))) == 1
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
