"""Start one MCP server as the agent would and list its tools (docs/agent-routing.md §3.1).

A verify Run's runner (``exradius-runner --verify``) calls ``report`` for every connection an
agent uses, in the workstation container of the verify pod, after its setup, and sends the
results to the internal API. ``exradius-runner --list-tools`` does one connection for the
skill's local check: the connection comes in ``EXRADIUS_LIST_TOOLS`` (the runner-facing spec,
secrets substituted) and the result goes to ``/tmp/exradius-list-tools.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from exradius.runtimes.base import LIST_TOOLS_FILE, LIST_TOOLS_SPEC

TIMEOUT = 50.0
"""Seconds for start, ``initialize`` and every page of ``tools/list``; the worker allows 60."""
MAX_PAGES = 50


def _tool(tool: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": tool.name, "description": tool.description}
    if tool.annotations is not None:
        annotations = tool.annotations.model_dump(mode="json", exclude_none=True, by_alias=True)
        if annotations:
            entry["annotations"] = annotations
    return entry


async def _list(session) -> list[dict[str, Any]]:
    from mcp.types import PaginatedRequestParams

    await session.initialize()
    tools: list[dict[str, Any]] = []
    cursor = None
    for _ in range(MAX_PAGES):
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools += [_tool(t) for t in result.tools]
        cursor = result.next_cursor
        if not cursor:
            break
    return tools


async def list_tools(spec: dict[str, Any], environ: dict[str, str], errlog=None) -> list[dict[str, Any]]:
    """The server's tools: name, description and annotations. ``errlog`` takes a stdio
    server's stderr."""
    from mcp import ClientSession

    if spec["transport"] == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=spec["command"], args=list(spec.get("args") or []),
            env={**environ, **(spec.get("env") or {})}, cwd=os.environ.get("EXRADIUS_WORK_DIR", "/work"),
        )
        async with stdio_client(params, errlog=errlog or sys.stderr) as (read, write), \
                ClientSession(read, write) as session:
            return await _list(session)
    if spec["transport"] == "http":
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        async with create_mcp_http_client(headers=dict(spec.get("headers") or {})) as http, \
                streamable_http_client(spec["url"], http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                return await _list(session)
    raise ValueError(f"unknown transport {spec['transport']!r}")


def _leaf(exc: BaseException) -> BaseException:
    """The first real error inside the task groups the MCP client runs."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def report(spec: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    """``{"ok", "tools", "error", "log"}`` for one connection; ``log`` is the tail of a stdio
    server's stderr."""
    with tempfile.TemporaryFile(mode="w+") as errlog:
        try:
            tools = await asyncio.wait_for(list_tools(spec, environ, errlog), TIMEOUT)
            result: dict[str, Any] = {"ok": True, "tools": tools, "error": None}
        except TimeoutError:
            result = {"ok": False, "tools": None, "error": f"the server did not list its tools within {TIMEOUT:.0f}s"}
        except BaseException as exc:  # noqa: BLE001 - every failure is the step's error
            leaf = _leaf(exc)
            traceback.print_exception(leaf)
            result = {"ok": False, "tools": None, "error": f"{type(leaf).__name__}: {leaf}"}
        errlog.seek(0)
        result["log"] = errlog.read()[-2000:] if not result["ok"] else ""
    return result


async def main(environ: dict[str, str]) -> int:
    """``environ`` is the container's environment (``RunnerEnvironment.container``)."""
    spec = json.loads(environ.pop(LIST_TOOLS_SPEC, "{}") or "{}")
    try:
        tools = await asyncio.wait_for(list_tools(spec, environ), TIMEOUT)
        report: dict[str, Any] = {"ok": True, "tools": tools, "error": None}
        print(f"{spec.get('name')}: {len(tools)} tools", flush=True)
    except TimeoutError:
        report = {"ok": False, "tools": None, "error": f"the server did not list its tools within {TIMEOUT:.0f}s"}
    except BaseException as exc:  # noqa: BLE001 - every failure is the step's error
        leaf = _leaf(exc)
        traceback.print_exception(leaf)
        report = {"ok": False, "tools": None, "error": f"{type(leaf).__name__}: {leaf}"}
    Path(os.environ.get("TMPDIR") or "/tmp", LIST_TOOLS_FILE).write_text(json.dumps(report))
    return 0 if report["ok"] else 1
