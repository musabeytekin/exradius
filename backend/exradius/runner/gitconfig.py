"""Git config files and hooks of a run (docs/agent-identity.md §3, docs/run-environment.md §3).

The hooks dispatcher is part of the runtime bundle: the bundle build writes every standard
hook name as a copy of ``DISPATCHER`` into ``/opt/exradius/runtime/git-hooks``, read-only in
the pod, and ``core.hooksPath`` points there at the git command-line level (the Run spec's
``git.env``). The runner writes the global git config into the state directory at every
start: ``<state>/git/config`` (``GIT_CONFIG_GLOBAL``) is created once and only includes
``<state>/git/exradius.gitconfig``, which is rewritten each time with the agent's identity and
two conveniences; the agent's own ``git config --global`` changes land in the first file and
are kept.
"""

from __future__ import annotations

import sys
from pathlib import Path

GIT_DIR = "git"
GLOBAL_CONFIG = "config"
MANAGED_CONFIG = "exradius.gitconfig"

HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
    "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge",
    "pre-push", "pre-receive", "update", "proc-receive", "post-receive", "post-update",
    "reference-transaction", "pre-auto-gc", "post-rewrite", "sendemail-validate",
    "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist", "p4-post-changelist",
    "p4-pre-submit", "post-index-change",
)
"""githooks(5), less ``push-to-checkout``: its mere presence replaces git's built-in
``updateInstead`` behaviour, so a dispatcher for it would change what a push does."""

DISPATCHER = """\
#!/bin/sh
# Part of the Exradius runtime bundle, read-only at /opt/exradius/runtime/git-hooks
# (docs/run-environment.md §3, docs/kubernetes.md §7.2).
# core.hooksPath points here at the git command-line level. Every hook name is this one
# script: it does Exradius's part (prepare-commit-msg: the trailers that link the commit to
# its task), then runs the project's own hook of the same name and exits with its status.
name=$(basename "$0")
here=$(cd "$(dirname "$0")" && pwd -P)

if [ "$name" = prepare-commit-msg ] && [ -n "$1" ]; then
  # Unless the message already has them (amend, rebase, cherry-pick).
  [ -n "$EXRADIUS_TASK_ID" ] && git interpret-trailers --in-place --if-exists doNothing \\
    --trailer "Exradius-Task: $EXRADIUS_TASK_ID" "$1" </dev/null
  [ -n "$EXRADIUS_AGENT" ] && git interpret-trailers --in-place --if-exists doNothing \\
    --trailer "Exradius-Agent: $EXRADIUS_AGENT" "$1" </dev/null
fi

# The hooks path git would use without Exradius: the last value from a config file (system,
# global, local, worktree), not from the command line; else the repository's hooks folder.
# Not `git rev-parse --git-path hooks`: that answers with the forced path.
dir=
while IFS= read -r line; do
  case "$line" in
    "command\t"*) ;;
    *"\t"*) dir=${line#*\t} ;;
  esac
done <<CONFIG
$(git config --show-scope --type=path --get-all core.hooksPath </dev/null 2>/dev/null)
CONFIG
if [ -z "$dir" ]; then
  common=$(git rev-parse --git-common-dir </dev/null 2>/dev/null) || exit 0
  dir="$common/hooks"
fi
hook="$dir/$name"
if [ -f "$hook" ] && [ -x "$hook" ]; then
  [ "$(cd "$dir" 2>/dev/null && pwd -P)" = "$here" ] && exit 0
  exec "$hook" "$@"
fi
exit 0
"""


def _quote(value: str) -> str:
    """A git config value in double quotes (``;`` and ``#`` start comments otherwise)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def managed_config(name: str, email: str) -> str:
    lines = [
        "# Written by the Exradius runner at every start; edits here are lost.",
        "# Use `git config --global` instead: that writes the including file, which is kept.",
        "# The hooks path and push credentials are git command-line config (GIT_CONFIG_COUNT).",
        "[user]",
        f"\tname = {_quote(name)}",
        f"\temail = {_quote(email)}",
        "[push]",
        "\tautoSetupRemote = true",
        "[init]",
        "\tdefaultBranch = main",
    ]
    return "\n".join(lines) + "\n"


def global_config() -> str:
    return (
        "# Global git config of this run (GIT_CONFIG_GLOBAL). Kept across resumes.\n"
        "[include]\n"
        f"\tpath = {MANAGED_CONFIG}\n"
    )


def write_git_config(state_dir: Path, name: str, email: str) -> None:
    """The run's global git config in ``<state>/git``; the including file is kept."""
    git_dir = state_dir / GIT_DIR
    git_dir.mkdir(parents=True, exist_ok=True)
    managed = git_dir / MANAGED_CONFIG
    managed.write_text(managed_config(name, email))
    managed.chmod(0o644)
    if not (git_dir / GLOBAL_CONFIG).exists():
        (git_dir / GLOBAL_CONFIG).write_text(global_config())


def write_hooks(directory: Path) -> None:
    """Every hook name as a copy of the dispatcher (the bundle build calls this)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        hook = directory / name
        hook.write_text(DISPATCHER)
        hook.chmod(0o755)


if __name__ == "__main__":  # python -m exradius.runner.gitconfig <directory>
    write_hooks(Path(sys.argv[1]))
