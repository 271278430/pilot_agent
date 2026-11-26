"""Model-aware settings for the official compactor; no custom summary or loop."""


def compaction_config(providers):
    policies = []
    for provider, profile in providers.items():
        for model in profile.get("models", []):
            context, output = model.get("contextWindow"), model.get("maxTokens")
            if (
                type(context) is not int
                or type(output) is not int
                or context <= output
                or output < 1
            ):
                continue
            message_budget = context - output
            summary = min(8192, output, max(256, context // 8))
            headroom = min(summary, max(1, message_budget // 4))
            threshold = min(int(context * 0.7), message_budget - headroom)
            retained = min(2048, max(1, context // 16), max(1, threshold // 3))
            if retained >= threshold:
                continue
            policies.append(
                {
                    "provider": provider,
                    "model": model["id"],
                    "maxTokens": summary,
                    "headroomTokens": headroom,
                    "retainTokens": retained,
                }
            )
    return {
        "auto": True,
        "thresholdRatio": 0.7,
        "headroomTokens": 1024,
        "retainTokens": 2048,
        "maxTokens": 4096,
        "summarizationProvider": "",
        "summarizationModel": "",
        "modelPolicies": policies,
    }
