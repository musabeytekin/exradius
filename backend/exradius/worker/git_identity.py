"""The agent's git identity and git config in a run pod (docs/agent-identity.md §3, §4;
docs/run-environment.md §3; docs/kubernetes.md §7.2).

The worker computes the variables the Run spec carries (``git.env``, ``git.setupEnv``); none
of them holds a secret except by reference. The workstation container gets:

- ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` from the agent's identity, ``EXRADIUS_AGENT`` and
  ``EXRADIUS_TASK_ID``;
- ``GIT_CONFIG_GLOBAL`` naming ``<state>/git/config``, which the runner writes at every start
  (``exradius.runner.gitconfig``);
- for each repository with ``push`` and a credential, the credential's token as
  ``EXRADIUS_GIT_CREDENTIAL_<N>`` (from the per-run Secret, key ``push.<N>``) and a credential
  helper for that remote URL that answers from the variable. Nothing is written into the
  clones;
- ``core.hooksPath`` pointing at the bundle's read-only hooks dispatcher.

``core.hooksPath`` and the credential helpers are git command-line config
(``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>``), which ranks above
every config file, so a repository-local value (husky, lefthook, ``git config
core.hooksPath``) cannot replace them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from exradius.git import RepoSpec

STATE_MOUNT = "/exradius/state"
RUNTIME_MOUNT = "/opt/exradius/runtime"
HOOKS_PATH = f"{RUNTIME_MOUNT}/git-hooks"
GLOBAL_CONFIG = f"{STATE_MOUNT}/git/config"
CREDENTIAL_VARIABLE = "EXRADIUS_GIT_CREDENTIAL_{index}"
PUSH_KEY = "push.{index}"
"""The per-run Secret key of a push credential's token."""


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    agent: str
    """The agent's slug."""
    task_id: str


def credential_username(auth: str) -> tuple[str, str]:
    """``user:token`` -> (user, token); a bare token gets ``x-access-token``, as for clones."""
    if ":" in auth:
        user, _, token = auth.partition(":")
        return user, token
    return "x-access-token", auth


@dataclass(frozen=True)
class PushCredential:
    url: str
    username: str
    variable: str
    """``EXRADIUS_GIT_CREDENTIAL_<N>``"""
    secret_key: str
    """``push.<N>``: where the token is in the per-run Secret."""
    token: str


def push_credentials(repos: list[RepoSpec]) -> list[PushCredential]:
    """The repositories runs may push to. N is the repository's index in the project's list.
    Only http(s) remotes can use a credential helper."""
    found = []
    for index, repo in enumerate(repos):
        if not (repo.push_allowed and repo.auth) or urlsplit(repo.url).scheme not in ("http", "https"):
            continue
        username, token = credential_username(repo.auth)
        found.append(PushCredential(repo.url, username, CREDENTIAL_VARIABLE.format(index=index),
                                    PUSH_KEY.format(index=index), token))
    return found


def helper_command(username: str, variable: str) -> str:
    """A shell credential helper that answers ``get`` from the variable (never stores)."""
    return f'!f() {{ test "$1" = get || exit 0; echo "username={username}"; echo "password=${variable}"; }}; f'


SAFE_DIRECTORY = ("safe.directory", "*")
"""For every container: the checkout under /work need not be owned by the run user as git
sees it, and project images carry no system git config of ours."""


def command_line_config(credentials: list[PushCredential]) -> list[tuple[str, str]]:
    """The git config of the workstation at the command-line level, in order."""
    entries = [SAFE_DIRECTORY, ("core.hooksPath", HOOKS_PATH)]
    hosts: list[str] = []
    for credential in credentials:
        parts = urlsplit(credential.url)
        host = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        if host not in hosts:
            hosts.append(host)
    for host in hosts:
        # Ask with the repository path, so two remotes on one host can carry different
        # credentials.
        entries.append((f"credential.{host}.useHttpPath", "true"))
    for credential in credentials:
        # The empty value first drops helpers configured anywhere else (image, global
        # config, the clone's own config): all of them are read before the command line.
        entries += [
            (f"credential.{credential.url}.helper", ""),
            (f"credential.{credential.url}.helper", helper_command(credential.username, credential.variable)),
        ]
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
"""The command-line git config of containers that are not the agent's (setup commands)."""


def identity_environment(identity: Identity) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.name,
        "GIT_COMMITTER_EMAIL": identity.email,
        "GIT_CONFIG_GLOBAL": GLOBAL_CONFIG,
        "EXRADIUS_AGENT": identity.agent,
        "EXRADIUS_TASK_ID": identity.task_id,
    }


def workstation_git(identity: Identity, repos: list[RepoSpec]) -> tuple[list[dict[str, str]], dict[str, str]]:
    """The workstation's git variables as Run spec env entries, after every other variable,
    and the per-run Secret data they reference (push tokens)."""
    credentials = push_credentials(repos)
    entries = [{"name": k, "value": v} for k, v in identity_environment(identity).items()]
    entries += [{"name": c.variable, "secretKey": c.secret_key} for c in credentials]
    entries += [{"name": k, "value": v} for k, v in config_environment(command_line_config(credentials)).items()]
    return entries, {c.secret_key: c.token for c in credentials}


def setup_git() -> list[dict[str, str]]:
    """The git variables of setup containers (and of verify Runs' workstation)."""
    return [{"name": k, "value": v} for k, v in BASE_ENVIRONMENT.items()]
