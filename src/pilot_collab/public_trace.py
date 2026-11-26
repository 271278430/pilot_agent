"""Allowlisted user-facing facts; not raw logs, prompts or hidden reasoning."""

from typing import Literal

from pydantic import Field

from pilot_core.contracts import Contract

from .store import clean

ROLES = {
    "system": "交付验收器",
    "operator": "用户",
    "supervisor": "Supervisor",
    "repo": "RepoPilot",
    "infra": "InfraPilot",
    "verifier": "独立验收",
    "delivery_supervisor": "Supervisor",
    "delivery_repo": "RepoPilot",
    "delivery_infra": "InfraPilot",
}
TOOLS = {
    "project_tree": "查看项目文件与模型候选",
    "project_read": "读取项目源码参考",
    "plan_created": "生成待确认计划",
    "control": "任务控制",
    "preflight": "检查模型与环境",
    "delegate_repo": "委派源码修复",
    "delegate_infra": "交接部署任务",
    "read": "读取源码",
    "edit": "修改源码",
    "source_test": "运行源码测试",
    "candidate": "交接通过测试的代码",
    "inspect": "检查部署环境",
    "repair_env": "修复环境故障",
    "deploy": "部署服务",
    "verify": "核验模型与接口",
    "finish": "汇总交付结果",
    "probe": "只读环境探测",
    "stage": "准备部署目录",
    "start": "启动服务",
    "diagnose": "诊断故障",
    "repair": "修复本任务故障",
    "session_end": "角色会话结束",
    "prepare_model": "准备模型运行环境",
    "model_download": "下载并校验模型资产",
    "write": "编写应用代码",
    "recovery_requested": "复核已交付版本",
    "delivery_reconciled": "确认交付可用",
}
SAFE_VALUES = {
    "passed",
    "candidate_id",
    "path",
    "model_id",
    "adapter_loaded",
    "dtype",
    "device",
    "changed",
    "action",
    "status",
    "proposed_owner",
    "failure_kind",
    "code",
    "exit_code",
    "returncode",
    "tests_passed",
    "environment_revision",
    "port",
    "healthy",
}


class PublicStep(Contract):
    seq: int
    time: str
    role: str
    action: str
    outcome: Literal["requested", "returned", "passed", "failed", "recorded"]
    facts: dict = Field(default_factory=dict)
    detail: str | None = None
    failure_id: str | None = None
    recovery: Literal["resolved", "retried_and_verified", "delivery_rechecked"] | None = None


FAILURES = {
    "engine_socket": "服务未能连接模型引擎，需修正连接配置并重新部署。",
    "generation_limit": "服务生成长度配置不符合接口约定。",
    "port_conflict": "本任务服务端口被占用，需调整端口。",
    "api_contract": "模型接口响应不符合约定，需修复并重新验收。",
}


