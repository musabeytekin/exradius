"""Git command helpers shared by the api and worker (config-repository mirrors) and by the
runner in a run pod (clones, docs/kubernetes.md §7.5).

Everything here is synchronous; async callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import base64
import fcntl
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    """A git command failed; the message has its output with credentials removed."""


@dataclass(frozen=True)
class RepoSpec:
    name: str
    """The folder under ``/work``."""
    url: str
    branch: str | None
    """``None``: the remote's default branch."""
    auth: str | None = None
    """Secret value for HTTPS auth: ``user:password`` or a bare token."""
    push_allowed: bool = False
    """Runs get a credential helper for this remote (docs/agent-identity.md §4)."""


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}


def basic_credentials(auth: str) -> str:
    """``user:password`` as given, or ``x-access-token:<token>`` for a bare token."""
    return auth if ":" in auth else f"x-access-token:{auth}"


def auth_args(auth: str | None) -> list[str]:
    """``-c http.extraHeader=...`` for one command: the credential never reaches a config file."""
    if not auth:
        return []
    header = "Authorization: Basic " + base64.b64encode(basic_credentials(auth).encode()).decode()
    return ["-c", f"http.extraHeader={header}"]


_URL_USERINFO = re.compile(r"(\w+://)[^/@\s]+@")


def redact(text: str, auth: str | None = None) -> str:
    """Remove credentials from git output: the secret, its HTTP Basic form and any
    ``user:password@`` in URLs."""
    if auth:
        credentials = basic_credentials(auth)
        for secret in (auth, base64.b64encode(credentials.encode()).decode(), credentials):
            text = text.replace(secret, "***")
    return _URL_USERINFO.sub(r"\1***@", text)


def git(*args: str, cwd: Path | None = None, auth: str | None = None, timeout: float | None = None) -> str:
    cmd = ["git", *auth_args(auth), *args]
    try:
        result = subprocess.run(cmd, cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(redact(f"git {' '.join(args)} timed out after {timeout:.0f}s", auth)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(redact(f"git {' '.join(args)} failed: {detail}", auth))
    return result.stdout


@dataclass(frozen=True)
class RemoteRefs:
    branches: list[str]
    default_branch: str | None


def ls_remote(url: str, auth: str | None = None, timeout: float = 30, cwd: Path | None = None) -> RemoteRefs:
    """Branches and default branch (HEAD) of a remote, without cloning it."""
    output = git("ls-remote", "--symref", url, "HEAD", "refs/heads/*", auth=auth, timeout=timeout, cwd=cwd)
    branches, default = [], None
    for line in output.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            target = line.removeprefix("ref: ").split("\t")[0]
            default = target.removeprefix("refs/heads/")
        elif "\trefs/heads/" in line:
            branches.append(line.split("\trefs/heads/", 1)[1])
    return RemoteRefs(branches=sorted(branches), default_branch=default)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise mirror updates across concurrent readers in one pod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sync_mirror(mirror: Path, url: str, auth: str | None, timeout: float | None = None) -> Path:
    """Create or fetch a bare mirror of ``url``, serialised by a file lock next to it."""
    with _locked(mirror):
        if (mirror / "HEAD").exists():
            git("remote", "set-url", "origin", url, cwd=mirror)
            git("fetch", "--prune", "--force", "origin", cwd=mirror, auth=auth, timeout=timeout)
        else:
            if mirror.exists():
                shutil.rmtree(mirror)
            git("clone", "--mirror", url, str(mirror), auth=auth, timeout=timeout)
        _follow_remote_head(mirror, auth, timeout)
    return mirror


def _follow_remote_head(mirror: Path, auth: str | None, timeout: float | None) -> None:
    """Point the mirror's HEAD at the remote's current default branch (a fetch leaves it
    where the first clone put it); repositories without a branch check it out."""
    try:
        refs = ls_remote("origin", auth, timeout or 30, cwd=mirror)
    except GitError:
        return
    if refs.default_branch and refs.default_branch in refs.branches:
        git("symbolic-ref", "HEAD", f"refs/heads/{refs.default_branch}", cwd=mirror)


def clone(url: str, dest: Path, branch: str | None, auth: str | None, timeout: float | None = None) -> None:
    """Clone ``url`` into ``dest`` (the remote's default branch without ``branch``). The
    credential is used for this command only; ``origin`` keeps the plain URL."""
    git("clone", *(["--branch", branch] if branch else []), url, str(dest), auth=auth, timeout=timeout)
    git("remote", "set-url", "origin", url, cwd=dest)
