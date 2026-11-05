""""Test connection": start an MCP server and list its tools (architecture §8)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from exradius.schemas.api import ConnectionTestOut

TEST_TIMEOUT_SECONDS = 60


async def _list_tools(spec: dict[str, Any]) -> list[dict[str, Any]]:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    if spec["transport"] == "stdio":
        target: Any = StdioServerParameters(
            command=spec["command"],
            args=spec["args"],
            env={**os.environ, **spec["env"]} if spec["env"] else None,
            cwd=tempfile.gettempdir(),
        )
    else:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers=spec["headers"] or None, timeout=30)
        target = streamable_http_client(spec["url"], http_client=http)

    async with Client(target) as client:
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend({"name": t.name, "description": t.description or ""} for t in page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools


async def test_connection(spec: dict[str, Any]) -> ConnectionTestOut:
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            tools = await _list_tools(spec)
    except TimeoutError:
        return ConnectionTestOut(ok=False, error=f"timed out after {TEST_TIMEOUT_SECONDS}s")
    except BaseException as exc:  # noqa: BLE001 - exception groups from anyio
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        return ConnectionTestOut(ok=False, error=_describe(exc))
    return ConnectionTestOut(ok=True, tools=tools)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"
