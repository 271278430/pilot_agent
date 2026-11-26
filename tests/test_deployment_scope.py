"""Release manifests must reject user/model/private paths before SSH access."""
import importlib
import json
from pathlib import Path

import pytest


def deployment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("deploy")


def test_cleanup_rejects_protected_data_before_connection(tmp_path, monkeypatch):
    release = deployment(monkeypatch)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "connect", lambda: pytest.fail("SSH must not be used"))
    plan = tmp_path / "unsafe.json"
    for path in ("runs/apps/id/candidates/c1/app.py", ".local/glm.env", "src/../runs/model",
                 "src/.local/key", "src/node_modules/file", "src/.env", "src/private.pem",
                 "/data/model", "../outside.py"):
        plan.write_text(json.dumps({"delete": [path]}))
        monkeypatch.setattr("sys.argv", ["deploy.py", "--cleanup-plan", str(plan), "--apply"])
        with pytest.raises(AssertionError, match="protected paths"):
            release.main()


def test_source_bundle_omits_private_state_and_caches(tmp_path, monkeypatch):
    release = deployment(monkeypatch)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    for name in ("src/pilot_host/cli.py", "src/pilot_host/__pycache__/cli.pyc",
                 "src/pilot_host/.local/key", "src/pilot_host/.env",
                 "src/project.egg-info/PKG-INFO",
                 "runs/model.bin", ".local/ssh.env", "scripts/node_modules/file"):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture")
    assert set(release.sources()) == {"src/pilot_host/cli.py"}
    assert release.valid_delete("src/retired/driver.py")
