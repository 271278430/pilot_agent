"""Standard-library MCP fixture for real container isolation tests; never production code."""

import json
import os
import socket
import sys
import time
from pathlib import Path

count = 0
schemas = {
    "count": {},
    "read": {"path": {"type": "string"}},
    "write": {"path": {"type": "string"}, "text": {"type": "string"}},
    "env": {"name": {"type": "string"}},
    "network": {},
    "wait": {},
}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sandbox-fixture", "version": "1"},
            "instructions": "Container isolation fixture",
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": "Isolation test " + name,
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "additionalProperties": False,
                    },
                }
                for name, properties in schemas.items()
            ]
        }
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments", {})
        try:
            if name == "count":
                count += 1
                text = str(count)
            elif name == "read":
                text = Path(args["path"]).read_text()
            elif name == "write":
                Path(args["path"]).write_text(args["text"])
                text = "written"
            elif name == "env":
                text = os.getenv(args["name"], "absent")
            elif name == "network":
                connection = socket.create_connection(("1.1.1.1", 443), timeout=0.2)
                connection.close()
                text = "connected"
            else:
                time.sleep(2)
                text = "waited"
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except OSError as error:
            result = {
                "content": [{"type": "text", "text": "denied: " + str(error)}],
                "isError": True,
            }
    else:
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            ),
            flush=True,
        )
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
