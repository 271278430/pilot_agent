"""Isolated MCP server used by native client tests; no host/GPU operations."""
import os
import sys

from mcp.server.fastmcp import FastMCP

http_port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--http" else None
server = FastMCP("pilot-test", instructions="fixture contextual instructions", host="127.0.0.1", port=http_port or 8000, json_response=True, stateless_http=True)


@server.tool()
def echo(text: str) -> str:
    """Echo input to verify the native transport and canonical result."""
    return text


@server.tool()
def fail() -> str:
    """Produce an explicit MCP tool failure."""
    raise ValueError("fixture failure")


@server.tool()
def check_reference() -> str:
    """Check presence of a referenced variable without exposing its value."""
    return "present" if os.environ.get("PILOT_MCP_FIXTURE") else "missing"


if __name__ == "__main__":
    server.run(transport="streamable-http" if http_port else "stdio")
