"""Report-only delivery check (docs/environment-and-services.md §4, docs/kubernetes.md §8.5).

After the session ends (completed, failed or cancelled) the runner inspects the clones under
``/work`` in a separate process, as the run user, with an empty environment, and sends the
warnings with its outcome. It changes nothing in the clones and pushes nothing. It runs in
the pod, never in the worker: a clone's ``.git/config`` can run commands, and in the pod it
has no more rights than the agent. A runner killed by its memory limit sends none.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT = 15.0
"""Seconds; it must fit the pod's termination grace period (30 s) on cancel."""

INSPECT_CLONES_SCRIPT = r"""
for d in */; do
  name=${d%/}
  [ -e "$name/.git" ] || continue
  (
    cd "$name" || exit 0
    n=$(git --no-optional-locks status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    printf 'dirty	%s	%s
' "$name" "$n"
    git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null | while read -r b; do
      c=$(git rev-list --count "refs/heads/$b" --not --remotes 2>/dev/null)
      printf 'unpushed	%s	%s	%s
' "$name" "$b" "$c"
    done
    if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
      c=$(git rev-list --count HEAD --not --remotes --branches 2>/dev/null)
      printf 'unpushed	%s		%s
' "$name" "$c"
    fi
  )
done
"""
"""Per clone, the number of uncommitted paths and, per local branch, the commits no
remote-tracking ref contains."""


def _environment(home: str) -> dict[str, str]:
    """Nothing of the run's: a ``PATH`` to find git, an empty home, no system config."""
    return {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }


async def check(work_dir: Path, timeout: float = TIMEOUT) -> list[str]:
    """The warnings for the clones in ``work_dir``; none when the check fails."""
    if not work_dir.is_dir() or not any((p / ".git").exists() for p in work_dir.iterdir() if p.is_dir()):
        return []
    try:
        with tempfile.TemporaryDirectory() as home:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", INSPECT_CLONES_SCRIPT, cwd=str(work_dir), env=_environment(home),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                process.kill()
                log.warning("the delivery check did not finish within %.0fs", timeout)
                return []
    except Exception:  # noqa: BLE001 - never block the outcome
        log.exception("the delivery check failed")
        return []
    warnings = delivery_warnings(stdout.decode(errors="replace"))
    if process.returncode != 0 and not warnings:
        log.warning("the delivery check exited with %s: %s", process.returncode, stderr.decode(errors="replace")[-500:])
    return warnings


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def delivery_warnings(output: str) -> list[str]:
    """Warnings from the check's ``dirty`` and ``unpushed`` lines, in the order printed."""
    warnings: list[str] = []
    for line in output.splitlines():
        fields = line.rstrip("\r").split("\t")
        try:
            if fields[0] == "dirty" and len(fields) == 3:
                repo, count = fields[1], int(fields[2] or 0)
                if count:
                    warnings.append(
                        f"{repo}: uncommitted changes in {count} {_plural(count, 'file', 'files')} "
                        "were discarded with the task directory"
                    )
            elif fields[0] == "unpushed" and len(fields) == 4:
                repo, branch, count = fields[1], fields[2], int(fields[3] or 0)
                if count:
                    where = f"on {branch}" if branch else "on a detached HEAD"
                    verb = _plural(count, "was", "were")
                    warnings.append(
                        f"{repo}: {count} {_plural(count, 'commit', 'commits')} {where} {verb} not pushed "
                        f"and {verb} discarded with the task directory"
                    )
        except ValueError:
            continue
    return warnings
