"""Evidence-labelled hard filtering; a vendor label never proves execution."""

import re


def version(value):
    parts = re.findall(r"\d+", value or "")
    return tuple(int(x) for x in parts[:4]) if parts else None


def select_stacks(recipes, observation, model_config, capabilities=()):
    result = []
    platform = observation.get("accelerator_platform")
    architecture = observation.get("identity", {}).get("architecture")
    driver = next((g.get("driver") for g in observation.get("gpus", []) if g.get("driver")), None)
    for record in recipes:
        recipe = record.get("recipe", record)
        reasons, unknown = [], []
        if platform != recipe["platform"]:
            reasons.append("设备平台不匹配")
        if architecture is None:
            unknown.append("CPU架构未观测")
        elif architecture != recipe["architecture"]:
            reasons.append("CPU架构不匹配")
        model_type = model_config.get("model_type")
        if model_type is None:
            unknown.append("模型结构未核验")
        elif model_type not in recipe["model_types"]:
            reasons.append("模型类型不在支持证据范围")
        quantization = (model_config.get("quantization_config") or {}).get("quant_method", "none")
        if quantization not in recipe.get("quantizations", ["none"]):
            reasons.append("量化方式不在配方支持范围：" + str(quantization))
        missing = set(capabilities) - set(recipe["capabilities"])
        if missing:
            reasons.append("配方未声明能力：" + "、".join(sorted(missing)))
        if recipe.get("minimum_driver"):
            actual, minimum = version(driver), version(recipe["minimum_driver"])
            if actual is None or minimum is None:
                unknown.append("驱动版本未核验")
            elif actual < minimum:
                reasons.append("驱动低于配方最低要求")
        related = [
            r
            for r in record.get("runtime_receipts", [])
            if r.get("platform") == platform
            and r.get("architecture") == architecture
            and r.get("model_type") == model_type
            and r.get("quantization") == quantization
            and r.get("scope") == "quality_and_identity"
            and r.get("evidence_sha256")
        ]
        result.append(
            {
                "id": recipe["id"],
                "label": recipe["label"],
                "vendor": recipe["vendor"],
                "platform": recipe["platform"],
                "engine": recipe["engine"],
                "matched": not reasons and not unknown,
                "reasons": reasons,
                "unknown": unknown,
                "evidence_url": recipe["evidence_url"],
                "evidence_note": recipe["evidence_note"],
                "verification": "historical_runtime" if related else "declared_support",
                "related_acceptances": len(related),
                "note": "硬约束匹配后仍须节点镜像、设备准入和实际推理验收",
            }
        )
    return sorted(result, key=lambda r: (not r["matched"], -r["related_acceptances"], r["id"]))
