"""Broker contracts and filesystem attacks; real Docker evidence is separate."""

import json
import os
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from pilot_core import execution_sandbox as sandbox
from pilot_host.bridge import HostDomain


def test_snapshot_cannot_include_control_files_or_follow_links(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "runs/workspace"
    source.mkdir(parents=True)
    secret = root / ".local/key.txt"
    secret.parent.mkdir()
    secret.write_text("credential-canary")
    (source / "public.txt").write_text("source")
    (source / ".env").write_text("secret")
    target = root / "copy"
    target.mkdir()
    assert sandbox.inputs_snapshot(root, source, target) == {"files": 1, "bytes": 6}
    assert not (target / ".env").exists()
    with pytest.raises(ValueError, match="control"):
        sandbox.inputs_snapshot(root, root, target)
    if os.name == "nt":
        os.link(secret, source / "link")
        reason = "hardlink"
    else:
        os.symlink(secret, source / "link")
        reason = "symbolic"
    with pytest.raises(ValueError, match=reason):
        sandbox.inputs_snapshot(root, source, target)
    assert secret.read_text() == "credential-canary"


def test_owner_only_bridge_and_unknown_contract_are_rejected_without_launch(tmp_path):
    domain = HostDomain(tmp_path)
    with pytest.raises(ValueError, match="trusted"):
        domain.call("sandbox-view", role="infra-lead")
    assert domain.call("sandbox-view", role="owner")["active_count"] == 0
    if os.name == "nt":
        with pytest.raises(ValueError, match="no host fallback"):
            sandbox.prepare(tmp_path, {})
    else:
        with pytest.raises(ValueError, match="launch contract"):
            sandbox.prepare(tmp_path, {"privileged": True})


def test_container_argv_has_no_host_writes_credentials_gpu_or_network(tmp_path):
    value = {
        "id": "a" * 32,
        "owner_token": "b" * 32,
        "name": "test-sandbox",
        "deadline": 9999999999,
        "env_names": ["DOC_TOKEN"],
        "image": "sha256:" + "c" * 64,
        "command": "/usr/bin/python3",
        "args": ["-V"],
    }
    args = sandbox.container_args(tmp_path, value)
    assert "--privileged" not in args and "--gpus" not in args and "--device" not in args
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--user") + 1] != "0:0"
    mounts = [args[i + 1] for i, v in enumerate(args) if v == "--mount"]
    assert len(mounts) == 1 and mounts[0].endswith("dst=/input,readonly")
    assert "docker.sock" not in " ".join(args)
    assert args[args.index("--env-file") + 1].endswith("runtime.env")
    assert "--memory-swap" in args and "size=128m" in " ".join(args)


def test_policy_corruption_and_cleanup_cannot_adopt_foreign_container(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "lock", lambda _: nullcontext())
    directory = sandbox.base(tmp_path) / ("a" * 32)
    directory.mkdir()
    value = {
        "version": 1,
        "id": directory.name,
        "root": str(tmp_path.resolve()),
        "owner_token": "mine",
        "name": "same-name",
        "status": "prepared",
    }
    sandbox.atomic(directory / "receipt.json", value)
    calls = []

    def foreign(args, **kwargs):
        from types import SimpleNamespace

        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"Id": "foreign", "Config": {"Labels": {}}}]),
            stderr="",
        )

    monkeypatch.setattr(sandbox, "docker", foreign)
    with pytest.raises(ValueError, match="Foreign"):
        sandbox.stop(tmp_path, directory.name)
    assert calls == [["inspect", "same-name"]]
    assert json.loads((directory / "receipt.json").read_text())["status"] == "prepared"


def state_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "lock", lambda _: nullcontext())
    directory = sandbox.base(tmp_path) / ("a" * 32)
    directory.mkdir()
    (directory / "inputs").mkdir()
    state = {"version": 1, "id": directory.name, "root": str(tmp_path.resolve()),
             "owner_token": "first", "name": "mine", "status": "running",
             "created_at": time.time(), "deadline": time.time() + 30,
             "env_names": [], "image": "sha256:" + "b" * 64,
             "command": "/bin/true", "args": []}
    sandbox.atomic(directory / "receipt.json", state)
    monkeypatch.setattr(sandbox, "inspect_owned", lambda _: None)
    monkeypatch.setattr(sandbox, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout="mine", stderr=""))
    monkeypatch.setattr(sandbox, "attest", lambda *a: {"fixture": True})
    return directory, state


def test_one_initialization_rebuild_keeps_deadline_and_stale_broker_cannot_stop_it(tmp_path, monkeypatch):
    directory, before = state_fixture(tmp_path, monkeypatch)
    sandbox.stop(tmp_path, before["id"], final=False, owner_token="first")
    assert sandbox.view(tmp_path)["active_count"] == 1
    fresh = sandbox.launch(tmp_path, before["id"], {})
    assert fresh["attempt"] == 1 and fresh["deadline"] == before["deadline"]
    sandbox.stop(tmp_path, before["id"], final=False, owner_token="first")
    assert sandbox.receipt(tmp_path, before["id"])[1]["status"] == "running"
    sandbox.stop(tmp_path, before["id"], final=False, owner_token=fresh["owner_token"])
    with pytest.raises(ValueError, match="no replay"):
        sandbox.launch(tmp_path, before["id"], {})
    assert not (directory / "inputs").exists()


