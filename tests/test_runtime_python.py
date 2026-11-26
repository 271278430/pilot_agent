import json
import sys

from pilot_host.cli import runtime_environment
from repopilot.harness import composition


def test_mcp_preserves_virtual_environment_interpreter(run_dir, monkeypatch):
    interpreter = '/workspace/.venv/bin/python'
    monkeypatch.setattr(sys, 'executable', interpreter)
    path = composition(run_dir)
    rows = json.loads(path.read_text())[-1]['insert']
    mcp = next(row for row in rows if row['id'] == 'repopilot-mcp')
    assert mcp['config']['command'] == interpreter


def test_official_runtime_uses_private_temporary_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', '/shared-full-tmp')
    home = tmp_path / 'harness-web'
    environment = runtime_environment(home, {'REPOPILOT_API_KEY': 'test-only'})
    assert environment['TMPDIR'] == str(home / 'tmp')
    assert environment['TMP'] == environment['TEMP'] == environment['TMPDIR']
    assert (home / 'tmp').is_dir()
