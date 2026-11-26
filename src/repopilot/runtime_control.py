"""Cooperative cancellation through the public SDK lifecycle, never process killing."""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any


@contextlib.contextmanager
def cancel_on_request(harness: Any, requested: threading.Event | None) -> Iterator[None]:
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.15):
            if requested is not None and requested.is_set():
                harness.close()
                return

    thread = threading.Thread(target=watch, daemon=True) if requested is not None else None
    if thread:
        thread.start()
    try:
        if requested is not None and requested.is_set():
            raise InterruptedError("operator_stop_requested")
        yield
    finally:
        finished.set()
        if thread:
            thread.join(timeout=1)
