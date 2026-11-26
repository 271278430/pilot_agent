"""GPU query failures never masquerade as an empty device."""

from types import SimpleNamespace

import pytest

from pilot_infra import fleet_worker
from pilot_infra.resource_assessment import check_gpu_occupancy, gpu_occupancy_known
from pilot_infra.resource_probe import probe_machine, probe_program


def observe_fixture(monkeypatch, platform, processes, *, process_exit=0):
    gpu = "0, GPU-fixture, GPU Test, 550, 80000, 70000, 0\n"
    musa = "Driver Version: 1\n0 Test GPU | 0000:00:00.0 | 0% 0MiB(80000MiB)\n"

    def run(args, **_kwargs):
        if args[0] == "nvidia-smi" and args[1].startswith("--query-gpu="):
            return SimpleNamespace(returncode=0 if platform == "cuda" else 1, stdout=gpu if platform == "cuda" else "", stderr="")
        if args[0] == "nvidia-smi" and args[1].startswith("--query-compute-apps="):
            return SimpleNamespace(returncode=process_exit, stdout=processes if platform == "cuda" else "", stderr="")
        if args[0] == "mthreads-gmi":
            return SimpleNamespace(returncode=process_exit, stdout=musa + processes, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/mthreads-gmi" if platform == "musa" and name == "mthreads-gmi" else None)
    return probe_machine()


@pytest.mark.parametrize(
    ("text", "exit_code", "expected"),
    [
        ("", 0, True),
        ("GPU-fixture, 123, worker, 120\n", 0, True),
        ("GPU-fixture, malformed, worker, 120\n", 0, False),
        ("GPU-foreign, 123, worker, 120\n", 0, False),
        ("", 1, False),
        ("x" * 150001, 0, False),
        ("x" * 140000, 0, False),
        ('"unclosed', 0, False),
    ],
    ids=["idle", "busy", "bad-pid", "unknown-gpu", "command-failed", "truncated", "oversize-field", "bad-csv"],
)
def test_cuda_probe_marks_unreadable_or_truncated_processes_unknown(monkeypatch, text, exit_code, expected):
    observation = observe_fixture(monkeypatch, "cuda", text, process_exit=exit_code)
    assert observation["gpu_processes_known"] is expected
    assert gpu_occupancy_known(observation) is expected
    compile(probe_program(), "probe", "exec")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Processes:\nNo running processes found\n", True),
        ("Processes:\n0 123 python 120MiB\n", True),
        ("Processes unavailable\n", False),
        ("Processes:\n0 123 python unknown-memory\n", False),
        ("Processes:\n1 123 python 120MiB\n", False),
        ("Processes:\npermission denied\n", False),
        ("Processes:\n", False),
        ("Processes:\nNo running processes found\n0 123 python 120MiB\n", False),
        ("Processes:\nID   PID  Process name  GPU Memory\nUsage\n+-----+\n0 123 python 120MiB\n", True),
        ("1 Unknown GPU | 0000:00:01.0 | unavailable\nProcesses:\n", False),
    ],
)
def test_musa_probe_requires_a_complete_recognized_process_section(monkeypatch, text, expected):
    observation = observe_fixture(monkeypatch, "musa", text)
    assert observation["gpu_processes_known"] is expected
    assert gpu_occupancy_known(observation) is expected


def test_legacy_and_malformed_observations_are_unknown():
    observation = {"gpus": [{"index": 0, "uuid": "GPU-fixture"}], "gpu_processes": []}
    assert not gpu_occupancy_known(observation)
    observation["gpu_processes_known"] = True
    assert gpu_occupancy_known(observation)
    observation["gpu_processes"] = [{"gpu_uuid": "unknown"}]
    assert not gpu_occupancy_known(observation)


def test_local_launch_checks_live_occupancy_before_creating_any_container(tmp_path, monkeypatch):
    from pilot_infra.engine import SGLangEngine

    engine = object.__new__(SGLangEngine)
    engine.registry = SimpleNamespace(root=tmp_path)
    engine.task = {"plan": {"machine_id": "node", "gpu_ids": [0]}}
    engine.image = {}
    engine.registered_identity = lambda: True
    engine.inspect = lambda: None
    engine.network = lambda **_: pytest.fail("GPU admission must happen before network creation")
    monkeypatch.setattr("pilot_infra.container_runtime.image_admitted", lambda _: None)
    monkeypatch.setattr("pilot_infra.resource_registry.ResourceRegistry",
                        lambda _: SimpleNamespace(get=lambda _id: {"policy": {"gpu_sharing": "exclusive"}}))
    observation = {"gpus": [{"index": 0, "uuid": "GPU-fixture"}],
                   "gpu_processes": [], "gpu_processes_known": False}
    monkeypatch.setattr("pilot_infra.resource_probe.probe_machine", lambda: observation)
    with pytest.raises(ValueError, match="占用未知"):
        engine.launch()
    observation["gpu_processes_known"] = True
    observation["gpu_processes"] = [{"gpu_uuid": "GPU-fixture"}]
    with pytest.raises(ValueError, match="已有业务占用"):
        engine.launch()
    check_gpu_occupancy(observation, [0], allow_shared=True)


