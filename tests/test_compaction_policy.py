"""Small models retain headroom; long-history summaries cannot use a fixed 1K cap."""

from pilot_host.compaction_policy import compaction_config


def test_each_routed_model_has_bounded_summary_and_feasible_retention():
    value = compaction_config(
        {
            "models": {
                "models": [
                    {"id": "large", "contextWindow": 1000000, "maxTokens": 6000},
                    {"id": "flash", "contextWindow": 32768, "maxTokens": 4096},
                    {"id": "small", "contextWindow": 8192, "maxTokens": 6000},
                ]
            }
        }
    )
    budgets = {p["model"]: p for p in value["modelPolicies"]}
    assert budgets["large"]["maxTokens"] == 6000 and budgets["flash"]["maxTokens"] == 4096
    for model in [
        {"id": "large", "contextWindow": 1000000, "maxTokens": 6000},
        {"id": "flash", "contextWindow": 32768, "maxTokens": 4096},
        {"id": "small", "contextWindow": 8192, "maxTokens": 6000},
    ]:
        policy = budgets[model["id"]]
        assert policy["maxTokens"] <= model["maxTokens"]
        assert policy["retainTokens"] < min(
            int(model["contextWindow"] * 0.7),
            model["contextWindow"] - model["maxTokens"] - policy["headroomTokens"],
        )
    assert value["summarizationProvider"] == value["summarizationModel"] == ""
