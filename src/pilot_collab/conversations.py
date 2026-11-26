"""Durable user conversations, separate from Agent messages and tool events."""

import hashlib
import json
import time
import uuid
from typing import Literal

from pydantic import Field, field_validator

from pilot_core.contracts import Contract, RunId
from repopilot.storage import now

from .routing import PendingChoice, RoutingContext
from .store import clean


class TaskReference(Contract):
    kind: Literal["service", "ml", "delivery", "app"]
    id: RunId


class ConversationCreate(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    task: TaskReference | None = None
    mode: Literal["task", "model"] = "task"
    project_id: RunId | None = None


class UserMessage(Contract):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    text: str = Field(min_length=1, max_length=3000)
    max_new_tokens: int = Field(default=768, ge=1, le=1024)

    @field_validator("text")
    @classmethod
    def nonempty(cls, text):
        if not text.strip():
            raise ValueError("Message cannot be blank")
        return text


class ConversationUpdate(Contract):
    action: Literal["rename", "archive", "delete", "restore"]
    title: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Title cannot be blank")
        return value.strip() if value is not None else None


class ChatMessage(Contract):
    seq: int
    role: Literal["user", "assistant"]
    text: str
    created_at: str
    routing: dict | None = None
    generation: dict | None = None


class ConversationView(Contract):
    id: RunId
    title: str
    lifecycle: Literal["active", "archived", "deleted"] = "active"
    mode: Literal["task", "model"] = "task"
    project_id: RunId | None = None
    updated_at: str
    task: TaskReference | None = None
    pending: PendingChoice | None = None
    messages: list[ChatMessage] = Field(default_factory=list)


class ConversationStore:
    def __init__(self, database):
        self.db = database
        with self.db.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, request_id TEXT UNIQUE,
                title TEXT, created_at TEXT, updated_at TEXT, task_kind TEXT, task_id TEXT,
                touched_at REAL DEFAULT 0, pending TEXT);
              CREATE TABLE IF NOT EXISTS conversation_turns(conversation_id TEXT,request_id TEXT,
                input_sha TEXT,status TEXT,route TEXT,PRIMARY KEY(conversation_id,request_id));
              CREATE TABLE IF NOT EXISTS conversation_messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,turn_id TEXT,role TEXT,text TEXT,created_at TEXT,
                UNIQUE(conversation_id,turn_id,role));
              CREATE INDEX IF NOT EXISTS chat_message_order ON conversation_messages(conversation_id,seq);
            """)
            # Migrate existing user conversations in place, retaining IDs/history.
            columns = {r["name"] for r in db.execute("PRAGMA table_info(conversations)")}
            if "lifecycle" not in columns:
                db.execute(
                    "ALTER TABLE conversations ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'"
                )
            if "mode" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'task'")
            if "project_id" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT")
            turn_columns = {r["name"] for r in db.execute("PRAGMA table_info(conversation_turns)")}
            if "generation" not in turn_columns:
                db.execute("ALTER TABLE conversation_turns ADD COLUMN generation TEXT")

    def get(self, conversation_id, *, messages=True):
        self.db.directory(conversation_id)  # Validate ID; it does not create a task.
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise KeyError(conversation_id)
            history = (
                db.execute(
                    "SELECT m.*,t.route,t.generation FROM conversation_messages m LEFT JOIN conversation_turns t ON t.conversation_id=m.conversation_id AND t.request_id=m.turn_id WHERE m.conversation_id=? ORDER BY m.seq DESC LIMIT 100",
                    (conversation_id,),
                ).fetchall()
                if messages
                else []
            )
        task = TaskReference(kind=row["task_kind"], id=row["task_id"]) if row["task_id"] else None
        return ConversationView(
            id=row["id"],
            title=row["title"],
            lifecycle=row["lifecycle"],
            mode=row["mode"],
            project_id=row["project_id"],
            updated_at=row["updated_at"],
            task=task,
            pending=PendingChoice.model_validate_json(row["pending"]) if row["pending"] else None,
            messages=[
                ChatMessage(
                    **{k: r[k] for k in ("seq", "role", "text", "created_at")},
                    routing=self.public_route(r),
                    generation=json.loads(r["generation"])
                    if r["role"] == "assistant" and r["generation"]
                    else None,
                )
                for r in reversed(history)
            ],
        )

    @staticmethod
    def public_route(row):
        if row["role"] != "assistant" or not row["route"]:
            return None
        from .public_trace import route_view
        from .routing import RouteDecision

        try:
            return route_view(RouteDecision.model_validate_json(row["route"]))
        except ValueError:
            return None

    def list(self, lifecycle="active"):
        with self.db.connect() as db:
            ids = [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM conversations WHERE lifecycle=? ORDER BY updated_at DESC LIMIT 200",
                    (lifecycle,),
                )
            ]
        return [self.get(value, messages=False) for value in ids]

    def create(self, request: ConversationCreate, title="新对话"):
        task = request.task
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM conversations WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if old:
                if old["project_id"] != request.project_id or old["mode"] != request.mode or (old["task_kind"], old["task_id"]) != (
                    (task.kind, task.id) if task else (None, None)
                ):
                    raise ValueError("Request key reused with a different task binding")
                identifier = old["id"]
            else:
                identifier = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO conversations(id,request_id,title,created_at,updated_at,task_kind,task_id,touched_at,pending,mode,project_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        request.request_id,
                        clean(title)[:100],
                        now(),
                        now(),
                        task.kind if task else None,
                        task.id if task else None,
                        time.time() if task else 0,
                        None,
                        request.mode,
                        request.project_id,
                    ),
                )
        return self.get(identifier)

    def update(self, conversation_id, request: ConversationUpdate):
        self.db.directory(conversation_id)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise KeyError(conversation_id)
            if db.execute(
                "SELECT 1 FROM conversation_turns WHERE conversation_id=? AND status='processing' LIMIT 1",
                (conversation_id,),
            ).fetchone():
                raise ValueError("当前对话仍在处理消息，请完成后再管理会话。")
            if request.action == "rename":
                if not request.title or row["lifecycle"] == "deleted":
                    raise ValueError("请提供标题；已删除的对话须先恢复。")
                db.execute(
                    "UPDATE conversations SET title=?,updated_at=? WHERE id=?",
                    (clean(request.title), now(), conversation_id),
                )
            else:
                target = {"archive": "archived", "delete": "deleted", "restore": "active"}[
                    request.action
                ]
                if request.action == "archive" and row["lifecycle"] == "deleted":
                    raise ValueError("已删除的对话须先恢复。")
                if target != row["lifecycle"]:
                    db.execute(
                        "UPDATE conversations SET lifecycle=?,updated_at=? WHERE id=?",
                        (target, now(), conversation_id),
                    )
        return self.get(conversation_id)

    def context(self, conversation_id, status=None):
        self.get(conversation_id, messages=False)
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        return RoutingContext(
            active_id=row["task_id"],
            active_kind=row["task_kind"],
            active_status=status,
            touched_at=row["touched_at"],
            pending=PendingChoice.model_validate_json(row["pending"]) if row["pending"] else None,
        )

    def begin(self, conversation_id, request: UserMessage):
        current = self.get(conversation_id, messages=False)
        text = clean(request.text)
        digest = hashlib.sha256(
            (
                text + ("\0" + str(request.max_new_tokens) if current.mode == "model" else "")
            ).encode()
        ).hexdigest()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT lifecycle FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()["lifecycle"]
                != "active"
            ):
                raise ValueError("请先恢复对话，再发送消息。")
            row = db.execute(
                "SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=?",
                (conversation_id, request.request_id),
            ).fetchone()
            if row:
                if row["input_sha"] != digest:
                    raise ValueError("Idempotency key reused with different text")
                return row["status"]
            if db.execute(
                "SELECT 1 FROM conversation_turns WHERE conversation_id=? AND status='processing' LIMIT 1",
                (conversation_id,),
            ).fetchone():
                raise ValueError(
                    "A previous turn is still processing; inspect before sending another"
                )
            db.execute(
                "INSERT INTO conversation_turns(conversation_id,request_id,input_sha,status,route) VALUES(?,?,?,?,?)",
                (conversation_id, request.request_id, digest, "processing", None),
            )
            db.execute(
                "INSERT INTO conversation_messages(conversation_id,turn_id,role,text,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, request.request_id, "user", text, now()),
            )
            db.execute(
                "UPDATE conversations SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated_at=? WHERE id=?",
                (text.replace("\n", " ")[:36], now(), conversation_id),
            )
        return "new"

    def finish(self, conversation_id, request_id, text, decision, binding=None, generation=None):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO conversation_messages(conversation_id,turn_id,role,text,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, request_id, "assistant", clean(text)[:20000], now()),
            )
            db.execute(
                "UPDATE conversation_turns SET status='done',route=? WHERE conversation_id=? AND request_id=?",
                (decision.model_dump_json(), conversation_id, request_id),
            )
            if generation:
                db.execute(
                    "UPDATE conversation_turns SET generation=? WHERE conversation_id=? AND request_id=?",
                    (json.dumps(clean(generation)), conversation_id, request_id),
                )
            db.execute(
                "UPDATE conversations SET pending=?,updated_at=? WHERE id=?",
                (
                    decision.pending.model_dump_json() if decision.pending else None,
                    now(),
                    conversation_id,
                ),
            )
            if binding:
                db.execute(
                    "UPDATE conversations SET task_kind=?,task_id=?,touched_at=? WHERE id=?",
                    (binding.kind, binding.id, time.time(), conversation_id),
                )
            elif decision.intent in {"status", "control"} and not decision.needs_clarification:
                db.execute(
                    "UPDATE conversations SET touched_at=? WHERE id=?",
                    (time.time(), conversation_id),
                )
        return self.get(conversation_id)
