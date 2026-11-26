"""Analyze completed paired trials without changing the registered task or grader."""

import argparse
import json
import math
import random
import statistics
from pathlib import Path


def wilson(successes, count):
    z = 1.959963984540054
    p = successes / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def analyze(directory):
    records = json.loads((directory / "records.json").read_text(encoding="utf-8"))
    protocol = json.loads((directory / "protocol.json").read_text(encoding="utf-8"))
    pairs = {}
    for row in records:
        group = pairs.setdefault((row["repeat"], row["profile"]), {})
        if row["orchestration"] in group:
            raise ValueError(
                "Duplicate trial; cannot summarize repeated records as independent evidence"
            )
        group[row["orchestration"]] = row
    if (
        len(pairs) != len(protocol["profiles"]) * protocol["repeats"]
        or any(not {"single", "multi"} <= p.keys() for p in pairs.values())
        or any(
            not set(range(protocol["repeats"])) == {key[0] for key in pairs if key[1] == profile}
            for profile in protocol["profiles"]
        )
        or (
            protocol.get("deterministic_baseline")
            and any("deterministic" not in p for p in pairs.values())
        )
    ):
        raise ValueError("Do not summarize incomplete or unpaired trials as a final result")
    groups = {}
    for mode in ["single", "multi", "deterministic"]:
        rows = [r for r in records if r["orchestration"] == mode]
        if not rows:
            continue
        successes = sum(r["status"] == "completed" for r in rows)
        latencies = [
            case["latency_ms"]
            for r in rows
            if r["status"] == "completed"
            for case in (r["state"].get("verification") or {}).get("cases", [])
        ]
        consistent = sum(
            all(r["status"] == "completed" for r in rows if r["profile"] == profile)
            for profile in protocol["profiles"]
        )
        groups[mode] = {
            "attempts": len(rows),
            "successes": successes,
            "success_rate": successes / len(rows),
            "success_rate_wilson95": wilson(successes, len(rows)),
            "median_seconds": statistics.median(r["seconds"] for r in rows),
            "controller_tokens": sum(
                r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in rows
            ),
            "controller_calls": sum(r["usage"]["calls"] for r in rows),
            "human_interventions": sum(r["state"]["human_interventions"] for r in rows),
            "unresolved_tasks": len(rows) - successes,
            "deployments": sum(r["state"]["rounds"] for r in rows),
            "source_candidates": sum(r["state"]["candidate_count"] for r in rows),
            "first_deployment_accepted": sum(
                r["status"] == "completed"
                and r["state"]["rounds"] == 1
                and not r["state"].get("failures", [])
                for r in rows
            ),
            "all_repeats_successful_profiles": consistent,
            "served_api_observations_terminal_acceptance": len(latencies),
            "served_api_median_ms_terminal_acceptance": statistics.median(latencies)
            if latencies
            else None,
        }
    a, b = groups["single"], groups["multi"]
    comparison = {
        "success_gain_pp": 100 * (b["success_rate"] - a["success_rate"]),
        "controller_token_change_pct": 100 * (b["controller_tokens"] / a["controller_tokens"] - 1),
        "median_time_change_pct": 100 * (b["median_seconds"] / a["median_seconds"] - 1),
        "paired_median_time_difference_seconds": statistics.median(
            p["multi"]["seconds"] - p["single"]["seconds"] for p in pairs.values()
        ),
        "multi_only_success": sum(
            p["multi"]["status"] == "completed" and p["single"]["status"] != "completed"
            for p in pairs.values()
        ),
        "single_only_success": sum(
            p["single"]["status"] == "completed" and p["multi"]["status"] != "completed"
            for p in pairs.values()
        ),
        "exploratory_first_deployment_gain_pp": 100
        * (
            b["first_deployment_accepted"] / b["attempts"]
            - a["first_deployment_accepted"] / a["attempts"]
        ),
    }
    discordant = comparison["multi_only_success"] + comparison["single_only_success"]
    comparison["paired_exact_p"] = (
        min(
            1,
            2
            * sum(
                math.comb(discordant, k)
                for k in range(
                    min(comparison["multi_only_success"], comparison["single_only_success"]) + 1
                )
            )
            / 2**discordant,
        )
        if discordant
        else 1.0
    )
    # Resample whole profiles, keeping repeated observations together.
    profile_differences = [
        statistics.mean(
            int(p["multi"]["status"] == "completed") - int(p["single"]["status"] == "completed")
            for key, p in pairs.items()
            if key[1] == profile
        )
        for profile in protocol["profiles"]
    ]
    rng = random.Random(20261117)
    boot = sorted(
        100 * statistics.mean(rng.choices(profile_differences, k=len(profile_differences)))
        for _ in range(10000)
    )
    comparison["profile_cluster_bootstrap95_gain_pp"] = [boot[249], boot[9749]]
    profile_rows = []
    for profile in protocol["profiles"]:
        item = {"profile": profile}
        for mode in groups:
            rows = [r for r in records if r["profile"] == profile and r["orchestration"] == mode]
            item[mode] = {
                "successes": sum(r["status"] == "completed" for r in rows),
                "attempts": len(rows),
                "median_seconds": statistics.median(r["seconds"] for r in rows),
                "tokens": sum(
                    r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in rows
                ),
            }
        profile_rows.append(item)
    return {
        "groups": groups,
        "comparison": comparison,
        "profiles": profile_rows,
        "limitations": [
            f"{len(protocol['profiles'])} controlled source fixtures, repeated {protocol['repeats']} times; not independent real repositories",
            "Same GLM with provider-default sampling, no fixed generation seed",
            "Warm real trained 8B engine; download and initial load excluded",
            "Controller tokens include all official Sessions and provider cache usage; GPU worker/grader tokens excluded",
            "No currency price estimate; no educational accuracy reevaluation",
            "No observed discordances gives no evidence of success improvement; bootstrap [0,0] does not prove equality",
            "One-pass deterministic comparator has no autonomous repair, not a mature automation benchmark",
        ],
    }


