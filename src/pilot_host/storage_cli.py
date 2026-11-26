"""Owner storage inspection and reversible housekeeping."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from pilot_core.storage import StorageLayout
from pilot_core.storage_housekeeping import archive, migrate_caches


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "archive", "migrate-caches", "run"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--require-data-disk", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args, command = parser.parse_known_args(argv)
    layout = StorageLayout(args.root)
    if args.require_data_disk:
        layout.require_data_disk()
    if args.operation == "run":
        command = command[1:] if command[:1] == ["--"] else command
        if not command:
            parser.error("run requires an executable after --")
        env = layout.environment(strict=args.require_data_disk)
        if os.name != "nt":
            os.execvpe(command[0], command, env)
        raise SystemExit(subprocess.call(command, env=env, cwd=layout.root))
    if command:
        parser.error("Unexpected arguments")
    if args.operation == "prepare":
        value = layout.prepare(args.require_data_disk)
    elif args.operation == "migrate-caches":
        value = migrate_caches(layout.root, args.apply)
    else:
        value = archive(layout.root, args.apply)
    print(json.dumps(value, ensure_ascii=False, indent=2))
