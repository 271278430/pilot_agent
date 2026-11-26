"""Applied settings bridge, native profile layering and Owner preflight."""

import json

import pytest

from pilot_core.config import credentials, host_settings
from pilot_core.model_protocol import chat_thinking
from pilot_host.bridge import HostDomain
from pilot_host.cli import prepare_profile


def settings(root):
    model = {
        "model_id": "fixture",
        "base_url": "http://127.0.0.1:1234/v1",
        "context_window": 16384,
        "max_output_tokens": 4096,
        "auth_required": False,
        "credential_ref": "PILOT_MODEL_NOAUTH",
        "credential_file": str(root / ".credentials.yaml"),
    }
    return {
        "version": 1,
        "revision": 2,
        "applied_revision": 1,
        "desired": {"workspace_root": str(root), "model": {**model, "model_id": "draft"}},
        "applied": {"workspace_root": str(root), "model": model},
    }


def test_only_applied_parameters_reach_python_and_other_roots_stay_isolated(tmp_path, monkeypatch):
    local = tmp_path / ".local"
    local.mkdir()
    value = settings(tmp_path)
    (local / "host-settings.json").write_text(json.dumps(value))
    monkeypatch.setenv("PILOT_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("REPOPILOT_MODEL", "ambient")
    monkeypatch.setenv("REPOPILOT_API_KEY", "ambient-fixture")
    current = credentials()
    assert current["REPOPILOT_MODEL"] == "fixture"
    assert current["REPOPILOT_API_KEY"] == "pilot-local-no-auth"
    value["applied_revision"] = 0
    (local / "host-settings.json").write_text(json.dumps(value))
    assert credentials()["REPOPILOT_MODEL"] == "ambient"
    assert host_settings(tmp_path / "other") is None


def test_malformed_public_settings_and_secret_fields_fail_without_echo(tmp_path):
    local = tmp_path / ".local"
    local.mkdir()
    path = local / "host-settings.json"
    for value in [
        {},
        {**settings(tmp_path), "applied_revision": 10},
        {**settings(tmp_path), "applied": {"model": {"api_key": "private-sentinel"}}},
    ]:
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="Host settings invalid") as exc:
            host_settings(tmp_path)
        assert "private-sentinel" not in str(exc.value)


def test_preflight_accepts_history_but_refuses_active_or_unsettled_infra(tmp_path):
    from tests.test_pilot_infra import plan, setup
    service = setup(tmp_path)
    task = service.create({"request_id": "settings-history-fixture", "plan": plan()})["task"]
    domain = HostDomain(tmp_path)
    assert domain.call("settings-preflight", role="owner") == {"idle": True}
    for state in [dict(status="running"), dict(status="paused", active_job="unsettled"),
                  dict(status="paused", active_job=None, service_candidate="retained")]:
        service.store.update(task["id"], **state)
        assert domain.call("settings-preflight", role="owner") == {"idle": False}
    service.store.update(task["id"], status="failed", active_job=None, service_candidate=None)
    assert domain.call("settings-preflight", role="owner") == {"idle": True}


def test_generated_native_bundle_preserves_user_layer_and_existing_profile(tmp_path):
    profile = tmp_path / "profiles/web"
    profile.mkdir(parents=True)
    user = profile / "cordis.patch.yml"
    user.write_text("- id: personal-setting\n  config: {theme: dark}\n")
    manifest = {
        "name": "custom",
        "dependencies": {"fixture": "1.0"},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}},
    }
    (profile / "package.json").write_text(json.dumps(manifest))
    patch = tmp_path / "generated.json"
    patch.write_text("[]")
    before = user.read_bytes()
    prepare_profile(tmp_path, patch)
    prepare_profile(tmp_path, patch)
    assert user.read_bytes() == before
    after = json.loads((profile / "package.json").read_text())
    assert after["dependencies"] == manifest["dependencies"]
    assert after["dsh"]["profile"]["bundles"].count("@pilot/host-composition") == 1
    assert (
        json.loads((profile / "node_modules/@pilot/host-composition/cordis.patch.json").read_text())
        == []
    )


def test_sync_preflight_blocks_unconfirmed_build_operation_without_touching_it(tmp_path):
    domain = HostDomain(tmp_path)
    assert domain.call("settings-preflight", role="owner") == {"idle": True}
    with pytest.raises(ValueError, match="Owner"):
        domain.call("settings-preflight", role="infra-lead")
    # The gate reads persisted activity directly, without running inspection or cleanup.
    from pilot_infra.build_service import BuildService

    service = BuildService(tmp_path)
    with service.store.db() as db:
        db.execute(
            "INSERT INTO build_tasks VALUES(?,?,?,?)",
            (
                "fixture",
                "fixture",
                None,
                json.dumps(
                    {
                        "id": "fixture",
                        "plan": {},
                        "status": "unknown",
                        "active_operation": "unsettled",
                        "candidates": [],
                        "results": [],
                    }
                ),
            ),
        )
    # Build presentation uses the normal schema; isolate it to test the persisted status gate.
    service.present = lambda row: row
    original = BuildService.present
    try:
        BuildService.present = lambda _, row: row
        assert domain.call("settings-preflight", role="owner") == {"idle": False}
    finally:
        BuildService.present = original
    with service.store.db() as db:
        assert (
            json.loads(db.execute("SELECT data FROM build_tasks").fetchone()[0])["active_operation"]
            == "unsettled"
        )
    assert chat_thinking("other-model") == {}
    assert chat_thinking("glm-5")["thinking"]["type"] == "disabled"
