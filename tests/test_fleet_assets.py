"""Registration never implies execution; node operations retain exact ownership."""

import uuid
from types import SimpleNamespace

import pytest

from pilot_infra.asset_service import AssetService
from pilot_infra.fleet_contracts import AssetSave, ExecutorUpdate, StackRecipe
from pilot_infra.stack_catalog import select_stacks
from tests.test_resource_authority import fixture


def recipe():
    return dict(
        id="sglang-musa",
        label="SGLang MUSA test recipe",
        vendor="Owner declared fixture",
        platform="musa",
        architecture="x86_64",
        engine="sglang",
        model_types=["qwen3"],
        capabilities=["chat", "completions"],
        evidence_url="https://github.com/MooreThreads/sglang",
        evidence_note="Pinned fixture support conditions, not an actual GPU result",
        minimum_driver="3.3.8",
        attention_backends=["torch_native"],
        device_templates=["/dev/mtgpu.{index}"],
        shared_devices=[],
        verified=False,
    )


def model_request(server):
    return dict(
        request_id=uuid.uuid4().hex,
        expected_revision=0,
        asset_id="qwen3-node",
        server_id=server,
        label="Qwen3 test node model",
        kind="model",
        source={"kind": "huggingface", "location": "Qwen/Qwen3-0.6B", "revision": "a" * 40},
        max_bytes=1024**3,
    )


class Transport:
    def __init__(self):
        self.observation = {
            "identity": {"machine_fingerprint": "f" * 64, "architecture": "x86_64"},
            "accelerator_platform": "musa",
            "gpus": [{"driver": "3.3.8"}],
        }
        self.transport = SimpleNamespace(observe=lambda _: self.observation)
        self.calls = []
        self.jobs = {}
        self.lost = False

    def bootstrap(self, server, executor):
        return {
            "machine_identity": "f" * 64,
            "boot_id": "boot",
            "docker": "fixture",
            "worker_sha256": "c" * 64,
        }

    def inspect_storage(self, server, request):
        return {"ready": True, "supported": True, "reasons": [],
                "paths": {k: {"path": request[k], "device": 42, "data_disk": True, "ready": True}
                          for k in ("root", "asset_root")}, "suggested": {}}

    def call(self, server, executor, action, payload):
        self.calls.append((action, payload))
        if action == "probe":
            return {"boot_id": "boot"}
        if action == "submit":
            self.jobs[payload["operation_id"]] = {
                "status": "running",
                "pid": 123,
                "boot_id": "boot",
            }
            if self.lost:
                raise TimeoutError("Response lost after durable submission")
            return self.jobs[payload["operation_id"]]
        return self.jobs[payload["operation_id"]]


def fleet(tmp_path):
    fixture(tmp_path)
    transport = Transport()
    service = AssetService(tmp_path, transport=transport)
    server = service.registry.catalog()[0]
    service.registry.connection_result(server["id"], server["revision"], {"ready": True})
    service.configure_executor(
        dict(
            server_id=server["id"],
            expected_revision=server["revision"],
            enabled=True,
            root="/data/pilot-test-executor",
        )
    )
    return service, transport, server


def test_contracts_require_fixed_sources_and_device_scope():
    value = model_request("node")
    value["source"]["revision"] = "main"
    with pytest.raises(ValueError, match="40位"):
        AssetSave.model_validate(value)
    for directory in [
        "/",
        "/etc/pilot-executor",
        "/data/../pilot-executor",
        "relative/pilot-executor",
    ]:
        with pytest.raises(ValueError):
            ExecutorUpdate.model_validate(
                dict(server_id="node", expected_revision=1, root=directory)
            )
    value = recipe()
    value["shared_devices"] = ["/dev/mem"]
    with pytest.raises(ValueError, match="device"):
        StackRecipe.model_validate(value)
    value = recipe()
    value["verified"] = True
    with pytest.raises(ValueError, match="回执"):
        StackRecipe.model_validate(value)


def test_owner_registration_is_idle_data_and_prepare_is_idempotent(tmp_path):
    service, transport, server = fleet(tmp_path)
    request = model_request(server["id"])
    asset = service.save(request)
    assert asset["status"] == "registered" and not transport.calls
    with pytest.raises(ValueError, match="版本冲突"):
        service.save(request)
    args = dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
    first = service.prepare(args)
    second = service.prepare(args)
    assert first["id"] == second["id"]
    assert len([c for c in transport.calls if c[0] == "submit"]) == 1
    transport.jobs[first["id"]] = {
        "status": "completed",
        "result": {
            "path": "/data/pilot-test-executor/assets/model",
            "owned": True,
            "manifest": {
                "digest": "d" * 64,
                "config": {"model_type": "qwen3"},
                "files": [{"path": "model.safetensors", "bytes": 100, "sha256": "e" * 64}],
            },
        },
    }
    service.refresh(first["id"])
    assert service.asset(asset["asset_id"])["status"] == "ready"
    models, images = service.projected()
    assert models[0]["server_id"] == server["id"] and not images


