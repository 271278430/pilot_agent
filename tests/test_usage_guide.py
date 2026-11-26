"""Guide availability is a read projection, not execution or a grant."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from pilot_infra.contracts import InfraPlan
from pilot_infra.service import InfraService
from pilot_infra.usage_guide import catalog, current_guide, markdown


def stub(root, recipes=()):
    assets = {"ready": True,
              "machines": [{"id": "machine", "allowed_gpu_ids": [7], "executor_available": True, "password": "secret-sentinel"}],
              "models": [{"id": "model", "path": "/private/model", "api_key": "secret-sentinel"}],
              "images": [{"id": "runtime", "path": "/private/python"}],
              "engine_source_profiles": list(recipes)}
    return SimpleNamespace(root=root, assets=SimpleNamespace(read=lambda: assets))


def availability(guide, identity):
    return next(i["availability"]["state"] for i in guide["features"] if i["id"] == identity)


def test_guide_tracks_recipe_and_executor_registration_without_execution(tmp_path):
    infra = stub(tmp_path)
    assert availability(current_guide(infra), "engine-source") == "unconfigured"
    infra = stub(tmp_path, [{"id": "sglang", "image_id": "runtime"}])
    guide = current_guide(infra)
    assert availability(guide, "engine-source") == "registered"
    assert availability(guide, "workbench") == "unconfigured"
    local = tmp_path / ".local"
    local.mkdir()
    path = local / "workbench-profiles.json"
    path.write_text(json.dumps({"profiles": [{"id": "cpu", "password": "secret-sentinel"}]}))
    assert availability(current_guide(infra), "workbench") == "registered"
    path.write_text('{"profiles":[]}')
    assert availability(current_guide(infra), "workbench") == "unconfigured"
    exported = json.dumps(current_guide(infra))
    assert "secret-sentinel" not in exported and "/private/" not in exported
    assert guide["configuration"]["execution_quotas"][0]["gpu_ids"] == [7]
    assert not (tmp_path / "runs").exists()


def test_image_guide_uses_only_applied_enabled_models_without_credentials(tmp_path):
    from pilot_core.config import host_settings
    from tests.test_host_settings_contract import settings

    local = tmp_path / ".local"
    local.mkdir()
    path = local / "host-settings.json"
    path.write_text(json.dumps(settings(tmp_path)))
    value = host_settings(tmp_path)
    vision = {**value["desired"]["model"], "config_id": "a" * 32, "display_name": "图片诊断", "vision": True}
    value["desired"]["models"].append(vision)
    path.write_text(json.dumps(value))
    assert availability(current_guide(stub(tmp_path)), "image_inputs") == "unconfigured"
    value["applied"]["models"].append(vision)
    path.write_text(json.dumps(value))
    guide = current_guide(stub(tmp_path))
    assert availability(guide, "image_inputs") == "registered"
    assert "credential_ref" not in json.dumps(guide) and "credential_file" not in json.dumps(guide)
    value["applied"]["models"][-1]["enabled"] = False
    path.write_text(json.dumps(value))
    assert availability(current_guide(stub(tmp_path)), "image_inputs") == "unconfigured"


def test_guide_is_owner_read_without_session_tasks_probe_or_execution(tmp_path, monkeypatch):
    infra = InfraService(tmp_path, spawn_jobs=False)
    monkeypatch.setattr(infra, "task", lambda _: pytest.fail("Guide must not resolve a Session"))
    monkeypatch.setattr(infra, "submit", lambda *args: pytest.fail("Guide must not submit Jobs"))
    before = infra.store.list()
    value = infra.dispatch("infra-guide", "deleted-session", "owner", {})
    assert value["features"] and infra.store.list() == before
    assert availability(value, "deploy") == "unconfigured"
    with pytest.raises(ValueError, match="Owner"):
        infra.dispatch("infra-guide", "", "repo-expert", {})


def test_bad_configuration_is_unknown_instead_of_claimed_enabled(tmp_path):
    infra = stub(tmp_path)
    infra.assets.read = lambda: (_ for _ in ()).throw(ValueError("secret-sentinel"))
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local/workbench-profiles.json").write_text("not JSON")
    value = current_guide(infra)
    assert availability(value, "engine-source") == "unconfigured"
    assert availability(value, "workbench") == "unconfigured"
    assert len(value["configuration"]["warnings"]) == 2
    assert "secret-sentinel" not in json.dumps(value)


def test_catalogue_covers_actual_business_kinds_and_generated_guide():
    value = catalog()
    assert {i["business_kind"] for i in value["features"] if "business_kind" in i} == set(get_args(InfraPlan.model_fields["kind"].annotation))
    assert len({i["id"] for i in value["features"]}) == len(value["features"])
    for item in value["features"]:
        assert set(item["workflow"]) == {"scenario", "actors", "resources", "decisions", "outputs", "failure"}
        assert all(item["workflow"].values())
    assert len(value["environment_setup"]["steps"]) == 12
    assert "历史记录 → 历史评测" in next(i["entry"] for i in value["features"] if i["id"] == "domain-evaluation")
    guide = Path(__file__).resolve().parents[1] / "docs/CURRENT_GUIDE.md"
    assert guide.read_text(encoding="utf-8") == markdown()
