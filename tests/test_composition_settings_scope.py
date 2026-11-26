"""Native scripted fixtures must not consume the user's live model catalogue."""
import json

from pilot_host.cli import composition


def test_composition_can_separate_bundle_root_and_settings_root(tmp_path):
    project = tmp_path / "project"
    (project / ".local").mkdir(parents=True)
    model = {"config_id": "apps", "display_name": "saved", "model_id": "saved-model",
        "base_url": "https://saved.invalid/v1", "context_window": 8192,
        "max_output_tokens": 1024, "auth_required": False, "credential_ref": "PILOT_MODEL_NOAUTH",
        "credential_file": str(project / ".local/credentials.yaml"),
        "enabled": True, "vision": False, "reasoning": "off"}
    applied = {"workspace_root": str(project / "runs/inbox"), "models": [model],
               "model": model, "default_model_id": "apps"}
    (project / ".local/host-settings.json").write_text(json.dumps({"version": 2,
        "revision": 1, "applied_revision": 1, "desired": applied, "applied": applied}))
    config = {"REPOPILOT_MODEL": "fixture-model", "REPOPILOT_BASE_URL": "https://fixture.invalid/v1"}
    saved = composition(project, config)
    isolated = composition(project, config, settings_root=tmp_path / "isolated")
    assert next(r for r in saved if r.get("id") == "agent-default-model")["config"]["model"] == "saved-model"
    assert next(r for r in isolated if r.get("id") == "agent-default-model")["config"]["model"] == "fixture-model"
    assert next(r for r in isolated if r.get("id") == "llm-pi-ai")["config"]["providers"]["apps"]["baseURL"] == config["REPOPILOT_BASE_URL"]
