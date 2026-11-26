"""Reproducible real-server task suite. Run from the project virtualenv.

Setup is outside measured agent time. Every task, including failures, gets an
immutable result; --phase separates development pilot and confirmation data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import time
from datetime import datetime
from pathlib import Path

from infra_pilot.agent import execute_infra, prepare_infra
from infra_pilot.spec import PACKAGES, REVISION, STAGES, InfraSpec
from infra_pilot.transport import Remote
from repopilot.storage import atomic_json, now, read_json

PROJECT = Path(__file__).resolve().parents[1]
WEIGHT_SHA = "5af571cbf074e6d21a03528d2330792e532ca608f24ac70a143f6b369968ab8c"

SETUP = r'''
import hashlib,json,pathlib,shutil,sys,time
root=pathlib.Path(sys.argv[1]); source=pathlib.Path(sys.argv[2]); task=sys.argv[3]; scenario=sys.argv[4]
if root.parent!=pathlib.Path('/data/pilot-owner') or root.name!='infrapilot-bench-20261109' or root.is_symlink(): raise ValueError('unsafe benchmark root')
if not (root/'.venv/bin/python').exists(): shutil.copytree(source/'.venv',root/'.venv',symlinks=True,dirs_exist_ok=True)
shutil.copytree(source/'models/smollm',root/'models/smollm',dirs_exist_ok=True)
site=root/'.venv/lib/python3.10/site-packages'
for path in (source/'.venv/lib/python3.10/site-packages').glob('peft*'):
 dest=site/path.name
 if not dest.exists(): shutil.copytree(path,dest,symlinks=True)
out={'scenario':scenario,'task_id':task,'setup_at':time.time(),'triggered':False,'cache_condition':'copied dedicated venv and model'}
if scenario=='cache_corrupt':
 p=root/'models/smollm/model.safetensors'; before=p.stat().st_size
 with p.open('r+b') as stream: stream.truncate(4096)
 out.update(triggered=True,triggered_at=time.time(),original_bytes=before,corrupt_bytes=p.stat().st_size,corrupt_sha256=hashlib.sha256(p.read_bytes()).hexdigest())
if scenario=='dependency_missing':
 backup=root/'.fault-backups'/task; backup.mkdir(parents=True,exist_ok=True)
 removed=[]
 for p in site.glob('peft*'):
  if p.resolve().parent!=site.resolve(): raise ValueError('unexpected package path')
  shutil.move(str(p),str(backup/p.name)); removed.append(p.name)
 if len(removed)!=2: raise ValueError('expected package and metadata')
 out.update(triggered=True,triggered_at=time.time(),removed=removed,absent_after=not (site/'peft').exists())
hook=root/'fault-hook'; hook.mkdir(exist_ok=True)
(hook/'sitecustomize.py').write_text("import os,sys\nif os.environ.get('INFRAPILOT_OOM')=='1' and sys.prefix.endswith('/.venv'):\n import torch\n setter=torch.cuda.set_per_process_memory_fraction\n def limited(fraction,device=None):\n  return setter(0.0001,device)\n torch.cuda.set_per_process_memory_fraction=limited\n")
print(json.dumps(out))
'''

COLLECT = r'''
import json,pathlib,sys,time
root=pathlib.Path(sys.argv[1]); ids=json.loads(sys.argv[2]); jobs=[]
for job in ids:
 p=root/'jobs'/(job+'.json')
 if p.is_file(): jobs.append(json.loads(p.read_text()))
def processes():
 out=[]
 for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
  try:
   argv=p.read_bytes().decode().split('\0')
   if str(root/'worker.py') in argv and any(a in argv for a in ['job','server','nccl_rank']): out.append({'pid':int(p.parent.name),'argv':argv})
  except (OSError,UnicodeError): pass
 return out
end=time.monotonic()+45
while processes() and time.monotonic()<end: time.sleep(1)
jobs=[json.loads((root/'jobs'/(job+'.json')).read_text()) for job in ids if (root/'jobs'/(job+'.json')).is_file()]
print(json.dumps({'jobs':jobs,'remaining_owned_processes':processes()}))
'''

VALIDATE = r'''
import hashlib,importlib.metadata,json,math,os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]); completed=json.loads(sys.argv[2]); expected=json.loads(sys.argv[3]); checks={}
checks['versions']=all(importlib.metadata.version(k)==v for k,v in expected.items())
pip=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True)
checks['pip_check']=pip.returncode==0
value=hashlib.sha256()
with (root/'models/smollm/model.safetensors').open('rb') as stream:
 while chunk:=stream.read(1024*1024):value.update(chunk)
checks['weight_sha256']=value.hexdigest()==sys.argv[4]
cuda=completed.get('cuda_smoke',{}); nccl=completed.get('nccl_smoke',{})
checks['cuda_result']=cuda.get('passed') is True and cuda.get('cuda')=='12.4'
checks['nccl_result']=nccl.get('verified') is True and len(nccl.get('ranks',[]))==2 and all(r.get('result')==r.get('expected')==3.0 for r in nccl['ranks'])
train=completed.get('train',{}); losses=train.get('losses',[])
checks['training']=len(losses)==24 and all(math.isfinite(v) for v in losses) and train.get('finite_gradients') is True and train.get('adapter_parameter_delta_l2',0)>0
checks['resume']=train.get('resume_from_step')==12 and train.get('resume_max_parameter_delta',1)<=1e-6
bench=completed.get('serve_benchmark',{}); samples=bench.get('samples',[])
checks['http_samples']=bench.get('failed_requests')==0 and len(samples)==6 and all(s.get('output_tokens')==24 and s.get('input_tokens')>0 and s.get('text','').strip() and s.get('ttft_ms',0)>0 and s.get('tpot_ms',0)>0 for s in samples)
import torch
from transformers import AutoModelForCausalLM,AutoTokenizer
from peft import PeftModel
checks['torch_runtime']=torch.__version__=='2.6.0+cu124' and torch.version.cuda=='12.4'
torch.set_num_threads(4); torch.cuda.set_per_process_memory_fraction(0.10)
saved=torch.load(root/'checkpoints/midpoint.pt',map_location='cpu',weights_only=True)
checks['checkpoint_file']=saved['step']==12 and bool(saved['optimizer']['state']) and 'rng' in saved and 'cuda_rng' in saved
checks['checkpoint_sha256']=hashlib.sha256((root/'checkpoints/midpoint.pt').read_bytes()).hexdigest()==train.get('checkpoint_sha256')
tokenizer=AutoTokenizer.from_pretrained(root/'models/smollm',local_files_only=True,trust_remote_code=False)
model=AutoModelForCausalLM.from_pretrained(root/'models/smollm',local_files_only=True,trust_remote_code=False,torch_dtype=torch.float32,attn_implementation='eager').to('cuda')
model=PeftModel.from_pretrained(model,root/'checkpoints/adapter',is_trainable=False);model.eval()
inputs=tokenizer.apply_chat_template([{'role':'user','content':'Why save a training checkpoint?'}],return_tensors='pt',add_generation_prompt=True).to('cuda')
with torch.inference_mode(): output=model.generate(inputs,max_new_tokens=8,do_sample=False,pad_token_id=tokenizer.eos_token_id)
text=tokenizer.decode(output[0,inputs.shape[1]:],skip_special_tokens=True)
checks['fresh_adapter_inference']=bool(text.strip()) and output.shape[1]>inputs.shape[1]
print(json.dumps({'passed':all(checks.values()),'checks':checks,'installed_versions':{k:importlib.metadata.version(k) for k in expected},'torch_runtime':torch.__version__,'cuda_runtime':torch.version.cuda,'fresh_output':text,'weight_sha256':value.hexdigest(),'pip_check_output':pip.stdout.strip(),'validation_peak_mib':torch.cuda.max_memory_allocated()/1024**2}))
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo = int(position)
    return ordered[lo] + (ordered[min(lo + 1, len(ordered)-1)] - ordered[lo]) * (position-lo)


def rate(passed, total):
    if not total:
        return {"passed": passed, "total": 0, "rate": None, "wilson_95": None}
    z, p = 1.959963984540054, passed / total
    scale = 1 + z*z/total
    center = (p + z*z/(2*total))/scale
    half = z*((p*(1-p)/total + z*z/(4*total*total))**0.5)/scale
    return {"passed": passed, "total": total, "rate": p, "wilson_95": [max(0, center-half), min(1, center+half)]}


def event_recovery(run, fixture, jobs):
    events = [json.loads(line) for line in (run / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()]
    detected, recovered = None, None
    target = {"port_conflict": "serve_benchmark", "lost_ack": "inference", "ssh_drop": "inference"}.get(fixture["scenario"])
    if not target:
        return None
    if fixture["scenario"] in {"lost_ack", "ssh_drop"}:
        detected = fixture.get("triggered_at")
    for event in events:
        if event.get("kind") != "action_end" or event.get("tool") != "inspect":
            continue
        result = event.get("result", {})
        if result.get("stage") != target:
            continue
        stamp = datetime.fromisoformat(event["time"]).timestamp()
        if result.get("status") == "failed" and detected is None:
            detected = stamp
        elif result.get("status") == "completed":
            recovered = stamp
    if detected is not None and recovered is not None and recovered >= detected:
        return recovered - detected
    return None


def summarize(output):
    rows = [read_json(path) for path in sorted((output / "tasks").glob("*/result.json"))]
    supported = [r for r in rows if r["recovery_supported"]]
    faults = [r for r in rows if r["scenario"] != "normal"]
    diagnosed = [r for r in rows if r["expected_diagnosis"] is not None]
    successful = [r for r in rows if r["passed"]]
    samples = [s for r in successful for s in r.get("benchmark", {}).get("samples", [])]
    wall = sum(r.get("benchmark", {}).get("wall_seconds", 0) for r in successful)
    summary = {
        "phase": output.name, "updated_at": now(), "controller": "rules", "controller_is_llm": False,
        "overall_completion": rate(sum(r["passed"] for r in rows), len(rows)),
        "supported_completion": rate(sum(r["passed"] for r in supported), len(supported)),
        "all_injected_fault_recovery": rate(sum(r["passed"] for r in faults), len(faults)),
        "diagnosis_accuracy": rate(sum(r.get("diagnosis_correct", False) for r in diagnosed), len(diagnosed)),
        "boundary_safe_stop": rate(sum(r.get("safe_stop", False) for r in rows if not r["recovery_supported"]), sum(not r["recovery_supported"] for r in rows)),
        "scenario_results": {s: rate(sum(r["passed"] for r in rows if r["scenario"] == s), sum(r["scenario"] == s for r in rows)) for s in sorted({r["scenario"] for r in rows})},
        "task_duration_all_seconds": {"p50": quantile([r["duration_seconds"] for r in rows], .5), "p95": quantile([r["duration_seconds"] for r in rows], .95)},
        "task_duration_success_seconds": {"p50": quantile([r["duration_seconds"] for r in successful], .5), "p95": quantile([r["duration_seconds"] for r in successful], .95)},
        "detected_recovery_seconds": {s: {"n": len(v := [r["recovery_seconds"] for r in rows if r["scenario"] == s and r.get("recovery_seconds") is not None]), "p50": quantile(v, .5), "p95": quantile(v, .95)} for s in ["port_conflict", "lost_ack", "ssh_drop"]},
        "duplicate_inference_launches": sum(r.get("duplicate_inference_launches", 0) for r in rows),
        "http": {"successful_requests": len(samples), "attempted_requests_in_successful_tasks": sum(r.get("benchmark", {}).get("requests", 0) for r in successful), "ttft_p50_ms": quantile([s["ttft_ms"] for s in samples], .5), "ttft_p95_ms": quantile([s["ttft_ms"] for s in samples], .95), "tpot_p50_ms": quantile([s["tpot_ms"] for s in samples], .5), "tpot_p95_ms": quantile([s["tpot_ms"] for s in samples], .95), "output_tokens_per_request_window_second": sum(s["output_tokens"] for s in samples)/wall if wall else None},
        "peak_training_allocated_mib": max((r["training"]["peak_allocated_mib"] for r in successful), default=None),
        "max_resume_parameter_delta": max((r["training"]["resume_max_parameter_delta"] for r in successful), default=None),
        "limitations": read_json(PROJECT / "evals/infra/protocol.json")["limitations"],
        "tasks": [{k: r.get(k) for k in ["task_id", "scenario", "passed", "duration_seconds", "failure_category", "diagnosis_correct", "safe_stop", "run"]} for r in rows],
    }
    atomic_json(output / "summary.json", summary)
    return summary


def run_task(task_id, scenario, spec, output, phase):
    task_dir = output / "tasks" / task_id
    task_dir.mkdir(parents=True)
    result = {"task_id": task_id, "scenario": scenario["id"], "phase": phase, "recovery_supported": scenario["recovery_supported"], "expected_diagnosis": scenario["expected_diagnosis"], "passed": False, "started_at": now()}
    started = time.monotonic()
    remote = None
    try:
        run = prepare_infra(spec, PROJECT / "runs", PROJECT / ".local/ssh.env", PROJECT / ".local/known_hosts")
        result["run"] = str(run)
        remote = Remote(spec, PROJECT / ".local/ssh.env", PROJECT / ".local/known_hosts")
        fixture = remote.command(["python3", "-c", SETUP, spec.remote, "/data/pilot-owner/infrapilot-smollm-001", task_id, scenario["id"]], timeout=180)
        if scenario["id"] == "dependency_missing":
            fixture["import_check"] = remote.command([str(Path(spec.remote) / ".venv/bin/python").replace("\\", "/"), "-c", "import importlib.util,json;print(json.dumps({'peft_absent':importlib.util.find_spec('peft') is None}))"])
        atomic_json(run / "fault.json", fixture)
        config = read_json(run / "infra-run.json")
        config["benchmark"] = {"phase": phase, "task_id": task_id, "protocol_sha256": digest(PROJECT / "evals/infra/protocol.json")}
        atomic_json(run / "infra-run.json", config)
        print(json.dumps({"event": "task_start", "task": task_id, "scenario": scenario["id"], "run": str(run)}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        report = execute_infra(run)
        result["duration_seconds"] = round(time.monotonic()-started, 3)
        state = read_json(run / "checkpoint.json")
        fixture = read_json(run / "fault.json")
        atomic_json(task_dir / "fault.json", fixture)
        ids = {r["job"] for r in state["jobs"]}
        for key in ["active", "pending"]:
            if state.get(key):
                ids.add(state[key]["job"])
        collected = remote.command(["python3", "-c", COLLECT, spec.remote, json.dumps(sorted(ids))], timeout=60)
        atomic_json(task_dir / "remote-jobs.json", collected)
        for job in collected["jobs"]:
            remote.fetch("logs/" + job["job"] + ".log", task_dir / "raw" / (job["job"] + ".log"))
        if any(j["stage"] == "serve_benchmark" for j in collected["jobs"]):
            remote.fetch("logs/server.log", task_dir / "raw/server.log")
        for name in ["report.json", "checkpoint.json", "trajectory.jsonl", "meter.json", "harness-events.jsonl", "infra-run.json"]:
            if (run / name).exists():
                shutil.copyfile(run / name, task_dir / name)
        result.update(framework=report["framework"], actions=state["actions"], completed_stages=list(state["completed"]), failure_category=(state.get("diagnosis") or {}).get("category"), failure=state.get("failed"), halt_reason=state.get("halt_reason"), benchmark=state["completed"].get("serve_benchmark"), training=state["completed"].get("train"))
        # A repaired port failure has no terminal diagnosis; inspect its trace.
        diagnoses = [json.loads(line).get("result", {}) for line in (run / "trajectory.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(line).get("tool") == "diagnose" and json.loads(line).get("kind") == "action_end"]
        result["diagnoses"] = diagnoses
        result["diagnosis_correct"] = any(d.get("category") == scenario["expected_diagnosis"] for d in diagnoses) if scenario["expected_diagnosis"] else None
        inference_jobs = [j for j in collected["jobs"] if j["stage"] == "inference"]
        result["duplicate_inference_launches"] = max(0, len(inference_jobs)-1)
        checks = {
            "all_stages": set(state["completed"]) == set(STAGES) and report["passed"],
            "no_pending": not any(state.get(k) for k in ["active", "pending", "failed"]),
            "fresh_jobs": {j["stage"] for j in collected["jobs"] if j["status"] == "completed"} == set(STAGES[2:]),
            "no_owned_processes": not collected["remaining_owned_processes"],
            "fault_demonstrated": scenario["id"] == "normal" or fixture.get("triggered", False) or scenario["id"] == "port_conflict" and any(d["category"] == "port_conflict" for d in diagnoses),
        }
        if scenario["id"] in {"lost_ack", "ssh_drop"}:
            checks["single_original_job"] = len(inference_jobs) == 1 and inference_jobs[0]["job"] == fixture.get("job")
        if scenario["id"] == "cache_corrupt":
            checks["cache_was_corrupted"] = fixture.get("corrupt_bytes") == 4096 and fixture.get("corrupt_sha256") != WEIGHT_SHA
        if scenario["id"] == "dependency_missing":
            checks["dependency_was_absent"] = fixture.get("import_check", {}).get("peft_absent") is True
        if checks["all_stages"]:
            versions = dict(p.split("==") for p in PACKAGES)
            versions["torch"] = "2.6.0"
            acceptance = remote.command(["env", "CUDA_VISIBLE_DEVICES=" + str(spec.gpu_ids[0]), "OMP_NUM_THREADS=4", spec.remote + "/.venv/bin/python", "-c", VALIDATE, spec.remote, json.dumps(state["completed"]), json.dumps(versions), WEIGHT_SHA], timeout=60)
            checks["independent_artifacts"] = acceptance["passed"]
            atomic_json(task_dir / "acceptance.json", acceptance)
        else:
            checks["independent_artifacts"] = False
            atomic_json(task_dir / "acceptance.json", {"passed": False, "reason": "incomplete required stages; never accept stale artifacts"})
        result["acceptance_checks"] = checks
        result["passed"] = all(checks.values())
        result["safe_stop"] = not scenario["recovery_supported"] and result["diagnosis_correct"] is True and not state["repairs"] and not collected["remaining_owned_processes"] and not any(state.get(k) for k in ["active", "pending"])
        result["recovery_seconds"] = event_recovery(run, fixture, collected["jobs"])
        if (run / "raw-benchmark").exists():
            shutil.copytree(run / "raw-benchmark", task_dir / "raw", dirs_exist_ok=True)
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", duration_seconds=round(time.monotonic()-started, 3))
    finally:
        if remote:
            remote.close()
    result["finished_at"] = now()
    atomic_json(task_dir / "result.json", result)
    print(json.dumps({"event": "task_end", "task": task_id, "passed": result["passed"], "seconds": result["duration_seconds"], "diagnosis": result.get("failure_category"), "error": result.get("error")}, ensure_ascii=False), flush=True)
    summarize(output)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["pilot", "confirmation"], required=True)
    parser.add_argument("--only", help="engineering smoke or resume selection; summary includes every recorded task")
    args = parser.parse_args()
    protocol = read_json(PROJECT / "evals/infra/protocol.json")
    output = PROJECT / "reports" / ("infra-bench-" + args.phase)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "protocol.json", protocol)
    manifest_path = output / "manifest.json"
    paths = [*sorted((PROJECT / "src/infra_pilot").glob("*.py")), Path(__file__).resolve()]
    manifest = {"phase": args.phase, "created_at": now(), "protocol_sha256": digest(PROJECT / "evals/infra/protocol.json"), "source_sha256": {str(p.relative_to(PROJECT)): digest(p) for p in paths}, "revision": REVISION, "model_weight_sha256": WEIGHT_SHA}
    if manifest_path.exists() and read_json(manifest_path)["source_sha256"] != manifest["source_sha256"]:
        raise ValueError("Source changed within one phase: preserve results and use a separate phase")
    if not manifest_path.exists():
        atomic_json(manifest_path, manifest)
        for path in paths:
            snapshot = output / "source_snapshot" / path.relative_to(PROJECT)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, snapshot)
    original = read_json(PROJECT / "reports/infra-001/report.json")["remote"]
    original.update(project="infrapilot-bench-20261109", service_port=19600, inject_port_conflict=False, stage_timeout=protocol["stage_timeout_seconds"])
    repetitions = protocol[args.phase + "_repetitions"]
    for repetition in range(1, repetitions+1):
        scenarios = list(protocol["scenarios"])
        random.Random(protocol["ordering_seed"]+repetition).shuffle(scenarios)
        for index, scenario in enumerate(scenarios):
            if args.only and scenario["id"] != args.only:
                continue
            task_id = scenario["id"] + f"-r{repetition}"
            if (output / "tasks" / task_id / "result.json").exists():
                continue
            spec = InfraSpec.model_validate({**original, "service_port": 19600 + 2*(repetition*10+index), "inject_port_conflict": scenario["id"] == "port_conflict"})
            run_task(task_id, scenario, spec, output, args.phase)
    print(json.dumps(summarize(output), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
