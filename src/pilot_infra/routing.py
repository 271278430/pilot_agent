"""Evidence admission only; native Sessions own expert execution and history."""

import re

VERSION = "pilot-routing-v2"
INFRA_ERRORS = re.compile(r"out of memory|cudaoutofmemory|no space left|enospc|permission denied|connection refused|timed? out|timeout|no module named|modulenotfounderror|undefined symbol|driver|missing (?:package|dependency)", re.I)
SOURCE_ERRORS = re.compile(r"nameerror|name .+ is not defined|syntaxerror|indentationerror|attributeerror", re.I)


def scopes(result):
    layer, error = result.get("failure_layer"), str(result.get("error", ""))
    if INFRA_ERRORS.search(error):
        return {"infra"}
    if SOURCE_ERRORS.search(error):
        return {"source"}
    if layer in {"adapter", "engine_build", "engine"}:
        return {"source"}
    if layer in {"runtime", "inference", "environment", "resource", "network"}:
        return {"infra"}
    return set()


def decision(task, events):
    # The latest successful business retest resolves earlier evidence for
    # routing purposes. It never substitutes for finish's identity checks.
    cutoff = max((e["seq"] for e in events if e["kind"] == "job_settled"
                  and e["data"].get("action") in {"test", "rollback"}
                  and e["data"].get("status") == "completed"
                  and e["data"].get("result", {}).get("passed") is True), default=0)
    active = [e for e in events if e["seq"] > cutoff]
    evidence, kinds = [], set()
    for event in active:
        data = event["data"]
        if event["kind"] == "job_settled" and (data.get("status") == "failed" or
                                                data.get("result", {}).get("passed") is False):
            found = scopes(data.get("result", {}))
            if not found and data.get("action") in {"launch", "test", "rollback"}:
                found = {"infra"}
            if not found and data.get("action") == "engine_build":
                found = {"source"}
            if found:
                kinds |= found
                evidence.append(str(event["seq"]))
        if event["kind"] == "hypothesis":
            referenced = {str(e["seq"]) for e in active if e["seq"] < event["seq"]}
            if referenced.intersection(data.get("evidence_ids", [])):
                if data.get("needs_source"):
                    kinds.add("source")
                if data.get("needs_infra"):
                    kinds.add("infra")
                if data.get("needs_source") or data.get("needs_infra"):
                    evidence.append(str(event["seq"]))
    requested = task["plan"]["strategy"]
    mode = "multi" if requested == "multi" or requested == "auto" and kinds else "single"
    experts = (["infra-expert"] if "infra" in kinds else []) + (["repo-expert"] if "source" in kinds else [])
    if mode == "single":
        experts = []
        reason = "用户指定单主控执行" if requested == "single" else "当前无待处理的专家证据，主控直接执行"
    elif not experts:
        experts, reason = ["infra-expert"], "用户指定协作，先请Infra专家核对环境与实验方案"
    else:
        reason = "依据当前故障证据，准入" + "、".join("Infra专家" if e == "infra-expert" else "Repo专家" for e in experts)
    return {"mode": mode, "reason": reason, "experts": experts, "requested": requested,
            "policy": VERSION, "evidence_ids": evidence[-6:], "resolved_before_event": cutoff}
