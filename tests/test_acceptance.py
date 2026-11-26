from repopilot.acceptance import verify
from repopilot.demo import DEMO_ACTIONS
from repopilot.tools import ToolService


def repair(run_dir):
    service = ToolService(run_dir)
    for tool, arguments in DEMO_ACTIONS:
        if tool == "edit":
            assert service.call(tool, arguments)["exit_code"] == 0
    return service


def test_independent_clean_baseline_accepts_source_only_patch(run_dir):
    repair(run_dir)
    result = verify(run_dir)
    assert result.passed
    assert result.baseline_target["exit_code"] == 1
    assert result.target["exit_code"] == result.regression["exit_code"] == 0
    assert result.changed_files == ["checkout.py", "money.py"]


def test_tampered_tests_are_rejected_even_if_agent_claims_success(run_dir):
    service = repair(run_dir)
    (service.repo / "tests/test_checkout.py").write_text("def test_cheat():\n    assert True\n", encoding="utf-8")
    result = verify(run_dir)
    assert not result.passed
    assert result.reason == "protected_files_modified"


def test_forged_patch_does_not_match_workspace(run_dir):
    repair(run_dir)
    (run_dir / "patch.diff").write_text("", encoding="utf-8")
    result = verify(run_dir)
    assert not result.passed


def test_empty_patch_fails_acceptance(run_dir):
    assert verify(run_dir).reason == "empty_patch"