@pytest.mark.parametrize("sealed", [True, False])
def test_tool_admission_or_final_stop_permanently_blocks_transport_rebuild(tmp_path, monkeypatch, sealed):
    directory, before = state_fixture(tmp_path, monkeypatch)
    if sealed:
        assert sandbox.dispatch(tmp_path, "seal", {"id": before["id"]})["sealed"]
    sandbox.stop(tmp_path, before["id"], final=not sealed)
    sandbox.stop(tmp_path, before["id"], final=False)
    with pytest.raises(ValueError, match="no replay"):
        sandbox.launch(tmp_path, before["id"], {})
    assert sandbox.view(tmp_path)["active_count"] == 0
    assert not (directory / "inputs").exists()


def test_failed_attestation_removes_only_the_owned_created_container(tmp_path, monkeypatch):
    directory, state = state_fixture(tmp_path, monkeypatch)
    state["status"] = "prepared"
    sandbox.atomic(directory / "receipt.json", state)
    monkeypatch.setattr(sandbox, "attest", lambda *a: (_ for _ in ()).throw(ValueError("contract mismatch")))
    calls = []
    monkeypatch.setattr(sandbox, "inspect_owned", lambda v: {"Id": "mine"} if v.get("container_id") else None)
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="mine", stderr="")
    monkeypatch.setattr(sandbox, "docker", run)
    with pytest.raises(ValueError, match="contract mismatch"):
        sandbox.launch(tmp_path, state["id"], {})
    assert ["rm", "--force", "mine"] in calls
    assert sandbox.view(tmp_path)["active_count"] == 0
    assert not (directory / "inputs").exists()


def test_quota_and_expiry_are_enforced_before_allocation(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "lock", lambda _: nullcontext())
    image = "sha256:" + "b" * 64
    monkeypatch.setattr(sandbox, "docker", lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": image}]), stderr=""))
    monkeypatch.setattr(sandbox, "inspect_owned", lambda _: None)
    args = dict(server_name="fixture", root_session="A", image=image, command="/bin/true",
                args=[], env_names=[], cwd=str(tmp_path), workspace_read=False, lease_seconds=30)
    slots = [sandbox.prepare(tmp_path, args) for _ in range(4)]
    # Closed discovery receipts must never hide active leases from reconciliation.
    for i in range(105):
        directory = sandbox.base(tmp_path) / f"{i:032x}"
        directory.mkdir()
        value = dict(sandbox.receipt(tmp_path, slots[0]["id"])[1], id=directory.name,
                     status="closed", created_at=time.time())
        sandbox.atomic(directory / "receipt.json", value)
    with pytest.raises(ValueError, match="concurrency"):
        sandbox.prepare(tmp_path, args)
    directory, value = sandbox.receipt(tmp_path, slots[0]["id"])
    value["deadline"] = 0
    sandbox.atomic(directory / "receipt.json", value)
    with pytest.raises(ValueError, match="expired"):
        sandbox.dispatch(tmp_path, "seal", {"id": slots[0]["id"]})
    sandbox.prepare(tmp_path, args)
    assert sandbox.receipt(tmp_path, slots[0]["id"])[1]["status"] == "closed"
    assert sandbox.view(tmp_path)["active_count"] == 4


def test_old_self_test_cannot_certify_changed_configuration(tmp_path):
    directory = sandbox.base(tmp_path)
    report = dict(image="sha256:" + "b" * 64, policy_revision=1, passed=True)
    sandbox.atomic(directory / "self-test.json", report)
    sandbox.atomic(directory / "policy.json", dict(version=1, revision=2, probe_image=report["image"]))
    assert sandbox.view(tmp_path)["last_self_test"]["current"] is False


def test_sudo_authentication_has_a_separate_channel_and_helper_reuse(tmp_path, monkeypatch):
    from pilot_core import sandbox_stdio
    monkeypatch.setattr(sandbox_stdio, "prefix", lambda: (["sudo", "-S", "/usr/bin/docker"], "private-canary"))
    command, env = sandbox_stdio.attach_command(tmp_path)
    assert "-A" in command and "-S" not in command and "private-canary" not in str(command)
    content = (tmp_path / "askpass").read_text()
    assert "private-canary" not in content
    assert env["SUDO_ASKPASS"] == str(tmp_path / "askpass")
    assert sandbox_stdio.attach_command(tmp_path)[0] == command


def test_self_test_cleanup_failure_cannot_publish_a_pass(tmp_path, monkeypatch):
    image = "sha256:" + "b" * 64
    sandbox.atomic(sandbox.base(tmp_path) / "policy.json", dict(version=1, revision=1, probe_image=image))
    monkeypatch.setattr(sandbox, "prepare", lambda *a: {"id": "fixture"})
    monkeypatch.setattr(sandbox, "launch", lambda *a: {"container_id": "fixture", "verified": True})
    monkeypatch.setattr(sandbox, "docker", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"passed": True, "checks": {}})))
    monkeypatch.setattr(sandbox, "stop", lambda *a: (_ for _ in ()).throw(ValueError("cleanup refused")))
    with pytest.raises(ValueError, match="cleanup refused"):
        sandbox.self_test(tmp_path)
    assert sandbox.view(tmp_path)["last_self_test"]["passed"] is False
