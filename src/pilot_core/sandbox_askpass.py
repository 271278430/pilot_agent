"""Sudo's private authentication callback. Never invoke as a user-facing tool."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pilot_core.docker import prefix  # noqa: E402

_, password = prefix()
if not password:
    raise SystemExit(1)
sys.stdout.write(password + "\n")
