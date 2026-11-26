from types import SimpleNamespace

from tests.test_pilot_infra import create, setup, start


def test_acceptance_default_factory_uses_original_task_resource_limits(tmp_path, monkeypatch):
    service = setup(tmp_path)
    task = start(service, create(service))
    candidate = service.candidate(
        task,
        {
            "request_id": "acceptance-candidate",
            "serving": task["plan"]["serving"],
            "reason": "默认执行器和实际限额必须一致",
        },
    )
    identity = {"container_id": "actual-container", "started_at": "start-one"}
    task = service.store.update(
        task["id"],
        service_candidate=candidate["id"],
        experiments=[
            {
                "candidate_id": candidate["id"],
                "identity": identity,
                "job_id": "test-job",
                "passed": True,
            }
        ],
    )
    service.engine_factory = None
    observed = []

    def factory(registry, execution, frozen):
        observed.append(execution)
        return SimpleNamespace(live_acceptance=lambda accepted: accepted == identity)

    monkeypatch.setattr("pilot_infra.process_engine.engine_for", factory)
    assert service.current_acceptance(task, task)["current"]
    assert observed[0]["_resource_limits"] == service.resources.execution_limits(task)
    monkeypatch.setattr(
        "pilot_infra.process_engine.engine_for",
        lambda *args: SimpleNamespace(live_acceptance=lambda accepted: False),
    )
    assert not service.current_acceptance(task, task)["current"]


def test_manual_ownership_blocks_agent_writes_but_retains_source_reads(tmp_path):
    import pytest

    service = setup(tmp_path)
    task = start(service, create(service))
    service.store.update(task["id"], execution_mode="manual")
    sid = task["state"]["session_id"]
    read = service.call_tool("source_read", sid, "infra-lead", {})
    with pytest.raises(ValueError, match="manual control"):
        service.call_tool(
            "source_write",
            sid,
            "infra-lead",
            {"expected_sha256": read["sha256"], "content": "def build_options(): return {}"},
        )
    assert service.call_tool("source_read", sid, "infra-lead", {})["sha256"] == read["sha256"]
