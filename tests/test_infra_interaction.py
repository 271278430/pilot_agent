"""Check Infra RPC concurrency and retained native chat result presentation."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_infra_rpc_and_chat_receipts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Web plugin checks")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("infra_interaction.test.mjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
