"""Architecture reads must preserve state, guard Owner access and stay current."""

import json
from xml.etree import ElementTree

import pytest

from pilot_infra import architecture
from pilot_infra.diagram import svg
from pilot_infra.service import ROLES, InfraService


def test_architecture_is_owner_read_without_session_or_workload(tmp_path, monkeypatch):
    infra = InfraService(tmp_path, spawn_jobs=False)
    monkeypatch.setattr(infra, "task", lambda _: pytest.fail("Architecture must not resolve a Session"))
    monkeypatch.setattr(infra, "submit", lambda *args: pytest.fail("Architecture must not submit Jobs"))
    monkeypatch.setattr(infra.assets, "read", lambda: {"machines": [], "models": [], "images": [], "engine_source_profiles": [], "password": "private-sentinel"})
    before = infra.store.list()
    data = infra.dispatch("infra-architecture", "deleted-session", "owner", {})
    assert infra.store.list() == before
    assert data["default_strategy"] == "auto"
    assert data["entry"] == "system"
    assert {v["id"] for v in data["views"] if v["parent"] == "system"} == {"entry", "intent_routing", "loop", "collaboration", "context", "memory", "tools_system", "governance", "execution", "model_access"}
    assert "private-sentinel" not in json.dumps(data)
    assert len(json.dumps(data).encode()) < 4_000_000
    assert all(v["svg"].startswith("<svg") for v in data["views"])
    for role in ("infra-lead", "infra-expert", "repo-expert", "native-approval", ""):
        with pytest.raises(ValueError, match="Owner"):
            infra.dispatch("architecture", "", role, {})


def test_architecture_matches_current_roles_tools_and_generated_document():
    data = architecture.catalog()
    nodes = data["components"]
    assert {n["role"]: set(n["tools"]) for n in nodes.values() if "role" in n} == ROLES
    assert (architecture.ROOT / "docs/PILOT_ARCHITECTURE_MAP.md").read_text(encoding="utf-8") == architecture.markdown()
    pages = {v["id"]: v for v in data["views"]}
    assert len(pages) == 11
    assert data["aliases"]["loop_step"] == "loop"
    assert data["aliases"]["context_refresh"] == "context"
    assert data["aliases"]["tools_bridge"] == "tools_system"
    assert all(v["presentation"] == "diagram" for v in pages.values())
    assert {"reply", "tools", "native_child"} <= {n.get("component") for n in pages["loop"]["diagram"]["nodes"]}
    root = pages["system"]["diagram"]
    assert any(e["from"] == "result" and e["to"] == "context" for e in root["edges"])
    assert any(e["bidirectional"] for e in root["edges"] if "bidirectional" in e)
    for view in pages.values():
        assert (architecture.ROOT / "docs/architecture" / (view["id"] + ".svg")).read_text(encoding="utf-8") == svg(view, nodes)


def test_svg_is_generated_safe_markup_not_arbitrary_html():
    data = architecture.catalog()
    view = data["views"][0]
    view["diagram"]["nodes"][0]["label"] = '<script>alert("bad")</script>'
    output = svg(view, data["components"])
    assert "<script>" not in output and "&lt;script&gt;" in output
    parsed = ElementTree.fromstring(output)
    assert not any(element.tag.endswith(("script", "foreignObject")) for element in parsed.iter())


def test_boundaries_distinguish_execution_and_logical_scopes():
    data = architecture.catalog()
    pages = {v["id"]: v for v in data["views"]}
    regions = {b["id"]: b for b in pages["system"]["diagram"]["boundaries"]}
    assert regions["host_boundary"]["kind"] == "host"
    assert not {"mcp_load", "build_load", "gpu_load", "control_model", "remote_gpu"} & set(regions["host_boundary"]["members"])
    assert regions["mcp_boundary"]["kind"] == "container"
    assert regions["expert_boundary"]["kind"] == "session"
    assert regions["remote_container"]["parent"] == "node_boundary"
    assert all(v["diagram"].get("boundaries") for v in pages.values())
    xml = ElementTree.fromstring(svg(pages["system"], data["components"]))
    assert len([e for e in xml.iter() if e.get("data-architecture-boundary")]) == len(regions)


@pytest.mark.parametrize("defect", ["kind", "member", "cycle", "overlap", "hidden_member", "title_space", "markup", "caption_far", "caption_node", "caption_nan"])
def test_boundary_contract_rejects_wrong_execution_locations(tmp_path, monkeypatch, defect):
    data = json.loads(architecture.CATALOG.read_text(encoding="utf-8"))
    diagram = data["views"][0]["diagram"]
    regions = {b["id"]: b for b in diagram["boundaries"]}
    if defect == "kind":
        regions["mcp_boundary"]["kind"] = "privileged"
    elif defect == "member":
        regions["mcp_boundary"]["members"] = ["model_client"]
    elif defect == "cycle":
        regions["host_boundary"]["parent"] = "runtime_boundary"
    elif defect == "overlap":
        regions["api_boundary"].update(x=1600, w=500)
    elif defect == "hidden_member":
        regions["host_boundary"]["members"].remove("memory")
    elif defect == "title_space":
        regions["mcp_boundary"]["y"] = 1650
    elif defect.startswith("caption_"):
        diagram["edges"][0].update(label="授权", label_at={"caption_far": [1000, 1900], "caption_node": [735, 475], "caption_nan": [float("nan"), 20]}[defect])
    else:
        regions["mcp_boundary"]["label"] = '<script>alert("bad")</script>'
        output = svg(data["views"][0], data["components"])
        assert "<script>" not in output and "&lt;script&gt;" in output
        return
    path = tmp_path / "architecture.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(architecture, "CATALOG", path)
    with pytest.raises(ValueError):
        architecture.catalog()


@pytest.mark.parametrize("defect", ["source", "edge", "role", "tool", "position", "cycle", "link", "layer", "overlap", "cross_node", "overlap_edge", "port", "target"])
def test_architecture_rejects_stale_or_unsafe_references(tmp_path, monkeypatch, defect):
    data = json.loads(architecture.CATALOG.read_text(encoding="utf-8"))
    flow = data["views"][0]["diagram"]
    if defect == "source":
        data["components"]["lead"]["refs"] = [".local/private.json"]
    elif defect == "edge":
        flow["edges"][0]["to"] = "missing"
    elif defect == "role":
        data["components"]["lead"]["role"] = "unknown"
    elif defect == "tool":
        data["components"]["dispatch"]["tools"] = ["arbitrary_host_shell"]
    elif defect == "position":
        flow["nodes"][0]["x"] = -10
    elif defect == "cycle":
        next(v for v in data["views"] if v["id"] == "loop")["parent"] = "loop_step"
    elif defect == "link":
        data["views"][1]["links"] = [{"view": "missing", "note": "invalid"}]
    elif defect == "layer":
        data["views"][0]["layers"][0]["modules"].append("loop")
    elif defect == "overlap":
        flow["nodes"][1].update(x=flow["nodes"][0]["x"], y=flow["nodes"][0]["y"]+5)
    elif defect == "cross_node":
        edge = next(e for e in flow["edges"] if e["from"] == "result" and e["to"] == "context")
        edge["points"][1] = [585, 880]
    elif defect == "overlap_edge":
        flow["edges"].append(dict(flow["edges"][0]))
    elif defect == "port":
        flow["edges"][0]["points"][0] = [0, 0]
    else:
        flow["nodes"][0]["target"] = "missing"
    path = tmp_path / "architecture.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(architecture, "CATALOG", path)
    with pytest.raises(ValueError):
        architecture.catalog()
