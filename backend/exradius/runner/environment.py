"""The runner is a guest in the project's container (docs/runtime-bundles.md §5).

At start the runner takes a copy of the environment exactly as the container received it,
then cleans its own process: it removes the variables that steer an interpreter, its run
token and the bundle location its launcher passed. The agent's session gets the copy (plus
only ``CLAUDE_CONFIG_DIR``), so the agent's shell sees the project image's tools and the
project's variables, never the runner's.

The run token is a file, never a variable of the pod (docs/kubernetes.md §7.8): in the
workstation it is the copy ``prepare`` handed over, which the runner deletes once read, so
the agent (the same user) cannot read it afterwards; elsewhere it is the Secret's read-only
volume, where no project code runs.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass, field
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
"""The token as a variable: tests and development only. Neither the agent nor MCP servers
see it."""

RUN_TOKEN_FILE = "EXRADIUS_RUN_TOKEN_FILE"
"""Where the token is: read, then deleted when it can be (the workstation's handoff)."""

KUBELET_VARIABLES = re.compile(r"^KUBERNETES_(SERVICE_(HOST|PORT|PORT_HTTPS)|PORT(_\d+_(TCP|UDP|SCTP)(_PROTO|_PORT|_ADDR)?)?)$")
"""The API server's address, which the kubelet adds to every container whatever
``enableServiceLinks`` says. Not the project's: the agent does not get them (they give no
access anyway: run pods have no ServiceAccount token)."""

RUNTIME_DIR = "EXRADIUS_RUNTIME_DIR"
"""Set by the bundle's launcher (``bin/exradius-runner``); not part of the container's
environment."""

EGRESS_VARIABLES = ("EXRADIUS_EGRESS_URL", "EXRADIUS_EGRESS_PEERS")
"""The egress proxy's reverse URL and the name of its pods, for the runner's clones and its
wait for the agent phase (``exradius.runner.egress``); the agent's tools use the proxy
through ``HTTPS_PROXY`` and their git config instead."""


@dataclass(frozen=True)
class RunnerEnvironment:
    container: dict[str, str]
    """The environment as the container received it, without the run token and the
    kubelet's API server variables: what the agent's session starts from."""
    run_token: str | None
    runtime_dir: Path | None
    """The bundle directory, or ``None`` outside a bundle (tests, development)."""
    egress: dict[str, str] = field(default_factory=dict)
    """``EGRESS_VARIABLES`` as the container got them."""


def isolate(environ: MutableMapping[str, str] | None = None) -> RunnerEnvironment:
    """Take the container's environment and clean the runner's own process (``environ``
    defaults to ``os.environ``). Call once, before anything else reads the environment."""
    environ = os.environ if environ is None else environ
    runtime_dir = environ.pop(RUNTIME_DIR, None)
    run_token = environ.pop(RUN_TOKEN, None)
    token_file = environ.pop(RUN_TOKEN_FILE, None)
    if token_file:
        run_token = read_token(Path(token_file)) or run_token
    egress = {name: environ.pop(name) for name in EGRESS_VARIABLES if name in environ}
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
        egress=egress,
    )


PR_SET_DUMPABLE = 4


def protect_process() -> bool:
    """Make the runner's process non-dumpable (``PR_SET_DUMPABLE`` 0): another process of
    the same user, such as the agent's shell, can then no longer read its memory or
    ``/proc/<pid>/environ`` or attach to it, so the token the runner holds stays its own.
    Programs the runner starts are dumpable again once they exec. Linux only; returns
    whether it took effect."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def read_token(path: Path) -> str | None:
    """The run token in ``path``, which is then deleted: the handoff in the workstation is
    the only copy the agent's user could open. The Secret's volume is read-only, and stays."""
    try:
        token = path.read_text().strip() or None
    except OSError:
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    return token
