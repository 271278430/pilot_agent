"""Shared model credentials; no legacy Web or Session driver imports."""

import json
import os
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

PROJECT = Path(__file__).resolve().parents[2]


def host_settings(root):
    """Read the applied public contract shared with the native Host coordinator."""
    path = Path(root) / ".local/host-settings.json"
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["version"] in (1, 2)
        assert type(value["revision"]) is int and type(value["applied_revision"]) is int
        assert 0 <= value["applied_revision"] <= value["revision"]
        for section in ("desired", "applied"):
            item = value[section]
            assert Path(item["workspace_root"]).is_absolute()
            item.setdefault("resource_root", str(Path(root).resolve() / "resources"))
            assert Path(item["resource_root"]).is_absolute()
            models = [item["model"]] if value["version"] == 1 else item["models"]
            assert isinstance(models, list) and 1 <= len(models) <= 16
            ids = set()
            for model in models:
                validate_model(model, value["version"])
                if value["version"] == 2:
                    assert model["config_id"] not in ids
                    ids.add(model["config_id"])
            if value["version"] == 1:
                item["models"] = [
                    {
                        **item["model"],
                        "config_id": "apps",
                        "display_name": item["model"]["model_id"],
                        "enabled": True,
                        "vision": False,
                        "reasoning": "auto",
                    }
                ]
                item["default_model_id"] = "apps"
            selected = next(m for m in item["models"] if m["config_id"] == item["default_model_id"])
            assert selected["enabled"]
            if value["version"] == 2:
                assert selected == item["model"]
            item["model"] = selected
        value["version"] = 2
        return value
    except (AssertionError, KeyError, TypeError, ValueError, OSError, StopIteration):
        raise ValueError("Host settings invalid; repair with the settings coordinator") from None


def validate_model(model, version):
    assert set(model) == {
        "model_id",
        "base_url",
        "context_window",
        "max_output_tokens",
        "auth_required",
        "credential_ref",
        "credential_file",
    } | ({"config_id", "display_name", "enabled", "vision", "reasoning"} if version == 2 else set())
    assert isinstance(model["model_id"], str) and model["model_id"]
    assert isinstance(model["base_url"], str) and model["base_url"].startswith(
        ("http://", "https://")
    )
    assert type(model["context_window"]) is int and 2048 <= model["context_window"] <= 2000000
    assert type(model["max_output_tokens"]) is int and 64 <= model["max_output_tokens"] <= 32768
    assert model["max_output_tokens"] < model["context_window"]
    assert type(model["auth_required"]) is bool
    assert re.fullmatch(
        r"REPOPILOT_API_KEY|PILOT_MODEL_(NOAUTH|KEY_[A-F0-9]{32})", model["credential_ref"]
    )
    assert Path(model["credential_file"]).is_absolute()
    if version == 2:
        assert re.fullmatch(r"apps|[a-f0-9]{32}", model["config_id"])
        assert type(model["enabled"]) is bool and type(model["vision"]) is bool
        assert model["reasoning"] in {"auto", "off", "low"}
        assert isinstance(model["display_name"], str) and 1 <= len(model["display_name"]) <= 80


def provider_of(model):
    return "apps" if model["config_id"] == "apps" else "pilot_model_" + model["config_id"]


def native_model_profile(model):
    required = model["model_id"].startswith("glm-5.3")
    mode = model["reasoning"]
    efforts = (
        False
        if mode == "off"
        else {"low": "low", "high": "high"}
        if mode == "low" or required
        else {"off": None, "low": "low"}
    )
    return {
        "displayName": model["display_name"],
        "api": "openai-completions",
        "apiKeyEnv": model["credential_ref"],
        "baseURL": model["base_url"],
        "timeoutMs": 180000,
        "reasoning": ("low" if required else "off") if mode == "auto" else mode,
        "compat": {
            "supportsDeveloperRole": False,
            **({"thinkingFormat": "zai"} if model["model_id"].startswith("glm") else {}),
        },
        "models": [
            {
                "id": model["model_id"],
                "name": model["display_name"] + (" · 图片" if model["vision"] else " · 文本"),
                "input": ["text", "image"] if model["vision"] else ["text"],
                "contextWindow": model["context_window"],
                "maxTokens": model["max_output_tokens"],
                "reasoningEfforts": efforts,
            }
        ],
    }


def credentials():
    values = dict(dotenv_values(PROJECT / ".env"))
    values.update({k: v for k, v in dotenv_values(PROJECT / ".local/glm.env").items() if v})
    values.update(
        {k: v for k, v in dotenv_values(PROJECT / ".local/workbench/model.env").items() if v}
    )
    current = {
        key: os.getenv(key) or values.get(key, "")
        for key in ("REPOPILOT_MODEL", "REPOPILOT_BASE_URL", "REPOPILOT_API_KEY")
    }
    root = Path(os.getenv("PILOT_HOST_ROOT", str(PROJECT))).resolve()
    saved = host_settings(root)
    if saved:
        model = saved["applied"]["model"]
        if saved.get("applied_revision", 0) > 0:
            current["REPOPILOT_MODEL"] = model["model_id"]
            current["REPOPILOT_BASE_URL"] = model["base_url"]
            if not model["auth_required"]:
                current["REPOPILOT_API_KEY"] = "pilot-local-no-auth"
            elif (
                model["credential_ref"] != "REPOPILOT_API_KEY"
                or Path(model["credential_file"]).is_file()
            ):
                helper = PROJECT / "harness/runtime/plugins/host-settings/key-reader.mjs"
                try:
                    result = subprocess.run(
                        ["node", str(helper)],
                        input=json.dumps(
                            {"path": model["credential_file"], "ref": model["credential_ref"]}
                        ),
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise ValueError("Model credential reader unavailable") from None
                if result.returncode:
                    raise ValueError("Model credential unavailable; synchronize settings again")
                value = json.loads(result.stdout)["value"]
                if value:
                    current["REPOPILOT_API_KEY"] = value
                elif model["credential_ref"] != "REPOPILOT_API_KEY":
                    raise ValueError("Model credential not configured")
    return current
