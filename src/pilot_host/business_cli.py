"""Owner CLI for reviewed software and network tasks in the same registries."""

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("category", choices=["software", "network"])
    parser.add_argument("action")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--task-id")
    args = parser.parse_args(argv)
    payload = json.loads(args.file.read_text(encoding="utf-8")) if args.file else {}
    if args.category == "software":
        from pilot_infra.build_service import BuildService

        service = BuildService(args.root)
        structured = {
            "recommend": service.recommend,
            "source-export": service.export_source,
            "source-download": lambda value: service.export_source(value, downloading=True),
            "save-profile": service.save_profile,
            "create": service.create,
            "read": service.read,
            "write": service.edit,
            "patch": service.patch,
            "run": service.run,
        }
        target = {
            "view": service.task,
            "prepare": service.prepare,
            "freeze": service.freeze,
            "recover": service.recover,
            "cleanup": service.cleanup,
        }
        if args.action == "catalog":
            value = service.catalog()
        elif args.action in structured:
            value = structured[args.action](payload)
        elif args.action in target and args.task_id:
            value = target[args.action](args.task_id)
        else:
            parser.error("Unknown software action or missing --task-id")
    else:
        from pilot_infra.network_service import NetworkService

        service = NetworkService(args.root)
        if args.action == "catalog":
            value = service.list()
        elif args.action == "recommend":
            value = service.recommend(payload["server_ids"])
        elif args.action == "create":
            value = service.create(payload)
        elif args.action in {"view", "run", "cleanup"} and args.task_id:
            value = {"view": service.get, "run": service.run, "cleanup": service.cleanup}[
                args.action
            ](args.task_id)
        else:
            parser.error("Unknown network action or missing --task-id")
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
