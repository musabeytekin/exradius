"""``exradius-runner --list-tools`` (docs/agent-routing.md §3.1) against a small MCP server
started over stdio, as Verify does in the workstation."""

from __future__ import annotations

import json
import sys

from exradius.runner import list_tools
from exradius.runtimes.base import LIST_TOOLS_FILE, LIST_TOOLS_SPEC

SERVER = """\
import os
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("docs")


@server.tool(description="Search the docs.", annotations=ToolAnnotations(readOnlyHint=True))
def search(query: str) -> str:
    return query


@server.tool(description="Write a page for " + os.environ["DOCS_SPACE"] + ".")
def write_page(title: str) -> str:
    return title


if os.environ.get("DOCS_TOKEN") != "secret-value":
    raise SystemExit("DOCS_TOKEN missing")
server.run()
"""


def _spec(tmp_path, **env) -> dict:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    return {"name": "docs", "transport": "stdio", "command": sys.executable, "args": [str(script)], "env": env}


async def test_lists_the_tools_with_annotations(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("EXRADIUS_WORK_DIR", str(tmp_path))
    environ = {"PATH": "/usr/bin:/bin", "DOCS_SPACE": "ENG",
               LIST_TOOLS_SPEC: json.dumps(_spec(tmp_path, DOCS_TOKEN="secret-value"))}
    assert await list_tools.main(environ) == 0
    assert LIST_TOOLS_SPEC not in environ  # the server never sees it
    report = json.loads((tmp_path / LIST_TOOLS_FILE).read_text())
    assert report == {"ok": True, "error": None, "tools": [
        {"name": "search", "description": "Search the docs.", "annotations": {"readOnlyHint": True}},
        {"name": "write_page", "description": "Write a page for ENG."},  # the container's environment
    ]}


async def test_a_server_that_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("EXRADIUS_WORK_DIR", str(tmp_path))
    environ = {"PATH": "/usr/bin:/bin", "DOCS_SPACE": "ENG", LIST_TOOLS_SPEC: json.dumps(_spec(tmp_path))}
    assert await list_tools.main(environ) == 1
    report = json.loads((tmp_path / LIST_TOOLS_FILE).read_text())
    assert report["ok"] is False and report["tools"] is None and report["error"]