def issue(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("failure", "latest_failure"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    for key in ("result", "inspection", "verification"):
        found = issue(payload.get(key))
        if found:
            return found
    return None


def facts(value, depth=0):
    if not isinstance(value, dict) or depth > 2:
        return {}
    selected = {
        k: v
        for k, v in value.items()
        if k in SAFE_VALUES and isinstance(v, (str, int, float, bool))
    }
    for key in ("result", "verification", "deployment", "latest_failure", "fault"):
        if isinstance(value.get(key), dict):
            selected.update(facts(value[key], depth + 1))
    return clean({k: v[:220] if isinstance(v, str) else v for k, v in selected.items()})


def project_event(event):
    kind, data = event["kind"], event.get("data") or {}
    operation = data.get("operation") if kind in {"tool_requested", "tool_returned"} else kind
    if operation not in TOOLS:
        return None
    payload = data.get("result", data) if kind == "tool_returned" else data
    outcome = "requested" if kind == "tool_requested" else "recorded"
    if isinstance(payload, dict):
        inner = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if payload.get("status") == "requested":
            outcome = "requested"
        elif (
            payload.get("ok") is False
            or payload.get("passed") is False
            or inner.get("passed") is False
            or payload.get("status") == "error"
        ):
            outcome = "failed"
        elif payload.get("passed") is True or inner.get("passed") is True:
            outcome = "passed"
        elif payload.get("ok") is True or kind == "tool_returned":
            outcome = "returned"
    failure = issue(payload)
    detail = None
    if failure:
        outcome = "failed"
        detail = FAILURES.get(failure.get("category"), "该次检查发现故障，需要修复后重新验收。")
    elif isinstance(payload, dict) and payload.get("error"):
        if kind == "session_end":
            outcome = "failed"
        detail = (
            "尚未取得当前故障的检查证据，修复调用被保护规则拒绝；没有执行环境修改。"
            if payload["error"] == "An evidenced current environment failure is required"
            else "该次操作未完成。请核对后续检查与任务最终结果。"
        )
    action = TOOLS[operation]
    if operation == "recovery_requested":
        outcome, detail = "requested", "原会话异常已保留，复核现有结果，不重复修改或部署。"
    elif operation == "delivery_reconciled":
        outcome, detail = "passed", "当前代码、模型和运行容器已复核，复用验收结果完成交付。"
    if operation in {"delegate_repo", "delegate_infra"} and outcome in {"returned", "passed"}:
        action = "源码协作结果返回" if operation == "delegate_repo" else "部署协作结果返回"
    return PublicStep(
        seq=event["seq"],
        time=event.get("time", event.get("created_at", "")),
        role=ROLES.get(event.get("agent", event.get("role", "")), "执行器"),
        action=action,
        outcome=outcome,
        facts=facts(payload),
        detail=detail,
        failure_id=failure.get("failure_id") if failure else None,
    )


def task_trace(db, reference):
    if reference.kind == "ml":
        return []  # ML stage progress remains in the task view, not invented events.
    if reference.kind == "app":
        from pilot_apps.store import AppStore

        events = AppStore(db.root).events(reference.id)
        steps = [project_event(event) for event in events]
        reconciled = [event["seq"] for event in events if event["kind"] == "delivery_reconciled"]
        for step in steps:
            if step and step.outcome == "failed" and any(seq > step.seq for seq in reconciled):
                step.recovery = "delivery_rechecked"
        return [s.model_dump() for s in steps if s]
    table, key, clock, role = (
        ("delivery_events", "release_id", "created_at", "role")
        if reference.kind == "delivery"
        else ("events", "run_id", "time", "agent")
    )
    import json

    with db.connect() as connection:
        rows = connection.execute(
            f"SELECT seq,{clock},{role},kind,data FROM {table} WHERE {key}=? ORDER BY seq DESC LIMIT 160",
            (reference.id,),
        ).fetchall()
        state = {}
        if reference.kind == "delivery":
            release = connection.execute(
                "SELECT state FROM delivery_releases WHERE id=?", (reference.id,)
            ).fetchone()
            if release:
                state = json.loads(release["state"])
    result = []
    for row in reversed(rows):
        step = project_event({**dict(row), "data": json.loads(row["data"])})
        if step:
            if step.failure_id in state.get("resolved_failure_ids", []):
                step.recovery = "resolved"
            if (
                step.action == TOOLS["repair_env"]
                and step.outcome == "failed"
                and not step.failure_id
                and step.detail
                and "保护规则拒绝" in step.detail
                and (state.get("verification") or {}).get("passed")
                and any(
                    r["seq"] > row["seq"]
                    and r["kind"] == "repair_env"
                    and json.loads(r["data"]).get("ok") is True
                    for r in rows
                )
            ):
                step.recovery = "retried_and_verified"
            result.append(step.model_dump())
    return result


def route_view(decision):
    return clean(
        {
            "intent": decision.intent,
            "stages": [s.model_dump() for s in decision.funnel],
            "candidates": [
                {"title": c.title, "similarity": c.similarity} for c in decision.candidates[:3]
            ],
            "model": decision.model_used,
            "fallback": decision.fallback,
            "tools": decision.tools,
            "observations": decision.observations,
            "execution": "未由路由直接执行；写操作需确认计划",
        }
    )
