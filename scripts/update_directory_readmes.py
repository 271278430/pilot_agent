"""Maintain a concise inventory for each source directory."""
from __future__ import annotations

import argparse
import ast
import os
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
TOP = (".github", "docker", "docs", "evals", "harness", "prompts", "scripts", "skills", "src", "tasks", "tests")
SKIP = {".git", ".venv", ".local", ".cache", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}
DIRECTORIES = {
    ".github": "GitHub 自动化配置。",
    ".github/workflows": "持续集成工作流，负责安装依赖和执行检查。",
    "docker": "受限执行环境使用的镜像定义。",
    "docs": "项目架构、使用方法、开发约定和能力边界。",
    "docs/architecture": "根据架构目录生成的模块图，修改源目录后重新生成。",
    "docs/assets": "README 使用的架构图和流程动画；动画由 `scripts/render_workflow.py` 生成。",
    "evals": "固定评测协议与测试材料。",
    "evals/collaboration": "主控和专家协作的评测协议。",
    "evals/external": "外部代码修复评测使用的公开素材及其来源信息。",
    "evals/infra": "环境与推理交付的评测协议和数据来源。",
    "harness": "官方 Harness 的项目侧配置和扩展。",
    "harness/plugin": "角色、权限、预算和上下文相关的通用插件模块。",
    "harness/runtime": "固定版本的 Harness 运行时依赖及工作区适配。",
    "harness/runtime/plugins": "Pilot 的业务工具、策略和界面插件。",
    "prompts": "主控及各专家角色使用的提示词。",
    "scripts": "启动、测试、文档生成及受限环境操作脚本。旧环境专用脚本需先按目标环境核对。",
    "skills": "主控与专家按角色加载的任务技能。",
    "src": "Python 包源码：Host、业务服务、节点执行、验收和兼容入口。",
    "src/infra_pilot": "独立的训练与推理环境实验入口，使用受限配方。",
    "src/pilot_apps": "模型应用计划、工作区、生命周期和验收。",
    "src/pilot_collab": "协作状态、历史记录与保留的实验协议。",
    "src/pilot_core": "配置、模型协议、执行隔离与公共存储。",
    "src/pilot_delivery": "模型引擎、预览和交付协议。",
    "src/pilot_eval": "领域评测、预算准入、评分和报告。",
    "src/pilot_host": "官方 Host 装配、业务桥接和命令行入口。",
    "src/pilot_infra": "服务器、资产、任务、节点执行、源码构建与推理验收。",
    "src/pilot_projects": "项目登记、资源预检、源码快照和模型接入。",
    "src/repopilot": "仓库修复实验、检索、工具和验收入口。",
    "tasks": "开发状态与可复制的任务示例。",
    "tests": "Python 和 Node 回归测试、夹具与业务覆盖索引。",
}
PLUGIN = {
    "brand": "产品外观、标识和界面样式",
    "context": "会话上下文适配",
    "delivery": "模型交付工具和状态",
    "delivery-ui": "模型交付界面及预览通信",
    "execution-sandbox": "执行沙箱策略",
    "history": "会话历史隔离",
    "host-settings": "主控配置、密钥读取和设置界面",
    "infra-domain": "推理任务领域接口与工作台",
    "infra-tools": "基础设施领域工具",
    "intent-router": "业务意图识别与路由",
    "model-assets": "模型资产工具",
    "policy": "权限与执行策略",
    "repo-tools": "仓库和源码操作工具",
    "tool-system": "工具与 MCP 服务配置",
    "workspace": "工作区登记、选择和目录管理",
}
WORDS = {
    "acceptance": "验收", "admission": "准入", "agent": "智能体", "analysis": "分析",
    "api": "API", "app": "应用", "apps": "应用", "architecture": "架构", "asset": "资产",
    "assets": "资产", "atomic": "原子写入", "authority": "授权", "backend": "后端",
    "benchmark": "基准测试", "brand": "界面标识", "budget": "预算", "build": "构建",
    "business": "业务", "catalog": "目录", "check": "检查", "cleanup": "清理", "cli": "命令行",
    "client": "客户端", "collaboration": "协作", "compaction": "上下文压缩", "compat": "兼容性",
    "compatibility": "兼容性", "config": "配置", "container": "容器", "context": "上下文",
    "contract": "协议", "contracts": "协议", "control": "控制", "cpu": "CPU", "delivery": "交付",
    "deployment": "部署", "diagram": "图示", "distributed": "跨节点", "docker": "Docker",
    "engine": "引擎", "evaluation": "评测", "eval": "评测", "execution": "执行", "expert": "专家",
    "fixture": "测试夹具", "fixtures": "测试夹具", "fleet": "远端节点", "full": "完整", "gpu": "GPU",
    "history": "历史记录", "host": "主控", "http": "HTTP", "inference": "推理", "infra": "基础设施",
    "intent": "意图", "lifecycle": "生命周期", "local": "本地", "mcp": "MCP", "metrics": "指标",
    "model": "模型", "native": "原生", "network": "网络", "node": "节点", "occupancy": "占用",
    "official": "官方", "operator": "算子", "paths": "路径", "plan": "方案", "policy": "策略",
    "preview": "预览", "probe": "探测", "process": "进程", "project": "项目", "projects": "项目",
    "protocol": "协议", "python": "Python", "ray": "Ray", "recovery": "恢复", "registry": "登记",
    "relay": "中继", "remote": "远端", "repo": "仓库", "request": "请求", "resource": "资源",
    "resources": "资源", "result": "结果", "routing": "路由", "runtime": "运行时", "sandbox": "沙箱",
    "scope": "范围", "service": "服务", "settings": "设置", "snapshot": "快照", "source": "源码",
    "storage": "存储", "store": "持久化", "system": "系统", "task": "任务", "test": "测试",
    "tool": "工具", "tools": "工具", "transport": "传输", "ui": "界面", "usage": "使用说明",
    "validation": "校验", "vllm": "vLLM", "worker": "执行器", "workbench": "工作台", "workspace": "工作区",
    "approval": "审批", "reservations": "预留", "domain": "领域", "isolation": "隔离", "interaction": "交互",
    "environment": "环境", "harness": "Harness", "controls": "控制", "guide": "指南", "folders": "文件夹",
    "version": "版本", "authorization": "权限", "completion": "完成", "memory": "记忆", "composition": "组合",
    "default": "默认值", "terminal": "终端", "planning": "规划", "failed": "失败", "logs": "日志",
    "git": "Git", "goal": "目标", "applications": "应用", "integration": "集成", "download": "下载",
    "knowledge": "知识", "live": "实时", "multimodal": "多模态", "management": "管理", "registration": "登记",
    "pilot": "Pilot", "revision": "修订", "stream": "流式输出", "retrieval": "检索", "image": "镜像",
    "owner": "所有者", "start": "启动", "picker": "选择器", "stub": "替身",
    "analyze": "分析", "application": "应用", "candidate": "候选", "coordinate": "协调", "deploy": "部署",
    "lead": "主控", "owned": "自有", "recover": "恢复", "repair": "修复", "supervisor": "主控",
    "single": "单角色", "ml": "机器学习", "issue": "问题",
}
FILES = {
    "__init__.py": "Python 包入口。",
    "README.md": "本目录的用途和文件索引。",
    "SKILL.md": "该角色技能的职责、使用条件和操作约定。",
    "package.json": "Node 包信息、入口或依赖声明。",
    "package-lock.json": "Node 依赖的精确版本与完整性信息。",
    "index.mjs": "插件或模块的注册入口。",
    "client.js": "对应功能的浏览器界面和交互。",
    "cli.mjs": "对应功能的 Node 命令行入口。",
    "ci.yml": "安装依赖并运行完整回归的 CI 工作流。",
    "Dockerfile": "定义隔离执行环境的容器镜像。",
    "architecture.json": "系统组件、关系与模块图的同源数据。",
    "usage_guide.json": "业务入口、步骤和权限说明的同源目录。",
    "intent_catalog.json": "业务意图及动作分类目录。",
    "workflow.gif": "主页展示的四阶段工作流程动画。",
    "architecture.svg": "主页展示的系统分层图。",
    "board.json": "当前开发事项和状态。",
    "real-task.example.json": "仓库修复任务的可编辑示例。",
    "conftest.py": "为 pytest 提供共享配置和测试夹具。",
    "ARCHITECTURE.md": "系统分层、数据流和代码边界。",
    "CAPABILITIES.md": "已实现能力与仍需验证的范围。",
    "CURRENT_GUIDE.md": "从业务目录生成的使用指南。",
    "DEVELOPMENT.md": "开发流程、回归命令和文档维护方法。",
    "DEVELOPMENT_LOG.md": "版本开发记录。",
    "PILOT_ARCHITECTURE_MAP.md": "从架构目录生成的组件和模块图册。",
    "PLAN.md": "近期计划与未完成工作。",
    "BUSINESS_TEST_MATRIX.md": "业务功能与相关测试脚本的映射。",
    "FULL_TEST_CATALOG.md": "自动发现的 Python、Node 测试及辅助脚本目录。",
    "business_test_map.json": "业务功能到测试文件的机器可读映射。",
    "render_workflow.py": "生成主页流程动画，需安装 Pillow 和中文字体。",
    "update_directory_readmes.py": "生成或检查各目录的 README 索引。",
    "cpu_ray_gateway.py": "隔离 CPU Ray 实验使用的受限网络中继原型。",
}


