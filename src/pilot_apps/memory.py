"""Role-specific factual context; full history remains in SQLite and Session logs."""

import json

from pilot_core.context import ContextBlock, compile_context, render_context
from repopilot.storage import atomic_json


def snapshot(store, id, role, budget_tokens=4096):
    row = store.get(id)
    state = row.state
    blocks = [
        ContextBlock(
            id="approved-goal",
            source="approved ApplicationPlan",
            task_id=id,
            layer="working_state",
            priority=100,
            content=json.dumps(row.plan.model_dump(), ensure_ascii=False),
        ),
        ContextBlock(
            id="current-status",
            source="SQLite current projection",
            task_id=id,
            layer="working_state",
            trust="verified_data",
            priority=90,
            content=json.dumps(
                {"status": row.status, "role": role, "rounds": state.get("rounds", 0)},
                ensure_ascii=False,
            ),
        ),
    ]
    if state.get("feedback"):
        blocks.append(
            ContextBlock(
                id="latest-feedback",
                source="operator revision",
                task_id=id,
                layer="working_state",
                priority=99,
                content=json.dumps(state["feedback"][-1], ensure_ascii=False),
            )
        )
    evidence = {
        name: state[name]
        for name in ("model", "candidate", "deployment", "verification")
        if state.get(name)
    }
    if evidence:
        blocks.append(
            ContextBlock(
                id="current-evidence",
                source="domain-tool state",
                task_id=id,
                layer="evidence",
                trust="verified_data",
                priority=95,
                content=json.dumps(evidence, ensure_ascii=False),
            )
        )
    failure = state.get("last_error") or state.get("error")
    if failure:
        blocks.append(
            ContextBlock(
                id="failure-to-resolve",
                source="last tool failure",
                task_id=id,
                layer="evidence",
                priority=98,
                content=json.dumps(
                    {"candidate": (state.get("candidate") or {}).get("id"), "failure": failure},
                    ensure_ascii=False,
                ),
            )
        )
    events = store.events(id)
    blocks.append(
        ContextBlock(
            id="history-reference",
            source="SQLite application events",
            task_id=id,
            layer="history_reference",
            trust="verified_data",
            priority=10,
            content=json.dumps(
                {
                    "authoritative_history": "DeepSeek Harness Session",
                    "official_root_session_id": state.get("harness_session") or (state.get("native_runtime") or {}).get("id"),
                    "event_cursor_source": "SQLite application events (not Harness step numbers)",
                    "latest_event_seq": events[-1]["seq"] if events else 0,
                    "prior_session_count": len(state.get("sessions", [])),
                }
            ),
        )
    )
    return compile_context(role, id, blocks, budget_tokens)


def refresh_sessions(store, id):
    row = store.get(id)
    atomic_json(store.directory(id) / "runtime-status.json", {"status": row.status})
    native = row.state.get("native_runtime")
    if native:
        for role in ("supervisor", "repo", "infra"):
            directory = store.path(id, native["directory"]) / "roles" / role
            if directory.is_dir():
                manifest = snapshot(store, id, role)
                atomic_json(directory / "context-manifest.json", manifest.model_dump())
                atomic_json(directory / "context.json", {"text": render_context(manifest)})
    for session in row.state.get("sessions", []):
        directory = store.path(id, session["directory"])
        if not directory.is_dir():
            continue
        manifest = snapshot(store, id, session["role"])
        atomic_json(directory / "context-manifest.json", manifest.model_dump())
        atomic_json(directory / "context.json", {"text": render_context(manifest)})
