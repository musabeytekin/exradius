"""Runtimes: the things that execute an agent. Add a class and an enum value here."""

from __future__ import annotations

from enum import StrEnum

from exradius.runtimes.base import ConnectionSpec, EventSink, RunOutcome, RunSpec, Runtime


class RuntimeName(StrEnum):
    CLAUDE_CODE = "claude_code"


RUNTIME_LABELS: dict[RuntimeName, str] = {
    RuntimeName.CLAUDE_CODE: "Claude Code (Claude Agent SDK)",
}


def get_runtime(name: str) -> Runtime:
    match RuntimeName(name):
        case RuntimeName.CLAUDE_CODE:
            from exradius.runtimes.claude_code import ClaudeCodeRuntime

            return ClaudeCodeRuntime()


__all__ = [
    "ConnectionSpec",
    "EventSink",
    "RunOutcome",
    "RunSpec",
    "Runtime",
    "RUNTIME_LABELS",
    "RuntimeName",
    "get_runtime",
]