def topic(name: str) -> str:
    parts = re.split(r"[-_.]+", name.lower())
    return " ".join(WORDS.get(word, word) for word in parts if word and word not in {"test", "tests", "py", "js", "mjs"}) or "目录"


def describe_directory(path: Path) -> str:
    relative = path.relative_to(ROOT).as_posix()
    if relative in DIRECTORIES:
        return DIRECTORIES[relative]
    if relative.startswith("harness/runtime/plugins/") and path.name in PLUGIN:
        return PLUGIN[path.name] + "插件。"
    if relative.startswith("skills/"):
        return topic(path.name) + "角色技能。"
    return topic(path.name) + "相关文件。"


def describe_file(path: Path) -> str:
    if path.name in FILES:
        return FILES[path.name]
    name = path.name
    if path.suffix == ".py":
        if "fixture" in path.stem:
            return "为" + topic(path.stem.replace("fixture", "")) + "测试提供夹具。"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            doc = (ast.get_docstring(tree) or "").splitlines()
        except (OSError, SyntaxError, UnicodeError):
            doc = []
        if path.parent.name == "tests" or name.startswith("test_"):
            return "验证" + topic(path.stem) + "相关行为与边界。"
        if doc and len(doc[0]) <= 140:
            return doc[0].strip().replace("|", "\\|")
        return topic(path.stem) + "相关实现。"
    if name.endswith((".test.mjs", ".test.js", ".test.cjs")):
        return "验证" + topic(name) + "的交互或插件行为。"
    if path.suffix == ".md":
        try:
            first = next((s.strip("# ").strip() for s in path.read_text(encoding="utf-8").splitlines() if s.startswith("# ")), "")
        except (OSError, UnicodeError):
            first = ""
        return (first or topic(path.stem)) + "。"
    if path.suffix == ".svg":
        return topic(path.stem) + "架构图。"
    if path.suffix == ".json":
        return topic(path.stem) + "的结构化配置或数据。"
    if path.suffix in {".mjs", ".js"}:
        return topic(path.stem) + "相关的 Node 模块。"
    if path.suffix in {".sh", ".ps1", ".cmd"}:
        return topic(path.stem) + "操作脚本。"
    if path.suffix == ".txt":
        return topic(path.stem) + "角色提示词。"
    if name.endswith(".tar.gz"):
        return "固定来源的公开评测源码归档。"
    return topic(path.stem) + "相关文件。"


