"""Human CLI for the live Host's native workspace registry, shared with Web."""

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18888)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--parent", default=None)
    create.add_argument("--request-id", default=None, help=argparse.SUPPRESS)
    opened = sub.add_parser("open")
    opened.add_argument("--path", required=True)
    session = sub.add_parser("session")
    session.add_argument("workspace_id")
    session.add_argument("--request-id", default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    entry = urlsplit(json.loads((args.root / ".local/harness-web/entry.json").read_text())["url"])
    if entry.scheme not in {"http", "https"} or entry.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Workspace CLI only uses the local Pilot Host entry")
    origin = f"{entry.scheme}://127.0.0.1:{args.port}"
    login = urlunsplit(
        (entry.scheme, f"127.0.0.1:{args.port}", entry.path, entry.query, entry.fragment)
    )
    payload = (
        {}
        if args.command in {"list", "open"}
        else {"request_id": args.request_id or str(uuid.uuid4())}
    )
    if args.command == "create":
        payload["title"] = args.name
    elif args.command == "session":
        payload["workspace_id"] = args.workspace_id
    method = {
        "list": "workspace-catalog",
        "create": "workspace-create",
        "session": "workspace-session",
        "open": "workspace-catalog",
    }[args.command]
    if args.command == "session" or (args.command == "create" and args.request_id):
        print("Workspace request: " + payload["request_id"], file=sys.stderr)
    try:
        with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
            client.get(login).raise_for_status()

            def rpc(endpoint, data, native=False):
                response = client.post(
                    origin + ("/api/" if native else "/pilot-infra-api/") + endpoint,
                    json={
                        "type": "client-request",
                        "rpcId": str(uuid.uuid4()),
                        "method": endpoint,
                        "payload": {"args": data} if native else data,
                    },
                )
                response.raise_for_status()
                result = response.json()["result"]
                if not result["ok"]:
                    raise ValueError(result["error"]["message"])
                return result["value"]

            if args.command == "open" or (args.command == "create" and not args.request_id):
                catalog = rpc("workspace-catalog", {})
                if args.command == "create":
                    parent = args.parent or catalog["root"]
                    path = rpc(
                        "directoryPicker/createDirectory", {"path": parent, "name": args.name}, True
                    )
                else:
                    path = args.path
                    rpc("directoryPicker/list", {"path": path}, True)
                workspace = rpc("workspace/create", {"request": {"path": path}}, True)["workspace"]
                session = rpc(
                    "session/create",
                    {
                        "request": {
                            "workspaceId": workspace["workspaceId"],
                            "agentPreset": "infra-lead",
                        }
                    },
                    True,
                )
                value = {
                    "workspace": {
                        "id": workspace["workspaceId"],
                        "title": workspace["title"],
                        "path": workspace["path"],
                    },
                    "sessionId": session["sessionId"],
                }
            else:
                value = rpc(method, payload)
    except httpx.HTTPStatusError as error:
        raise ValueError(f"Pilot Host returned HTTP {error.response.status_code}") from None
    except httpx.RequestError:
        raise ValueError(
            "Pilot Host unavailable; verify the local service and port, then retry with the same --request-id"
        ) from None
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
