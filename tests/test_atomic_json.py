import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from repopilot import storage


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_atomic_json_retries_while_destination_is_open(tmp_path, monkeypatch):
    path = tmp_path / "budget-policy.json"
    path.write_text('{"revision": 1}', encoding="utf-8")
    blocked = threading.Event()
    original_replace = os.replace

    def observed_replace(source, destination):
        try:
            return original_replace(source, destination)
        except PermissionError:
            blocked.set()
            raise

    monkeypatch.setattr(storage.os, "replace", observed_replace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with path.open(encoding="utf-8"):
            future = pool.submit(storage.atomic_json, path, {"revision": 2})
            assert blocked.wait(timeout=2), "the open file should block replacement on Windows"
        future.result(timeout=3)
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 2}
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_atomic_json_fails_if_windows_reader_keeps_file_locked(tmp_path):
    path = tmp_path / "budget-policy.json"
    path.write_text('{"revision": 1}', encoding="utf-8")
    with path.open(encoding="utf-8"):
        with pytest.raises(PermissionError):
            storage.atomic_json(path, {"revision": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_reports_permanent_errors_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "budget-policy.json"
    path.write_text('{"revision": 1}', encoding="utf-8")

    def deny_replace(*_):
        raise PermissionError("denied")

    monkeypatch.setattr(storage.os, "replace", deny_replace)
    with pytest.raises(PermissionError, match="denied"):
        storage.atomic_json(path, {"revision": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"revision": 1}
    assert list(tmp_path.glob("*.tmp")) == []
