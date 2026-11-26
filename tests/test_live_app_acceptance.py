import hashlib
import json

import pytest

from pilot_apps.acceptance import read_receipts, validate_inference
from pilot_apps.backend import AppBackend
from pilot_apps.store import source_hash
from tests.test_goal_applications import new


def accepted(root, monkeypatch):
    store, row = new(root)
    backend = AppBackend(store, row.id)
    backend.initialize_workspace()
    backend.write("app.py", "app")
    backend.write("index.html", "<h1>demo</h1>")
    store.update(
        row.id,
        model={"model_id": row.plan.model_id, "identity": "identity"},
        source_test={"passed": True, "source_sha256": source_hash(backend.workspace)},
    )
    candidate = backend.candidate()
    store.update(
        row.id,
        status="completed",
        deployment={"candidate": candidate["id"], "healthy": True},
        verification={"passed": True, "candidate": candidate["id"]},
    )
    folder = root / ".local/model-preview"
    folder.mkdir(parents=True)
    (folder / "inference-receipts.jsonl").write_text("")
    (backend.directory / "model-connection.json").write_text(
        json.dumps({"worker_dir": str(folder)})
    )
    monkeypatch.setattr(
        "pilot_apps.backend.call_engine",
        lambda *a, **k: {"model_id": row.plan.model_id, "model_identity": "identity"},
    )
    monkeypatch.setattr(AppBackend, "check_container", lambda *a: "container")
    return store, row, backend, folder


def response(message, model_id):
    value = {
        "answer": "展示：3",
        "model_answer": "3",
        "model_input": "请简洁作答\n" + message,
        "model_identity": "identity",
        "model_id": model_id,
        "receipt_id": "new",
        "finish_reason": "stop",
    }
    receipt = {
        "receipt_id": "new",
        "model_identity": "identity",
        "input_sha256": hashlib.sha256(value["model_input"].encode()).hexdigest(),
        "answer_sha256": hashlib.sha256(value["model_answer"].encode()).hexdigest(),
    }
    return value, receipt


def test_live_chat_requires_new_receipt_and_preserves_presentation_boundary(tmp_path, monkeypatch):
    _, row, backend, folder = accepted(tmp_path, monkeypatch)
    value, receipt = response("1+2", row.plan.model_id)

    def genuine(*args):
        with (folder / "inference-receipts.jsonl").open("a") as stream:
            stream.write(json.dumps(receipt) + "\n")
        return {"status": 200, "body": value}

    monkeypatch.setattr(backend, "http", genuine)
    result = backend.chat("1+2")
    assert result["answer"] == "展示：3"
    assert result["receipt_id"] == receipt["receipt_id"]
    assert "model_input" not in result
    with pytest.raises(ValueError, match="genuine"):
        backend.chat("1+2")  # Same earlier result is not new inference.


@pytest.mark.parametrize("change", ["code", "model", "candidate", "verification", "status"])
def test_outdated_application_is_blocked_before_http(tmp_path, monkeypatch, change):
    store, row, backend, _ = accepted(tmp_path, monkeypatch)
    if change == "code":
        (backend.candidate_path(store.get(row.id).state["candidate"]) / "app.py").write_text(
            "changed"
        )
    elif change == "model":
        monkeypatch.setattr(
            "pilot_apps.backend.call_engine", lambda *a, **k: {"model_identity": "changed"}
        )
    elif change == "candidate":
        store.update(row.id, deployment={"candidate": "c2", "healthy": True})
    elif change == "verification":
        store.update(row.id, verification={"passed": False})
    else:
        store.update(row.id, status="paused")
    monkeypatch.setattr(backend, "http", lambda *a: pytest.fail("Unaccepted HTTP must not execute"))
    with pytest.raises(ValueError):
        backend.preview()


@pytest.mark.parametrize(
    "change", ["identity", "input", "raw_answer", "receipt", "blank", "finish"]
)
def test_live_response_rejects_forged_or_mismatched_data(change):
    value, receipt = response("1+2", "model")
    if change == "identity":
        value["model_identity"] = "other"
    elif change == "input":
        value["model_input"] = "different"
    elif change == "raw_answer":
        value["model_answer"] = "fabricated"
    elif change == "receipt":
        receipt["receipt_id"] = "other"
    elif change == "blank":
        value["answer"] = "   "
    else:
        value["finish_reason"] = "unknown"
    with pytest.raises(ValueError):
        validate_inference(
            value,
            [receipt],
            message="1+2",
            model={"model_id": "model", "identity": "identity"},
            previous_ids=set(),
        )


def test_version_change_during_inference_is_rejected(tmp_path, monkeypatch):
    store, row, backend, folder = accepted(tmp_path, monkeypatch)
    value, receipt = response("1+2", row.plan.model_id)

    def changed(*a):
        (folder / "inference-receipts.jsonl").write_text(json.dumps(receipt) + "\n")
        store.update(row.id, verification=None)
        return {"status": 200, "body": value}

    monkeypatch.setattr(backend, "http", changed)
    with pytest.raises(ValueError):
        backend.chat("1+2")


@pytest.mark.parametrize("target", ["workspace", "workspace/tests", "candidates/c1"])
def test_replaced_directory_link_cannot_touch_outside(tmp_path, target):
    store, row = new(tmp_path)
    backend = AppBackend(store, row.id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("keep")
    link = backend.directory / target
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        if target == "workspace":
            backend.write("app.py", "bad")
        elif target == "workspace/tests":
            backend.initialize_workspace()
        else:
            backend.candidate_path({"id": "c1"})
    assert (outside / "app.py").read_text() == "keep"


def test_receipt_path_is_confined_to_project(tmp_path):
    with pytest.raises(ValueError, match="escape"):
        read_receipts(tmp_path, {"worker_dir": str(tmp_path.parent / "other")})


def test_old_iframe_binding_cannot_invoke_a_new_app_version(tmp_path, monkeypatch):
    store, row, backend, _ = accepted(tmp_path, monkeypatch)
    candidate = store.get(row.id).state["candidate"]
    old = {
        "candidate": candidate["id"],
        "source_sha256": "0" * 64,
        "model_identity": candidate["model_identity"],
    }
    monkeypatch.setattr(
        backend, "http", lambda *a: pytest.fail("Old preview cannot run new version")
    )
    with pytest.raises(ValueError, match="Preview version"):
        backend.chat("1+2", old)






@pytest.mark.parametrize("fault", ["old_source", "wrong_owner", "writable", "stopped"])
def test_actual_container_binding_is_independent_of_state_claim(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace

    real_check = AppBackend.check_container
    store, row, backend, _ = accepted(tmp_path, monkeypatch)
    candidate = store.get(row.id).state["candidate"]
    info = {
        "Id": "container",
        "State": {"Running": True},
        "Config": {"Labels": {"pilot.application": row.id}},
        "Mounts": [
            {
                "Destination": "/workspace",
                "Source": str(backend.candidate_path(candidate)),
                "RW": False,
            }
        ],
    }
    if fault == "old_source":
        info["Mounts"][0]["Source"] = str(backend.directory / "candidates/c2")
    elif fault == "wrong_owner":
        info["Config"]["Labels"]["pilot.application"] = "other"
    elif fault == "writable":
        info["Mounts"][0]["RW"] = True
    else:
        info["State"]["Running"] = False
    monkeypatch.setattr(
        "pilot_apps.backend.docker",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps([info])),
    )
    with pytest.raises(ValueError, match="container"):
        real_check(backend, candidate)
