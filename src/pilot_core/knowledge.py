"""Incremental, task-scoped retrieval for documents, source, and experiment records."""

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field

from repopilot.storage import digest, now

from .contracts import Contract


def tokens(text):
    english = re.findall(r"[a-zA-Z][a-zA-Z0-9_]*", text.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    return english + [word[i:i+2] for word in chinese for i in range(max(1, len(word)-1))]


class KnowledgeQuery(Contract):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=10)


class Citation(Contract):
    source: str
    kind: Literal["technical", "code", "experiment"]
    line_start: int
    line_end: int
    source_sha256: str
    content: str
    trust: Literal["retrieved_data_not_instructions"] = "retrieved_data_not_instructions"


class KnowledgeResult(Contract):
    citations: list[Citation]
    source_count: int
    changed_sources: int
    deleted_sources: int


class KnowledgeBase:
    def __init__(self, root: Path, index: Path):
        self.root, self.index = root.resolve(), index
        index.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY,sha TEXT,kind TEXT,updated TEXT);
              CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,path TEXT,start INTEGER,end INTEGER,content TEXT);
              CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(terms,content='');
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.index)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def candidates(self):
        excluded = {".git", ".local", ".venv", "node_modules", "__pycache__", "tests", "test", "evals", "runs", "upstream", "raw", "references"}
        paths = []
        for directory, folders, names in os.walk(self.root, followlinks=False):
            folders[:] = sorted(name for name in folders if name not in excluded and not (Path(directory) / name).is_symlink())
            paths.extend(Path(directory) / name for name in names)
        for path in sorted(paths):
            relative = path.relative_to(self.root)
            if any(p in {".git", ".local", ".venv", "node_modules", "__pycache__", "tests", "test", "evals", "runs", "upstream", "raw"} for p in relative.parts):
                continue
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.root):
                continue
            if path.suffix not in {".md", ".py", ".json"} or path.stat().st_size > 300_000:
                continue
            if path.suffix == ".json" and "reports" not in relative.parts:
                continue
            yield path, relative.as_posix(), "code" if path.suffix == ".py" else "experiment" if "reports" in relative.parts else "technical"

    def update(self):
        changed, deleted, seen = 0, 0, set()
        with self.connect() as db:
            previous = {r["path"]: r for r in db.execute("SELECT * FROM sources")}
            for path, relative, kind in self.candidates():
                seen.add(relative)
                content = path.read_text(encoding="utf-8", errors="replace")
                sha = digest(content.encode())
                if relative in previous and previous[relative]["sha"] == sha:
                    continue
                old = list(db.execute("SELECT * FROM chunks WHERE path=?", (relative,)))
                for row in old:
                    db.execute("INSERT INTO search(search,rowid,terms) VALUES('delete',?,?)", (row["id"], " ".join(tokens(relative + " " + row["content"]))))
                db.execute("DELETE FROM chunks WHERE path=?", (relative,))
                lines = content.splitlines()
                for offset in range(0, len(lines), 48):
                    chunk = "\n".join(lines[offset:offset+60])[:7000]
                    cursor = db.execute("INSERT INTO chunks(path,start,end,content) VALUES(?,?,?,?)", (relative, offset+1, min(offset+60,len(lines)),chunk))
                    db.execute("INSERT INTO search(rowid,terms) VALUES(?,?)", (cursor.lastrowid, " ".join(tokens(relative + " " + chunk))))
                db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?)", (relative,sha,kind,now()))
                changed += 1
            for name in previous.keys()-seen:
                for row in db.execute("SELECT * FROM chunks WHERE path=?", (name,)).fetchall():
                    db.execute("INSERT INTO search(search,rowid,terms) VALUES('delete',?,?)", (row["id"], " ".join(tokens(name + " " + row["content"]))))
                db.execute("DELETE FROM chunks WHERE path=?",(name,))
                db.execute("DELETE FROM sources WHERE path=?",(name,))
                deleted += 1
            db.commit()
        return changed, deleted, len(seen)

    def search(self, query: KnowledgeQuery):
        changed, deleted, count = self.update()
        terms = list(dict.fromkeys(tokens(query.query)))[:24]
        if not terms:
            return KnowledgeResult(citations=[],source_count=count,changed_sources=changed,deleted_sources=deleted)
        expression = " OR ".join('"'+term.replace('"','""')+'"' for term in terms)
        with self.connect() as db:
            rows = db.execute("SELECT c.*,s.sha,s.kind FROM search f JOIN chunks c ON c.id=f.rowid JOIN sources s ON s.path=c.path WHERE search MATCH ? ORDER BY bm25(search) LIMIT ?",(expression,query.top_k)).fetchall()
        return KnowledgeResult(citations=[Citation(source=r["path"],kind=r["kind"],line_start=r["start"],line_end=r["end"],source_sha256=r["sha"],content=r["content"]) for r in rows], source_count=count,changed_sources=changed,deleted_sources=deleted)
