"""Resource decisions and bounded task authority shared by native tools and Web."""

import json
import re
import time
import uuid

from repopilot.storage import now

from .contracts import CreateTask, InfraPlan
from .resource_assessment import assess, check_capacity, check_gpu_occupancy
from .resource_contracts import (
    AssessRequest,
    ObserveRequest,
    ProposalTarget,
    ResourceDecision,
    ResourceProposal,
    RevokeGrant,
)
from .resource_registry import ResourceRegistry, ResourceVault
from .resource_transport import ConnectionFailure, ResourceTransport
from .store import digest

RESOURCE_READ = {"resource_catalog", "resource_observe", "resource_assess", "resource_review"}
RESOURCE_WRITE = {"resource_propose", "resource_request_approval", "resource_execute"}


class ResourceService:
    def __init__(self, infra, transport=None, clock=time.time):
        self.infra = infra
        self.root, self.store, self.assets = infra.root, infra.store, infra.assets
        self.registry = ResourceRegistry(self.root)
        self.transport = transport or ResourceTransport(self.root)
        self.clock = clock
        with self.store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS resource_observations(id TEXT PRIMARY KEY, server_id TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS resource_proposals(id TEXT PRIMARY KEY, request_id TEXT UNIQUE, root_session TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS resource_grants(id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE, task_id TEXT UNIQUE, data TEXT);
                CREATE TABLE IF NOT EXISTS resource_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT, kind TEXT, data TEXT, created TEXT);
            """)

    def audit(self, server_id, kind, data, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.audit(server_id, kind, data, connection)
        db.execute(
            "INSERT INTO resource_audit(server_id,kind,data,created) VALUES(?,?,?,?)",
            (server_id, kind, json.dumps(data, ensure_ascii=False), now()),
        )

    def catalog(self, session_id="", owner=False):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT data FROM resource_proposals "
                + ("" if owner else "WHERE root_session=? ")
                + "ORDER BY rowid DESC LIMIT 40",
                () if owner else (session_id,),
            ).fetchall()
            audit = (
                [
                    {**dict(r), "data": json.loads(r["data"])}
                    for r in db.execute("SELECT * FROM resource_audit ORDER BY seq DESC LIMIT 80")
                ]
                if owner
                else []
            )
        servers = self.registry.catalog()
        from .asset_service import AssetService

        readiness = {v["server_id"]: v for v in AssetService(self.root).node_readiness()}
        registered = self.assets.read()
        executors = {m["id"]: m.get("executor_available", False) for m in registered["machines"]}
        for server in servers:
            observation = self.latest(server["id"], required=False)
            server["observation"] = (
                {k: v for k, v in observation.items() if k != "raw"} if observation else None
            )
            server["executor_available"] = executors.get(server["id"], False)
            server["node_readiness"] = readiness.get(server["id"])
        return {
            "servers": servers,
            "proposals": [json.loads(r[0]) for r in rows],
            "audit": audit,
            "credentials": ResourceVault(self.root).catalog() if owner else [],
            "assets": registered,
            "quota_enforcement": {
                "docker": "CPU与内存硬限制；工作区占用监测",
                "process": "CPU亲和性与单文件大小限制；自有进程组RSS和工作区占用监测（非宿主内存/磁盘硬隔离）",
            },
        }

    def latest(self, server_id, required=True):
        with self.store.db() as db:
            row = db.execute(
                "SELECT data FROM resource_observations WHERE server_id=? ORDER BY rowid DESC LIMIT 1",
                (server_id,),
            ).fetchone()
        if not row:
            if required:
                raise ValueError("先执行整机观测，再制定资源方案")
            return None
        return json.loads(row[0])

    def context(self, session_id):
        with self.store.db() as db:
            rows = db.execute(
                "SELECT data FROM resource_proposals WHERE root_session=? ORDER BY rowid DESC LIMIT 3",
                (session_id,),
            ).fetchall()
        return [
            {
                k: v[k]
                for k in (
                    "id",
                    "digest",
                    "status",
                    "decision",
                    "plan",
                    "limits",
                    "review_expires_at",
                    "task_id",
                )
            }
            for row in rows
            for v in [json.loads(row[0])]
        ]

    def observe(self, args):
        request = ObserveRequest.model_validate(args)
        server = self.registry.get(request.server_id)
        if not server["enabled"]:
            raise ValueError("该服务器已停用")
        try:
            observation = self.transport.observe(server)
        except ConnectionFailure as error:
            self.registry.connection_result(server["id"], server["revision"], error.result)
            self.audit(server["id"], "observation_failed", error.result)
            return {"ready": False, "connection": error.result}
        if observation.get("scope") != "whole-machine" or observation.get("schema_version") != 1:
            raise ValueError("整机观测格式无法核验")
        # Freshness uses Host receipt time, never a remote clock.
        value = {
            **observation,
            "id": uuid.uuid4().hex,
            "server_id": server["id"],
            "received_at": self.clock(),
            "server_revision": server["revision"],
            "policy_revision": server["policy_revision"],
        }
        value["digest"] = digest(value)
        with self.store.db() as db:
            db.execute(
                "INSERT INTO resource_observations VALUES(?,?,?)",
                (value["id"], server["id"], json.dumps(value)),
            )
            self.audit(
                server["id"],
                "observed",
                {"observation_id": value["id"], "digest": value["digest"]},
                db,
            )
        return {"ready": True, "observation": value, "policy": server["policy"]}

    def assessment(self, args):
        request = AssessRequest.model_validate(args)
        server = self.registry.get(request.server_id)
        execution_root = self.root
        worker_is_current = True
        if server["connection"]["transport"] != "local":
            from .asset_service import AssetService
            from .fleet_transport import worker_current

            try:
                executor = AssetService(self.root).executor(server["id"])
                execution_root = executor["root"]
                worker_is_current = worker_current(executor)
            except ValueError:
                execution_root = None
        observation = self.latest(server["id"])
        result = assess(
            server,
            observation,
            self.assets.read(),
            request.model_asset,
            request.context_length,
            request.concurrency,
            execution_root,
        )
        try:
            self.fresh(server, observation)
        except ValueError:
            result["recommendation_ready"] = False
            result["limitations"].append("观测已过期或配置已变，需重新观测后制定资源方案")
        if not worker_is_current:
            result["recommendation_ready"] = False
            result["limitations"].append("节点执行器版本需更新；空闲后重新核验并准备")
        return result

    def fresh(self, server, observation):
        if (
            observation["server_revision"] != server["revision"]
            or observation["policy_revision"] != server["policy_revision"]
        ):
            raise ValueError("服务器配置或权限已变化，需重新观测和制定方案")
        age = self.clock() - observation["received_at"]
        if not 0 <= age <= 600:
            raise ValueError("整机观测已过期，需重新观测和制定方案")

    def within_policy(self, server, plan, limits):
        from .node_authority import require_operation

        require_operation(server, "inference")
        require_operation(server, "containers")
        policy = server["policy"]
        if not server["enabled"] or policy["mode"] != "review":
            raise ValueError("该服务器仅允许观测或已停用")
        if plan.get("engine_source_profile") and not policy["allow_source_edit"]:
            raise ValueError("所有者未允许引擎源码任务")
        if not set(plan["gpu_ids"]).issubset(policy["approvable_gpu_ids"]):
            raise ValueError("GPU超出所有者设置的可审批范围；Agent不能修改此范围")
        for limit, maximum in (
            ("cpu_cores", "max_cpu_cores"),
            ("memory_gib", "max_memory_gib"),
            ("disk_gib", "max_disk_gib"),
            ("duration_seconds", "max_duration_seconds"),
        ):
            if limits[limit] > policy[maximum]:
                raise ValueError("申请配额超过所有者上限：" + limit)
        if server["connection"]["transport"] != "local":
            from .asset_service import AssetService

            AssetService(self.root).executor(server["id"])

    def snapshot_assets(self, plan):
        assets = self.assets.read()
        model = next((m for m in assets["models"] if m["id"] == plan["model_asset"]), None)
        image = next((m for m in assets["images"] if m["id"] == plan["image_asset"]), None)
        if not model or not image:
            raise ValueError("执行方案必须选择已登记模型和引擎")
        value = {"model": model, "image": image, "adapter_image": assets.get("adapter_image")}
        if plan.get("engine_source_profile"):
            from .engine_source import profile

            value["engine_source_profile"] = profile(self.root, plan["engine_source_profile"])
        return value

    def propose(self, args, session_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", session_id):
            raise ValueError("资源方案必须绑定官方根会话")
        request = ResourceProposal.model_validate(args)
        payload = request.model_dump()
        with self.store.db() as db:
            previous = db.execute(
                "SELECT root_session,data FROM resource_proposals WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
        if previous:
            value = json.loads(previous["data"])
            if previous["root_session"] != session_id or value["request_digest"] != digest(payload):
                raise ValueError("资源请求ID冲突或跨会话使用")
            return value
        if self.store.binding(session_id):
            raise ValueError("当前会话已有任务，资源方案请使用新会话")
        from .build_service import BuildService

        if BuildService(self.root).bound(session_id):
            raise ValueError("当前会话已有源码任务，请使用新会话")
        plan, limits = payload["plan"], payload["limits"]
        server = self.registry.get(plan["machine_id"])
        observation = self.latest(server["id"])
        self.fresh(server, observation)
        self.within_policy(server, plan, limits)
        frozen = self.snapshot_assets(plan)
        from .asset_service import AssetService

        if server["connection"]["transport"] != "local":
            from .fleet_transport import worker_current

            executor = AssetService(self.root).executor(server["id"])
            if not worker_current(executor):
                raise ValueError("节点执行器版本需更新；先在空闲状态重新核验并准备")
            execution_root = executor["root"]
        else:
            execution_root = self.root
        check_gpu_occupancy(
            observation, plan["gpu_ids"],
            allow_shared=server["policy"].get("gpu_sharing") == "shared",
        )
        check_capacity(observation, plan, limits, frozen["model"], execution_root)
        scope = {
            "plan": plan,
            "limits": limits,
            "server_id": server["id"],
            "server_revision": server["revision"],
            "policy_revision": server["policy_revision"],
            "observation_id": observation["id"],
            "observation_digest": observation["digest"],
            "assets": frozen,
            "root_session": session_id,
        }
        value = {
            "id": uuid.uuid4().hex,
            **scope,
            "digest": digest(scope),
            "reason": request.reason,
            "request_digest": digest(payload),
            "status": "pending",
            "decision": None,
            "created_at": self.clock(),
            "review_expires_at": observation["received_at"] + 600,
            "grant_id": None,
            "task_id": None,
        }
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM resource_proposals WHERE root_session=? AND json_extract(data,'$.status') IN ('pending','approved','activated')",
                (session_id,),
            ).fetchone():
                raise ValueError("该会话已有资源方案；审阅原方案或使用新会话")
            db.execute(
                "INSERT INTO resource_proposals VALUES(?,?,?,?)",
                (value["id"], request.request_id, session_id, json.dumps(value)),
            )
            self.audit(
                server["id"],
                "proposed",
                {"proposal_id": value["id"], "digest": value["digest"], "root_session": session_id},
                db,
            )
        return value

    def proposal(self, args, session_id="", owner=False, db=None):
        request = ProposalTarget.model_validate(args)
        if db is None:
            with self.store.db() as connection:
                return self.proposal(args, session_id, owner, connection)
        row = db.execute(
            "SELECT root_session,data FROM resource_proposals WHERE id=?", (request.proposal_id,)
        ).fetchone()
        if not row or (not owner and row["root_session"] != session_id):
            raise ValueError("资源方案不存在或属于其他会话")
        value = json.loads(row["data"])
        if value["digest"] != request.expected_digest:
            raise ValueError("资源方案摘要不匹配，请重新审阅")
        return value

    def recheck(self, value):
        server = self.registry.get(value["server_id"])
        if (
            server["revision"] != value["server_revision"]
            or server["policy_revision"] != value["policy_revision"]
        ):
            raise ValueError("权限或连接版本已变化，旧批准不能继续使用")
        if self.clock() > value["review_expires_at"]:
            raise ValueError("方案审阅已过期，请重新观测和制定方案")
        self.within_policy(server, value["plan"], value["limits"])
        if self.snapshot_assets(value["plan"]) != value["assets"]:
            raise ValueError("模型或引擎登记已变化，需重新制定方案")
        return server

    def decide(self, args, session_id="", owner=False, source="owner-web"):
        request = ResourceDecision.model_validate(args)
        target = {"proposal_id": request.proposal_id, "expected_digest": request.expected_digest}
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            value = self.proposal(target, session_id, owner, db)
            if value["decision"]:
                if value["decision"] != request.decision:
                    raise ValueError("此方案已有最终审批结果，不能覆盖")
                return value
            if request.decision == "allowed-once":
                self.recheck(value)
            value.update(
                decision=request.decision,
                status="approved" if request.decision == "allowed-once" else "denied",
                decided_at=self.clock(),
                decision_source=source,
            )
            db.execute(
                "UPDATE resource_proposals SET data=? WHERE id=?", (json.dumps(value), value["id"])
            )
            self.audit(
                value["server_id"],
                "decided",
                {
                    "proposal_id": value["id"],
                    "digest": value["digest"],
                    "decision": request.decision,
                    "source": source,
                    "root_session": value["root_session"],
                },
                db,
            )
        return value

    def activate(self, args, session_id="", owner=False):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            value = self.proposal(args, session_id, owner, db)
            if value["status"] == "activated":
                task_id = value["task_id"]
            else:
                if value["status"] != "approved" or value["decision"] != "allowed-once":
                    raise ValueError("此方案未获一次性批准")
                server = self.recheck(value)
                if db.execute(
                    "SELECT 1 FROM bindings WHERE session_id=?", (value["root_session"],)
                ).fetchone():
                    raise ValueError("根会话已经绑定其他任务")
                grant_id = uuid.uuid4().hex
                plan = {**value["plan"], "authorization_id": grant_id}
                request = CreateTask(
                    request_id="resource-" + value["id"], plan=InfraPlan.model_validate(plan)
                )
                task_id = self.store.create_record(db, request)
                row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
                state = {**json.loads(row[0]), "session_id": value["root_session"]}
                db.execute("UPDATE tasks SET state=? WHERE id=?", (json.dumps(state), task_id))
                db.execute(
                    "INSERT INTO bindings VALUES(?,?,?)",
                    (value["root_session"], task_id, "planning"),
                )
                grant = {
                    "id": grant_id,
                    "proposal_id": value["id"],
                    "task_id": task_id,
                    "root_session": value["root_session"],
                    "plan_digest": digest(value["plan"]),
                    "server_id": server["id"],
                    "server_revision": server["revision"],
                    "policy_revision": server["policy_revision"],
                    "assets_digest": digest(value["assets"]),
                    "limits": value["limits"],
                    "expires_at": self.clock() + value["limits"]["duration_seconds"],
                    "source_edit": server["policy"]["allow_source_edit"],
                    "revoked": False,
                }
                db.execute(
                    "INSERT INTO resource_grants VALUES(?,?,?,?)",
                    (grant_id, value["id"], task_id, json.dumps(grant)),
                )
                value.update(status="activated", grant_id=grant_id, task_id=task_id)
                db.execute(
                    "UPDATE resource_proposals SET data=? WHERE id=?",
                    (json.dumps(value), value["id"]),
                )
                self.audit(
                    server["id"],
                    "task_authorized",
                    {
                        "proposal_id": value["id"],
                        "grant_id": grant_id,
                        "task_id": task_id,
                        "expires_at": grant["expires_at"],
                    },
                    db,
                )
        task = self.store.get(task_id)
        self.store.sync(task)
        return {
            **self.infra.view(task),
            "requires_start": task["state"]["status"] == "draft",
            "sessionId": value["root_session"],
        }

    def check_task(self, task, action, candidate=None):
        # Cleanup remains possible after denial/revocation/expiry, but engines
        # still verify exact ownership before stopping anything.
        if action == "stop":
            return None
        plan = task["plan"]
        server = self.registry.get(plan["machine_id"])
        if not server["enabled"] or server["policy"]["mode"] != "review":
            raise ValueError("当前服务器权限不允许执行")
        # Baseline GPU grants must obey the same operation switches as newly
        # reviewed grants. Stopping an owned service remains possible above.
        from .node_authority import require_operation

        require_operation(server, "inference")
        require_operation(server, "containers")
        grant_id = plan.get("authorization_id")
        if not grant_id:
            if not set(plan["gpu_ids"]).issubset(server["baseline_gpu_ids"]):
                raise ValueError("此任务需要绑定资源审批，不能扩大原执行配额")
            if action == "source_write" and not server["policy"]["allow_source_edit"]:
                raise ValueError("所有者已关闭源码修改权限")
            return None
        with self.store.db() as db:
            row = db.execute("SELECT data FROM resource_grants WHERE id=?", (grant_id,)).fetchone()
        if not row:
            raise ValueError("任务授权不存在")
        grant = json.loads(row[0])
        frozen_plan = {**plan, "authorization_id": None}
        if (
            grant["task_id"] != task["id"]
            or grant["root_session"] != task["state"]["session_id"]
            or grant["plan_digest"] != digest(frozen_plan)
        ):
            raise ValueError("授权绑定的任务、会话或方案已变化")
        if grant["revoked"] or self.clock() >= grant["expires_at"]:
            raise ValueError("任务授权已撤销或到期；仍可清理自有服务")
        if (
            server["revision"] != grant["server_revision"]
            or server["policy_revision"] != grant["policy_revision"]
        ):
            raise ValueError("服务器权限版本变化，旧任务授权已失效")
        if digest(self.snapshot_assets(plan)) != grant["assets_digest"]:
            raise ValueError("授权绑定的模型或引擎已变化")
        self.within_policy(server, plan, grant["limits"])
        if action == "source_write" and not grant["source_edit"]:
            raise ValueError("此授权不允许源码修改")
        if candidate:
            serving = candidate["serving"]
            for key in ("context_length", "mem_fraction_static", "max_new_tokens"):
                if serving[key] > plan["serving"][key]:
                    raise ValueError("候选超出获批方案：" + key)
        return grant

    def execution_limits(self, task, grant=None):
        if grant:
            return grant["limits"]
        policy = self.registry.get(task["plan"]["machine_id"])["policy"]
        return {
            "cpu_cores": min(4, policy["max_cpu_cores"]),
            "memory_gib": min(16, policy["max_memory_gib"]),
            "disk_gib": min(5, policy["max_disk_gib"]),
        }

    def check_usage(self, task, grant, engine=None):
        if not grant:
            return
        base = self.store.directory(task["id"])
        used = sum(p.stat().st_size for p in base.rglob("*") if p.is_file() and not p.is_symlink())
        if used > grant["limits"]["disk_gib"] * 1024**3:
            raise ValueError("任务自有工作区磁盘配额已超限")
        if engine and hasattr(engine, "resource_usage"):
            usage = engine.resource_usage()
            if usage.get("rss_bytes", 0) > grant["limits"]["memory_gib"] * 1024**3:
                raise ValueError("任务自有进程组内存配额已超限")

    def validate_plan(self, plan):
        with self.store.db() as db:
            row = db.execute(
                "SELECT task_id FROM resource_grants WHERE id=?", (plan.authorization_id,)
            ).fetchone()
        if not row:
            raise ValueError("任务授权不存在")
        task = self.store.get(row[0])
        if InfraPlan.model_validate(task["plan"]).model_dump() != plan.model_dump():
            raise ValueError("该授权不能借用于其他方案")
        return self.check_task(task, "validate")

    def revoke(self, args):
        request = RevokeGrant.model_validate(args)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM resource_grants WHERE id=?", (request.grant_id,)
            ).fetchone()
            if not row:
                raise ValueError("授权不存在")
            grant = json.loads(row[0])
            grant.update(revoked=True, revoked_at=self.clock())
            db.execute(
                "UPDATE resource_grants SET data=? WHERE id=?",
                (json.dumps(grant), request.grant_id),
            )
            self.audit(
                grant["server_id"],
                "revoked",
                {"grant_id": request.grant_id, "task_id": grant["task_id"]},
                db,
            )
        return grant

    def dispatch(self, endpoint, session_id, role, args):
        owner = role == "owner"
        if (
            endpoint
            in {
                "resource-save",
                "resource-policy",
                "resource-secret",
                "resource-trust",
                "resource-test",
                "resource-decision",
                "resource-revoke",
            }
            and not owner
        ):
            raise ValueError("资源配置、凭据和审批决定只允许所有者操作")
        if endpoint == "resource-catalog":
            return self.catalog(session_id, owner)
        if endpoint == "resource-observe":
            return self.observe(args)
        if endpoint == "resource-assess":
            return self.assessment(args)
        if endpoint == "resource-save":
            value = self.registry.save(args)
        elif endpoint == "resource-policy":
            value = self.registry.policy(args)
        elif endpoint == "resource-trust":
            value = self.registry.trust(args)
        elif endpoint == "resource-secret":
            return ResourceVault(self.root).put(args)
        elif endpoint == "resource-test":
            server = self.registry.get(ObserveRequest.model_validate(args).server_id)
            value = self.registry.connection_result(
                server["id"], server["revision"], self.transport.test(server)
            )
            self.audit(server["id"], "connection_tested", value)
            return value
        elif endpoint == "resource-propose":
            return self.propose(args, session_id)
        elif endpoint == "resource-review":
            return self.proposal(args, session_id, owner)
        elif endpoint == "resource-decision":
            return self.decide(args, session_id, True)
        elif endpoint == "resource-native-decision":
            if role != "native-approval":
                raise ValueError("此决定只能由官方审批桥提交")
            return self.decide(args, session_id, False, "native-approval")
        elif endpoint == "resource-execute":
            return self.activate(args, session_id, owner)
        elif endpoint == "resource-revoke":
            return self.revoke(args)
        else:
            raise ValueError("未知资源操作")
        self.audit(
            value["id"],
            endpoint,
            {"revision": value["revision"], "policy_revision": value["policy_revision"]},
        )
        return value
