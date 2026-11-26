"""Validated, read-only architecture catalogue shared by Web and documentation."""

import json
from pathlib import Path

from .diagram import svg, validate

CATALOG = Path(__file__).with_name("architecture.json")
ROOT = Path(__file__).resolve().parents[2]
KINDS = {"entry", "agent", "policy", "tool", "worker", "memory"}


def catalog():
    from .service import ROLES

    value = json.loads(CATALOG.read_text(encoding="utf-8"))
    components = value["components"]
    if value["default_strategy"] != "auto":
        raise ValueError("Architecture default strategy must match the Pilot preset")
    for identity, node in components.items():
        if not identity.replace("_", "").isalnum() or node["kind"] not in KINDS:
            raise ValueError("Invalid architecture component")
        if set(node) - {"label", "subtitle", "kind", "points", "refs", "tools", "role"}:
            raise ValueError("Unknown architecture component field")
        for ref in node["refs"]:
            path = ROOT / ref
            if not ref.startswith(("src/", "harness/", "skills/")) or not path.resolve().is_relative_to(ROOT) or not path.is_file():
                raise ValueError("Invalid architecture source reference")
        if not set(node["tools"]) <= set().union(*ROLES.values()):
            raise ValueError("Architecture refers to unregistered tools")
        if "role" in node:
            if node["role"] not in ROLES:
                raise ValueError("Architecture refers to an unregistered role")
            node["tools"] = sorted(ROLES[node["role"]])
    if {n.get("role") for n in components.values() if n.get("role")} != set(ROLES):
        raise ValueError("Architecture must cover all Pilot roles")
    pages = {v["id"]: v for v in value["views"]}
    if len(pages) != len(value["views"]):
        raise ValueError("Duplicate architecture view")
    entry = value["entry"]
    if entry not in pages or pages[entry]["parent"] is not None:
        raise ValueError("Invalid architecture entry")
    for view in value["views"]:
        lineage = set()
        cursor = view["id"]
        while cursor != entry:
            if cursor in lineage or cursor not in pages or pages[cursor]["parent"] not in pages:
                raise ValueError("Invalid architecture hierarchy")
            lineage.add(cursor)
            cursor = pages[cursor]["parent"]
        if view["presentation"] != "diagram":
            raise ValueError("Invalid architecture presentation")
        if any(link["view"] not in pages for link in view["links"]):
            raise ValueError("Invalid architecture module link")
        validate(view["diagram"], components, pages)
    modules = [m for layer in pages[entry]["layers"] for m in layer["modules"]]
    if len(modules) != len(set(modules)) or set(modules) != {v["id"] for v in pages.values() if v["parent"] == entry}:
        raise ValueError("System layers must cover every top-level module once")
    return value


def current_architecture(infra):
    from .usage_guide import current_guide

    value = catalog()
    return {**value, "views": [{**v, "svg": svg(v, value["components"])} for v in value["views"]],
            "configuration": current_guide(infra)["configuration"]}


def markdown():
    value = catalog()
    lines = ["# Pilot 项目架构图", "", f"更新：{value['updated_at']}；与 Web「设置 → 项目架构」来自同一目录。", "",
             value["intro"], "", value["note"], "", "默认策略：auto。主控按需使用零位、一位或先后两位专家；最多一位活动专家。", ""]
    pages = {v["id"]: v for v in value["views"]}
    for view in value["views"]:
        trail = [view]
        while trail[-1]["parent"] is not None:
            trail.append(pages[trail[-1]["parent"]])
        lines += [f'<a id="{view["id"]}"></a>', "", "## " + view["label"], "",
                  " → ".join(f'[{v["label"]}](#{v["id"]})' for v in reversed(trail)), "", view["description"], ""]
        lines += [f'![{view["label"]}](architecture/{view["id"]}.svg)', "", f'[打开完整SVG](architecture/{view["id"]}.svg)', ""]
        if view["diagram"].get("boundaries"):
            lines += ["图内边界：", ""] + [f'- {b["label"]}：{b["subtitle"]}' for b in view["diagram"]["boundaries"]] + [""]
        lines += ["- " + p for p in view["notes"]] + [""]
        children = [v for v in value["views"] if v["parent"] == view["id"]]
        if children and view["id"] != value["entry"]:
            lines += ["展开内部设计：", ""] + [f'- [{v["label"]}](#{v["id"]})' for v in children] + [""]
        if view["links"]:
            lines += ["协作模块：", ""] + [f'- [{pages[link["view"]]["label"]}](#{link["view"]})：{link["note"]}' for link in view["links"]] + [""]
    lines += ["## 组件与代码入口", ""]
    for component in value["components"].values():
        lines += ["### " + component["label"], "", component["subtitle"], ""]
        lines += ["- " + p for p in component["points"]]
        lines += ["", "代码：" + "；".join(f"[{ref}](../{ref})" for ref in component["refs"]), ""]
        if component["tools"]:
            lines += ["当前领域工具：" + "、".join("`pilot_infra_" + t + "`" for t in component["tools"]), ""]
    lines += ["## 当前边界", ""] + ["- " + b for b in value["boundaries"]]
    return "\n".join(lines) + "\n"