def test_remote_worker_refuses_missing_or_malformed_occupancy_data():
    assert fleet_worker.cuda_busy_devices("", {"GPU-fixture": 0}) == set()
    assert fleet_worker.cuda_busy_devices("GPU-fixture, 123", {"GPU-fixture": 0}) == {0}
    for text in ("bad record", "GPU-foreign, 123", "GPU-fixture, no-pid"):
        with pytest.raises(ValueError, match="occupancy"):
            fleet_worker.cuda_busy_devices(text, {"GPU-fixture": 0})
    assert fleet_worker.musa_busy_devices("Processes:\n0 123 python 120MiB\n", {0}) == {0}
    assert fleet_worker.musa_busy_devices("Processes:\nNo running processes found", {0}) == set()
    for text in ("missing section", "Processes:\n0 123 python unknown\n", "Processes:\n1 123 python 120MiB\n",
                 "Processes:\npermission denied\n", "Processes:\n", "Processes:\nNo running processes found\n0 123 python 120MiB\n"):
        with pytest.raises(ValueError, match="occupancy"):
            fleet_worker.musa_busy_devices(text, {0})


def test_workbench_gpu_admission_refuses_unknown_occupancy_but_cpu_still_works(tmp_path, monkeypatch):
    from pilot_infra.registered_workbench import RegisteredWorkbench
    from pilot_infra.resource_contracts import Authority

    server = {
        "id": "node", "revision": 1, "enabled": True,
        "policy": Authority(mode="review", approvable_gpu_ids=[0],
                            operations=["software", "containers"]).model_dump(),
    }
    observation = {"gpus": [{"index": 0, "uuid": "GPU-fixture", "free_mib": 10000}],
                   "gpu_processes": [], "gpu_processes_known": False}
    monkeypatch.setattr("pilot_infra.registered_workbench.ResourceRegistry",
                        lambda _: SimpleNamespace(get=lambda _id: server))
    workbench = object.__new__(RegisteredWorkbench)
    workbench.project = tmp_path
    workbench.server = server
    workbench.profile = {"timeout": 90}
    workbench.scope = {"gpu_ids": [0]}
    workbench.transport = SimpleNamespace(transport=SimpleNamespace(observe=lambda _: observation))
    with pytest.raises(ValueError, match="占用未知"):
        workbench.admit()
    observation["gpu_processes_known"] = True
    assert workbench.admit() == ["GPU-fixture"]
    observation["gpu_processes"] = [{"gpu_uuid": "GPU-fixture"}]
    with pytest.raises(ValueError, match="已有业务占用"):
        workbench.admit()
    workbench.scope["gpu_ids"] = []
    observation["gpu_processes_known"] = False
    assert workbench.admit() == []


def test_old_remote_worker_cannot_start_new_gpu_job_but_can_stop_or_reconcile(monkeypatch):
    from pilot_infra.remote_engine import RemoteEngine

    engine = object.__new__(RemoteEngine)
    engine.executor = {"machine_identity": "node", "probe": {"worker_sha256": "old"}}
    engine.task = {"id": "task", "plan": {"gpu_ids": [0]}, "state": {}}
    engine.candidate = {"id": "candidate"}
    engine.model = {"id": "model"}
    engine.image = {"id": "image", "stack_recipe": {"platform": "musa"}}
    monkeypatch.setattr(RemoteEngine, "limits", lambda _: {"cpu_cores": 1})
    calls = []
    previous = None

    def call(action, payload):
        calls.append(action)
        if action == "probe":
            return {"boot_id": "boot"}
        if action == "lookup":
            return previous
        if action == "submit":
            return {}
        if action == "status":
            return {"status": "completed", "result": {"passed": True}}
        raise AssertionError(action)

    engine.call = call
    with pytest.raises(ValueError, match="执行器版本需更新"):
        engine.operation("launch")
    assert calls == ["probe", "lookup"]
    calls.clear()
    assert engine.operation("stop") == {"passed": True}
    assert calls == ["probe", "lookup", "submit", "status"]
    calls.clear()
    previous = {"request": {"action": "launch", "task_id": "task", "candidate": engine.candidate,
                            "gpu_ids": [0], "model": engine.model, "image": engine.image,
                            "recipe": engine.image["stack_recipe"], "limits": {"cpu_cores": 1},
                            "machine_identity": "node", "deadline": 1, "boot_id": "old"}}
    assert engine.operation("launch") == {"passed": True}
    assert calls == ["probe", "lookup", "status"]
