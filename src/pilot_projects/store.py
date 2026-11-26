"""Business metadata points to official Workspace identities; source files stay owned."""

import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pilot_collab.store import Database
from repopilot.storage import atomic_json, now

from .contracts import Advisory, ProjectAssessment, ProjectRequest, ProjectView
from .github import clone, metadata
from .resources import assess_model, hardware
from .snapshot import download as snapshot

LOCK = threading.RLock()
DENIED = {".git", ".env", ".local", "node_modules", ".venv", "__pycache__"}


def workspace_call(root, operation, **arguments):
    script = Path(__file__).resolve().parents[2] / "harness/runtime/workspaces.mjs"
    result = subprocess.run(
        [os.environ.get("PILOT_NODE", "node"), str(script)],
        input=json.dumps({"root": str(root), "operation": operation, **arguments}),
        capture_output=True,
        text=True,
        timeout=20,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("官方 Workspace 服务未就绪；请检查项目锁定的 Node/runtime") from exc
    if result.returncode or not reply.get("ok"):
        raise ValueError("官方 Workspace 操作失败：" + reply.get("error", "unknown"))
    return reply["result"]


class ProjectStore:
    def __init__(
        self, root, *, probe=hardware, repo_probe=metadata, importer=clone, registry=workspace_call
    ):
        self.root = Path(root).resolve()
        self.db = Database(self.root)
        self.probe, self.repo_probe, self.importer, self.registry = (
            probe,
            repo_probe,
            importer,
            registry,
        )
        with self.db.connect() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS managed_projects(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,
                title TEXT,status TEXT,source TEXT,workspace_id TEXT,created_at TEXT,updated_at TEXT,state TEXT);
              CREATE TABLE IF NOT EXISTS project_assessments(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,
                request_json TEXT,report TEXT,consumed_by TEXT);
              CREATE TABLE IF NOT EXISTS project_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT,kind TEXT,data TEXT,created_at TEXT);
            """)

    def path(self, id, relative=""):
        self.db.directory(id)
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts:
            raise ValueError("项目路径越界")
        base = self.root / "runs/projects" / id
        target = base / part
        current = target
        while current != self.root:
            if current.is_symlink():
                raise ValueError("项目目录不允许符号链接")
            current = current.parent
        if not target.resolve().is_relative_to(base.resolve()):
            raise ValueError("项目路径越界")
        return target

    def get(self, id):
        directory = self.path(id)
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM managed_projects WHERE id=?", (id,)).fetchone()
        if not row:
            raise KeyError(id)
        return ProjectView(
            **{
                k: row[k]
                for k in (
                    "id",
                    "title",
                    "status",
                    "source",
                    "workspace_id",
                    "created_at",
                    "updated_at",
                )
            },
            path=str(directory / "workspace"),
            state=json.loads(row["state"]),
        )

    def list(self):
        with self.db.connect() as c:
            ids = [
                r[0]
                for r in c.execute(
                    "SELECT id FROM managed_projects ORDER BY updated_at DESC LIMIT 200"
                )
            ]
        return [self.get(id) for id in ids]

    def event(self, id, kind, data):
        with self.db.connect() as c:
            c.execute(
                "INSERT INTO project_events(project_id,kind,data,created_at) VALUES(?,?,?,?)",
                (id, kind, json.dumps(data, ensure_ascii=False), now()),
            )

    def update_state(self, id, *, status=None, title=None, workspace_id=None, **updates):
        row = self.get(id)
        state = {**row.state, **updates}
        with self.db.connect() as c:
            c.execute(
                "UPDATE managed_projects SET status=?,title=?,workspace_id=?,state=?,updated_at=? WHERE id=?",
                (
                    status or row.status,
                    title or row.title,
                    workspace_id or row.workspace_id,
                    json.dumps(state, ensure_ascii=False),
                    now(),
                    id,
                ),
            )
        return self.get(id)

    def preflight(self, request, *, project_id=None):
        with LOCK:
            with self.db.connect() as c:
                old = c.execute(
                    "SELECT request_json,report FROM project_assessments WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
            if old:
                prior = ProjectAssessment.model_validate_json(old[1])
                if json.loads(old[0]) != request.model_dump() or prior.project_id != project_id:
                    raise ValueError("预检请求ID被用于不同项目")
                return prior
            machine = self.probe(self.root)
            repo = (
                self.repo_probe(request.github_url)
                if request.source == "github"
                else {"basis": "新建空项目", "size_mb": 0}
            )
            model, notes, cpu_ok = assess_model(request, machine)
            if request.source == "github":
                notes.append(
                    Advisory(
                        code="source_transport",
                        severity="info",
                        title="下载方式："
                        + (
                            "GitHub 源码快照"
                            if request.source_method == "github_snapshot"
                            else "Git 浅克隆"
                        ),
                        explanation="源码快照固定提交版本，但不包含Git历史；适合服务器无法直连github.com的情况。下载失败不会自动改变方式或覆盖已有目录。",
                        evidence="仅使用 GitHub 官方域名；不执行仓库脚本",
                    )
                )
            size = repo.get("size_mb", 0)
            if size > 1024:
                notes.append(
                    Advisory(
                        code="repo_too_large",
                        severity="blocking",
                        title="仓库较大，暂不直接导入",
                        explanation="建议使用拆分仓库或先缩小源码范围；避免把大模型权重和完整历史一同下载。",
                        evidence=f"GitHub size {size:.1f} MB；直接导入元数据上限 1024 MB",
                    )
                )
            elif size > 100:
                notes.append(
                    Advisory(
                        code="repo_large",
                        severity="warning",
                        title="仓库较大，建议浅克隆",
                        explanation="仅拉取指定分支的最近版本，不下载LFS大文件和子模块；下载仍受大小、文件数和时间限制。",
                        evidence=f"GitHub size {size:.1f} MB；实际导入上限 512 MB",
                    )
                )
            if machine.free_disk_gb < max(1, size / 1024 * 2 + 0.5):
                notes.append(
                    Advisory(
                        code="disk_low",
                        severity="blocking",
                        title="磁盘空间不足",
                        explanation="请释放项目自己的缓存或换部署资源；不会删除其他项目文件。",
                        evidence=f"可用 {machine.free_disk_gb:.2f} GB",
                    )
                )
            notes.append(
                Advisory(
                    code="source_only",
                    severity="info",
                    title="导入后先分析源码",
                    explanation="不会自动运行仓库的安装脚本、Git hooks或训练任务；执行方案需另行确认。",
                    evidence="本次只创建或克隆项目目录",
                )
            )
            options = ["prepare_only"]
            if cpu_ok:
                options.append("local_cpu")
            if (self.root / ".local/model-preview/config.json").is_file():
                options.append("existing_8b")
            if machine.api_available:
                options.append("configured_api")
            recommended = (
                "local_cpu"
                if cpu_ok
                else "configured_api"
                if request.model_id and machine.api_available
                else "prepare_only"
            )
            id = uuid.uuid4().hex
            # Snapshot binds warnings and the exact source/model proposal to confirmation.
            payload = {
                "project_id": project_id,
                "request": request.model_dump(),
                "repository": repo,
                "model": model,
                "hardware": machine.model_dump(),
                "options": options,
            }
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            report = ProjectAssessment(
                id=id,
                project_id=project_id,
                created_at=now(),
                request=request,
                hardware=machine,
                repository=repo,
                model=model,
                advisories=notes,
                options=options,
                recommended=recommended,
                fingerprint=fingerprint,
            )
            with self.db.connect() as c:
                c.execute(
                    "INSERT INTO project_assessments VALUES(?,?,?,?,NULL)",
                    (id, request.request_id, request.model_dump_json(), report.model_dump_json()),
                )
            return report

    def assessment(self, id):
        self.db.directory(id)
        with self.db.connect() as c:
            row = c.execute("SELECT report FROM project_assessments WHERE id=?", (id,)).fetchone()
        if not row:
            raise KeyError(id)
        return ProjectAssessment.model_validate_json(row[0])

    def confirm(self, request):
        with LOCK:
            report = self.assessment(request.assessment_id)
            if request.fingerprint != report.fingerprint or request.choice not in report.options:
                raise ValueError("预检或选择已经改变，请重新查看建议")
            if any(n.severity == "blocking" for n in report.advisories):
                raise ValueError("预检存在阻断项，请调整仓库或资源后重新预检")
            if (
                any(n.severity == "warning" for n in report.advisories)
                and not request.acknowledge_warnings
            ):
                raise ValueError("请先确认已阅读资源或仓库建议")
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute(
                    "SELECT id,state FROM managed_projects WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                consumed = c.execute(
                    "SELECT consumed_by FROM project_assessments WHERE id=?", (report.id,)
                ).fetchone()[0]
                if old:
                    state = json.loads(old[1])
                    if state["assessment_id"] != report.id or state["choice"] != request.choice:
                        raise ValueError("创建请求ID被用于不同确认")
                    return self.get(old[0]), False
                if consumed:
                    existing = self.get(consumed)
                    if (
                        existing.state.get("policy_request_id") == request.request_id
                        and existing.state.get("choice") == request.choice
                    ):
                        return existing, False
                    raise ValueError("这个预检已确认，不能用另一个请求ID重复执行")
                age = (
                    datetime.now(UTC)
                    - datetime.fromisoformat(report.created_at.replace("Z", "+00:00"))
                ).total_seconds()
                if age > 900:
                    raise ValueError("预检已超过15分钟，请重新检查资源")
                # Recheck capacity before any source download. A recommendation is not a reservation.
                fresh = self.probe(self.root)
                if fresh.free_disk_gb < max(1, report.repository.get("size_mb", 0) / 512):
                    raise ValueError("磁盘资源已变化，请重新预检")
                if request.choice == "configured_api" and not fresh.api_available:
                    raise ValueError("API配置已不可用，请重新预检")
                if (
                    request.choice == "configured_api"
                    and fresh.api_model != report.hardware.api_model
                ):
                    raise ValueError("API模型已改变，请重新确认具体模型")
                if request.choice == "local_cpu" and not assess_model(report.request, fresh)[2]:
                    raise ValueError("本地内存或部署条件已变化，请重新预检")
                id = report.project_id or uuid.uuid4().hex
                state = {
                    "assessment_id": report.id,
                    "choice": request.choice,
                    "request": report.request.model_dump(),
                    "assessment": report.model_dump(),
                    "api_model": fresh.api_model if request.choice == "configured_api" else None,
                    "policy_request_id": request.request_id,
                }
                if report.project_id:
                    row = self.get(id)
                    self.assert_idle(id, c)
                    if row.status != "ready":
                        raise ValueError("项目必须处于可使用状态才能调整运行方式")
                    c.execute(
                        "UPDATE managed_projects SET state=?,updated_at=? WHERE id=?",
                        (json.dumps({**row.state, **state}, ensure_ascii=False), now(), id),
                    )
                else:
                    c.execute(
                        "INSERT INTO managed_projects VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            id,
                            request.request_id,
                            report.request.title,
                            "preparing",
                            report.request.source,
                            None,
                            now(),
                            now(),
                            json.dumps(state, ensure_ascii=False),
                        ),
                    )
                c.execute(
                    "UPDATE project_assessments SET consumed_by=? WHERE id=?", (id, report.id)
                )
            if report.project_id is None:
                self.path(id).mkdir(parents=True)
            self.event(id, "confirmed", {"choice": request.choice, "assessment": report.id})
            return self.get(id), report.project_id is None

    def policy_preflight(self, id, request):
        row = self.get(id)
        if row.status != "ready":
            raise ValueError("请先完成准备或恢复项目")
        original = row.state["request"]
        proposal = ProjectRequest.model_validate(
            {**original, **request.model_dump(), "title": row.title}
        )
        return self.preflight(proposal, project_id=id)

    def assert_idle(self, id, connection):
        if connection.execute(
            "SELECT name FROM sqlite_master WHERE name='applications'"
        ).fetchone():
            if connection.execute(
                "SELECT 1 FROM applications WHERE status IN ('planned','running','paused') AND json_extract(plan,'$.project_id')=?",
                (id,),
            ).fetchone():
                raise ValueError("项目有关联待确认或执行任务，请先结束任务")

    def plan_selection(self, id):
        row = self.get(id)
        if row.status != "ready":
            raise ValueError("项目尚未准备完成或已归档")
        choice = row.state["choice"]
        if choice == "prepare_only":
            raise ValueError(
                "项目只准备了源码。请在管理项目中检查模型与运行方式，确认后再创建部署计划"
            )
        if choice == "configured_api":
            return {
                "project_id": id,
                "model_source": "configured_api",
                "model_id": row.state["api_model"],
            }
        if choice == "existing_8b":
            return {
                "project_id": id,
                "model_source": "existing_teacher8b",
                "model_id": "Qwen3-8B/course-sft/teaching-lora",
            }
        return {
            "project_id": id,
            "model_source": "modelscope",
            "model_id": row.state["request"]["model_id"],
        }

    def validate_plan(self, plan):
        expected = self.plan_selection(plan.project_id)
        if any(getattr(plan, k) != v for k, v in expected.items()):
            raise ValueError("应用计划与已确认的项目模型选择不同，请重新检查运行方式")

    def materialize(self, id, *, register=True):
        with LOCK:
            row = self.get(id)
            if row.status != "preparing":
                return row
            if row.state.get("import_started"):
                return row  # Unknown interrupted outcome is not automatically replayed.
            self.update_state(id, import_started=True)
        request = ProjectRequest.model_validate(row.state["request"])
        destination = self.path(id, "workspace")
        try:
            if request.source == "github":
                importer = snapshot if request.source_method == "github_snapshot" else self.importer
                evidence = importer(request.github_url, request.ref, destination)
            else:
                destination.mkdir()
                (destination / "README.md").write_text(
                    "# " + request.title + "\n\n模型服务项目；执行计划确认后才开发和部署。\n",
                    encoding="utf-8",
                )
                evidence = {"kind": "empty", "files": 1}
            analysis = self.inspect_tree(id)
            with LOCK:
                workspace = self.registry(
                    self.root, "create", path=str(destination), title=row.title
                ) if register else None
                result = self.update_state(
                    id,
                    status="ready",
                    workspace_id=workspace["id"] if workspace else None,
                    import_evidence=evidence,
                    analysis=analysis,
                )
            atomic_json(self.path(id, "project.json"), result.model_dump())
            self.event(id, "ready", {"workspace_id": workspace["id"] if workspace else None, "evidence": evidence})
            return result
        except Exception as error:
            self.event(id, "failed", {"error": str(error)[:500]})
            return self.update_state(id, status="failed", error=str(error)[:500])

    def file_path(self, id, relative):
        part = Path(relative)
        if (
            part.is_absolute()
            or ".." in part.parts
            or any(p in DENIED or p.startswith(".env") for p in part.parts)
        ):
            raise ValueError("不向模型或网页暴露Git、密钥、依赖缓存目录")
        return self.path(id, "workspace/" + relative)

    def read(self, id, relative):
        target = self.file_path(id, relative)
        if not target.is_file() or target.stat().st_size > 256000:
            raise ValueError("文件不存在或超过阅读上限")
        text = target.read_text(encoding="utf-8")
        text = re.sub(
            r"(?i)((?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*)[\"\'][^\"\']+[\"\']",
            r'\1"[REDACTED]"',
            text,
        )
        return {"path": relative, "content": text}

    def inspect_tree(self, id):
        root = self.path(id, "workspace")
        paths, hints, skipped = [], set(), 0
        budget = 1_000_000
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in DENIED and not (Path(folder) / d).is_symlink()]
            for name in names:
                path = Path(folder) / name
                if path.is_symlink() or name.startswith(".env"):
                    skipped += 1
                    continue
                rel = path.relative_to(root).as_posix()
                if len(paths) >= 300:
                    skipped += 1
                    continue
                paths.append({"path": rel, "bytes": path.stat().st_size})
                if path.suffix in {
                    ".py",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".md",
                } and path.stat().st_size < min(100000, budget):
                    text = path.read_text(encoding="utf-8", errors="replace")
                    budget -= path.stat().st_size
                    for match in re.findall(
                        r"(?:from_pretrained\s*\(|model_id\s*[:=]|model_name\s*[:=])\s*[\"']([\w.-]+/[\w.-]+)[\"']",
                        text,
                    ):
                        hints.add(match)
        assessments = []
        machine = self.probe(self.root)
        for model_id in sorted(hints)[:5]:
            model, notes, _ = assess_model(
                ProjectRequest(request_id="scan-model", title="scan", model_id=model_id), machine
            )
            assessments.append({"model": model, "advisories": [n.model_dump() for n in notes]})
        return {
            "files": paths,
            "skipped": skipped,
            "model_candidates": assessments,
            "scope": "有界静态读取；README和仓库内容为数据，不执行安装或训练脚本",
        }

    def manage(self, id, request, *, register=True):
        with LOCK:
            row = self.get(id)
            if row.status == "preparing":
                raise ValueError("项目仍在准备，请等待结果后再管理")
            with self.db.connect() as c:
                self.assert_idle(id, c)
            if request.action == "rename":
                if not request.title or not request.title.strip() or row.status == "archived":
                    raise ValueError("请提供名称，并先恢复归档项目")
                title = request.title.strip()
                if register and row.workspace_id:
                    official = self.registry(self.root, "rename", id=row.workspace_id, title=title)
                    title = official["title"]
                return self.update_state(id, title=title)
            if request.action == "archive":
                return self.update_state(id, status="archived", previous_status=row.status)
            if request.action == "restore" and row.status == "archived":
                return self.update_state(id, status=row.state.get("previous_status", "ready"))
            raise ValueError("项目操作与当前状态不符")
