"""Runtimes: the things that execute an agent. Add a class and an enum value here."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from exradius.runtimes.base import (
    AuthMethod,
    ConnectionSpec,
    EventSink,
    ProbeResult,
    ProviderCredential,
    ProviderKind,
    RunOutcome,
    RunSpec,
    Runtime,
    RuntimeCapabilities,
    SettingField,
)


class RuntimeName(StrEnum):
    CLAUDE_CODE = "claude_code"


def runtime_class(name: str) -> type[Runtime]:
    match RuntimeName(name):
        case RuntimeName.CLAUDE_CODE:
            from exradius.runtimes.claude_code import ClaudeCodeRuntime

            return ClaudeCodeRuntime


def get_runtime(name: str, runtime_dir: Path | None = None) -> Runtime:
    """``runtime_dir`` is the runtime bundle the runner runs from, if any."""
    return runtime_class(name)(runtime_dir)


def all_runtimes() -> list[type[Runtime]]:
    return [runtime_class(name) for name in RuntimeName]


__all__ = [
    "AuthMethod",
    "ConnectionSpec",
    "EventSink",
    "ProbeResult",
    "ProviderCredential",
    "ProviderKind",
    "RunOutcome",
    "RunSpec",
    "Runtime",
    "RuntimeCapabilities",
    "RuntimeName",
    "SettingField",
    "all_runtimes",
    "get_runtime",
    "runtime_class",
]
