"""Public evaluation projection; never publish injection or reference material."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pilot_infra.store import digest

from .problems import VERSION, problems, public_problem


def summarize(manifest, trials):
    plan = manifest["plan"]
    scheduled = len(plan["case_ids"]) * len(plan["strategies"]) * plan["repetitions"]
    rows = []
    for trial in trials:
        usage = trial.get("usage", {})
        price = None
        if plan["input_price_per_million"] is not None:
            price = (usage.get("input_tokens", 0) * plan["input_price_per_million"] + usage.get("output_tokens", 0) * plan["output_price_per_million"]) / 1000000
        verdict = trial.get("grade") or {}
        rows.append({"id": trial["id"], "case_id": trial["case_id"], "strategy": trial["strategy"],
                     "repetition": trial["repetition"], "phase": trial["phase"], "controller_kind": trial["controller_kind"],
                     "resolved_mode": trial.get("resolved_mode"), "admitted_expert_roles": trial.get("admitted_expert_roles", []),
                     "baseline_valid": verdict.get("baseline_valid", False), "passed": verdict.get("passed", False),
                     "false_completion": verdict.get("false_completion", False), "result": trial.get("result", "running"),
                     "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0), "usage_estimated": usage.get("estimated", False),
                     "cost": price, "currency": plan["currency"], "duration_seconds": trial.get("elapsed_seconds"),
                     "experts": trial.get("experts", []), "expert_calls": trial.get("expert_calls", []), "trajectory_complete": trial.get("trajectory_complete", False),
                     "interventions": [{"kind": i["kind"], "time": i["time"]} for i in trial.get("interventions", [])],
                     "native_approvals": trial.get("native_approvals", 0),
                     "cleanup_passed": trial.get("cleanup", {}).get("passed", False),
                     "error": "试验未通过，详细证据保存在所有者私有记录中。" if trial.get("error") else None,
                     "grader_receipt_sha256": digest(verdict) if trial.get("grade") else None})
    aggregates = []
    for strategy in plan["strategies"]:
        selected = [row for row in rows if row["strategy"] == strategy]
        valid = [row for row in selected if row["baseline_valid"] and row["phase"] == "settled" and row["controller_kind"] == "real_model"]
        aggregates.append({"strategy": strategy, "scheduled": len(plan["case_ids"]) * plan["repetitions"],
                           "executed_valid": len(valid), "passed": sum(row["passed"] for row in valid),
                           "false_completions": sum(row["false_completion"] for row in valid),
                           "tokens": sum(row["tokens"] for row in selected),
                           "unavailable": sum(row["phase"] == "unavailable" for row in selected)})
    paired = len(plan["strategies"]) > 1 and all(any(row["case_id"] == identity and row["strategy"] == strategy and row["repetition"] == repetition and row["baseline_valid"] and row["phase"] == "settled" for row in rows)
                 for identity in plan["case_ids"] for strategy in plan["strategies"] for repetition in range(1, plan["repetitions"] + 1))
    return {"protocol": VERSION, "manifest_sha256": digest(manifest), "phase": plan["phase"],
            "identities": manifest["identities"], "scheduled": scheduled, "recorded": len(rows),
            "max_tokens": plan["max_tokens"], "used_tokens": sum(row["tokens"] for row in rows),
            "pricing": "priced" if plan["input_price_per_million"] is not None else "unpriced",
            "fully_paired": paired, "trials": rows, "strategies": aggregates,
            "limits": ["开发集成本校准不能作为留出集效果。", "同家族变体不是独立根因样本；本轮留出包含已有源码经验的引擎。",
                       "配对不完整或样本较小时不能宣称领先或统计显著。", "原生审批由受控评测授权器处理，与修复及预算人工介入分别记录。",
                       "健康对照和故障注入采用确定性编排，不计为 Agent 推理。", "token预算累计控制主控及原生子会话；目标推理服务的探针用量单列在私有业务证据中。"]}


def publish(root, run_id, report):
    from repopilot.storage import atomic_json
    directory = Path(root) / "reports/domain-evaluation"
    name = run_id + ".json"
    if Path(name).name != name or not all(c in "0123456789abcdef" for c in run_id):
        raise ValueError("Invalid evaluation run name")
    atomic_json(directory / name, report)
    atomic_json(directory / "current.json", {"file": name, "sha256": digest(report)})


def load_report(path, expected=None):
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("Invalid evaluation report file")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("protocol") != VERSION or expected is not None and digest(report) != expected:
        raise ValueError("Evaluation report identity changed")
        # Only expose the expected public structure, including numeric fields used by UI.
    if not isinstance(report.get("trials"), list) or not isinstance(report.get("strategies"), list) or not isinstance(report.get("identities"), dict) or not isinstance(report.get("limits"), list):
        raise ValueError("Incomplete evaluation report")
    for key in ("scheduled", "recorded", "max_tokens", "used_tokens"):
        if not isinstance(report.get(key), int) or report[key] < 0:
            raise ValueError("Invalid evaluation counts")
    for row in report["trials"]:
        if not isinstance(row.get("tokens"), int) or not isinstance(row.get("experts"), list) or not isinstance(row.get("interventions"), list):
            raise ValueError("Invalid trial report")
    for row in report["strategies"]:
        if not all(isinstance(row.get(k), int) for k in ("scheduled", "executed_valid", "passed", "false_completions", "tokens", "unavailable")):
            raise ValueError("Invalid strategy report")
    return report


def current(root, report_id=None):
    directory = Path(root) / "reports/domain-evaluation"
    result = {"protocol": VERSION, "cases": [public_problem(row) for row in problems()], "report": None, "error": None,
              "history": [], "selected_report_id": None}
    for path in sorted(directory.glob("*.json"), key=lambda p: p.lstat().st_mtime, reverse=True):
        if not re.fullmatch(r"[0-9a-f]{1,64}", path.stem):
            continue
        item = {"id": path.stem, "saved_at": datetime.fromtimestamp(path.lstat().st_mtime, UTC).isoformat(), "available": False}
        try:
            report = load_report(path)
            item.update(available=True, phase=report.get("phase"), recorded=report["recorded"], used_tokens=report["used_tokens"], sha256=digest(report))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            item["error"] = "历史报告格式或身份未确认"
        result["history"].append(item)
    index = directory / "current.json"
    if not index.is_file() and not result["history"]:
        return result
    try:
        if index.is_symlink() or report_id is not None and not isinstance(report_id, str):
            raise ValueError("Invalid evaluation report selection")
        pointer = json.loads(index.read_text(encoding="utf-8")) if index.is_file() and not index.is_symlink() else None
        name = (report_id + ".json") if report_id is not None and isinstance(report_id, str) else pointer["file"] if pointer else result["history"][0]["id"] + ".json"
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{1,64}\.json", name):
            raise ValueError("Invalid evaluation report name")
        result["selected_report_id"] = Path(name).stem
        report = load_report(directory / name, pointer["sha256"] if pointer and pointer["file"] == name else None)
        result["report"] = report
        result["report_sha256"] = digest(report)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["error"] = "评测报告暂不可读取或身份未确认，请核对正式发布回执。"
    return result
