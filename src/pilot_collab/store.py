from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

from repopilot.storage import now, redact

from .contracts import Message, NewRun


def clean(value):
    text = json.dumps(redact(value), ensure_ascii=False)
    project = Path(__file__).resolve().parents[2]
    for path in (project / ".env", project / ".local/glm.env", project / ".local/workbench/model.env"):
        for key, secret in dotenv_values(path).items():
            if (
                secret
                and len(secret) >= 8
                and any(x in key.upper() for x in ("KEY", "SECRET", "PASSWORD"))
            ):
                text = text.replace(secret, "[REDACTED]")

    def scrub(obj):
        if isinstance(obj, dict):
            return {
                k: "[REDACTED]"
                if any(x in k.lower() for x in ("password", "api_key", "secret"))
                else scrub(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    return scrub(json.loads(text))


class Database:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / "runs/collaboration"
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = self.base / "state.sqlite"
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, request_id TEXT UNIQUE,
                title TEXT, goal TEXT, controller TEXT, status TEXT, control TEXT,
                created_at TEXT, updated_at TEXT, data TEXT, pid INTEGER);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, run_id TEXT, sender TEXT,
                operation TEXT, status TEXT, result TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, time TEXT, agent TEXT, kind TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
            CREATE INDEX IF NOT EXISTS messages_pending ON messages(run_id,sender,status);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, run_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run ID")
        path = self.base / run_id
        if path.is_symlink() or path.resolve().parent != self.base.resolve():
            raise ValueError("Run directory escape")
        return path

    def create(self, request: NewRun):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM runs WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if old:
                if (
                    old["goal"] != clean(request.goal)
                    or old["title"] != clean(request.title)
                    or old["controller"] != request.controller
                    or json.loads(old['data']).get('recipe_id', 'order-service-v1') != request.recipe_id
                    or json.loads(old['data']).get('orchestration', 'multi') != request.orchestration
                ):
                    raise ValueError("Idempotency key reused with different task content")
                run_id = old["id"]
            else:
                run_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        request.request_id,
                        clean(request.title),
                        clean(request.goal),
                        request.controller,
                        "planned",
                        "",
                        now(),
                        now(),
                        json.dumps(
                            {
                                'recipe_id': request.recipe_id,
                                'orchestration': request.orchestration,
                                "agents": {
                                    "supervisor": "ready",
                                    "repo": "waiting",
                                    "infra": "waiting",
                                    "verifier": "waiting",
                                }
                            }
                        ),
                        None,
                    ),
                )
        if old:
            return self.get(run_id)
        self.directory(run_id).mkdir()
        self.event(run_id, "operator", "plan_created", {"goal": request.goal})
        return self.get(run_id)

    def get(self, run_id):
        self.directory(run_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        row = dict(row)
        row["data"] = json.loads(row["data"])
        return row

    def list(self):
        with self.connect() as db:
            ids = [
                r["id"] for r in db.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 80")
            ]
        return [self.get(i) for i in ids]

    def update(self, run_id, *, status=None, control=None, data=None, pid=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            merged = json.loads(row["data"])
            if data:
                merged.update(clean(data))
            db.execute(
                "UPDATE runs SET status=?,control=?,data=?,pid=?,updated_at=? WHERE id=?",
                (
                    status if status is not None else row["status"],
                    control if control is not None else row["control"],
                    json.dumps(merged, ensure_ascii=False),
                    pid if pid is not None else row["pid"],
                    now(),
                    run_id,
                ),
            )

    def agent(self, run_id, role, status):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            value = json.loads(
                db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()["data"]
            )
            value["agents"][role] = status
            db.execute(
                "UPDATE runs SET data=?,updated_at=? WHERE id=?", (json.dumps(value), now(), run_id)
            )

    def session(self, run_id, role, session_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            value = json.loads(
                db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()["data"]
            )
            value.setdefault("session_ids", {})[role] = session_id
            db.execute(
                "UPDATE runs SET data=?,updated_at=? WHERE id=?", (json.dumps(value), now(), run_id)
            )

    def event(self, run_id, role, kind, data=None):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,time,agent,kind,data) VALUES(?,?,?,?,?)",
                (run_id, now(), role, kind, json.dumps(clean(data or {}), ensure_ascii=False)),
            )

    def events(self, run_id, after=0):
        self.get(run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 300",
                (run_id, after),
            ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def import_repo_events(self, run_id, events, next_offset):
        """Commit source events and their byte cursor together for resume."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            value = json.loads(row["data"])
            if next_offset <= value.get("repo_event_offset", 0):
                return
            for event in events:
                db.execute(
                    "INSERT INTO events(run_id,time,agent,kind,data) VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        event["time"],
                        "repo",
                        event["kind"],
                        json.dumps(clean(event), ensure_ascii=False),
                    ),
                )
            value["repo_event_offset"] = next_offset
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(value), run_id))

    def send(self, message: Message):
        self.get(message.run_id)
        with self.connect() as db:
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                (
                    message.message_id,
                    message.run_id,
                    message.sender,
                    message.operation,
                    "pending",
                    None,
                    now(),
                ),
            )
        self.event(
            message.run_id,
            message.sender,
            "tool_requested",
            {"message_id": message.message_id, "operation": message.operation},
        )

    def claim(self, run_id, sender):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM messages WHERE run_id=? AND sender=? AND status='pending' ORDER BY created_at LIMIT 1",
                (run_id, sender),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE messages SET status='running' WHERE id=? AND status='pending'",
                    (row["id"],),
                )
                return dict(row)
        return None

    def reply(self, message, result):
        with self.connect() as db:
            db.execute(
                "UPDATE messages SET status='done',result=? WHERE id=?",
                (json.dumps(clean(result), ensure_ascii=False), message["id"]),
            )
        self.event(
            message["run_id"],
            message["sender"],
            "tool_returned",
            {"operation": message["operation"], "result": result},
        )

    def result(self, message_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT status,result FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        return json.loads(row["result"]) if row and row["status"] == "done" else None
