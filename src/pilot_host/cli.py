"""Launch the official Web profile with domain plugins and official Presets."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pilot_apps.contracts import ApplicationPlan
from pilot_apps.roles import CONTROLS, DOMAINS, HISTORY_TOOLS, PROMPTS
from pilot_apps.service import DESCRIPTIONS
from pilot_core.config import PROJECT, credentials, host_settings
from pilot_core.model_protocol import context_window, reasoning_options
from pilot_core.storage import StorageLayout
from repopilot.harness import dsh_executable
from repopilot.models import Budget
from repopilot.storage import atomic_json

from .compaction_policy import compaction_config

GROUPS = {
    "repo-tools": {
        "tree",
        "read",
        "write",
        "source_test",
        "candidate",
        "project_tree",
        "project_read",
    },
    "infra-tools": {"deploy", "inspect"},
    "model-assets": {"prepare_model"},
    "delivery": {"status", "verify", "finish", "plan", "resources", "revise_plan", "feedback"},
}


def plan_parameters():
    """Project the contract into the installed official tool schema DSL."""
    contract = ApplicationPlan.model_json_schema()
    required = set(contract["required"])

    def value(node):
        if "anyOf" in node:
            return {"oneOf": [value(branch) for branch in node["anyOf"]]}
        result = {"type": node["type"]}
        for key in ("enum", "const", "default"):
            if key in node:
                result[key] = node[key]
        if node["type"] == "array":
            result["items"] = value(node["items"])
        # Pydantic remains responsible for lengths, patterns and cross-field
        # validation; the official DSL deliberately supports fewer keywords.
        return result

    properties = {
        key: dict(value(node), **({"required": True} if key in required else {}))
        for key, node in contract["properties"].items()
    }
    properties["acceptance_examples"]["description"] = (
        "Concrete messages sent to the application, without expected answers or UI actions."
    )
    return {
        "plan": {
            "required": True,
            "description": "Pass the plan as a JSON object. Legacy serialized JSON objects are also accepted.",
            "oneOf": [
                {"type": "object", "properties": properties, "additionalProperties": False},
                {
                    "type": "string",
                    "description": "Compatibility only: one serialized JSON object, not double encoded.",
                },
            ],
        },
        "request_id": {
            "type": "string",
            "required": True,
            "description": "Stable idempotency key (8–80 letters, digits, underscores or hyphens). Reuse for identical retries.",
        },
    }


def tool(name):
    parameters = {}
    if name in {"read", "project_read", "write"}:
        parameters["path"] = {"type": "string", "required": True}
    if name == "write":
        parameters["content"] = {"type": "string", "required": True}
    if name == "plan":
        parameters = plan_parameters()
    if name == "revise_plan":
        parameters = {
            **plan_parameters(),
            "expected_version": {
                "type": "integer",
                "required": True,
                "description": "Current plan_version from pilot_status. Reload after a version conflict.",
            },
        }
    if name == "feedback":
        parameters = {
            "request_id": {
                "type": "string",
                "required": True,
                "description": "Stable idempotency key (8–80 letters/digits/underscores/hyphens).",
            },
            "message": {
                "type": "string",
                "required": True,
                "description": "The user's concrete requested application changes, 3–2000 characters.",
            },
        }
    descriptions = {
        "plan": "Propose a reviewable model application plan. Success ends this turn; the user starts it in the delivery panel. Required: title, goal, model_source, application_requirements, acceptance_examples. The supported application runtime is a Python standard-library HTTP server (app.py accepting --port) and browser HTML/JS (index.html). GET /health and POST /chat use the protected model_client. Additional Python modules live under modules/; data and frontend assets under assets/. No third-party framework packages are installed in the app container. Plan within this runtime. Acceptance examples are concrete input messages, never expected results or click instructions. runtime=cpu describes the application container; existing_teacher8b reuses the registered GPU model engine.",
        "status": "Read the current delivery plan/state. Before a plan exists, returns status=unplanned and the next planning action.",
        "resources": "Inspect observed hardware and admitted model resources without modifying them.",
        "revise_plan": "Edit an existing unstarted plan in the same application. Read pilot_status first; pass the full updated ApplicationPlan and current plan_version. Preserve requirements not changed by the user. configured_api must use the registered API model from pilot_resources. Success saves a reviewable plan and ends this turn; user clicks Start. Cannot replace an executed plan or change project identity.",
        "feedback": "Save user-requested functionality revisions for a completed, paused or failed application. Read pilot_status first. Preserve model and candidates, invalidate old acceptance, then wait for the user's Continue action. This does not change model_source; an executed application needs a new plan to switch models. Never promise nonexistent manual controls.",
    }
    return {
        "name": name,
        "description": descriptions.get(name, DESCRIPTIONS.get(name, name)),
        "parameters": parameters,
    }


def composition(root, config, *, settings_root=None):
    root = Path(root).resolve()
    reasoning = reasoning_options(config["REPOPILOT_MODEL"])
    # Start with the installed official Web bundle, disable its generic Presets
    # and model side channels; role tools and domain permissions are explicit.
    patch = [
        {"id": id, "disabled": True}
        for id in (
            "preset-standard",
            "preset-cordis",
            "preset-ptc",
            "preset-minimal",
            "llm-deepseek",
            "llm-deepseek-account",
            "session-title-llm",
            "deepseek-account",
            "deepseek-llm-api-extensions",
            "session-log-deepseek",
            "plugin-package-inventory-deepseek",
            "account-controller",
            "desktop-product-telemetry",
            "product-analytics",
            "otel",
            "session-telemetry-otel",
            "tool-working-directory",
            "harness-dev-context",
            "ui-brand-official",
            "ui-settings-account",
            "ui-settings-web-search",
            "ui-settings-plugin-inventory",
            "ui-agent-preset",
            "entry-customization",
            "ui-plugin-manager",
            "ui-cordis",
            "ui-schedule",
            "ui-jobs",
            "ui-goal",
            "ui-plan",
            "ui-workflow-run",
            "ui-sidebar-terminal",
            "terminal-controller",
            "ui-message-feedback",
            "ui-settings-shell",
            "ui-settings-agent-loop",
            "ui-settings-subagent",
            "ui-settings-models",
            "ui-settings-plugins",
            "command-feedback",
            "session-log-download",
            "ui-permission",
            "directory-picker",
            "open-in-app",
            "ui-open-in-app",
            "mcp-resources",
        )
    ]
    patch += [
        {
            "id": "web-runtime",
            "config": {"openBrowser": False, "printUrl": False, "surfaceContext": False},
        },
        {
            "id": "connection",
            "inject": ["credentials", "webStartup", "webServer"],
            "config": {"trustedHosts": []},
        },
        {"id": "agent-preset-registry", "config": {"default": "infra-lead"}},
        {
            "id": "agent-default-model",
            "config": {
                "provider": "apps",
                "model": config["REPOPILOT_MODEL"],
                "reasoningEffort": reasoning["reasoning"],
            },
        },
        {
            "id": "llm-pi-ai",
            "config": {
                "providers": {
                    "apps": {
                        "apiKeyEnv": "REPOPILOT_API_KEY",
                        "api": "openai-completions",
                        "baseURL": config["REPOPILOT_BASE_URL"],
                        "timeoutMs": 180000,
                        "reasoning": reasoning["reasoning"],
                        "compat": {
                            "supportsDeveloperRole": False,
                            "thinkingFormat": "zai",
                            "supportsReasoningEffort": reasoning["supportsReasoningEffort"],
                        },
                        "models": [
                            {
                                "id": config["REPOPILOT_MODEL"],
                                "contextWindow": context_window(config["REPOPILOT_MODEL"]),
                                "maxTokens": 6000,
                                "reasoningEfforts": reasoning["reasoningEfforts"],
                            }
                        ],
                    }
                }
            },
        },
        {
            "id": "working-directory",
            "config": {"defaultDirectory": str(root / "runs/harness-inbox")},
        },
        {
            "id": "sandbox-policy",
            "config": {
                "mode": "workspace-write",
                "workspaceRoot": str(root / "runs/harness-inbox"),
            },
        },
        {"id": "approval", "config": {"policy": "ask"}},
        {
            "id": "permission",
            "config": {
                "defaultPreset": "review",
                "presets": {
                    "observe": {
                        "sandbox": "read-only",
                        "approval": "never",
                        "label": "仅观察",
                        "description": "只读观测与分析；不申请或执行变更。",
                    },
                    "review": {
                        "sandbox": "workspace-write",
                        "approval": "ask",
                        "label": "审阅执行",
                        "description": "在所有者范围内提交有界方案，需要审批后启动。",
                    },
                },
            },
        },
        {
            "id": "session-query-sqlite",
            "config": {
                "path": ":memory:",
                "openAt": "first-search",
                "defaultLimit": 10,
                "maxLimit": 10,
            },
        },
        {"id": "agent-loop", "config": {"maxParallelToolCalls": 1}},
        {"id": "subagent", "config": {"maxDepth": 1, "maxActiveSubagents": 1}},
    ]
    applied = host_settings(root if settings_root is None else settings_root)
    if applied:
        if applied.get("applied_revision", 0) > 0:
            from pilot_core.config import native_model_profile, provider_of

            selected = applied["applied"]["model"]
            for row in patch:
                if row["id"] == "llm-pi-ai":
                    row["config"]["providers"] = {
                        **{
                            k: v
                            for k, v in row["config"]["providers"].items()
                            if k != "apps" and not k.startswith("pilot_model_")
                        },
                        **{
                            provider_of(m): native_model_profile(m)
                            for m in applied["applied"]["models"]
                            if m["enabled"]
                        },
                    }
                elif row["id"] == "agent-default-model":
                    row["config"].update(
                        provider=provider_of(selected),
                        model=selected["model_id"],
                    )
                    if selected["reasoning"] == "off":
                        row["config"].pop("reasoningEffort", None)
                    else:
                        row["config"]["reasoningEffort"] = native_model_profile(selected)[
                            "reasoning"
                        ]
                elif row["id"] in {"working-directory", "sandbox-policy"}:
                    row["config"][
                        "defaultDirectory" if row["id"] == "working-directory" else "workspaceRoot"
                    ] = applied["applied"]["workspace_root"]
    compaction = compaction_config(
        next(row["config"]["providers"] for row in patch if row["id"] == "llm-pi-ai")
    )
    rows = [
        {
            "id": "pilot-workspace",
            "name": "@pilot/workspace",
            "config": {
                "root": str(root),
                "python": sys.executable,
                "pythonPath": str(Path(__file__).resolve().parents[2] / "src"),
            },
        }
    ]
    allowed = {}
    for role, domain in DOMAINS.items():
        names = set(domain)
        if role == "supervisor":
            names |= {"plan", "resources", "revise_plan", "feedback"}
        plugins = []
        for group, members in GROUPS.items():
            selected = names & members
            if selected:
                plugins.append(
                    {
                        "id": "pilot-" + group,
                        "name": "@pilot/" + group,
                        "config": {"role": role, "tools": [tool(n) for n in sorted(selected)]},
                    }
                )
        prompt = PROMPTS[role] + (
            " Before ending send observed results to your direct parent via send_message. Wait for official child completion before starting the next dependent child."
            if role != "supervisor"
            else " Discuss freely and inspect resources before execution. To propose a delivery use pilot_plan with the full ApplicationPlan schema, never execute before the user's Start action. Propose requirements for the admitted Python standard-library HTTP server and browser HTML/JS; app.py accepts --port, serves /health and /chat via the protected model_client. The app container has no third-party framework packages. When running use infra_agent then repo_agent and send_message to reuse children. Call list_agents on resume. Only one child may be active. End your turn while a child works; official notifications reactivate you. Use pilot_verify and pilot_finish for independent acceptance."
        )
        prompt += " Tool names use pilot_ prefixes. Use pilot_tree and edit admitted modules/assets/repository working-copy files. cwd is the actual application delivery directory. Preserve client/tests. Current plan/feedback is domain data; exact earlier sources come from official session history tools. Do not execute host commands or install repository dependencies."
        if role == "supervisor":
            prompt += " Existing unstarted plans ARE editable: use pilot_status then pilot_revise_plan, not pilot_plan again. The Web provides 编辑方案 next to Start and in the plan card. For completed/paused/failed apps, use pilot_feedback to save functional changes, then wait for Continue. Do not send the user to a raw JSON/config editor. User chat does not authorize Start; never delegate merely because a draft has an execution Session. While delivery is stopped, discuss/revise only."
        plugins += [
            {"id": "persona", "name": "@deepseek-ai/dsh-persona", "config": {"prefix": prompt}},
            {"id": "history", "name": "@pilot/history", "config": {"maxSearchResults": 10}},
            {"id": "controls", "name": "@deepseek-ai/dsh-tool-subagent-control"},
            {
                "id": "role-skills",
                "name": "cordis:group",
                "group": True,
                "isolate": {"skills": True},
                "config": [
                    {"id": "skills", "name": "@deepseek-ai/dsh-skill"},
                    {
                        "id": "skill-filesystem",
                        "name": "@deepseek-ai/dsh-skill-filesystem",
                        "config": {
                            "includeDefaultRoots": False,
                            "customSkillDirs": [str(root / "skills" / ("apps_" + role))],
                            "watch": False,
                        },
                    },
                    {"id": "tool-skill", "name": "@deepseek-ai/dsh-tool-skill"},
                ],
            },
            {
                "id": "compaction",
                "name": "cordis:group",
                "group": True,
                "isolate": {"compaction": True},
                "config": [
                    {
                        "id": "compaction-basic",
                        "name": "@deepseek-ai/dsh-compaction-basic",
                        "config": compaction,
                    },
                ],
            },
        ]
        allowed[role] = ["pilot_" + n for n in names] + ["skill", *CONTROLS, *HISTORY_TOOLS]
        if role == "supervisor":
            for child in ("repo", "infra"):
                plugins.append(
                    {
                        "id": "delegate-" + child,
                        "name": "@deepseek-ai/dsh-tool-subagent",
                        "config": {
                            "provider": "spawn",
                            "toolName": child + "_agent",
                            "agentPreset": child,
                            "maxDepth": 1,
                        },
                    }
                )
                allowed[role].append(child + "_agent")
        rows.append(
            {
                "id": "preset-pilot-" + role,
                "name": "@deepseek-ai/dsh-agent-preset",
                "config": {"id": role, "plugins": plugins},
            }
        )
    from pilot_infra.presets import PROMPT, ROLE_PROMPTS, tools
    from pilot_infra.service import READ_ONLY, ROLES

    for role in ROLES:
        plugins = [
            {
                "id": "pilot-infra-tools",
                "name": "@pilot/infra-tools",
                "config": {"role": role, "tools": tools(role)},
            },
            {
                "id": "persona",
                "name": "@deepseek-ai/dsh-persona",
                "config": {"prefix": PROMPT + ROLE_PROMPTS[role]},
            },
            {"id": "history", "name": "@pilot/history", "config": {"maxSearchResults": 10}},
            {"id": "controls", "name": "@deepseek-ai/dsh-tool-subagent-control"},
            {
                "id": "role-skills",
                "name": "cordis:group",
                "group": True,
                "isolate": {"skills": True},
                "config": [
                    {"id": "skills", "name": "@deepseek-ai/dsh-skill"},
                    {
                        "id": "skill-filesystem",
                        "name": "@deepseek-ai/dsh-skill-filesystem",
                        "config": {
                            "includeDefaultRoots": False,
                            "customSkillDirs": [str(root / "skills" / role)],
                            "watch": False,
                        },
                    },
                    {"id": "tool-skill", "name": "@deepseek-ai/dsh-tool-skill"},
                ],
            },
            {
                "id": "compaction",
                "name": "cordis:group",
                "group": True,
                "isolate": {"compaction": True},
                "config": [
                    {
                        "id": "compaction-basic",
                        "name": "@deepseek-ai/dsh-compaction-basic",
                        "config": compaction,
                    },
                ],
            },
        ]
        allowed[role] = ["pilot_infra_" + n for n in ROLES[role]] + [
            "skill",
            *CONTROLS,
            *HISTORY_TOOLS,
        ]
        if role == "infra-lead":
            plugins.append({"id": "ask-user", "name": "@deepseek-ai/dsh-tool-ask-user"})
            allowed[role].append("ask_user_question")
            for child in ("infra", "repo"):
                plugins.append(
                    {
                        "id": "delegate-" + child,
                        "name": "@deepseek-ai/dsh-tool-subagent",
                        "config": {
                            "provider": "spawn",
                            "toolName": child + "_specialist",
                            "agentPreset": child + "-expert",
                            "maxDepth": 1,
                        },
                    }
                )
                allowed[role].append(child + "_specialist")
        rows.append(
            {
                "id": "preset-pilot-" + role,
                "name": "@deepseek-ai/dsh-agent-preset",
                "config": {"id": role, "plugins": plugins},
            }
        )
    rows += [
        {
            "id": "pilot-execution-sandbox",
            "name": "@pilot/execution-sandbox",
            "config": {"root": str(root)},
        },
        {
            "id": "pilot-host-settings",
            "name": "@pilot/host-settings",
            "config": {"root": str(root)},
        },
        {
            "id": "pilot-directory-picker",
            "name": (root / "harness/runtime/plugins/workspace/directory-picker.mjs").as_uri(),
        },
        {
            "id": "pilot-directory-picker-ui",
            "name": "@deepseek-ai/dsh-client-ui-directory-picker-browse",
        },
        {"id": "pilot-delivery-lifecycle", "name": "@pilot/delivery", "config": {"host": True}},
        {
            "id": "pilot-intent-router",
            "name": "@pilot/intent-router",
            "config": {
                "root": str(root),
                "catalog": str(root / "src/pilot_infra/intent_catalog.json"),
                "read_tools": sorted("pilot_infra_" + name for name in READ_ONLY),
                "history_tools": list(HISTORY_TOOLS),
            },
        },
        {"id": "pilot-context", "name": "@pilot/context"},
        {
            "id": "pilot-policy",
            "name": "@pilot/policy",
            "config": {
                "root": str(root),
                "allowed": allowed,
                "budget": Budget(max_steps=100, max_tokens=500000, output_tokens=6000).model_dump(),
            },
        },
        {
            "id": "pilot-delivery-ui",
            "name": "@pilot/delivery-ui",
            "config": {
                "root": str(root),
                "clientUrl": config.get("PILOT_CLIENT_URL", "http://127.0.0.1:18889"),
            },
        },
        {"id": "pilot-brand", "name": "@pilot/brand"},
        {"id": "pilot-infra-domain", "name": "@pilot/infra-domain", "config": {"root": str(root)}},
        {"id": "pilot-tool-system", "name": "@pilot/tool-system", "config": {"root": str(root)}},
    ]

    def resolve_plugins(items):
        for item in items:
            if item.get("name", "").startswith("@pilot/"):
                package = item["name"].split("/", 1)[1]
                item["name"] = (root / "harness/runtime/plugins" / package / "index.mjs").as_uri()
            settings = item.get("config", {})
            nested = settings if isinstance(settings, list) else settings.get("plugins", [])
            resolve_plugins(nested)

    resolve_plugins(rows)
    return [*patch, {"insert": rows}]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "business":
        from pilot_host.business_cli import main as business_main

        business_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "storage":
        from pilot_host.storage_cli import main as storage_main

        storage_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "settings":
        from pilot_host.settings_cli import main as settings_main

        settings_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "workspaces":
        from pilot_host.workspace_cli import main as workspace_main

        workspace_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "assets":
        from pilot_host.asset_cli import main as assets_main

        assets_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "tools":
        from pilot_host.tool_cli import main as tools_main

        tools_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT)
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--client-url")
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ.update(
        StorageLayout(root).environment(
            strict=os.environ.get("PILOT_REQUIRE_DATA_DISK") == "1"
            or os.name != "nt"
            and root.is_relative_to(Path("/data"))
        )
    )
    tempfile.tempdir = None
    config = credentials()
    config["PILOT_CLIENT_URL"] = args.client_url or f"http://127.0.0.1:{args.port}"
    home = root / ".local/harness-web"
    home.mkdir(parents=True, exist_ok=True)
    (root / "runs/harness-inbox").mkdir(parents=True, exist_ok=True)
    patch = home / "pilot.patch.json"
    atomic_json(patch, composition(root, config))
    prepare_profile(home, patch)
    if args.prepare_only:
        print("Official Web composition prepared")
        return
    command = [
        dsh_executable(),
        "--profile",
        "web",
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
    ]
    if os.name == "nt" and command[0].endswith(".cmd"):
        command = ["cmd.exe", "/c", *command]
    print(f"Official Harness Web at http://127.0.0.1:{args.port}", flush=True)
    result = subprocess.run(
        command, cwd=root / "runs/harness-inbox", env=runtime_environment(home, config), check=False
    )
    raise SystemExit(result.returncode)


def prepare_profile(home, patch):
    """Put generated composition in a native bundle, below editable user settings."""
    home = Path(home).resolve()
    profile = home / "profiles/web"
    bundle = profile / "node_modules/@pilot/host-composition"
    for path in (profile, bundle):
        if path.is_symlink():
            raise ValueError("Host profile path is invalid")
        path.mkdir(parents=True, exist_ok=True)
    manifest_path = profile / "package.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "name": "dsh-profile-web",
            "private": True,
            "dependencies": {},
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}},
        }
    )
    bundles = manifest["dsh"]["profile"]["bundles"]
    if "@pilot/host-composition" not in bundles:
        bundles.append("@pilot/host-composition")
    atomic_json(
        bundle / "package.json",
        {
            "name": "@pilot/host-composition",
            "version": "0.1.0",
            "private": True,
            "dsh": {"bundle": {"patch": "./cordis.patch.json"}},
        },
    )
    atomic_json(bundle / "cordis.patch.json", json.loads(Path(patch).read_text(encoding="utf-8")))
    atomic_json(manifest_path, manifest)
    user_patch = profile / "cordis.patch.yml"
    if not user_patch.exists():
        atomic_json(user_patch, [])


def runtime_environment(home, config):
    """Keep official spill/temp files off a shared, possibly full /tmp."""
    temporary = Path(home) / "tmp"
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        temporary.chmod(0o700)
    return {
        **StorageLayout(Path(home).parents[1]).environment(),
        "DSH_HOME": str(home),
        "PILOT_HOST_ROOT": str(Path(home).parents[1]),
        "REPOPILOT_API_KEY": config["REPOPILOT_API_KEY"],
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
    }


if __name__ == "__main__":
    main()
