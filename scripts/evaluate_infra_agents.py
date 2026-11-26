"""Finite real-model evaluation through the installed official Web/Session APIs."""

import argparse
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from pilot_infra.contracts import InfraPlan
from pilot_infra.service import InfraService
from repopilot.storage import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["single", "multi", "auto", "source_auto", "status"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    service = InfraService(root)
    path = root / ".local/infra-agent-evaluation.json"
    saved = json.loads(path.read_text()) if path.is_file() else {"runs": []}
    entry = urlsplit(json.loads((root / ".local/harness-web/entry.json").read_text())["url"])
    origin = "http://127.0.0.1:19888"
    login = urlunsplit((entry.scheme, "127.0.0.1:19888", entry.path, entry.query, entry.fragment))
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        client.get(login).raise_for_status()

        def rpc(endpoint, payload, channel="pilot-infra-api"):
            result = client.post(
                origin + "/" + channel + "/" + endpoint,
                json={
                    "type": "client-request",
                    "rpcId": uuid.uuid4().hex,
                    "method": endpoint,
                    "payload": {"args": payload} if channel == "api" else payload,
                },
            )
            result.raise_for_status()
            result = result.json()["result"]
            if not result["ok"]:
                raise ValueError(result["error"])
            return result["value"]

        if args.mode != "status":
            if any(
                service.store.get(r["task_id"])["state"]["status"] == "running"
                for r in saved["runs"]
            ):
                raise ValueError(
                    "A real Agent task remains active; inspect it before starting another"
                )
            target_id = json.loads((root / ".local/domain-evaluation.json").read_text())["tasks"][
                "deploy_verify"
            ]
            if args.mode == "source_auto":
                evaluation = json.loads((root / ".local/domain-evaluation.json").read_text())
                task = service.store.get(evaluation["tasks"]["source_diagnose"])
                failures = [
                    e
                    for e in service.store.events(task["id"])
                    if e["kind"] == "job_settled" and e["data"]["status"] == "failed"
                ]
                if not failures and not service.owner(task)["state"]["service_candidate"]:
                    raise ValueError("Source case requires the real seeded deployment first")
                if task["state"]["status"] == "running":
                    service.control(
                        task["id"],
                        "pause",
                        {"request_id": uuid.uuid4().hex, "expected_revision": task["revision"]},
                    )
                opened = rpc("open", {"task_id": task["id"]})
                view = rpc("view", {"session_id": str(opened["sessionId"])})
                view["sessionId"] = opened["sessionId"]
            else:
                plan = InfraPlan(
                    title="真实 Agent 对照 · " + args.mode,
                    goal="对当前健康推理服务执行独立质量与性能复验，通过后完成任务。使用登记候选，勿重复部署。",
                    kind="test",
                    target_task_id=target_id,
                    model_asset="qwen3-06b",
                    image_asset="sglang-cu124",
                    strategy=args.mode,
                    workload={
                        "input_lengths": [128],
                        "concurrency": [1],
                        "requests_per_case": 4,
                        "max_new_tokens": 16,
                        "timeout_seconds": 30,
                    },
                    max_tokens=120000,
                )
                view = rpc("create", {"request_id": uuid.uuid4().hex, "plan": plan.model_dump()})
            row = {
                "strategy": args.mode,
                "task_id": view["task"]["id"],
                "session_id": str(view["sessionId"]),
                "controller_kind": "real_model",
                "fault_seed": "deterministic source injection and real deployment; Agent must observe failure"
                if args.mode == "source_auto"
                else None,
                "controller_model": "glm-5.3",
                "inference_kind": "real_sglang",
                "started_epoch": time.time(),
                "pricing": "unpriced",
            }
            saved["runs"].append(row)
            atomic_json(path, saved)
            rpc(
                "resume" if args.mode == "source_auto" else "start",
                {
                    "session_id": row["session_id"],
                    "request_id": uuid.uuid4().hex,
                    "expected_revision": view["task"]["revision"],
                },
            )
        for row in saved["runs"]:
            view = rpc("view", {"session_id": row["session_id"]})
            history = rpc(
                "session/events",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": row["session_id"]},
                        "maxMessages": 200,
                    }
                },
                "api",
            )
            records = [
                r
                for r in history["records"]
                if r.get("event", {}).get("type")
                in {"tool/call", "tool/result", "turn/end", "error", "subagent/spawn"}
            ]
            atomic_json(root / ".local/infra-agent-traces" / (row["session_id"] + ".json"), records)
            child_ids = set()
            for record in records:
                event = record["event"]
                if event["type"] == "tool/result":
                    for part in event["data"].get("message", {}).get("content", []):
                        child_ids.update(re.findall(r"started subagent ([a-z0-9-]+)", part.get("text", "")))
            for child_id in child_ids:
                child = rpc("session/events", {"request": {"address": {
                    "kind": "subagent", "parentSessionId": row["session_id"],
                    "childSessionId": child_id, "mode": "continuable",
                }, "maxMessages": 200}}, "api")
                atomic_json(root / ".local/infra-agent-traces" / ("child-" + child_id + ".json"), child["records"])
            row["child_ids"] = sorted(child_ids)
            row["trace_events"] = [
                {"type": r["event"]["type"], "data": r["event"]["data"]} for r in records
            ]
            task = view["task"]
            row.update(
                status=task["state"]["status"],
                job=task["state"]["active_job"],
                budget=view["budget"],
                strategy_history=task["state"]["strategy_history"],
                latest_event=view["events"][-1] if view["events"] else None,
                report=task["state"].get("outcome"),
                error=task["state"].get("error"),
            )
            if row["status"] in {"completed", "paused", "failed", "cancelled"}:
                row.setdefault("observed_end_epoch", time.time())
            observation = {"status": row["status"], "budget": view["budget"]}
            observations = row.setdefault("observations", [])
            if not observations or observation != {k: observations[-1][k] for k in observation}:
                observations.append({**observation, "time": time.time()})
            if row["status"] == "completed":
                row.setdefault("completed_epoch", time.time())
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in ["strategy", "task_id", "status", "job", "budget", "error"]
                    },
                    ensure_ascii=False,
                )
            )
        atomic_json(path, saved)


if __name__ == "__main__":
    main()
