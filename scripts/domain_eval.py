"""Owner CLI: freeze a protocol, run finite trials, inspect and export receipts."""

import argparse
import json
import time
from pathlib import Path

from pilot_eval.contracts import EvaluationPlan
from pilot_eval.report import publish
from pilot_eval.runner import Runner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["freeze", "start", "poll", "report", "batch", "recover", "abort", "intervention"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--trial-id")
    parser.add_argument("--case-id")
    parser.add_argument("--strategy", choices=["single", "multi", "auto"], default="single")
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--kind", choices=["repair", "budget", "inspection"], default="inspection")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    runner = Runner(args.root, args.origin)
    if args.operation == "freeze":
        plan = EvaluationPlan.model_validate(json.loads(args.plan.read_text()))
        print(json.dumps({"run_id": runner.store.freeze(plan, runner.identities())}), flush=True)
        return
    if args.operation == "start":
        value = runner.start(args.run_id, args.case_id, args.strategy, args.repetition)
    elif args.operation == "poll":
        value = runner.poll(args.trial_id)
    elif args.operation == "report":
        value = runner.report(args.run_id)
    elif args.operation == "recover":
        value = runner.recover(args.trial_id)
    elif args.operation == "abort":
        if not args.note:
            parser.error("abort requires --note")
        value = runner.abort(args.trial_id, args.note)
    elif args.operation == "intervention":
        if not args.note:
            parser.error("intervention requires --note")
        value = runner.intervention(args.trial_id, args.kind, args.note)
    else:
        manifest = runner.assert_identity(args.run_id)
        for case_id in manifest["plan"]["case_ids"]:
            for repetition in range(1, manifest["plan"]["repetitions"] + 1):
                for strategy in manifest["plan"]["strategies"]:
                    row = runner.start(args.run_id, case_id, strategy, repetition)
                    print(json.dumps({"trial_id": row["id"], "case_id": case_id, "strategy": strategy, "phase": row["phase"]}), flush=True)
                    last = None
                    while row["phase"] not in {"settled", "unavailable"}:
                        time.sleep(3)
                        row = runner.poll(row["id"])
                        progress = (row["phase"], row.get("usage", {}).get("input_tokens", 0) + row.get("usage", {}).get("output_tokens", 0))
                        if progress != last:
                            print(json.dumps({"trial_id": row["id"], "phase": row["phase"], "tokens": progress[1], "result": row.get("result")}), flush=True)
                            last = progress
                        publish(args.root, args.run_id, runner.report(args.run_id))
        value = runner.report(args.run_id)
    if args.run_id:
        publish(args.root, args.run_id, runner.report(args.run_id))
    print(json.dumps(value, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
