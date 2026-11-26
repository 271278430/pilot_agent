from __future__ import annotations

import importlib.metadata
import json
import os
import threading
import time
import uuid
from pathlib import Path

from deepseek_harness import DeepSeekHarness

from pilot_core.context import write_context
from pilot_core.harness_config import allowed_tools, prompt_manifest, skill_plugins, system_prompt
from pilot_core.model_protocol import reasoning_options

from .acceptance import verify
from .models import Budget, RunConfig, Task
from .repository import clone, snapshot
from .runtime_control import cancel_on_request
from .storage import Store, atomic_json, now, read_json, redact
from .tools import ToolService, recover

PROJECT = Path(__file__).resolve().parents[2]


def dsh_executable() -> str | None:
    explicit = os.getenv("REPOPILOT_DSH_BIN")
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise ValueError("REPOPILOT_DSH_BIN must name an existing official dsh executable")
        return str(path)
    local = PROJECT / "harness/runtime/node_modules/.bin" / ("dsh.cmd" if os.name == "nt" else "dsh")
    if local.exists():
        return str(local)
    if os.name == "nt":
        raise ValueError("Install the project-local official runtime: npm ci --prefix harness/runtime (Node >=24.9 required)")
    return None


def prepare(task: Task, output: Path, model: str, base_url: str, environment: str = "docker", variant: str = "compressed", budget: Budget | None = None, policy: str = "model") -> Path:
    run = output.resolve() / f"{task.id}-{uuid.uuid4().hex[:12]}"
    run.mkdir(parents=True)
    task = task.model_copy(update={"repo": str(Path(task.repo).resolve())})
    workspace = run / "workspace"
    baseline = clone(Path(task.repo), workspace, task.commit)
    hashes = snapshot(workspace)
    config = RunConfig(task=task, workspace=str(workspace), baseline=baseline, environment=environment, variant=variant, model=model, base_url=base_url.rstrip("/"), budget=budget or Budget(), policy=policy, source_hashes=hashes)
    atomic_json(run / "run.json", config.model_dump())
    store = Store(run)
    store.checkpoint({"baseline": baseline, "hashes": hashes, "actions": 0, "pending": None, "exclusions": [], "no_progress": 0, "halt_reason": None})
    (run / "patch.diff").write_text("", encoding="utf-8")
    store.event("run_prepared", config=config.model_dump(), sdk_version=importlib.metadata.version("deepseek-harness-sdk"))
    return run


def composition(run: Path) -> Path:
    config = RunConfig.model_validate(read_json(run / "run.json"))
    budget = config.budget
    reasoning = reasoning_options(config.model)
    # JSON is a valid YAML subset. Paths/commands stay structured, never shell strings.
    patch = [
        *[{"id": row, "disabled": True} for row in ("llm-deepseek", "persistent-bash", "persistent-pwsh", "terminal-bash", "terminal-pwsh", "tool-fs", "tool-working-directory", "session-title")],
        {"id": "agent-loop", "inject": ["agents", "sessions", "llm", "tools", "systemPrompt", "sessionProjections", "sessionPersistence"], "config": {"agents": [], "maxParallelToolCalls": 1}},
        {"id": "sessions", "config": {"root": str(run / "harness-sessions"), "compression": "none"}},
        {"id": "system-prompt", "config": {"includeHarnessIdentity": False, "includeRuntimeContext": False, "personaPrefix": system_prompt("repo")}},
        {"id": "compaction-basic", "config": {"auto": config.variant == "compressed", "thresholdRatio": 0.7, "headroomTokens": 512, "retainTokens": 512, "maxTokens": 1024, "summarizationProvider": config.provider, "summarizationModel": config.model}},
        {"insert": [
            *skill_plugins("repo"),
            {"id": "repopilot-llm", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {config.provider: {"apiKeyEnv": "REPOPILOT_API_KEY", "api": "openai-completions", "baseURL": config.base_url, "timeoutMs": budget.command_timeout * 1000, "streamIdleTimeoutMs": budget.command_timeout * 1000, "reasoning": reasoning["reasoning"], "compat": {"supportsDeveloperRole": False, "thinkingFormat": "zai", "supportsReasoningEffort": reasoning["supportsReasoningEffort"]}, "retryPolicy": {"mode": "normal", "maxRetries": 2}, "models": [{"id": config.model, "contextWindow": budget.context_tokens, "maxTokens": budget.output_tokens, "reasoningEfforts": reasoning["reasoningEfforts"]}]}}}},
            {"id": "repopilot-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {"serverName": "repopilot", "transport": "stdio", "command": os.sys.executable, "args": ["-m", "repopilot.mcp_server", str(run.resolve())], "cwd": str(PROJECT), "toolCallTimeoutMs": (budget.command_timeout + 20) * 1000, "startupTimeoutMs": 30000, "failOnStartupError": True, "reconnect": {"enabled": False}}},
            {"id": "repopilot-policy", "name": str(PROJECT / "harness/plugin/index.mjs"), "inject": ["agents", "tools", "llm", "sessionPersistence"], "config": {"allowedTools": allowed_tools("repo"), "meterPath": str(run / "meter.json"), "checkpointPath": str(run / "checkpoint.json"), "evidencePath": str(run / "evidence.json"), "contextPath": str(run / "context.json"), "retainEvidence": config.variant == "compressed", "nativeTracePath": str(run / "harness-events.jsonl"), "budget": budget.model_dump()}},
        ]},
    ]
    path = run / "harness.patch.yml"
    atomic_json(path, patch)
    return path


