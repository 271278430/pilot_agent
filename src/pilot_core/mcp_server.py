"""Official MCP lifecycle; business contracts and authorization remain local."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from .capabilities import Capability


def make_server(name: str, capabilities: list[Capability], dispatch: Callable) -> Server:
    server = Server(name, version="0.3.1")
    catalog = {row.name: row for row in capabilities}

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=row.name, description=row.description,
            inputSchema=row.input_model.model_json_schema(),
            outputSchema=row.output_model.model_json_schema(),
            annotations=types.ToolAnnotations(readOnlyHint=row.effect == "read", destructiveHint=row.effect in {"source_write", "command"}, openWorldHint=False),
        ) for row in capabilities]

    @server.call_tool()
    async def call_tool(tool: str, arguments: dict):
        try:
            if tool not in catalog:
                raise ValueError("Unregistered tool")
            row = catalog[tool]
            args = row.input(arguments)
            value = await dispatch(tool, args) if inspect.iscoroutinefunction(dispatch) else await anyio.to_thread.run_sync(dispatch, tool, args)
            value = row.output(value)
            failed = value.get("ok") is False or value.get("exit_code", 0) != 0
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value, isError=failed,
            )
        except (ValidationError, ValueError) as exc:
            # This is an MCP error, not a successful business receipt.
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc)[:4000])], isError=True)

    return server


def serve(name: str, capabilities: list[Capability], dispatch: Callable):
    async def run():
        server = make_server(name, capabilities, dispatch)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