def link(name: str, directory: bool = False) -> str:
    suffix = "/README.md" if directory else ""
    return f"[{name}]({quote(name)}{suffix})"


def render(path: Path) -> str:
    children = sorted((x for x in path.iterdir() if x.is_dir() and x.name not in SKIP and not x.name.endswith(".egg-info")), key=lambda p: p.name.casefold())
    files = sorted((x for x in path.iterdir() if x.is_file() and x.name != "README.md" and x.suffix not in {".pyc", ".pyo"}), key=lambda p: p.name.casefold())
    lines = [f"# {path.name}", "", describe_directory(path), ""]
    if children:
        lines.extend(["## 子目录", "", "| 目录 | 内容 |", "| --- | --- |"])
        lines.extend(f"| {link(child.name, True)} | {describe_directory(child)} |" for child in children)
        lines.append("")
    lines.extend(["## 文件", "", "| 文件 | 内容 |", "| --- | --- |", "| [README.md](README.md) | 本目录的用途和文件索引。 |"])
    lines.extend(f"| {link(file.name)} | {describe_file(file).replace(chr(10), ' ')} |" for file in files)
    lines.extend(["", "目录内容调整后，运行 `python scripts/update_directory_readmes.py` 更新索引。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directories = []
    for top in TOP:
        base = ROOT / top
        if not base.is_dir():
            continue
        for current, subdirs, _ in os.walk(base):
            subdirs[:] = [name for name in subdirs if name not in SKIP and not name.endswith(".egg-info")]
            directories.append(Path(current))
    stale = []
    for directory in sorted(directories):
        target = directory / "README.md"
        expected = render(directory)
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            if args.check:
                stale.append(target.relative_to(ROOT).as_posix())
            else:
                target.write_text(expected, encoding="utf-8")
    if stale:
        print("Stale or missing directory indexes:", *stale, sep="\n")
        return 1
    print(f"Directory indexes {'checked' if args.check else 'updated'}: {len(directories)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
