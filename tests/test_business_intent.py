"""Read-only capability hints shared with the semantic route; no secret paths."""
import json

import pytest

from tests.test_pilot_infra import setup


def test_routing_context_accepts_unlabelled_assets_without_tasks_or_secrets(tmp_path):
    infra = setup(tmp_path)
    result = infra.dispatch("infra-routing-context", "unbound", "infra-lead", {})
    assert result["models"] == ["fixture"]
    assert result["cpu_build_executors"] == 0
    assert result["ui_help"] and result["serving_registered"]
    assert str(tmp_path) not in json.dumps(result)
    assert not infra.store.list()
    with pytest.raises(ValueError, match="root routing"):
        infra.dispatch("infra-routing-context", "unbound", "repo-expert", {})
