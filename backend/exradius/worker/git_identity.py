"""The agent's git identity and git config in a run pod (docs/agent-identity.md §3, §4;
docs/run-environment.md §3; docs/kubernetes.md §7.2, §11).

The worker computes the variables the Run spec carries (``git.env``, ``git.setupEnv``); none
of them is a secret. The workstation container gets:

- ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` from the agent's identity, ``EXRADIUS_AGENT`` and
  ``EXRADIUS_TASK_ID``;
- ``GIT_CONFIG_GLOBAL`` naming ``<state>/git/config``, which the runner writes at every start
  (``exradius.runner.gitconfig``);
- ``core.hooksPath`` pointing at the bundle's read-only hooks dispatcher;
- for each http(s) repository, ``url.<proxy route>.insteadOf=<url>``: fetches and pushes go
  to the egress proxy, which adds the repository's credential and refuses a push to a
  repository without ``push: true``. No credential is in the pod.

All of it except the identity is git command-line config (``GIT_CONFIG_COUNT``/
``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>``), which ranks above every config file, so a
repository-local value (husky, lefthook, ``git config core.hooksPath``) cannot replace it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from exradius.git import RepoSpec, proxy_route

STATE_MOUNT = "/exradius/state"
RUNTIME_MOUNT = "/opt/exradius/runtime"
HOOKS_PATH = f"{RUNTIME_MOUNT}/git-hooks"
GLOBAL_CONFIG = f"{STATE_MOUNT}/git/config"


@dataclass(frozen=True)
class Identity:
    name: str
    email: str
    agent: str
    """The agent's slug."""
    task_id: str


SAFE_DIRECTORY = ("safe.directory", "*")
"""For every container: the checkout under /work need not be owned by the run user as git
sees it, and project images carry no system git config of ours."""


def command_line_config(repos: list[RepoSpec], egress_url: str | None) -> list[tuple[str, str]]:
    """The git config of the workstation at the command-line level, in order."""
    entries = [SAFE_DIRECTORY, ("core.hooksPath", HOOKS_PATH)]
    entries += proxy_rewrites(repos, egress_url)
    return entries


def proxy_rewrites(repos: list[RepoSpec], egress_url: str | None) -> list[tuple[str, str]]:
    """``url.<route>.insteadOf`` for each http(s) repository: git sends its requests to the
    egress proxy's git route, whatever remote name or command the agent uses."""
    if not egress_url:
        return []
    entries = []
    for repo in repos:
        route = proxy_route(egress_url, repo.url)
        if route:
            entries.append((f"url.{route}.insteadOf", repo.url))
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


def workstation_git(identity: Identity, repos: list[RepoSpec], egress_url: str | None) -> list[dict[str, str]]:
    """The workstation's git variables as Run spec env entries, after every other variable."""
    entries = [{"name": k, "value": v} for k, v in identity_environment(identity).items()]
    entries += [{"name": k, "value": v} for k, v in config_environment(command_line_config(repos, egress_url)).items()]
    return entries


def setup_git() -> list[dict[str, str]]:
    """The git variables of setup containers (and of verify Runs' workstation)."""
    return [{"name": k, "value": v} for k, v in BASE_ENVIRONMENT.items()]
