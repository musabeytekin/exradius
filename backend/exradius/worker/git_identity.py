"""Agent identity in run containers (docs/agent-identity.md §3, §4).

At every run container start (first start and each resume) the worker gives the
container:

- ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` from the agent's identity, ``EXRADIUS_AGENT`` and
  ``EXRADIUS_TASK_ID``;
- a global git config in the state mount, so it survives resume. It is not
  ``$HOME/.gitconfig`` (``$HOME`` is the run's home directory, which also persists, but is
  the agent's and its tools' to fill) but ``<state>/git/config``, named by
  ``GIT_CONFIG_GLOBAL``. That file is created once and only
  includes ``<state>/git/exradius.gitconfig``, which the worker rewrites at every start; the
  agent's own ``git config --global`` changes land in the first one and are kept;
- ``<state>/git-hooks/prepare-commit-msg``, which adds the ``Exradius-Task`` and
  ``Exradius-Agent`` trailers (``core.hooksPath`` points there, so a repository's own hooks
  do not run);
- for each repository with ``push_allowed`` and an auth secret, the credential as
  ``EXRADIUS_GIT_CREDENTIAL_<N>`` and a credential helper for that remote URL that answers
  from the variable. Nothing is written into the clones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from exradius.worker.prepare import RepoSpec

GIT_DIR = "git"
HOOKS_DIR = "git-hooks"
GLOBAL_CONFIG = "config"
MANAGED_CONFIG = "exradius.gitconfig"
CREDENTIAL_VARIABLE = "EXRADIUS_GIT_CREDENTIAL_{index}"

PREPARE_COMMIT_MSG = """\
#!/bin/sh
# Written by the Exradius worker: links every commit back to its task.
# Adds the trailers unless the message already has them (amend, rebase, cherry-pick).
msg_file="$1"
[ -n "$msg_file" ] || exit 0
[ -n "$EXRADIUS_TASK_ID" ] && git interpret-trailers --in-place --if-exists doNothing \\
  --trailer "Exradius-Task: $EXRADIUS_TASK_ID" "$msg_file"
[ -n "$EXRADIUS_AGENT" ] && git interpret-trailers --in-place --if-exists doNothing \\
  --trailer "Exradius-Agent: $EXRADIUS_AGENT" "$msg_file"
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


def managed_config(identity: Identity, state_mount: str, helpers: list[tuple[str, str, str]]) -> str:
    lines = [
        "# Written by the Exradius worker at every container start; edits here are lost.",
        "# Use `git config --global` instead: that writes the including file, which is kept.",
        "[user]",
        f"\tname = {_quote(identity.name)}",
        f"\temail = {_quote(identity.email)}",
        "[core]",
        f"\thooksPath = {_quote(f'{state_mount}/{HOOKS_DIR}')}",
        "[push]",
        "\tautoSetupRemote = true",
        "[init]",
        "\tdefaultBranch = main",
    ]
    hosts = []
    for url, _, _ in helpers:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        if host not in hosts:
            hosts.append(host)
    for host in hosts:
        # Ask with the repository path, so two remotes on one host can carry different
        # credentials.
        lines += [f"[credential {_quote(host)}]", "\tuseHttpPath = true"]
    for url, username, variable in helpers:
        lines += [
            f"[credential {_quote(url)}]",
            "\thelper =",  # an empty value drops helpers configured elsewhere (image, system)
            f"\thelper = {_quote(helper_command(username, variable))}",
        ]
    return "\n".join(lines) + "\n"


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
    """Write the git config and hook into ``state_dir`` (the host side of the state mount)
    and return the variables to add to the container's environment."""
    credentials, helpers = push_credentials(repos)
    git_dir, hooks_dir = state_dir / GIT_DIR, state_dir / HOOKS_DIR
    git_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / MANAGED_CONFIG).write_text(managed_config(identity, state_mount, helpers))
    (git_dir / MANAGED_CONFIG).chmod(0o644)
    if not (git_dir / GLOBAL_CONFIG).exists():
        (git_dir / GLOBAL_CONFIG).write_text(global_config())
    hook = hooks_dir / "prepare-commit-msg"
    hook.write_text(PREPARE_COMMIT_MSG)
    hook.chmod(0o755)
    if owner is not None and os.geteuid() == 0:
        uid, gid = owner
        for path in (git_dir, hooks_dir, git_dir / GLOBAL_CONFIG):
            os.chown(path, uid, gid)  # the run user may edit its global config
    return {**container_environment(identity, state_mount), **credentials}
