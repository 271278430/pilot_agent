"""The complete test entry discovers new scripts and detects a stale catalogue."""

import json

import pytest

from scripts.business_test_matrix import render
from scripts.full_project_check import discover, node_counts, pytest_counts, render_catalog


def test_discovery_includes_nested_test_scripts_but_keeps_fixtures_as_support(tmp_path):
    nested = tmp_path / "business"
    nested.mkdir()
    (nested / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
    (nested / "new.test.mjs").write_text("// node test\n", encoding="utf-8")
    (nested / "fixture.py").write_text("# helper\n", encoding="utf-8")
    found = discover(tmp_path)
    assert [p.name for p in found["python"]] == ["test_new.py"]
    assert [p.name for p in found["node"]] == ["new.test.mjs"]
    assert [p.name for p in found["support"]] == ["fixture.py"]


def test_catalogue_changes_when_test_scripts_or_definitions_change(tmp_path):
    test = tmp_path / "test_business.py"
    test.write_text("def test_one(): pass\n", encoding="utf-8")
    first = render_catalog(tmp_path)
    test.write_text("def test_one(): pass\ndef test_two(): pass\n", encoding="utf-8")
    second = render_catalog(tmp_path)
    assert "1 个测试定义" in first and "2 个测试定义" in second
    (tmp_path / "new.test.mjs").write_text("// node test\n", encoding="utf-8")
    assert "new.test.mjs" in render_catalog(tmp_path)


def test_business_matrix_rejects_new_features_and_missing_test_scripts(tmp_path):
    guide = tmp_path / "guide.json"
    mapping = tmp_path / "map.json"
    test_root = tmp_path / "tests"
    test_root.mkdir()
    (test_root / "test_feature.py").write_text("def test_feature(): pass\n", encoding="utf-8")
    guide.write_text(json.dumps({"features": [{"id": "deploy", "title": "部署"}]}), encoding="utf-8")
    mapping.write_text(json.dumps({"deploy": ["test_feature.py"]}), encoding="utf-8")
    assert "部署" in render(guide, mapping, test_root)
    guide.write_text(json.dumps({"features": [{"id": "deploy", "title": "部署"}, {"id": "new", "title": "新增"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="features and test map differ"):
        render(guide, mapping, test_root)
    guide.write_text(json.dumps({"features": [{"id": "deploy", "title": "部署"}]}), encoding="utf-8")
    mapping.write_text(json.dumps({"deploy": ["removed_test.py"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing test scripts"):
        render(guide, mapping, test_root)


def test_junit_summary_keeps_failures_and_skips_out_of_pass_count(tmp_path):
    (tmp_path / "pytest.xml").write_text(
        '<testsuites><testsuite tests="4" failures="1" errors="1" skipped="1"/></testsuites>',
        encoding="utf-8",
    )
    assert pytest_counts(tmp_path) == {"tests": 4, "failures": 1, "errors": 1, "skipped": 1, "passed": 1}


@pytest.mark.parametrize("prefix", ["#", "ℹ"])
def test_node_summary_parses_tap_and_spec_reporters(tmp_path, prefix):
    log = tmp_path / "node.log"
    log.write_text(f"{prefix} tests 5\n{prefix} pass 3\n{prefix} fail 1\n{prefix} skipped 1\n", encoding="utf-8")
    assert node_counts(log) == {"tests": 5, "pass": 3, "fail": 1, "skipped": 1}
