"""Routing and model projections; these are mechanism checks, not model scores."""

import json

import pytest

from pilot_infra.routing import decision
from pilot_infra.service import ROLES
from pilot_infra.usage_guide import catalog
from tests.test_pilot_infra import create, setup, start


def event(seq, action="test", layer="inference", error="", passed=False):
    return {"seq":seq,"kind":"job_settled","data":{"id":str(seq),"status":"completed" if passed else "failed",
            "action":action,"candidate_id":"c1","result":{"passed":passed,"failure_layer":layer,"error":error}}}


@pytest.mark.parametrize("layer,error,role",[("runtime","CUDA out of memory","infra-expert"),
    ("engine_build","No space left on device","infra-expert"),("engine","No module named flashinfer","infra-expert"),
    ("engine","undefined symbol: libcuda_abi","infra-expert"),("engine","name 'sampling_state' is not defined","repo-expert"),
    ("adapter","options violate contract","repo-expert"),(None,"","infra-expert")])
def test_auto_routes_observed_faults_without_a_model_hint(layer,error,role):
    assert decision({"plan":{"strategy":"auto"}},[event(1,layer=layer,error=error)])["experts"] == [role]


def test_explicit_modes_mixed_layers_and_successful_retest():
    failures=[event(1,layer="runtime"),event(2,action="engine_build",layer="engine_build")]
    task={"plan":{"strategy":"auto"}}
    assert decision(task,failures)["experts"] == ["infra-expert","repo-expert"]
    assert decision({"plan":{"strategy":"single"}},failures)["experts"] == []
    assert decision({"plan":{"strategy":"multi"}},[])["experts"] == ["infra-expert"]
    assert decision(task,[*failures,event(3,action="engine_build",passed=True)])["mode"] == "multi"
    result=decision(task,[*failures,event(4,passed=True)])
    assert result["mode"] == "single" and result["resolved_before_event"] == 4


def test_hypothesis_requires_current_referenced_evidence_and_never_creates_a_role_by_itself():
    hypothesis={"seq":3,"kind":"hypothesis","data":{"needs_source":True,"needs_infra":True,"evidence_ids":["1"]}}
    task={"plan":{"strategy":"auto"}}
    assert decision(task,[hypothesis])["mode"] == "single"
    observation={"seq":1,"kind":"observed_fault","data":{"detail":"new evidence"}}
    assert decision(task,[observation,hypothesis])["experts"] == ["infra-expert","repo-expert"]
    assert decision(task,[observation,hypothesis,event(4,passed=True)])["mode"] == "single"


def test_new_infra_diagnosis_preserves_root_and_expert_permissions(tmp_path):
    service=setup(tmp_path)
    task=start(service,create(service))
    sid=task["state"]["session_id"]
    service.store.event(task["id"],"observed_fault",{"error":"resource pressure"})
    seq=service.store.events(task["id"])[-1]["seq"]
    result=service.call_tool("diagnose",sid,"infra-lead",{"symptom":"服务启动后内存不足","cause":"可用显存与请求容量可能不匹配",
             "experiment":"降低同一候选并发后按相同协议重测","evidence_ids":[str(seq)],"needs_infra":True})
    assert result["strategy"]["experts"] == ["infra-expert"]
    assert service.store.get(task["id"])["state"]["session_id"] == sid
    for tool in ("source_write","engine_source_patch","launch","test","engine_build","finish"):
        assert tool not in ROLES["infra-expert"]


def test_compact_status_and_context_bound_history_preserve_raw_evidence(tmp_path):
    service=setup(tmp_path)
    task=start(service,create(service))
    sid=task["state"]["session_id"]
    experiments=[]
    for i in range(12):
        result={"passed":False,"quality":[{"output":"probe-text","events":["raw-stream"*1000]}],
                "cases":[{"attempted":2,"successful":0,"rows":["raw-sse"*1000]}],"artifact":{"runtime_files":{"f"+str(n):"hash" for n in range(1000)}}}
        experiments.append(result)
        service.store.event(task["id"],"job_settled",{"id":str(i),"action":"test","candidate_id":"c1","status":"completed","result":result,"evidence":"jobs/"+str(i)+"/result.json"})
    service.store.update(task["id"],experiments=experiments)
    before=service.store.get(task["id"])["state"]["experiments"]
    lead=service.call_tool("status",sid,"infra-lead",{})
    expert=service.call_tool("status",sid,"infra-expert",{})
    assert lead["task"]["state"]["experiment_count"] == 12 and len(lead["task"]["state"]["experiments"]) == 2
    assert len(lead["events"]) == 6 and len(expert["events"]) == 3
    assert expert["expert_scope"]["role"] == "infra-expert"
    context=service.dispatch("context",sid,"infra-expert",{})["text"]
    assert "raw-stream" not in json.dumps(lead) and "runtime_files" not in json.dumps(lead)
    assert "raw-sse" not in context and len(context) < 12000
    assert service.store.get(task["id"])["state"]["experiments"] == before
    raw=[e for e in service.store.events(task["id"]) if e["kind"] == "job_settled"][-1]
    assert raw["data"]["result"]["artifact"]["runtime_files"]


def test_role_guide_matches_actual_presets_and_default():
    from pilot_infra.contracts import InfraPlan

    feature=next(row for row in catalog()["features"] if row["id"] == "collaboration")
    assert {row["id"] for row in feature["roles"]} == set(ROLES)
    assert feature["default_strategy"] == InfraPlan.model_fields["strategy"].default == "auto"
    assert "Worker" in " ".join(feature["support_components"])


def test_old_expert_cannot_patch_after_successful_retest_returns_auto_to_single(tmp_path):
    service=setup(tmp_path)
    task=start(service,create(service))
    sid=task["state"]["session_id"]
    service.store.event(task["id"],"job_settled",event(1,layer="adapter")["data"])
    assert service.strategy(task)["experts"] == ["repo-expert"]
    service.store.event(task["id"],"job_settled",event(2,passed=True)["data"])
    source=service.call_tool("source_read",sid,"repo-expert",{})
    with pytest.raises(ValueError,match="not admitted"):
        service.call_tool("source_write",sid,"repo-expert",{"expected_sha256":source["sha256"],"content":"def build_options():\n    return {}\n"})
