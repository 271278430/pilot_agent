import hashlib
import json
import uuid

from pilot_collab.store import Database, clean
from repopilot.storage import atomic_json, now

from .contracts import ApplicationView


class AppStore:
    def __init__(self, root):
        self.root = root.resolve()
        self.db = Database(self.root)
        with self.db.connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,title TEXT,status TEXT,plan TEXT,state TEXT,created_at TEXT);
              CREATE TABLE IF NOT EXISTS application_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,application_id TEXT,role TEXT,kind TEXT,data TEXT,created_at TEXT);
              CREATE TABLE IF NOT EXISTS application_operations(id TEXT PRIMARY KEY,application_id TEXT,operation TEXT,status TEXT,result TEXT);
              CREATE TABLE IF NOT EXISTS application_plan_revisions(application_id TEXT,request_id TEXT,payload TEXT,PRIMARY KEY(application_id,request_id));
              CREATE TABLE IF NOT EXISTS application_budget_updates(application_id TEXT,request_id TEXT,payload TEXT,PRIMARY KEY(application_id,request_id));""")

    def directory(self, id):
        self.db.directory(id)
        path = self.root / "runs/apps" / id
        if any(
            p.is_symlink() for p in (self.root / "runs", self.root / "runs/apps", path)
        ) or not path.resolve().is_relative_to(self.root):
            raise ValueError("Application directory escape")
        return path

    def path(self, id, relative):
        """Recheck every ancestor at use time, including replaced workspace directories."""
        from pathlib import Path

        part = Path(relative)
        if part.is_absolute() or ".." in part.parts:
            raise ValueError("Application path escape")
        directory = self.directory(id)
        target = directory / part
        current = target
        while current != directory:
            if current.is_symlink():
                raise ValueError("Application symlink is not allowed")
            current = current.parent
        if not target.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Application path escape")
        return target

    def get(self, id):
        self.directory(id)
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM applications WHERE id=?", (id,)).fetchone()
        if not row:
            raise KeyError(id)
        return ApplicationView(
            id=id,
            title=row["title"],
            status=row["status"],
            plan=json.loads(row["plan"]),
            state=json.loads(row["state"]),
        )

    def create(self, request):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT id,plan FROM applications WHERE request_id=?", (request.request_id,)
            ).fetchone()
            if row:
                if json.loads(row["plan"]) != request.plan.model_dump():
                    raise ValueError("Request key reused with another app plan")
                id = row["id"]
            else:
                id = uuid.uuid4().hex
                directory = self.directory(id)
                directory.mkdir(parents=True)
                c.execute(
                    "INSERT INTO applications VALUES(?,?,?,?,?,?,?)",
                    (
                        id,
                        request.request_id,
                        clean(request.plan.title),
                        "planned",
                        request.plan.model_dump_json(),
                        json.dumps({"sessions": [], "rounds": 0, "plan_version": 1}),
                        now(),
                    ),
                )
                atomic_json(directory / "plan.json", request.plan.model_dump())
                atomic_json(
                    directory / "aggregate-meter.json",
                    {
                        "steps": 0,
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "estimated": False,
                    },
                )
        return self.get(id)

    def actions(self, row, connection=None):
        """The same lifecycle capabilities drive tools and visible controls."""
        if connection is None:
            with self.db.connect() as c:
                return self.actions(row, c)
        unknown = bool(connection.execute(
            "SELECT 1 FROM application_operations WHERE application_id=? AND status='running' LIMIT 1",
            (row.id,),
        ).fetchone())
        operated = bool(connection.execute(
            "SELECT 1 FROM application_operations WHERE application_id=? LIMIT 1", (row.id,),
        ).fetchone())
        executed = operated or any(row.state.get(k) for k in (
            "execution_started", "harness_request_id", "model", "candidate", "source_test", "deployment",
        ))
        executed = executed or any(self.path(row.id, name).exists() for name in (
            "model-connection.json", "model/worker/launch.json", "candidates",
        ))
        editable = row.status == "planned" and not executed and not unknown
        feedback = row.status in {"completed", "failed", "paused"} and not unknown and len(row.state.get("feedback", [])) < 5
        return {
            "edit_plan": editable,
            "feedback": feedback,
            "start": row.status in {"planned", "paused", "failed"} and not unknown,
            "pause": row.status == "running",
            "cancel": row.status in {"planned", "running", "paused", "failed"},
            "preview": row.status == "completed" and bool((row.state.get("verification") or {}).get("passed")),
            "edit_budget": row.status in {"planned", "paused", "failed", "completed"} and not unknown,
            "edit_reason": "" if editable else "执行后的方案已锁定；修改应用功能请提交修改要求。切换模型需要另建方案。",
            "blocked_reason": "上一次操作结果尚未确认，请检查执行记录后再继续。" if unknown else "",
        }

    def change_budget(self, id, request):
        """Owner control only. Keep the cumulative ledger and execution identity."""
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            payload = request.model_dump_json()
            previous = c.execute("SELECT payload FROM application_budget_updates WHERE application_id=? AND request_id=?",
                                 (id, request.request_id)).fetchone()
            if previous:
                if previous[0] != payload:
                    raise ValueError("预算请求标识已用于不同设置，请读取最新预算。")
                return self.get(id)
            row = self.get(id)
            if not self.actions(row, c)["edit_budget"]:
                raise ValueError("执行中或存在未确认操作，不能修改预算。请先暂停交付。")
            policy = row.state.get("budget_policy") or {"max_tokens": 500000, "revision": 1}
            if request.expected_revision != policy["revision"]:
                raise ValueError("预算已在其他位置修改，请读取最新设置。")
            updated = {"max_tokens": request.max_tokens, "revision": policy["revision"] + 1}
            state = {**row.state, "budget_policy": updated}
            c.execute("UPDATE applications SET state=? WHERE id=?", (json.dumps(state), id))
            c.execute("INSERT INTO application_budget_updates VALUES(?,?,?)", (id, request.request_id, payload))
            c.execute("INSERT INTO application_events(application_id,role,kind,data,created_at) VALUES(?,?,?,?,?)",
                      (id, "operator", "budget_changed", json.dumps({"previous": policy, "current": updated}), now()))
            atomic_json(self.directory(id) / "budget-policy.json", updated)
        return self.get(id)

    def revise_plan(self, id, request, validate=None):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self.get(id)
            payload = request.model_dump_json()
            retry = c.execute(
                "SELECT payload FROM application_plan_revisions WHERE application_id=? AND request_id=?",
                (id, request.request_id),
            ).fetchone()
            if retry:
                if json.loads(retry[0]) != json.loads(payload):
                    raise ValueError("Revision key reused with another plan")
                return row
            actions = self.actions(row, c)
            if not actions["edit_plan"]:
                raise ValueError(actions["blocked_reason"] or actions["edit_reason"])
            version = row.state.get("plan_version", 1)
            if request.expected_version != version:
                raise ValueError("方案已被更新，请重新读取当前方案后再保存。")
            if request.plan.project_id != row.plan.project_id:
                raise ValueError("修订不能更换所属项目，请另建方案。")
            if validate:
                validate(request.plan)
            state = {**row.state, "plan_version": version + 1}
            c.execute("UPDATE applications SET title=?,plan=?,state=? WHERE id=?", (
                clean(request.plan.title), request.plan.model_dump_json(), json.dumps(state), id,
            ))
            c.execute("INSERT INTO application_plan_revisions VALUES(?,?,?)", (id, request.request_id, payload))
            c.execute("INSERT INTO application_events(application_id,role,kind,data,created_at) VALUES(?,?,?,?,?)", (
                id, "operator", "plan_revised", json.dumps({"previous_version": version,
                    "version": version + 1, "previous_plan": row.plan.model_dump(),
                    "plan": request.plan.model_dump()}, ensure_ascii=False), now(),
            ))
            # This draft has no executable effects. Keep the companion file
            # consistent before committing, and never touch Session history.
            atomic_json(self.directory(id) / "plan.json", request.plan.model_dump())
        return self.get(id)

    def start(self, id, expected_version=None):
        import time

        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self.get(id)
            if not self.actions(row, c)["start"]:
                raise ValueError("当前状态不能开始，或上一次操作结果尚未确认 (unknown outcome)。")
            if expected_version is not None and expected_version != row.state.get("plan_version", 1):
                raise ValueError("方案已被更新，请重新查看后再开始交付。")
            state = {**row.state, "execution_started": True, "deadline_epoch": time.time() + 1200,
                     "previous_interruption": row.state.get("budget_stop") or row.state.get("error"),
                     "error": None, "budget_stop": None,
                     "harness_request_id": "delivery-" + id + "-" + uuid.uuid4().hex}
            c.execute("UPDATE applications SET status='running',state=? WHERE id=?", (json.dumps(state), id))
        atomic_json(self.directory(id) / "runtime-status.json", {"status": "running"})
        return self.get(id)

    def stop(self, id, operation):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self.get(id)
            if not self.actions(row, c)[operation]:
                raise ValueError("当前状态不能暂停或取消，请读取最新状态。")
            status = "paused" if operation == "pause" else "cancelled"
            c.execute("UPDATE applications SET status=? WHERE id=?", (status, id))
        atomic_json(self.directory(id) / "runtime-status.json", {"status": status})
        return self.get(id)

    def update(self, id, status=None, **state):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status,state FROM applications WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            current = json.loads(row["state"])
            current.update(clean(state))
            c.execute(
                "UPDATE applications SET status=?,state=? WHERE id=?",
                (status or row["status"], json.dumps(current), id),
            )
        row = self.get(id)
        atomic_json(self.directory(id) / "runtime-status.json", {"status": row.status})
        return row

    def event(self, id, role, kind, data):
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO application_events(application_id,role,kind,data,created_at) VALUES(?,?,?,?,?)",
                (id, role, kind, json.dumps(clean(data)), now()),
            )

    def complete(self, id):
        """Commit completion without overwriting a concurrent user pause/cancel."""
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status,state FROM applications WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "running":
                raise ValueError("Application stopped before completion")
            state = json.loads(row["state"])
            verification = state.get("verification") or {}
            if not verification.get("passed") or verification.get("candidate") != (
                state.get("candidate") or {}
            ).get("id"):
                raise ValueError("Accepted candidate changed before completion")
            state.update(error=None, last_error=None)
            c.execute(
                "UPDATE applications SET status='completed',state=? WHERE id=?",
                (json.dumps(state), id),
            )
        return self.get(id)

    def events(self, id):
        self.get(id)
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT * FROM application_events WHERE application_id=? ORDER BY seq DESC LIMIT 100",
                (id,),
            ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in reversed(rows)]

    def claim(self, id, operation):
        key = uuid.uuid4().hex
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM applications WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "running":
                raise ValueError("Application stopped before operation admission")
            if c.execute(
                "SELECT 1 FROM application_operations WHERE application_id=? AND status='running' LIMIT 1",
                (id,),
            ).fetchone():
                raise ValueError(
                    "An earlier application operation has unknown outcome; inspect before continuing"
                )
            c.execute(
                "INSERT INTO application_operations VALUES(?,?,?,?,?)",
                (key, id, operation, "running", None),
            )
        return key

    def feedback(self, id, request):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status,state FROM applications WHERE id=?", (id,)).fetchone()
            if not row:
                raise KeyError(id)
            state = json.loads(row["state"])
            history = state.get("feedback", [])
            old = next((r for r in history if r["request_id"] == request.request_id), None)
            if old:
                if old["message"] != request.message:
                    raise ValueError("Feedback key reused with other text")
            else:
                if row["status"] not in {"completed", "failed", "paused"}:
                    raise ValueError("Finish current execution before revising")
                if c.execute("SELECT 1 FROM application_operations WHERE application_id=? AND status='running'", (id,)).fetchone():
                    raise ValueError("上一次操作结果尚未确认，请检查后再保存修改。")
                if len(history) >= 5:
                    raise ValueError("Revision budget reached; start a new application goal")
                state.update(
                    feedback=[
                        *history,
                        {
                            "request_id": request.request_id,
                            "message": clean(request.message),
                            "candidate": (state.get("candidate") or {}).get("id"),
                        },
                    ],
                    verification=None,
                    error=None,
                    last_error=None,
                )
                c.execute(
                    "UPDATE applications SET status='paused',state=? WHERE id=?",
                    (json.dumps(state), id),
                )
        return self.get(id)

    def settle(self, key, result):
        with self.db.connect() as c:
            c.execute(
                "UPDATE application_operations SET status='done',result=? WHERE id=?",
                (json.dumps(clean(result)), key),
            )


def source_hash(directory):
    from .workspace import source_files

    h = hashlib.sha256()
    files = source_files(directory)
    for name in ("app.py", "index.html", "model_client.py"):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing or unsafe application source")
        h.update(name.encode() + b"\0" + path.read_bytes())
    # Keep the historical three-file digest for old candidates. Added source
    # participates in identity and invalidates an earlier passing test.
    for name in files:
        if name not in {"app.py", "index.html", "model_client.py"}:
            h.update(name.encode() + b"\0" + (directory / name).read_bytes())
    return h.hexdigest()
