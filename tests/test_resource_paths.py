import json

import pytest

from pilot_core.resource_paths import ensure_resource_root, layout, resource_root
from pilot_infra.fleet_worker import checked_asset_root


def test_managed_default_and_foreign_roots(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    base = ensure_resource_root(root)
    assert base == root / "resources" and resource_root(root) == base
    assert layout(root)["local_executor"] == str(root / "runs/nodes/pilot-executor")
    assert ensure_resource_root(root) == base
    marker = base / ".pilot-resources.json"
    marker.write_text(json.dumps({"owner_namespace": "foreign"}))
    with pytest.raises(ValueError, match="another"):
        ensure_resource_root(root)


def test_download_snapshot_cannot_follow_changed_node_default(tmp_path):
    root = tmp_path / "pilot-node"
    root.mkdir()
    assets = tmp_path / "resources"
    assets.mkdir()
    (assets / ".pilot-node-assets.json").write_text(json.dumps({"owner_namespace": "project"}))
    (root / "pilot-executor.json").write_text(
        json.dumps({"owner_namespace": "project", "asset_root": str(assets)})
    )
    assert checked_asset_root(root, {"asset_root": str(assets)}) == assets
    with pytest.raises(ValueError, match="changed"):
        checked_asset_root(root, {"asset_root": str(tmp_path / "old")})
    (assets / ".pilot-node-assets.json").write_text(json.dumps({"owner_namespace": "foreign"}))
    with pytest.raises(ValueError, match="ownership"):
        checked_asset_root(root, {})
