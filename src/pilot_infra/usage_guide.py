"""One business guide shared by the settings page and generated Markdown."""

import json
from datetime import UTC, datetime
from pathlib import Path

CATALOG = Path(__file__).with_name("usage_guide.json")


def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def current_guide(infra):
    value = catalog()
    warnings = []
    try:
        assets = infra.assets.read()
    except (OSError, ValueError, KeyError, TypeError):
        assets = {"machines": [], "models": [], "images": [], "engine_source_profiles": []}
        warnings.append("推理资产配置暂不可读取，登记条件待核对。")
    try:
        path = infra.root / ".local/workbench-profiles.json"
        builds = json.loads(path.read_text(encoding="utf-8"))["profiles"] if path.is_file() else []
        database = infra.root / "runs/pilot-infra/infra.sqlite3"
        if database.is_file():
            import sqlite3

            with sqlite3.connect("file:" + database.as_posix() + "?mode=ro", uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='software_recipes'").fetchone():
                    saved = [json.loads(row[0]) for row in db.execute("SELECT data FROM software_recipes")]
                    keys = {r["id"] for r in saved}
                    builds = [r for r in builds if r["id"] not in keys] + saved
    except (OSError, ValueError, KeyError, TypeError):
        builds = []
        warnings.append("独立工作区执行器配置暂不可读取，启用条件待核对。")
    machines = assets.get("machines", [])
    models, images = assets.get("models", []), assets.get("images", [])
    recipes = assets.get("engine_source_profiles", [])
    serving = bool(assets.get("ready"))
    engine = serving and any(p.get("image_id") in {i["id"] for i in images} for p in recipes)
    fleet = {"assets": [], "stacks": [], "executors": []}
    if (infra.root / ".local/resources/registry.sqlite3").is_file():
        try:
            from .asset_service import AssetService

            fleet = AssetService(infra.root).catalog()
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append("节点资产配置暂不可读取，登记条件待核对。")
    nodes = [e for e in fleet["executors"] if e.get("enabled")]
    agent_models = []
    try:
        from pilot_core.config import host_settings

        settings = host_settings(infra.root)
        if settings and settings["applied_revision"] > 0:
            agent_models = [
                {"label": m["display_name"], "vision": m["vision"], "enabled": m["enabled"]}
                for m in settings["applied"]["models"]
            ]
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append("控制模型目录暂不可读取，图片能力需核对已应用配置。")
    vision = any(m["vision"] and m["enabled"] for m in agent_models)
    gates = {
        "always": {
            "state": "available",
            "label": "已实现",
            "note": "操作入口按任务状态和权限显示。",
        },
        "serving": {
            "state": "registered" if serving else "unconfigured",
            "label": "已登记推理条件" if serving else "需先登记推理条件",
            "note": "需登记模型、环境和可执行服务器；任务仍须审阅和验收。",
        },
        "engine": {
            "state": "registered" if engine else "unconfigured",
            "label": "已登记源码配方" if engine else "需先登记匹配配方",
            "note": "源码配方必须与登记引擎环境匹配，加载前另行审阅制品。",
        },
        "workbench": {
            "state": "registered" if builds else "unconfigured",
            "label": "已登记软件配方" if builds else "需先登记软件配方",
            "note": "登记不代表通道可达；无执行器时没有创建入口。",
        },
        "fleet": {
            "state": "registered" if nodes else "unconfigured",
            "label": "已登记节点执行器" if nodes else "需启用节点执行器",
            "note": "上次核验和登记不能替代当前连接、资源、审批或GPU业务验收。",
        },
        "vision": {
            "state": "registered" if vision else "unconfigured",
            "label": "已登记图片模型" if vision else "需先添加图片模型",
            "note": "在工作区与模型保存同步支持图片的API，并在输入框选择；能力声明和连接测试不代表识图质量已验收。",
        },
    }
    # Project only public labels and quotas, never private paths or credentials.
    configuration = {
        "checked_at": datetime.now(UTC).isoformat(),
        "models": [str(i.get("label", i.get("id", ""))) for i in models],
        "engines": [str(i.get("label", i.get("id", ""))) for i in images],
        "engine_recipes": [str(i.get("label", i.get("id", ""))) for i in recipes],
        "cpu_executors": len(builds),
        "node_executors": len(nodes),
        "agent_models": agent_models,
        "node_assets": len(fleet["assets"]),
        "stack_recipes": len(fleet["stacks"]),
        "execution_quotas": [
            {
                "server": str(m.get("label", m.get("id", ""))),
                "gpu_ids": m.get("allowed_gpu_ids", []),
                "executor_registered": bool(m.get("executor_available")),
            }
            for m in machines
        ],
        "warnings": warnings,
    }
    return {
        **value,
        "configuration": configuration,
        "features": [{**item, "availability": gates[item["gate"]]} for item in value["features"]],
    }


def markdown():
    value = catalog()
    lines = [
        "# Pilot 当前指南",
        "",
        f"说明更新：{value['updated_at']}。本文件与 Web「设置 → 使用说明」来自同一业务目录。",
        "",
        value["intro"],
        "",
        value["configuration_note"],
        "",
        "## 从需求到交付",
        "",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(value["quick_start"], 1)]
    environment = value.get("environment_setup")
    if environment:
        lines += ["", "## " + environment["title"], "", environment["summary"], ""]
        lines += [f"{i}. {step}" for i, step in enumerate(environment["steps"], 1)]
        lines += ["", "边界：", ""] + ["- " + limit for limit in environment["limits"]]
    for group in value["groups"]:
        lines += ["", "## " + group["title"], ""]
        for item in value["features"]:
            if item["group"] != group["id"]:
                continue
            lines += ["### " + item["title"], "", item["summary"], "", "入口：" + item["entry"], ""]
            workflow = item.get("workflow")
            if workflow:
                lines += [
                    "典型场景：" + workflow["scenario"],
                    "",
                    "执行者：" + workflow["actors"],
                    "",
                    "资源来源：",
                    "",
                ]
                lines += ["- " + row for row in workflow["resources"]]
                lines += ["", "操作流程：", ""]
            if item["gate"] == "engine":
                lines += ["启用条件：匹配的引擎源码配方及可执行推理资产已登记。", ""]
            elif item["gate"] == "workbench":
                lines += [
                    "启用条件：可达的 CPU 执行器与固定配方已登记；无配置时不提供创建入口。",
                    "",
                ]
            elif item["gate"] == "serving":
                lines += ["启用条件：模型、引擎环境与可执行服务器已登记，任务获准后执行。", ""]
            elif item["gate"] == "fleet":
                lines += [
                    "启用条件：目标节点持久执行器已配置并核验；登记不代表当前可连或业务已验收。",
                    "",
                ]
            lines += [f"{i}. {step}" for i, step in enumerate(item["steps"], 1)]
            if workflow:
                for key, label in [
                    ("decisions", "需要你决定的地方"),
                    ("outputs", "交付结果"),
                    ("failure", "失败与恢复"),
                ]:
                    lines += ["", label + "：", ""] + ["- " + row for row in workflow[key]]
            if item.get("roles"):
                lines += [
                    "",
                    "默认协作：" + item["default_strategy"] + "。",
                    "",
                    "| 角色 | 使用场景 | 职责 | 默认行为 |",
                    "|---|---|---|---|",
                ]
                lines += [
                    "| "
                    + " | ".join(role[k] for k in ("title", "when", "responsibility", "default"))
                    + " |"
                    for role in item["roles"]
                ]
                lines += ["", "策略：", ""] + ["- " + row for row in item["strategies"]]
                lines += ["", "程序与历史角色：", ""] + [
                    "- " + row for row in item["support_components"]
                ]
            if item.get("example"):
                lines += ["", "示例需求：", "", "> " + item["example"]]
            lines += ["", "边界：", ""] + ["- " + limit for limit in item["limits"]]
    lines += ["", "## 尚未完成的能力", ""] + ["- " + limit for limit in value["pending"]]
    lines += [
        "",
        "## 当前配置与证据",
        "",
        "静态文档列出业务范围，服务器、模型、执行器及设备配额以 Web 打开说明时读取的当前配置为准。登记状态不是连通、权限批准或业务通过。",
        "",
        "当前能力和待验证范围见 [能力与边界](CAPABILITIES.md)；系统路线见 [架构](ARCHITECTURE.md)。整机管理见 [资源与权限](PILOT_RESOURCES_AND_AUTHORITY.md)，引擎链路见 [源码交付](PILOT_ENGINE_SOURCE.md)。",
        "",
        "各项能力的实现范围与待验证场景见 [能力与边界](CAPABILITIES.md)。",
    ]
    return "\n".join(lines) + "\n"
