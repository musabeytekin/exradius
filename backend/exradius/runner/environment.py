"""The runner is a guest in the project's container (docs/runtime-bundles.md §5).

At start the runner takes a copy of the environment exactly as the container received it,
then cleans its own process: it removes the variables that steer an interpreter, its run
token and the bundle location its launcher passed. The agent's session gets the copy (plus
only ``CLAUDE_CONFIG_DIR``), so the agent's shell sees the project image's tools and the
project's variables, never the runner's.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

INTERPRETER_VARIABLES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "VIRTUAL_ENV",
    "NODE_OPTIONS",
    "NODE_PATH",
)
"""Removed from the runner's own process only; the agent still gets the project's values."""

RUN_TOKEN = "EXRADIUS_RUN_TOKEN"
"""Given to the container for the runner alone; neither the agent nor MCP servers see it."""

KUBELET_VARIABLES = re.compile(r"^KUBERNETES_(SERVICE_(HOST|PORT|PORT_HTTPS)|PORT(_\d+_(TCP|UDP|SCTP)(_PROTO|_PORT|_ADDR)?)?)$")
"""The API server's address, which the kubelet adds to every container whatever
``enableServiceLinks`` says. Not the project's: the agent does not get them (they give no
access anyway: run pods have no ServiceAccount token)."""

RUNTIME_DIR = "EXRADIUS_RUNTIME_DIR"
"""Set by the bundle's launcher (``bin/exradius-runner``); not part of the container's
environment."""


@dataclass(frozen=True)
class RunnerEnvironment:
    container: dict[str, str]
    """The environment as the container received it, without the run token and the
    kubelet's API server variables: what the agent's session starts from."""
    run_token: str | None
    runtime_dir: Path | None
    """The bundle directory, or ``None`` outside a bundle (tests, development)."""


def isolate(environ: MutableMapping[str, str] | None = None) -> RunnerEnvironment:
    """Take the container's environment and clean the runner's own process (``environ``
    defaults to ``os.environ``). Call once, before anything else reads the environment."""
    environ = os.environ if environ is None else environ
    runtime_dir = environ.pop(RUNTIME_DIR, None)
    run_token = environ.pop(RUN_TOKEN, None)
    for name in [k for k in environ if KUBELET_VARIABLES.match(k)]:
        # From the runner's process too: the SDK starts the CLI with it, overlaid by the copy.
        environ.pop(name)
    container = dict(environ)
    for name in INTERPRETER_VARIABLES:
        environ.pop(name, None)
    return RunnerEnvironment(
        container=container,
        run_token=run_token,
        runtime_dir=Path(runtime_dir) if runtime_dir else None,
    )
