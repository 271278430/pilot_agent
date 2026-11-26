from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def now() -> str:
    # Windows may not ship an IANA database; avoid a tzdata runtime requirement.
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    except (KeyError, ImportError):
        from datetime import timedelta, timezone

        return datetime.now(timezone(timedelta(hours=8))).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Concurrent bridge requests must not share a temporary pathname.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # On Windows a reader without FILE_SHARE_DELETE can briefly prevent a
        # replacement. Keep the atomic operation and bound retries so actual
        # permission failures still surface to the caller.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                remaining = deadline - time.monotonic()
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                    raise
                time.sleep(min(0.02, remaining))
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def redact(value: object) -> object:
    secrets = [v for k, v in os.environ.items() if any(t in k.upper() for t in ("API_KEY", "TOKEN", "SECRET")) and len(v) >= 8]
    text = json.dumps(value, ensure_ascii=False)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"Bearer\s+[A-Za-z0-9_.-]{12,}", "Bearer [REDACTED]", text)
    return json.loads(text)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **payload: object) -> None:
        record = redact({"time": now(), "kind": kind, **payload})
        with (self.root / "trajectory.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def checkpoint(self, value: dict) -> None:
        # Integrity state must retain exact hashes, unlike public trace content.
        atomic_json(self.root / "checkpoint.json", value)

    def load(self) -> dict:
        return read_json(self.root / "checkpoint.json")
