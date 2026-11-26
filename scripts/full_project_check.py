"""Discover, catalogue and run the complete local Pilot regression suite."""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "FULL_TEST_CATALOG.md"
NODE_PATTERNS = ("*.test.mjs", "*.test.js", "*.test.cjs")
SOURCE_SUFFIXES = {".py", ".mjs", ".js", ".cjs"}


def discover(test_root: Path) -> dict[str, list[Path]]:
    """Discover executable test scripts and their local supporting scripts."""
    sources = sorted(
        (p for p in test_root.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES),
        key=lambda p: p.as_posix(),
    )
    python = [p for p in sources if p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py"))]
    node = [p for p in sources if any(p.match(pattern) for pattern in NODE_PATTERNS)]
    runnable = set(python + node)
    return {"python": python, "node": node, "support": [p for p in sources if p not in runnable]}


def python_definitions(path: Path) -> tuple[int, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in ast.walk(tree)
    )
    description = (ast.get_docstring(tree) or "").splitlines()
    return count, description[0].strip() if description else ""


def render_catalog(test_root: Path) -> str:
    inventory = discover(test_root)
    lines = [
        "# Pilot 全量测试脚本目录",
        "",
        "此目录由 `python scripts/full_project_check.py --update-catalog` 从现有测试脚本生成。",
        "执行 `python scripts/full_project_check.py` 自动发现并运行全部 Python 和 Node 测试，",
        "同时检查 Ruff、插件语法、使用说明与架构；用 `--check-catalog` 检查目录是否过期。",
        "业务功能与相关脚本的映射见 [业务测试对应表](BUSINESS_TEST_MATRIX.md)。",
        "",
        "直接运行的脚本按现有文件自动发现，辅助脚本供它们调用，不单独当成测试运行。",
        "下方 Python 数字是静态定义数量，参数化后的实际用例数以 pytest 结果为准。",
        "自动化测试中的模拟和条件跳过不等于真实模型、GPU 或远端业务验收。",
        "",
        f"Python 测试脚本：{len(inventory['python'])}；Node 测试脚本：{len(inventory['node'])}；辅助脚本：{len(inventory['support'])}。",
        "",
        "## Python 功能与业务回归",
        "",
    ]
    for path in inventory["python"]:
        count, description = python_definitions(path)
        label = path.relative_to(test_root).as_posix()
        text = f"- [{label}]({label})：{count} 个测试定义"
        if description:
            text += "；" + description.replace("|", "\\|")
        lines.append(text)
    lines.extend(["", "## Node 交互与插件回归", ""])
    for path in inventory["node"]:
        label = path.relative_to(test_root).as_posix()
        lines.append(f"- [{label}]({label})")
    lines.extend(["", "## 测试辅助脚本", ""])
    for path in inventory["support"]:
        label = path.relative_to(test_root).as_posix()
        lines.append(f"- [{label}]({label})")
    return "\n".join(lines) + "\n"


def plugin_files(root: Path) -> list[Path]:
    paths = [
        p for directory in (root / "harness/plugin", root / "harness/runtime/plugins")
        for p in directory.rglob("*") if p.is_file() and p.suffix in {".js", ".mjs", ".cjs"}
        and "node_modules" not in p.parts
    ]
    workspace = root / "harness/runtime/workspaces.mjs"
    if workspace.is_file():
        paths.append(workspace)
    return sorted(paths, key=lambda p: p.as_posix())


def run_command(name: str, command: list[str], report: Path) -> dict:
    print(f"\n=== {name} ===", flush=True)
    start = time.monotonic()
    log = report / f"{name}.log"
    try:
        with log.open("x", encoding="utf-8", newline="\n") as output:
            proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
            code = proc.wait()
    except OSError as exc:
        code = 127
        print(f"Cannot start {name}: {type(exc).__name__}", flush=True)
    return {"stage": name, "exit_code": code, "seconds": round(time.monotonic() - start, 2), "log": log.name}


def pytest_counts(report: Path) -> dict | None:
    path = report / "pytest.xml"
    if not path.is_file():
        return None
    tree = ET.parse(path)
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else list(tree.getroot().findall("testsuite"))
    totals = {key: sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return totals


def node_counts(log: Path) -> dict | None:
    if not log.is_file():
        return None
    found = dict(re.findall(r"(?m)^(?:#|ℹ)\s+(tests|pass|fail|skipped)\s+(\d+)\s*$", log.read_text(encoding="utf-8")))
    return {key: int(found[key]) for key in ("tests", "pass", "fail", "skipped")} if len(found) == 4 else None


def full_run(report: Path) -> int:
    inventory = discover(ROOT / "tests")
    if not inventory["python"] or not inventory["node"]:
        raise RuntimeError("Expected both Python and Node test scripts; refusing an incomplete suite")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for the complete suite")
    report.mkdir(parents=True, exist_ok=False)
    state = {"test_scripts": {key: len(value) for key, value in inventory.items()}, "stages": []}

    def save(result: dict) -> None:
        state["stages"].append(result)
        (report / "summary.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    python = sys.executable
    commands = [
        ("ruff", [python, "-m", "ruff", "check", "."]),
        ("catalog", [python, str(Path(__file__)), "--check-catalog"]),
        ("business-matrix", [python, "scripts/business_test_matrix.py", "--check"]),
        ("directory-readmes", [python, "scripts/update_directory_readmes.py", "--check"]),
        ("doc-links", [python, "scripts/check_doc_links.py"]),
        ("publication", [python, "scripts/check_publication.py"]),
        ("usage-guide", [python, "scripts/update_usage_guide.py", "--check"]),
        ("architecture", [python, "scripts/update_architecture.py", "--check"]),
    ]
    for name, command in commands:
        save(run_command(name, command, report))
    syntax_start = time.monotonic()
    syntax_errors = []
    files = plugin_files(ROOT)
    with (report / "plugin-syntax.log").open("x", encoding="utf-8") as log:
        for path in files:
            result = subprocess.run([node, "--check", str(path)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode:
                syntax_errors.append(path.relative_to(ROOT).as_posix())
                log.write(f"{path.relative_to(ROOT)}\n{result.stderr}\n")
        log.write(f"Checked {len(files)} plugin files; failures: {len(syntax_errors)}\n")
    print(f"\n=== plugin-syntax: {len(files)} files, {len(syntax_errors)} failures ===", flush=True)
    save({"stage": "plugin-syntax", "exit_code": int(bool(syntax_errors)), "seconds": round(time.monotonic() - syntax_start, 2), "log": "plugin-syntax.log", "checked": len(files), "failures": syntax_errors})
    result = run_command("node", [node, "--test", *(str(p) for p in inventory["node"])], report)
    result["counts"] = node_counts(report / "node.log")
    save(result)
    result = run_command("pytest", [python, "-B", "-m", "pytest", "-v", "--tb=short", f"--junitxml={report / 'pytest.xml'}"], report)
    result["counts"] = pytest_counts(report)
    save(result)
    state["passed"] = all(stage["exit_code"] == 0 for stage in state["stages"])
    (report / "summary.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport: {report}\nOverall: {'passed' if state['passed'] else 'failed'}", flush=True)
    return 0 if state["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="List automatically discovered test scripts")
    action.add_argument("--update-catalog", action="store_true", help="Regenerate the test catalogue")
    action.add_argument("--check-catalog", action="store_true", help="Fail if the test catalogue is stale")
    parser.add_argument("--report-dir", type=Path, help="New report directory; existing directories are never overwritten")
    args = parser.parse_args()
    if args.list:
        print(json.dumps({key: [p.relative_to(ROOT).as_posix() for p in paths] for key, paths in discover(ROOT / "tests").items()}, ensure_ascii=False, indent=2))
        return 0
    if args.update_catalog or args.check_catalog:
        generated = render_catalog(ROOT / "tests")
        if args.update_catalog:
            CATALOG.write_text(generated, encoding="utf-8")
            print(f"Updated {CATALOG.relative_to(ROOT)}")
            return 0
        if not CATALOG.is_file() or CATALOG.read_text(encoding="utf-8") != generated:
            print("Test catalogue is stale; run --update-catalog", file=sys.stderr)
            return 1
        print("Test catalogue matches current scripts")
        return 0
    report = (args.report_dir or ROOT / "reports/full-project-check" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    return full_run(report)


if __name__ == "__main__":
    sys.exit(main())