def execute(run: Path, api_key: str, resume: bool = False, acknowledge_unknown: bool = False, cancel_event: threading.Event | None = None) -> dict:
    if not api_key:
        raise ValueError("Set REPOPILOT_API_KEY in your local .env")
    run = run.resolve()
    config = RunConfig.model_validate(read_json(run / "run.json"))
    store = Store(run)
    if resume:
        store.event("recovery", result=recover(run, acknowledge_unknown))
    patch = composition(run)
    service = ToolService(run)
    evidence = service.evidence()
    atomic_json(run / "evidence.json", redact(evidence))
    write_context(run, "repo", config.task.id, evidence, "evidence.json",
                  budget_tokens=max(512, min(2048, config.budget.context_tokens // 3)))
    prompt_manifest(run, "repo")
    prompt = f"Issue:\n{config.task.issue}\n\nTarget command: {json.dumps(config.task.target)}\nRegression command: {json.dumps(config.task.regression)}"
    session_id = read_json(run / "session.json")["id"] if resume else f"repopilot-{uuid.uuid4().hex}"
    atomic_json(run / "session.json", {"id": session_id, "home": str(run / "harness-home")})
    started = time.monotonic()
    report = {"started_at": now(), "policy": config.policy, "framework": "deepseek-harness", "sdk_version": importlib.metadata.version("deepseek-harness-sdk"), "provider": config.provider, "model": config.model, "variant": config.variant, "task_id": config.task.id, "baseline": config.baseline, "run": str(run), "session_id": session_id, "status": "running", "error": None, "cost_priced": config.budget.input_price > 0 or config.budget.output_price > 0}
    report["generation_controls"] = {"output_tokens": config.budget.output_tokens, "sampling": "provider_defaults", "seed": None}
    env = {"REPOPILOT_API_KEY": api_key}
    # Credential is passed to Harness LLM, but MCP subprocesses receive scrubbed env.
    harness = DeepSeekHarness(dsh_bin=dsh_executable(), dsh_home=str(run / "harness-home"), cwd=config.workspace, runtime_cwd=str(PROJECT), provider=config.provider, model=config.model, max_tokens=config.budget.output_tokens, profile="sdk-minimal", patches=(str(patch),), env=env, initialize_timeout_seconds=60, shutdown_timeout_seconds=10)
    timer = threading.Timer(config.budget.wall_timeout, harness.close)
    timer.daemon = True
    try:
        with harness:
            timer.start()
            session = harness.resume_session(session_id) if resume else harness.start_session(session_id)
            with cancel_on_request(harness, cancel_event):
                result = session.run(prompt)
            report.update(status=result.finish_reason or "unknown", final_response=result.final_response)
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
        store.event("runtime_error", error=str(exc))
    finally:
        timer.cancel()
        harness.close()
    if cancel_event is not None and cancel_event.is_set():
        report.update(status="interrupted", error="operator_stop_requested")
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["meter"] = read_json(run / "meter.json") if (run / "meter.json").exists() else {}
    report["usage_source"] = "synthetic_fixture" if config.policy == "scripted" else "estimated_upper_bound" if report["meter"].get("estimated") else "provider_reported"
    report["actions"] = store.load()["actions"]
    acceptance = verify(run)
    report["acceptance"] = acceptance.model_dump()
    report["patch_accepted"] = acceptance.passed
    report["passed"] = bool(acceptance.passed and report["status"] == "completed")
    atomic_json(run / "report.json", redact(report))
    store.event("run_finished", passed=report["passed"], status=report["status"], duration_seconds=report["duration_seconds"])
    return report
