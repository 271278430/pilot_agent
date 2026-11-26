"""Private immutable manifests, serialized trial admission and durable phases."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from pilot_infra.store import digest
from repopilot.storage import atomic_json

from .contracts import EvaluationPlan
from .problems import VERSION, problem


class EvaluationStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / ".local/domain-evaluation"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "evaluation.sqlite3"
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS campaign(id INTEGER PRIMARY KEY,max_tokens INTEGER)")
            db.execute("INSERT OR IGNORE INTO campaign VALUES(1,1500000)")
            db.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,manifest TEXT,digest TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS trials(id TEXT PRIMARY KEY,run_id TEXT,data TEXT)")

    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def freeze(self, plan, identities):
        plan = EvaluationPlan.model_validate(plan)
        if plan.max_tokens > self.campaign()["max_tokens"]:
            raise ValueError("Plan exceeds the authorized campaign budget")
        manifest = {"protocol": VERSION, "plan": plan.model_dump(), "identities": identities,
                    "cases": [problem(identity) for identity in plan.case_ids]}
        identity = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?)", (identity, json.dumps(manifest), digest(manifest)))
        atomic_json(self.directory / (identity + "-manifest.json"), {"digest": digest(manifest), "manifest": manifest})
        return identity

    def campaign(self):
        with self.db() as db:
            cap = db.execute("SELECT max_tokens FROM campaign WHERE id=1").fetchone()[0]
            rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM trials")]
        used = sum(r.get("usage", {}).get("input_tokens", 0) + r.get("usage", {}).get("output_tokens", 0) for r in rows)
        return {"max_tokens": cap, "used_tokens": used, "scope": "all runs in this isolated campaign; controller family usage"}

    def manifest(self, identity):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Unknown evaluation run")
        value = json.loads(row["manifest"])
        if digest(value) != row["digest"]:
            raise ValueError("Evaluation manifest changed")
        return value

    def trials(self, run_id):
        self.manifest(run_id)
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM trials WHERE run_id=? ORDER BY rowid", (run_id,))]

    def admit(self, run_id, case_id, strategy, repetition):
        manifest = self.manifest(run_id)
        plan = manifest["plan"]
        if case_id not in plan["case_ids"] or strategy not in plan["strategies"] or not 1 <= repetition <= plan["repetitions"]:
            raise ValueError("Trial is outside the frozen plan")
        key = digest([run_id, case_id, strategy, repetition])[:32]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT data FROM trials WHERE id=?", (key,)).fetchone()
            if previous:
                return json.loads(previous[0]), False
            all_values = [json.loads(row[0]) for row in db.execute("SELECT data FROM trials")]
            values = [row for row in all_values if row["run_id"] == run_id]
            if any(row["phase"] not in {"settled", "unavailable"} for row in all_values):
                raise ValueError("An original trial is unsettled; inspect it before another admission")
            spent = sum(row.get("usage", {}).get("input_tokens", 0) + row.get("usage", {}).get("output_tokens", 0) for row in values)
            campaign_spent = sum(row.get("usage", {}).get("input_tokens", 0) + row.get("usage", {}).get("output_tokens", 0) for row in all_values)
            remaining = min(plan["max_tokens"] - spent, db.execute("SELECT max_tokens FROM campaign WHERE id=1").fetchone()[0] - campaign_spent)
            if remaining < 1000:
                raise ValueError("Frozen batch token budget exhausted")
            row = {"id": key, "run_id": run_id, "case_id": case_id, "strategy": strategy,
                   "repetition": repetition, "phase": "admitted", "started_at": time.time(),
                   "max_tokens": min(plan["trial_max_tokens"], remaining), "interventions": [],
                   "controller_kind": "real_model", "usage": {}, "grade": None}
            db.execute("INSERT INTO trials VALUES(?,?,?)", (key, run_id, json.dumps(row)))
        return row, True

    def get(self, identity):
        with self.db() as db:
            row = db.execute("SELECT data FROM trials WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Unknown evaluation trial")
        return json.loads(row[0])

    def update(self, identity, **changes):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM trials WHERE id=?", (identity,)).fetchone()
            if not row:
                raise ValueError("Unknown evaluation trial")
            value = json.loads(row[0])
            if value["phase"] in {"settled", "unavailable"}:
                raise ValueError("Settled evaluation receipt is immutable")
            value.update(changes)
            db.execute("UPDATE trials SET data=? WHERE id=?", (json.dumps(value), identity))
        atomic_json(self.directory / (identity + ".json"), value)
        return value
