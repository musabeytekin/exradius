"""Agent identity in run containers (docs/agent-identity.md §3, §4; docs/run-environment.md §3).

At every run container start (first start and each resume) the worker gives the
container:

- ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` from the agent's identity, ``EXRADIUS_AGENT`` and
  ``EXRADIUS_TASK_ID``;
- a global git config in the state mount, so it survives resume. It is not
  ``$HOME/.gitconfig`` (``$HOME`` is the run's home directory, which also persists, but is
  the agent's and its tools' to fill) but ``<state>/git/config``, named by
  ``GIT_CONFIG_GLOBAL``. That file is created once and only
  includes ``<state>/git/exradius.gitconfig``, which the worker rewrites at every start; the
  agent's own ``git config --global`` changes land in the first one and are kept. The
  managed file holds the identity and two conveniences, which a project may change;
- ``<state>/git-hooks/``, holding every standard hook name as a copy of one dispatcher:
  it adds the ``Exradius-Task`` and ``Exradius-Agent`` trailers in ``prepare-commit-msg``,
  then runs the project's own hook of that name (from the hooks path a config file sets,
  else ``.git/hooks``) with the same arguments and stdin, and exits with its status;
- for each repository with ``push_allowed`` and an auth secret, the credential as
  ``EXRADIUS_GIT_CREDENTIAL_<N>`` and a credential helper for that remote URL that answers
  from the variable. Nothing is written into the clones.

``core.hooksPath`` and the credential helpers are git command-line config
(``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>``), which ranks above
every config file, so a repository-local value (husky, lefthook, ``git config
core.hooksPath``) cannot replace them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from exradius.worker.prepare import RepoSpec

GIT_DIR = "git"
HOOKS_DIR = "git-hooks"
GLOBAL_CONFIG = "config"
MANAGED_CONFIG = "exradius.gitconfig"
CREDENTIAL_VARIABLE = "EXRADIUS_GIT_CREDENTIAL_{index}"

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
# Written by the Exradius worker at every container start (docs/run-environment.md §3).
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


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    agent: str
    """The agent's slug."""
    task_id: str


def _quote(value: str) -> str:
    """A git config value or subsection name in double quotes (``;`` and ``#`` start
    comments otherwise)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def credential_username(auth: str) -> tuple[str, str]:
    """``user:token`` -> (user, token); a bare token gets ``x-access-token``, as for mirrors."""
    if ":" in auth:
        user, _, token = auth.partition(":")
        return user, token
    return "x-access-token", auth


def push_credentials(repos: list[RepoSpec]) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Environment variables and (url, username, variable) for the repositories runs may
    push to. N is the repository's index in the project's list (sorted by name). Only
    http(s) remotes can use a credential helper."""
    env: dict[str, str] = {}
    helpers: list[tuple[str, str, str]] = []
    for index, repo in enumerate(repos):
        if not (repo.push_allowed and repo.auth) or urlsplit(repo.url).scheme not in ("http", "https"):
            continue
        username, token = credential_username(repo.auth)
        variable = CREDENTIAL_VARIABLE.format(index=index)
        env[variable] = token
        helpers.append((repo.url, username, variable))
    return env, helpers


def helper_command(username: str, variable: str) -> str:
    """A shell credential helper that answers ``get`` from the variable (never stores)."""
    return f'!f() {{ test "$1" = get || exit 0; echo "username={username}"; echo "password=${variable}"; }}; f'


def managed_config(identity: Identity) -> str:
    lines = [
        "# Written by the Exradius worker at every container start; edits here are lost.",
        "# Use `git config --global` instead: that writes the including file, which is kept.",
        "# The hooks path and push credentials are git command-line config (GIT_CONFIG_COUNT).",
        "[user]",
        f"\tname = {_quote(identity.name)}",
        f"\temail = {_quote(identity.email)}",
        "[push]",
        "\tautoSetupRemote = true",
        "[init]",
        "\tdefaultBranch = main",
    ]
    return "\n".join(lines) + "\n"


SAFE_DIRECTORY = ("safe.directory", "*")
"""For every container: the checkout under /work need not be owned by the run user as git
sees it (bind mounts from Docker Desktop hosts), and project images carry no system git
config of ours."""


def command_line_config(state_mount: str, helpers: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The git config of a run container at the command-line level, in order."""
    entries = [SAFE_DIRECTORY, ("core.hooksPath", f"{state_mount}/{HOOKS_DIR}")]
    hosts: list[str] = []
    for url, _, _ in helpers:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        if host not in hosts:
            hosts.append(host)
    for host in hosts:
        # Ask with the repository path, so two remotes on one host can carry different
        # credentials.
        entries.append((f"credential.{host}.useHttpPath", "true"))
    for url, username, variable in helpers:
        # The empty value first drops helpers configured anywhere else (image, global
        # config, the clone's own config): all of them are read before the command line.
        entries += [(f"credential.{url}.helper", ""), (f"credential.{url}.helper", helper_command(username, variable))]
    return entries


GIT_CONFIG_VARIABLES = re.compile(r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+|PARAMETERS)$")


def config_environment(entries: list[tuple[str, str]]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def without_command_line_config(env: dict[str, str]) -> dict[str, str]:
    """``env`` less any command-line git config in it: a project's ``env`` cannot replace
    Exradius's."""
    return {k: v for k, v in env.items() if not GIT_CONFIG_VARIABLES.match(k)}


BASE_ENVIRONMENT = config_environment([SAFE_DIRECTORY])
"""The command-line git config of containers that are not the agent's (setup commands,
the delivery check)."""


def global_config() -> str:
    return (
        "# Global git config of this run (GIT_CONFIG_GLOBAL). Kept across resumes.\n"
        "[include]\n"
        f"\tpath = {MANAGED_CONFIG}\n"
    )


def container_environment(identity: Identity, state_mount: str) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.name,
        "GIT_COMMITTER_EMAIL": identity.email,
        "GIT_CONFIG_GLOBAL": f"{state_mount}/{GIT_DIR}/{GLOBAL_CONFIG}",
        "EXRADIUS_AGENT": identity.agent,
        "EXRADIUS_TASK_ID": identity.task_id,
    }


def prepare_git(
    state_dir: Path, identity: Identity, repos: list[RepoSpec], *, state_mount: str, owner: tuple[int, int] | None = None
) -> dict[str, str]:
    """Write the git config and hooks into ``state_dir`` (the host side of the state mount)
    and return the variables the container gets after every other: the identity, the
    credentials and the command-line git config."""
    credentials, helpers = push_credentials(repos)
    git_dir, hooks_dir = state_dir / GIT_DIR, state_dir / HOOKS_DIR
    git_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / MANAGED_CONFIG).write_text(managed_config(identity))
    (git_dir / MANAGED_CONFIG).chmod(0o644)
    if not (git_dir / GLOBAL_CONFIG).exists():
        (git_dir / GLOBAL_CONFIG).write_text(global_config())
    for name in HOOK_NAMES:
        hook = hooks_dir / name
        hook.write_text(DISPATCHER)
        hook.chmod(0o755)
    if owner is not None and os.geteuid() == 0:
        uid, gid = owner
        for path in (git_dir, hooks_dir, git_dir / GLOBAL_CONFIG):
            os.chown(path, uid, gid)  # the run user may edit its global config
    return {
        **container_environment(identity, state_mount),
        **credentials,
        **config_environment(command_line_config(state_mount, helpers)),
    }
