"""Shared Z.ai wire compatibility; model changes must reach routing and execution."""


def context_window(model):
    # Z.ai GLM-5 docs: 200K; installed pi-ai catalog: GLM-5.3 1M.
    # This is per-request capacity, independent of cumulative task allowance.
    if model.startswith("glm-5.3"):
        return 1000000
    if model in {"glm-5", "glm-5-turbo"}:
        return 200000
    return 32768


def reasoning_options(model):
    required = model.startswith("glm-5.3")
    return {
        "reasoning": "low" if required else "off",
        "supportsReasoningEffort": required,
        "reasoningEfforts": {"low": "low", "high": "high"} if required else {"off": None, "low": "low"},
    }


def chat_thinking(model):
    if model.startswith("glm-5.3"):
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    return {"thinking": {"type": "disabled"}} if model.startswith("glm") else {}
