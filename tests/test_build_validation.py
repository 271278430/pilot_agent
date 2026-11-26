"""False success, candidate changes and cleanup have separate outcome semantics."""

from copy import deepcopy

from pilot_infra.build_validation import build_validation


def evidence():
    files = {"core.py": "a" * 64}
    return {
        "status": "prepared",
        "pipeline_stages": ["compile"],
        "active_operation": None,
        "files": files,
        "candidates": [{"candidate_id": "c1", "files": files}],
        "results": [
            {
                "candidate_id": "c1",
                "operation_id": "b" * 32,
                "source_manifest": files,
                "frozen_source_unchanged": True,
                "status": "completed",
                "passed": True,
                "steps": [{"stage": "compile", "exit_code": 0}],
            }
        ],
    }


def test_passed_build_attests_candidate_pipeline_only():
    verdict = build_validation(evidence())
    assert verdict["pipeline"] == "passed" and verdict["candidate_evidence_bound"]
    assert (
        verdict["business_goal"]
        == verdict["inference_service"]
        == verdict["gpu_numerical"]
        == "not_validated"
    )
    assert verdict["restoration"] == "pending"


def test_cleanup_cannot_convert_failed_build_to_success():
    task = evidence()
    task.update(status="restored", restored=True)
    task["results"][0].update(status="failed", passed=False)
    verdict = build_validation(task)
    assert verdict["restoration"] == "passed" and verdict["pipeline"] == "failed"
    assert verdict["business_goal"] == "not_validated"


def test_new_candidate_does_not_inherit_previous_acceptance():
    task = evidence()
    task["candidates"].append({"candidate_id": "c2", "files": {"core.py": "c" * 64}})
    verdict = build_validation(task)
    assert verdict["candidate_id"] == "c2" and verdict["pipeline"] == "not_run"


def test_passed_boolean_without_bound_complete_receipt_is_unverified():
    for key in ("source_manifest", "frozen_source_unchanged", "steps"):
        task = evidence()
        task["results"][0].pop(key)
        assert build_validation(task)["pipeline"] == "unverified"
    task = evidence()
    task["results"][0]["source_manifest"] = {"core.py": "d" * 64}
    assert build_validation(task)["pipeline"] == "unverified"


def test_zero_exit_subset_cannot_replace_declared_pipeline():
    task = evidence()
    task["pipeline_stages"] = ["environment", "compile", "verify"]
    assert build_validation(task)["pipeline"] == "unverified"
    task["pipeline_stages"] = ["verify"]
    assert build_validation(task)["pipeline"] == "unverified"
    task.pop("pipeline_stages")
    assert build_validation(task)["pipeline"] == "unverified"


def test_unknown_operation_and_failed_retest_do_not_keep_green_verdict():
    task = evidence()
    task["active_operation"] = "c" * 32
    assert build_validation(task)["pipeline"] == "unsettled"
    task["active_operation"] = None
    later = deepcopy(task["results"][0])
    later.update(status="failed", passed=False)
    task["results"].append(later)
    assert build_validation(task)["pipeline"] == "failed"


def test_working_edit_is_not_mistaken_for_tested_source():
    task = evidence()
    task["files"] = {"core.py": "d" * 64}
    verdict = build_validation(task)
    assert verdict["pipeline"] == "passed" and verdict["candidate_id"] == "c1"
    assert verdict["working_copy_matches_candidate"] is False


def test_host_view_and_context_expose_prepared_workbench_facts(tmp_path):
    from pilot_host.bridge import HostDomain
    from tests.test_build_workbench import plan, service

    builds = service(tmp_path)
    task = builds.create(plan(), "build-root")
    builds.prepare(task["id"])
    host = HostDomain(tmp_path)
    view = host.call("view", "build-root")
    assert view["build_task"]["status"] == "prepared" and view["application"] is None
    facts = host.call("context", "build-root")["text"]
    assert '"validation"' in facts and '"business_goal": "not_validated"' in facts


def test_serving_and_workbench_cannot_share_one_root(tmp_path):
    import pytest

    from tests.test_build_workbench import plan as build_plan
    from tests.test_build_workbench import service as build_fixture
    from tests.test_pilot_infra import plan as serving_plan
    from tests.test_pilot_infra import setup

    infra = setup(tmp_path)
    builds = build_fixture(tmp_path)
    task = builds.create(build_plan(), "build-root")
    with pytest.raises(ValueError, match="source workbench"):
        infra.create({"request_id": "attempt-serving-plan", "plan": serving_plan()}, "build-root")
    serving = infra.create(
        {"request_id": "separate-serving-plan", "plan": serving_plan()}, "serving-root"
    )
    with pytest.raises(ValueError, match="source workbench"):
        infra.dispatch("infra-bind", "build-root", "owner", {"task_id": serving["task"]["id"]})
    with pytest.raises(ValueError, match="serving task"):
        builds.bind(task["id"], "serving-root")
    assert infra.store.binding("build-root") is None
