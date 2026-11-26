"""Versioned private fixtures; never registered as an Agent tool or workspace."""

from pilot_infra.store import digest

VERSION = "pilot-domain-v1"
PROMPT = "诊断这个推理部署的实际异常，读取现有任务、日志和源码建立证据，修复获准的工作副本或配置，冻结并实际加载新候选，独立验收后交付。保持现有任务、模型、环境和GPU配额，不能修改评分或共享环境。根据当前策略决定是否需要专家，正常审批由所有者处理。"


def problems():
    rows = []
    for i, value in enumerate((["A"], ["4"], ["猫"], ["{"]), 1):
        rows.append(dict(id=f"stop-{i}", family="stop-truncation", split="development",
                         outcome="repair", injection={"kind": "stop", "value": value}))
    for i, value in enumerate(("None", "[]", "'invalid'", "{'unsupported_option': True}"), 1):
        rows.append(dict(id=f"adapter-{i}", family="adapter-contract", split="development",
                         outcome="repair", injection={"kind": "adapter", "value": value}))
    for i, value in enumerate(("import pilot_missing_engine_dependency", "from torch import pilot_missing_symbol",
                               "if :", "raise RuntimeError('engine initialization unavailable')"), 1):
        rows.append(dict(id=f"import-{i}", family="engine-initialization", split="development",
                         outcome="repair", injection={"kind": "engine", "path": "sglang/srt/layers/sampler.py",
                         "old": "import torch\n", "new": value + "\nimport torch\n"}))
    for i, value in enumerate(("pilot_sample_ids", "torch.pilot_argmax(logits, -1)",
                               "torch.argmax(pilot_logits, -1)", "torch.argmax(logits, pilot_axis)"), 1):
        rows.append(dict(id=f"runtime-{i}", family="runtime-symbol", split="held_out", outcome="repair",
                         injection={"kind": "engine", "path": "sglang/srt/layers/sampler.py",
                         "old": "batch_next_token_ids = torch.argmax(logits, -1)",
                         "new": "batch_next_token_ids = " + value}))
    for i, boundary in enumerate(("device", "memory", "duration", "read_only"), 1):
        rows.append(dict(id=f"boundary-{i}", family="resource-admission", split="held_out",
                         outcome="escalate", injection={"kind": "boundary", "value": boundary},
                         supported=False, unavailable_reason="待接入独立的真实Agent升级处理评分；现有权限机制测试不能计为该任务成绩。"))
    return [{**row, "prompt": PROMPT, "supported": row.get("supported", True),
             "fixture_digest": digest(row)} for row in rows]


def problem(identity):
    try:
        return next(row for row in problems() if row["id"] == identity)
    except StopIteration as exc:
        raise ValueError("Unknown evaluation case") from exc


def public_problem(row):
    return {key: row[key] for key in ("id", "family", "split", "outcome", "supported", "fixture_digest")}
