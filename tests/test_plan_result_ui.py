"""Run the thin UI plugin's history and delivery-action lifecycle checks."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_plan_result_ui_history_and_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Web plugin checks")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("plan_result_ui.test.mjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
