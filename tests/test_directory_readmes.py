"""Directory inventories and Git exclusions must follow the published tree."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import update_directory_readmes as indexes


def test_directory_index_detects_new_files(tmp_path, monkeypatch):
    folder = tmp_path / "src" / "example"
    folder.mkdir(parents=True)
    (folder / "worker.py").write_text('"""Run a task."""\n', encoding="utf-8")
    monkeypatch.setattr(indexes, "ROOT", tmp_path)
    monkeypatch.setattr(indexes, "TOP", ("src",))
    monkeypatch.setattr(sys, "argv", ["update_directory_readmes.py"])
    assert indexes.main() == 0
    assert "worker.py" in (folder / "README.md").read_text(encoding="utf-8")
    (folder / "new.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["update_directory_readmes.py", "--check"])
    assert indexes.main() == 1


def test_github_readme_keeps_homepage_and_directory_index(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text(
        "# Pilot\n\n![预览](docs/assets/preview.webp)\n\n[指南](docs/guide.md) [外部](https://example.com)\n",
        encoding="utf-8",
    )
    folder = tmp_path / ".github"
    (folder / "workflows").mkdir(parents=True)
    monkeypatch.setattr(indexes, "ROOT", tmp_path)
    result = indexes.render(folder)
    assert result.startswith("# Pilot\n")
    assert "![预览](../docs/assets/preview.webp)" in result
    assert "[指南](../docs/guide.md)" in result
    assert "[外部](https://example.com)" in result
    assert "[workflows](workflows/README.md)" in result


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_ignore_private_state_without_ignoring_workspace_plugin(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def ignored(path):
        result = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=tmp_path)
        assert result.returncode in (0, 1)
        return result.returncode == 0

    for path in (".local/resources/vault.key", ".env", ".env.production", "subdir/.env.local",
                 "workspace/private.txt", "reports/output.json", "harness/runtime/node_modules/library/index.js"):
        assert ignored(path)
    assert not ignored(".env.example")
    assert not ignored("harness/runtime/plugins/workspace/index.mjs")
