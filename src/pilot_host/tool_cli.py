"""CLI and Web share the exact Node configuration validator and private file."""

import argparse
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pilot-harness tools")
    parser.add_argument("--root", type=Path, default=PROJECT)
    parser.add_argument("operation", choices=["list", "put", "delete", "enable", "disable", "test"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--file", type=Path, help="Server definition JSON; credentials use environment references")
    parser.add_argument("--revision", type=int, help="Expected configuration revision for changes")
    args = parser.parse_args(argv)
    if args.operation in {"put", "delete", "enable", "disable"} and args.revision is None:
        parser.error("Changes require --revision from tools list")
    if args.operation == "put" and not args.file:
        parser.error("put requires --file")
    if args.operation not in {"put", "list"} and not args.name:
        parser.error("This operation requires a server name")
    command = ["node", str(PROJECT / "harness/runtime/plugins/tool-system/cli.mjs"),
               "--root", str(args.root.resolve()), args.operation]
    if args.name:
        command += [args.name]
    if args.file:
        command += ["--file", str(args.file.resolve())]
    if args.revision is not None:
        command += ["--revision", str(args.revision)]
    raise SystemExit(subprocess.run(command, cwd=PROJECT, check=False).returncode)