def test_lost_response_is_unknown_and_cannot_create_a_second_download(tmp_path):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    transport.lost = True
    with pytest.raises(ValueError, match="响应丢失"):
        service.prepare(
            dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
        )
    current = service.asset(asset["asset_id"])
    assert (
        current["operation_id"]
        and service.operation(current["operation_id"])["status"] == "unknown"
    )
    with pytest.raises(ValueError, match="禁止重放"):
        service.prepare(
            dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
        )
    assert len([c for c in transport.calls if c[0] == "submit"]) == 1
    assert service.refresh(current["operation_id"])["status"] == "running"


def test_policy_revocation_still_permits_original_node_cancel(tmp_path):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    operation = service.prepare(
        dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
    )
    service.registry.policy(
        dict(
            server_id=server["id"],
            expected_revision=server["revision"],
            policy={**server["policy"], "mode": "observe"},
        )
    )
    with pytest.raises(ValueError, match="失效"):
        service.executor(server["id"])
    assert service.refresh(operation["id"], cancel=True)["status"] == "running"
    assert transport.calls[-1][0] == "cancel"


def test_stack_matching_separates_unsupported_unknown_and_declared(tmp_path):
    service, transport, server = fleet(tmp_path)
    service.save_stack(dict(expected_revision=0, recipe=recipe()))
    rows = service.rows("fleet_stacks")
    matched = select_stacks(rows, transport.observation, {"model_type": "qwen3"})[0]
    assert matched["matched"] and matched["verification"] == "declared_support"
    assert not select_stacks(
        rows,
        transport.observation,
        {"model_type": "qwen3", "quantization_config": {"quant_method": "awq"}},
    )[0]["matched"]
    assert not select_stacks(
        rows, {**transport.observation, "accelerator_platform": "cuda"}, {"model_type": "qwen3"}
    )[0]["matched"]
    assert select_stacks(rows, {"accelerator_platform": "musa"}, {})[0]["unknown"]


def test_asset_management_is_owner_only_and_not_an_agent_permission(tmp_path):
    infra = fixture(tmp_path)[0]
    for endpoint in ["asset-save", "asset-prepare", "asset-stack-save", "resource-executor"]:
        with pytest.raises(ValueError, match="Owner"):
            infra.dispatch(endpoint, "root", "infra-lead", {})
    before = infra.store.list()
    catalog = infra.dispatch("asset-catalog", "", "owner", {})
    assert catalog["assets"] == [] and infra.store.list() == before


def test_once_review_is_bound_to_root_asset_executor_and_revision(tmp_path):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    args = dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
    review = service.request_prepare(args, "root-a")
    decision = dict(
        review_id=review["id"], expected_digest=review["digest"], decision="allowed-once"
    )
    with pytest.raises(ValueError, match="根会话"):
        service.decide_prepare(decision, "root-b")
    with service.registry.db() as db:
        import json

        executor = service.executor(server["id"])
        executor["root"] = "/data/pilot-another-root"
        db.execute(
            "UPDATE fleet_executors SET data=? WHERE server_id=?",
            (json.dumps(executor), server["id"]),
        )
    with pytest.raises(ValueError, match="审批失效"):
        service.decide_prepare(decision, "root-a")
    assert not any(c[0] == "submit" for c in transport.calls)


def test_executor_change_during_probe_prevents_submission(tmp_path):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    original = transport.call

    def call(server, executor, action, payload):
        if action == "probe":
            import json

            with service.registry.db() as db:
                db.execute(
                    "UPDATE fleet_executors SET data=? WHERE server_id=?",
                    (json.dumps({**executor, "root": "/data/pilot-changed"}), server["id"]),
                )
        return original(server, executor, action, payload)

    transport.call = call
    with pytest.raises(ValueError, match="预检期间变化"):
        service.prepare(
            dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex)
        )
    assert not service.operation_rows()


