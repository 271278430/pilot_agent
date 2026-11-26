import json
import subprocess
import sys

import pytest

from repopilot.repository import snapshot
from repopilot.storage import Store, digest
from repopilot.tools import ToolService, recover


def test_recover_edit_already_executed_without_repeating(run_dir):
    service = ToolService(run_dir)
    state = service.store.load()
    path = service.repo / "money.py"
    text = path.read_text().replace("(1 - percentage)", "(1 - percentage / 100)")
    state["pending"] = {"id": "crashed-edit", "tool": "edit", "args": {}, "before": state["hashes"], "expected": {**state["hashes"], "money.py": digest(text.encode())}}
    service.store.checkpoint(state)
    path.write_text(text, encoding="utf-8", newline="\n")
    recovered = recover(run_dir)
    assert recovered["result"]["recovered"]
    assert recovered["result"]["exit_code"] == 0
    assert path.read_text() == text
    assert recover(run_dir)["status"] == "consistent"
    assert service.store.load()["actions"] == 1


def test_unknown_shell_outcome_needs_explicit_ack(run_dir):
    store = Store(run_dir)
    state = store.load()
    state["pending"] = {"id": "crashed-shell", "tool": "shell", "args": {}, "before": state["hashes"]}
    store.checkpoint(state)
    with pytest.raises(ValueError, match="Unknown action outcome"):
        recover(run_dir)
    assert recover(run_dir, acknowledge_unknown=True)["result"]["error"] == "unknown_outcome"
    assert store.load()["pending"] is None


def test_drift_after_completed_action_is_refused(run_dir):
    service = ToolService(run_dir)
    (service.repo / "money.py").write_text("# external change\n")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        recover(run_dir)


def test_checkpoint_is_valid_json_and_raw_journal_is_jsonl(run_dir):
    service = ToolService(run_dir)
    service.call("read", {"path": "money.py"})
    assert service.store.load()["hashes"] == snapshot(service.repo)
    records = [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in records][-2:] == ["action_start", "action_end"]


def test_recovery_preserves_a_live_lock_owner(run_dir):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lock = run_dir / "action.lock"
    lock.write_text(str(child.pid))
    try:
        with pytest.raises(ValueError, match="still owns the workspace"):
            recover(run_dir)
        assert child.poll() is None
        assert lock.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert recover(run_dir)["status"] == "consistent"
    assert not lock.exists()
