import json
from pathlib import Path

from repopilot.models import RunConfig
from repopilot.retrieval import RepoMap
from repopilot.storage import read_json


def test_cross_file_symbols_rank_and_incremental_update(run_dir):
    config = RunConfig.model_validate(read_json(run_dir / "run.json"))
    repo = Path(config.workspace)
    index = RepoMap(repo, run_dir / "index.json")
    result = index.search("total apply_discount quantity")
    assert {"money.py", "checkout.py"} <= {r["file"] for r in result["ranked_files"]}
    assert index.entries["money.py"]["definitions"][0]["name"] == "apply_discount"
    index.refresh()
    assert index.updated == []
    path = repo / "money.py"
    path.write_text("def renamed(amount):\n    return amount\n", encoding="utf-8")
    index.refresh()
    assert index.updated == ["money.py"]
    assert index.entries["money.py"]["definitions"][0]["name"] == "renamed"


def test_deleted_file_drops_cached_symbols(run_dir):
    config = RunConfig.model_validate(read_json(run_dir / "run.json"))
    repo = Path(config.workspace)
    index = RepoMap(repo, run_dir / "index.json")
    index.refresh()
    (repo / "money.py").unlink()
    index.refresh()
    assert "money.py" not in index.entries


def test_search_ablation_does_not_emit_symbol_map(run_dir):
    config = RunConfig.model_validate(read_json(run_dir / "run.json"))
    index = RepoMap(Path(config.workspace), run_dir / "index.json")
    result = index.search("total quantity", use_map=False)
    assert result["hits"]
    assert all(not item["symbols"] for item in result["ranked_files"])


def test_large_module_retrieval_keeps_relevant_symbol_without_full_catalog(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'large.py').write_text(''.join(f'def operation_{i}():\n    return {i}\n' for i in range(800)), encoding='utf-8')
    index = RepoMap(repo, tmp_path / 'index.json')
    result = index.search('operation_799')
    ranked = result['ranked_files'][0]
    assert ranked['symbols'][0]['name'] == 'operation_799'
    assert ranked['symbols_omitted'] == 788
    assert len(index.entries['large.py']['definitions']) == 800
    assert len(json.dumps(result)) < 5000