def render(data):
    lines = [
        "# 真实8B模型服务交付：同条件对照",
        "",
        "对照任务、版本和预算见 protocol.json、runtime-snapshot.json；完整逐次结果见 records.json。",
        "",
        "| 指标 | 单 Agent | 多 Agent | 固定一次交付流程 |",
        "|---|---:|---:|---:|",
    ]
    groups = data["groups"]

    def values(fn):
        return " | ".join(
            fn(groups[m]) if m in groups else "未运行" for m in ["single", "multi", "deterministic"]
        )

    lines.extend(
        [
            "| 实际交付成功 | "
            + values(lambda r: f"{r['successes']}/{r['attempts']}（{r['success_rate']:.2%}）")
            + " |",
            "| 首次部署就通过验收（探索性） | "
            + values(lambda r: f"{r['first_deployment_accepted']}/{r['attempts']}")
            + " |",
            "| 全部尝试耗时中位数 | " + values(lambda r: f"{r['median_seconds']:.2f} 秒") + " |",
            "| 主控及子会话 token 总量 | " + values(lambda r: f"{r['controller_tokens']:,}") + " |",
            "| 实际人工修复次数 | " + values(lambda r: str(r["human_interventions"])) + " |",
            "| 未完成交付 | " + values(lambda r: str(r["unresolved_tasks"])) + " |",
            "| 两轮都成功的场景数 | "
            + values(lambda r: str(r["all_repeats_successful_profiles"]))
            + " |",
        ]
    )
    c = data["comparison"]
    lines.extend(
        [
            "",
            f"多 Agent相对单 Agent成功率差值 {c['success_gain_pp']:+.2f} pp；主控会话token变化 {c['controller_token_change_pct']:+.2f}%；耗时中位数变化 {c['median_time_change_pct']:+.2f}%。配对中多 Agent独立成功 {c['multi_only_success']} 次，单 Agent独立成功 {c['single_only_success']} 次。",
            "",
            f"首次部署就通过验收的比例差值 {c['exploratory_first_deployment_gain_pp']:+.2f} pp，仅作为探索性指标。成功率差值按场景聚类bootstrap的95%区间为 {c['profile_cluster_bootstrap95_gain_pp']} pp；配对精确检验 p={c['paired_exact_p']:.3f}。小样本重复场景不构成普遍性能保证。",
            "",
            "成功率相同或差异不足时，不能写成协作提升成功率。没有实际人的修复动作时，也不能宣称减少了人工介入。耗时或token增加应作为代价报告。",
            "",
            "## 场景明细",
            "",
            "| 场景 | 单 Agent 成功 | 多 Agent 成功 | 单/多耗时中位数 |",
            "|---|---:|---:|---:|",
        ]
    )
    for r in data["profiles"]:
        a, b = r["single"], r["multi"]
        lines.append(
            f"| {r['profile']} | {a['successes']}/{a['attempts']} | {b['successes']}/{b['attempts']} | {a['median_seconds']:.2f} / {b['median_seconds']:.2f} 秒 |"
        )
    lines.extend(["", "## 范围与限制", "", *["- " + value for value in data["limitations"]]])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory.resolve()
    root = Path(__file__).resolve().parents[1]
    if not directory.is_relative_to(root / "reports"):
        raise ValueError("Reports must stay inside the project")
    data = analyze(directory)
    (directory / "analysis.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "RESULTS.md").write_text(render(data), encoding="utf-8")
    print(json.dumps(data["comparison"]))
