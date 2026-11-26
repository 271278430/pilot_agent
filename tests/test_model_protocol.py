"""Validate actual serialized semantic requests after changing model generation."""
import json

import httpx
import pytest

from pilot_collab.semantic import SemanticRouter
from pilot_core.model_protocol import reasoning_options
from pilot_host.cli import composition


@pytest.mark.parametrize("model,capacity,effort", [("glm-5", 200000, "off"), ("glm-5.3", 1000000, "low"), ("scripted", 32768, "off")])
def test_host_uses_provider_capacity_and_compatible_initial_effort(tmp_path, model, capacity, effort):
    patch=composition(tmp_path, {"REPOPILOT_MODEL":model,"REPOPILOT_BASE_URL":"https://fixture.invalid/v1"})
    provider=next(p for p in patch if p['id']=='llm-pi-ai')['config']['providers']['apps']
    default=next(p for p in patch if p['id']=='agent-default-model')['config']
    assert provider['models'][0]['contextWindow']==capacity
    assert provider['models'][0]['maxTokens']==6000
    assert default['reasoningEffort']==effort


@pytest.mark.parametrize("model", ["glm-5", "glm-5.3"])
def test_semantic_and_harness_share_compatible_reasoning(model, monkeypatch):
    bodies = []

    def receive(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    client = httpx.Client
    monkeypatch.setattr("pilot_collab.semantic.httpx.Client", lambda **kwargs: client(
        transport=httpx.MockTransport(receive), **kwargs))
    router = SemanticRouter({"REPOPILOT_BASE_URL": "https://fixture.invalid/v1",
                             "REPOPILOT_MODEL": model, "REPOPILOT_API_KEY": "fixture-key"})
    assert json.loads(router.complete("Return JSON", {"query": "只读资源"}))["ok"]
    body = bodies[0]
    profile = reasoning_options(model)
    if model == "glm-5.3":
        assert body["thinking"]["type"] == "enabled" and body["reasoning_effort"] == "low"
        assert profile["reasoning"] == "low" and "off" not in profile["reasoningEfforts"]
        assert body["max_tokens"] >= 2048
    else:
        assert body["thinking"]["type"] == "disabled" and "reasoning_effort" not in body
        assert profile["reasoning"] == "off"
