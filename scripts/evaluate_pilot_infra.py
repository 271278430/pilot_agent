"""Finite real-GPU domain evaluations; orchestration here is deterministic."""

import argparse
import json
import uuid
from pathlib import Path

from pilot_infra.contracts import InfraPlan
from pilot_infra.service import InfraService
from repopilot.storage import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "deploy",
            "deploy_retry",
            "status",
            "test",
            "fixtures",
            "fault",
            "repair",
            "compare",
            "compare_next",
            "finish",
            "source_seed",
            "rebuild",
            "rebuild_launch",
            "stop",
        ],
    )
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    s = InfraService(args.root)
    index = args.root / ".local/domain-evaluation.json"
    saved = (
        json.loads(index.read_text())
        if index.exists()
        else {"controller_kind": "deterministic", "inference_kind": "real_sglang", "tasks": {}}
    )

    def submit(t, action, candidate):
        # Persist identity before side effects; retry requires inspecting the
        # recorded Job, never blindly starting another operation.
        atomic_json(index, saved)
        return s.submit(s.store.get(t["id"]), action, candidate)

    def create(kind, target=None, **extra):
        tag = extra.pop("tag", kind)
        goal = extra.pop(
            "goal",
            "在批准的整机范围与 GPU 7 配额内，验证实际模型身份、协议质量、负载矩阵及故障恢复",
        )
        p = InfraPlan(
            title="实机验收 · " + kind,
            goal=goal,
            kind=kind,
            model_asset="qwen3-06b",
            image_asset="sglang-cu124",
            target_task_id=target,
            workload={
                "input_lengths": [128, 512],
                "concurrency": [1, 2],
                "requests_per_case": 4,
                "max_new_tokens": 48,
                "timeout_seconds": 30,
            },
            **extra,
        )
        t = s.create({"request_id": uuid.uuid4().hex, "plan": p.model_dump()})["task"]
        s.store.update(t["id"], controller_kind="deterministic", evaluation=True)
        t = s.control(
            t["id"], "start", {"request_id": uuid.uuid4().hex, "expected_revision": t["revision"]}
        )["task"]
        saved["tasks"][tag] = t["id"]
        return t

    if args.mode == "deploy":
        if saved["tasks"].get("deploy_verify"):
            raise ValueError("Inspect the existing evaluation; do not duplicate it")
        orphan = [
            t
            for t in s.store.list()
            if t["state"].get("evaluation") and t["plan"]["kind"] == "deploy_verify"
        ]
        if len(orphan) > 1:
            raise ValueError("Multiple evaluation deployments require manual inspection")
        t = orphan[0] if orphan else create("deploy_verify")
        saved["tasks"]["deploy_verify"] = t["id"]
        c = s.candidate(
            t,
            {
                "request_id": "evaluation-healthy-candidate",
                "serving": t["plan"]["serving"],
                "reason": "健康部署与真实协议验收",
            },
        )
        saved["healthy"] = c["id"]
        saved["last_job"] = submit(t, "launch", c["id"])
    elif args.mode == "status":
        for kind, id in saved["tasks"].items():
            view = s.view(s.store.get(id))
            t = view["task"]
            print(
                json.dumps(
                    {
                        "kind": kind,
                        "id": id,
                        "status": t["state"]["status"],
                        "job": t["state"]["active_job"],
                        "latest": view["events"][-1] if view["events"] else None,
                    },
                    ensure_ascii=False,
                )[-6000:]
            )
        return
    else:
        target = s.store.get(saved["tasks"]["deploy_verify"])
        if args.mode == "deploy_retry":
            if target["state"]["active_job"] or target["state"].get("service_candidate"):
                raise ValueError("Inspect active effects before retry")
            if target["state"]["status"] in {"paused", "failed"}:
                target = s.control(
                    target["id"],
                    "resume",
                    {
                        "request_id": uuid.uuid4().hex,
                        "expected_revision": target["revision"],
                    },
                )["task"]
            c = s.candidate(
                target,
                {
                    "request_id": uuid.uuid4().hex,
                    "serving": target["plan"]["serving"],
                    "reason": "实测依赖修复后重新冻结环境身份",
                },
            )
            saved["healthy"] = c["id"]
            saved["last_job"] = submit(target, "launch", c["id"])
        elif args.mode == "test":
            t = s.store.get(saved["active_task"]) if saved.get("active_task") else target
            current = s.owner(t)["state"]["service_candidate"]
            if not current:
                raise ValueError("No admitted live candidate")
            saved["last_job"] = submit(t, "test", current)
        elif args.mode == "fixtures":
            # Prepare frozen control variants before ending the healthy task.
            cfg = {**target["plan"]["serving"], "stop": ["A", "4", "猫", "{"]}
            bad = s.candidate(
                target,
                {
                    "request_id": uuid.uuid4().hex,
                    "serving": cfg,
                    "reason": "受控故障：错误停止词导致协议输出截断",
                },
            )
            saved["bad_stop"] = bad["id"]
            s.finish(s.store.get(target["id"]))
            t = create("test", target["id"])
            saved["active_task"] = t["id"]
            saved["last_job"] = submit(t, "test", saved["healthy"])
        elif args.mode == "fault":
            if saved.get("active_task"):
                prior = s.store.get(saved["active_task"])
                if prior["state"]["status"] == "running":
                    s.finish(prior)
            t = create("diagnose_repair", target["id"])
            saved["active_task"] = t["id"]
            saved["last_job"] = submit(t, "launch", saved["bad_stop"])
        elif args.mode == "repair":
            t = s.store.get(saved["active_task"])
            failed = [
                e
                for e in s.store.events(t["id"])
                if e["kind"] == "job_settled" and e["data"]["result"].get("passed") is False
            ]
            if not failed:
                raise ValueError("Run a real failing baseline first")
            # Record an observed, provisional hypothesis. This driver is a
            # controlled fixture, not autonomous reasoning or a native Agent.
            h = {
                "symptom": "错误停止词导致多个实际探针没有输出",
                "cause": "候选停止词截断了所要求的输出",
                "evidence_ids": [str(failed[-1]["seq"])],
                "experiment": "仅清空停止词，重新部署同一模型并重跑完整矩阵",
                "needs_source": False,
                "status": "provisional",
            }
            s.store.update(t["id"], hypotheses=[h])
            c = s.candidate(
                t,
                {
                    "request_id": uuid.uuid4().hex,
                    "parent_id": saved["bad_stop"],
                    "serving": target["plan"]["serving"],
                    "reason": "受控复验：仅纠正停止词配置",
                },
            )
            saved["repaired"] = c["id"]
            saved["last_job"] = submit(t, "launch", c["id"])
        elif args.mode == "compare":
            s.finish(s.store.get(saved["active_task"]))
            t = create(
                "compare", target["id"], candidate_ids=[saved["bad_stop"], saved["repaired"]]
            )
            saved["active_task"] = t["id"]
            saved["last_job"] = submit(t, "launch", saved["bad_stop"])
        elif args.mode == "compare_next":
            t = s.store.get(saved["active_task"])
            if t["plan"]["kind"] != "compare":
                raise ValueError("Current task is not a comparison")
            saved["last_job"] = submit(t, "launch", saved["repaired"])
        elif args.mode == "finish":
            t = s.store.get(saved["active_task"])
            s.finish(t)
        elif args.mode == "source_seed":
            if saved["tasks"].get("source_diagnose"):
                previous = saved["tasks"]["source_diagnose"]
                saved["tasks"]["source_previous_" + previous[:8]] = previous
            t = create(
                "diagnose_repair",
                target["id"],
                tag="source_diagnose",
                max_tokens=300000,
                goal="先独立测试冻结的当前候选，保留真实失败基线。依据实际证据诊断、修复并冻结后代候选，重新部署和独立复验。只使用批准配额，不修改评分协议。",
            )
            saved["active_task"] = t["id"]
            source = s.workspace(target) / "adapter.py"
            source.write_bytes(b'def build_options():\n    return {"temperature": 1}\n')
            s.store.event(
                t["id"], "controlled_fault_seed", {"scope": "candidate adapter", "fixture": True}
            )
            c = s.candidate(
                t,
                {
                    "request_id": uuid.uuid4().hex,
                    "serving": target["plan"]["serving"],
                    "reason": "受控源码故障：请求选项违反确定性验收协议",
                },
            )
            saved["source_bad"] = c["id"]
            saved["last_job"] = submit(t, "launch", c["id"])
        elif args.mode == "rebuild":
            if saved["tasks"].get("clean_rebuild"):
                raise ValueError("Inspect the existing rebuild; do not duplicate it")
            current = s.owner(target)["state"]["service_candidate"]
            if not current:
                raise ValueError("Rebuild requires a verified owned live service")
            t = create("diagnose_repair", target["id"], tag="clean_rebuild",
                       goal="显式停止并重建已验收的自有候选，核对新进程身份并执行全矩阵复验。")
            saved["active_task"] = t["id"]
            saved["rebuild_candidate"] = current
            saved["last_job"] = submit(t, "stop", current)
        elif args.mode == "rebuild_launch":
            t = s.store.get(saved["tasks"]["clean_rebuild"])
            if s.owner(t)["state"]["service_candidate"] or t["state"]["active_job"]:
                raise ValueError("Explicit stop must settle before a new launch")
            saved["last_job"] = submit(t, "launch", saved["rebuild_candidate"])
        elif args.mode == "stop":
            if saved.get("active_task"):
                t = s.store.get(saved["active_task"])
                if t["state"]["status"] == "running":
                    s.finish(t)
            saved["last_job"] = submit(
                target, "stop", s.store.get(target["id"])["state"]["service_candidate"]
            )
    atomic_json(index, saved)
    print(json.dumps(saved, ensure_ascii=False))


if __name__ == "__main__":
    main()
