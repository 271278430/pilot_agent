"""Human Owner CLI for the same node registry used by Pilot Web."""

import argparse
import json
from pathlib import Path

from pilot_infra.asset_service import AssetService
from pilot_infra.fleet_worker import resolve_model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("docker")
    p.add_argument("server_id")
    for name in ["save", "stack", "executor", "check-executor", "prepare", "register-proposal"]:
        p = sub.add_parser(name)
        p.add_argument("--file", type=Path, required=True)
    for name in ["status", "cancel", "reconcile"]:
        p = sub.add_parser(name)
        p.add_argument("operation_id")
    p = sub.add_parser("match")
    p.add_argument("server_id")
    p.add_argument("asset_id")
    p = sub.add_parser("resolve-image")
    p.add_argument("server_id")
    p.add_argument("reference")
    p = sub.add_parser("resolve-model")
    p.add_argument("repository")
    p.add_argument("--revision", default="main")
    p.add_argument(
        "--endpoint",
        choices=["https://huggingface.co", "https://hf-mirror.com"],
        default="https://huggingface.co",
    )
    p = sub.add_parser("cleanup-executor")
    p.add_argument("server_id")
    p.add_argument("--confirm-root-removal", action="store_true", required=True)
    args = parser.parse_args(argv)
    service = AssetService(args.root)
    if args.command == "list":
        value = service.catalog()
    elif args.command == "docker":
        value = service.docker_inventory({"server_id": args.server_id})
    elif args.command in {"save", "stack", "executor", "check-executor", "prepare", "register-proposal"}:
        payload = json.loads(args.file.read_text(encoding="utf-8"))
        value = {
            "save": service.save,
            "stack": service.save_stack,
            "executor": service.configure_executor,
            "check-executor": service.inspect_executor,
            "prepare": service.prepare,
            "register-proposal": service.register_proposal,
        }[args.command](payload)
    elif args.command in {"status", "cancel", "reconcile"}:
        value = service.refresh(
            args.operation_id,
            cancel=args.command == "cancel",
            reconcile=args.command == "reconcile",
        )
    elif args.command == "resolve-image":
        value = service.resolve_image({"server_id": args.server_id, "reference": args.reference})
    elif args.command == "match":
        value = service.match(args.server_id, args.asset_id)
    elif args.command == "resolve-model":
        value = resolve_model(args.endpoint, args.repository, args.revision)
    else:
        value = service.cleanup_executor({"server_id": args.server_id})
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