def test_unsettled_operation_outside_catalog_page_blocks_executor(tmp_path):
    service, _, server = fleet(tmp_path)
    import json

    with service.registry.db() as db:
        for i in range(52):
            value = dict(
                id=str(i), server_id=server["id"], status="unknown" if i == 0 else "completed"
            )
            db.execute(
                "INSERT INTO fleet_operations VALUES(?,?,?,?)",
                (str(i), str(i), "fixture", json.dumps(value)),
            )
    assert not any(o["status"] == "unknown" for o in service.catalog()["operations"])
    with pytest.raises(ValueError, match="未结算"):
        service.configure_executor(
            dict(
                server_id=server["id"],
                expected_revision=server["revision"],
                enabled=True,
                root="/data/pilot-new-root",
            )
        )


def test_proposal_registration_is_atomic_and_repeatable(tmp_path):
    service, _, server = fleet(tmp_path)
    proposal = service.propose("asset", model_request(server["id"]), "root-a")
    decision = dict(
        proposal_id=proposal["id"], expected_digest=proposal["digest"], decision="register"
    )
    result = service.register_proposal(decision)
    assert (
        result["status"] == "registered"
        and service.asset(proposal["payload"]["asset_id"])["revision"] == 1
    )
    assert service.register_proposal(decision)["result"] == result["result"]
    assert len(service.rows("fleet_assets")) == 1


def test_unrelated_runtime_evidence_does_not_change_stack_eligibility():
    observation = Transport().observation
    base = recipe()
    a = {"recipe": {**base, "id": "a"}, "runtime_receipts": [{"model_type": "other"}]}
    b = {
        "recipe": {**base, "id": "b"},
        "runtime_receipts": [
            {
                "model_type": "qwen3",
                "quantization": "none",
                "platform": "musa",
                "architecture": "x86_64",
                "scope": "quality_and_identity",
                "evidence_sha256": "a" * 64,
            }
        ],
    }
    rows = select_stacks([a, b], observation, {"model_type": "qwen3"})
    assert rows[0]["id"] == "b" and rows[0]["verification"] == "historical_runtime"
    assert rows[1]["verification"] == "declared_support"


def test_cli_and_web_read_the_same_registry(tmp_path, capsys):
    import json

    from pilot_host.asset_cli import main

    service, _, server = fleet(tmp_path)
    request = model_request(server["id"])
    source = tmp_path / "registration.json"
    source.write_text(json.dumps(request), encoding="utf-8")
    main(["--root", str(tmp_path), "save", "--file", str(source)])
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    main(["--root", str(tmp_path), "list"])
    data = json.loads(capsys.readouterr().out)
    assert data["assets"] == service.catalog()["assets"]
    assert not data["operations"]


@pytest.mark.parametrize("decision", ["rejected", "cancelled", "unavailable"])
def test_asset_approval_rejection_has_no_node_side_effect(tmp_path, decision):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    review = service.request_prepare(
        dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex), "root-a"
    )
    result = service.decide_prepare(
        dict(review_id=review["id"], expected_digest=review["digest"], decision=decision), "root-a"
    )
    assert result["decision"] == decision and not transport.calls
    assert not service.operation_rows()


def test_once_approval_submits_one_exact_asset_and_cannot_be_reused(tmp_path):
    service, transport, server = fleet(tmp_path)
    asset = service.save(model_request(server["id"]))
    review = service.request_prepare(
        dict(asset_id=asset["asset_id"], expected_revision=1, request_id=uuid.uuid4().hex), "root-a"
    )
    args = dict(review_id=review["id"], expected_digest=review["digest"], decision="allowed-once")
    result = service.decide_prepare(args, "root-a")
    assert result["operation"]["root_session"] == "root-a"
    with pytest.raises(ValueError, match="已结算"):
        service.decide_prepare(args, "root-a")
    assert len([c for c in transport.calls if c[0] == "submit"]) == 1


def test_disable_does_not_require_node_connectivity_and_retains_cleanup_snapshot(tmp_path):
    service, transport, server = fleet(tmp_path)
    before = service.executor(server["id"])
    transport.transport.observe = lambda _: (_ for _ in ()).throw(TimeoutError())
    after = service.configure_executor(
        dict(
            server_id=server["id"],
            expected_revision=server["revision"],
            enabled=False,
            root=before["root"],
        )
    )
    assert not after["enabled"] and after["server_snapshot"] == before["server_snapshot"]
    with pytest.raises(ValueError, match="停用"):
        service.executor(server["id"])
    assert service.executor(server["id"], cleanup=True)["root"] == before["root"]
