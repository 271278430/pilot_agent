"""Validate and render the business-to-regression map against the live catalogue."""

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from .full_project_check import discover
else:
    from full_project_check import discover

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "src/pilot_infra/usage_guide.json"
MAP = ROOT / "tests/business_test_map.json"
OUTPUT = ROOT / "tests/BUSINESS_TEST_MATRIX.md"
LIVE_VALIDATORS = [
    ("scripts/domain_eval.py", "Owner 领域评测编排；执行前需配置隔离环境和预算"),
    ("scripts/evaluate_pilot_infra.py", "历史固定 GPU 业务验收；原 A100 资产已清理，须先重新配置"),
    ("scripts/evaluate_infra_agents.py", "历史真实模型 Agent 对照；须重新准备环境和预算"),
    ("scripts/infra_remote_eval.py", "历史 A100 QA 包装；依赖已清理的旧目录，不能直接使用"),
    ("scripts/benchmark_infra.py", "历史真实服务器基准；硬编码的旧实验目录已清理"),
    ("scripts/harness_host_live.py", "真实模型/应用交付验证；会产生 API 和部署副作用"),
]


def render(guide: Path, mapping: Path, test_root: Path, project_root: Path | None = None) -> str:
    features = json.loads(guide.read_text(encoding="utf-8"))["features"]
    links = json.loads(mapping.read_text(encoding="utf-8"))
    ids = [feature["id"] for feature in features]
    if len(ids) != len(set(ids)) or set(ids) != set(links):
        raise ValueError("Business features and test map differ; update business_test_map.json")
    found = discover(test_root)
    executable = {p.relative_to(test_root).as_posix() for p in found["python"] + found["node"]}
    lines = [
        "# Pilot 业务与测试对应表", "",
        "由使用说明中的功能目录和 `business_test_map.json` 生成。",
        "运行 `python scripts/business_test_matrix.py --update` 更新，`--check` 校验功能和脚本一致。",
        "这些关联表示相关自动化回归，不代表全部场景、真实GPU或模型业务已验收。", "",
        "| 业务功能 | 相关测试脚本 |", "|---|---|",
    ]
    for feature in features:
        paths = links[feature["id"]]
        if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)) or any(p not in executable for p in paths):
            raise ValueError(f"Invalid or missing test scripts for {feature['id']}")
        refs = "、".join(f"[{p}]({p})" for p in paths)
        lines.append(f"| {feature['title']} | {refs} |")
    if project_root is not None:
        lines.extend(["", "## 实机和真实模型评测脚本", "",
                      "以下脚本可能产生真实模型费用、服务器或 GPU 副作用，不纳入默认回归；旧目录清理后不能直接重放。", "",
                      "| 脚本 | 范围和前提 |", "|---|---|"])
        for path, note in LIVE_VALIDATORS:
            if not (project_root / path).is_file():
                raise ValueError(f"Missing live validator: {path}")
            lines.append(f"| [{path}](../{path}) | {note} |")
        lines.extend(["", "历史冻结协议与数据索引位于 `evals/`，验收范围见各次报告。"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--update", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        text = render(GUIDE, MAP, ROOT / "tests", ROOT)
    except (ValueError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.update:
        OUTPUT.write_text(text, encoding="utf-8")
        print(f"Updated {OUTPUT.relative_to(ROOT)}")
        return 0
    if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != text:
        print("Business test matrix is stale; run --update", file=sys.stderr)
        return 1
    print("Business test matrix matches current features and scripts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
