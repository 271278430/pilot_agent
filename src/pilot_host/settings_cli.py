"""Owner CLI for the same settings coordinator as the Web."""

import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pilot-harness settings")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument(
        "operation",
        choices=[
            "view",
            "save",
            "sync",
            "test-model",
            "check-directory",
            "check-resource-directory",
        ],
    )
    parser.add_argument("--file", type=Path, help="JSON settings payload; never printed")
    parser.add_argument("--revision", type=int)
    args = parser.parse_args(argv)
    if args.operation in {"save", "sync"} and args.revision is None:
        parser.error("Use --revision from settings view")
    if (
        args.operation in {"save", "test-model", "check-directory", "check-resource-directory"}
        and not args.file
    ):
        parser.error("This operation requires --file")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    payload = json.loads(args.file.read_text(encoding="utf-8")) if args.file else {}
    if args.revision is not None:
        payload["expected_revision"] = args.revision
    entry = urlsplit(json.loads((args.root / ".local/harness-web/entry.json").read_text())["url"])
    if entry.scheme not in {"http", "https"} or entry.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Settings CLI only connects to the local Pilot Host")
    authority = f"127.0.0.1:{args.port}"
    login = urlunsplit((entry.scheme, authority, entry.path, entry.query, entry.fragment))
    try:
        with httpx.Client(follow_redirects=True, timeout=45, trust_env=False) as client:
            client.get(login).raise_for_status()
            response = client.post(
                f"{entry.scheme}://{authority}/pilot-host-settings-api/{args.operation}",
                json={
                    "type": "client-request",
                    "rpcId": str(uuid.uuid4()),
                    "method": args.operation,
                    "payload": payload,
                },
            )
            response.raise_for_status()
            result = response.json()["result"]
    except httpx.HTTPStatusError as error:
        raise ValueError(f"Pilot Host returned HTTP {error.response.status_code}") from None
    except httpx.RequestError:
        raise ValueError("Pilot Host unavailable; verify the service and port") from None
    if not result["ok"]:
        raise ValueError(result["error"]["message"])
    print(json.dumps(result["value"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
