"""Repo MCP adapter; official SDK owns lifecycle, framing and cancellation."""

from __future__ import annotations

import sys
from pathlib import Path

from pilot_core.capabilities import for_role
from pilot_core.mcp_server import serve as sdk_serve

from .tools import ToolService


def serve(run: Path) -> None:
    service = None

    def dispatch(name, arguments):
        nonlocal service
        service = service or ToolService(run)
        return service.call(name, arguments)

    sdk_serve("repopilot", for_role("repo"), dispatch)


if __name__ == "__main__":
    serve(Path(sys.argv[1]).resolve())
