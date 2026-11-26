"""Container safety failures; these fixtures do not claim GPU runtime validation."""

import os
from types import SimpleNamespace

import pytest

from pilot_infra import container_runtime as cr
from pilot_infra.engine import SGLangEngine, VLLMEngine
from tests.test_pilot_infra import create, plan, setup, start


def test_never_fall_back_to_host_or_hide_daemon_failure(monkeypatch):
    with pytest.raises(ValueError, match="Docker container"):
        cr.image_admitted({"backend": "process"})
    monkeypatch.setattr(
        cr,
        "docker",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="permission denied", stdout=""),
    )
    with pytest.raises(ValueError, match="Cannot inspect"):
        cr.inspect("task")
    monkeypatch.setattr(
        cr,
        "docker",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="No such container", stdout=""),
    )
    assert cr.inspect("task") is None


def test_cleanup_rejects_replacement_id_or_wrong_owner(monkeypatch):
    removed = []
    obj = {"Id": "original", "Config": {"Labels": {"pilot.infra.task": "a"}}}
    monkeypatch.setattr(cr, "inspect", lambda name: obj)
    monkeypatch.setattr(cr, "docker", lambda args, **kw: removed.append(args))
    for labels, identity in [
        ({"pilot.infra.task": "b"}, "original"),
        ({"pilot.infra.task": "a"}, "replacement"),
    ]:
        with pytest.raises(ValueError, match="replaced or unowned"):
            cr.stop_owned("task", labels, identity)
    assert removed == []
    cr.stop_owned("task", {"pilot.infra.task": "a"}, "original")
    assert removed == [["rm", "-f", "original"]]


def test_container_contract_drops_caps_and_uses_fixed_image(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(cr, "image_admitted", lambda image: None)
    args = cr.base_args(
        "a",
        {"digest": "sha256:fixed"},
        {"pilot.infra.task": "a"},
        {"memory_gib": 6, "cpu_cores": 2},
        [("/models/a", "/model", True)],
        ["python3", "-m", "sglang.launch_server"],
        serving=True,
    )
    assert args[-3:] == ["sha256:fixed", "-m", "sglang.launch_server"]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--publish") + 1] == "127.0.0.1::30000"
    assert "--read-only" in args and "--privileged" not in args
    with pytest.raises(ValueError, match="delimiter"):
        cr.base_args(
            "a", {"digest": "fixed"}, {}, {}, [("/model,other", "/model", True)], ["python3"]
        )


def test_vllm_is_separate_command_and_token_prompt_protocol(tmp_path):
    service = setup(tmp_path)
    assets = service.assets.read()
    assets["images"][0]["engine"] = "vllm"
    service.assets.path.write_text(__import__("json").dumps(assets))
    task = start(service, create(service, plan=plan(serving={"attention_backend": "auto"})))
    candidate = service.candidate(
        task,
        {
            "request_id": "vllm-candidate",
            "serving": task["plan"]["serving"],
            "reason": "验证独立协议",
        },
    )
    engine = VLLMEngine(service.assets, task, candidate)
    assert "vllm.entrypoints.openai.api_server" in engine.command()
    assert "--max-model-len" in engine.command() and "--context-length" not in engine.command()
    payload = engine.performance_payload(5, {"max_new_tokens": 2})
    assert payload["prompt"] == [1024] * 5 and payload["max_tokens"] == 2
    assert "input_ids" not in payload
    cr.admit_backend(engine.image, "auto")
    with pytest.raises(ValueError, match="not supported"):
        cr.admit_backend(engine.image, "triton")
    with pytest.raises(ValueError, match="not supported"):
        cr.admit_backend({"engine": "sglang"}, "flash_attn")


def test_internal_bridge_endpoint_and_expired_acceptance(tmp_path, monkeypatch):
    service = setup(tmp_path)
    task = start(service, create(service))
    candidate = service.candidate(
        task,
        {
            "request_id": "endpoint-candidate",
            "serving": task["plan"]["serving"],
            "reason": "检查内部网络地址与验收失效",
        },
    )
    engine = SGLangEngine(service.assets, task, candidate)
    obj = {
        "NetworkSettings": {
            "Ports": {},
            "Networks": {
                engine.network_name(): {"IPAddress": "172.30.1.2", "NetworkID": "owned-network"}
            },
        }
    }
    monkeypatch.setattr(engine, "inspect", lambda: obj)
    monkeypatch.setattr(engine, "receipt", lambda obj: {})
    monkeypatch.setattr(engine, "network", lambda: {"Id": "owned-network"})
    assert engine.endpoint() == "http://172.30.1.2:30000"
    obj["NetworkSettings"]["Networks"][engine.network_name()]["NetworkID"] = "different-network"
    with pytest.raises(ValueError):
        engine.endpoint()
    accepted = {
        "container_id": "original",
        "started_at": "one",
        "image": "frozen",
        "config_digest": "candidate",
    }
    monkeypatch.setattr(engine, "verify_identity", lambda **kw: {"passed": True, **accepted})
    assert engine.live_acceptance(accepted)
    assert not engine.live_acceptance({**accepted, "started_at": "previous-start"})
    monkeypatch.setattr(engine, "verify_identity", lambda **kw: {"passed": False})
    assert not engine.live_acceptance(accepted)
