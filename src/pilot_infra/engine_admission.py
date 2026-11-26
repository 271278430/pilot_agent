"""Trusted pre-import gate. Its read-only directory is writable only by the Host."""

import json
import time
from pathlib import Path


def wait_for_authorization(path, expected, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(Path(path).read_text())
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if any(value.get(k) != v for k, v in expected.items()):
            raise ValueError('Launch authorization identity differs')
        if not value.get('pid') or not value.get('start_ticks'):
            raise ValueError('Launch authorization lacks durable process identity')
        return value
    raise TimeoutError('No durable launch authorization; editable engine was not imported')
