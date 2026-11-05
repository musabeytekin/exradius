"""Run directory preparation (architecture §5.1) and cleanup (§5.5).

Layout under the data directory::

    runs/<run_id>/project/                      container working directory (/work)
    runs/<run_id>/project/.exradius/attachments read-only attachments
    runs/<run_id>/state/                        runtime state kept across suspend/resume
    mirrors/<project>/<repo>.git                per-project bare mirrors (shared, kept)

Everything here is synchronous; the worker calls it through ``asyncio.to_thread``.
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

ATTACHMENTS_REL = Path(".exradius") / "attachments"


class PreparationError(Exception):
    pass


@dataclass(frozen=True)
class RunPaths:
    root: Path
    project: Path
    state: Path
    attachments: Path


@dataclass(frozen=True)
class RepoSpec:
    name: str
    url: str
    branch: str
    auth: str | None = None
    """Secret value for HTTPS auth: ``user:password`` or a bare token."""


def run_paths(data_dir: Path, run_id: str) -> RunPaths:
    root = data_dir / "runs" / run_id
    project = root / "project"
    return RunPaths(root=root, project=project, state=root / "state", attachments=project / ATTACHMENTS_REL)


def create_run_directory(data_dir: Path, run_id: str) -> RunPaths:
    paths = run_paths(data_dir, run_id)
    paths.attachments.mkdir(parents=True, exist_ok=True)
    paths.state.mkdir(parents=True, exist_ok=True)
    return paths


def mirror_path(data_dir: Path, project_slug: str, repo_name: str) -> Path:
    return data_dir / "mirrors" / project_slug / f"{repo_name}.git"


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}


def _auth_args(auth: str | None) -> list[str]:
    if not auth:
        return []
    credentials = auth if ":" in auth else f"x-access-token:{auth}"
    header = "Authorization: Basic " + base64.b64encode(credentials.encode()).decode()
    return ["-c", f"http.extraHeader={header}"]


_URL_USERINFO = re.compile(r"(\w+://)[^/@\s]+@")


def redact(text: str, auth: str | None = None) -> str:
    """Remove credentials from git output: the secret, its HTTP Basic form and any
    ``user:password@`` in URLs."""
    if auth:
        credentials = auth if ":" in auth else f"x-access-token:{auth}"
        for secret in (auth, base64.b64encode(credentials.encode()).decode(), credentials):
            text = text.replace(secret, "***")
    return _URL_USERINFO.sub(r"\1***@", text)


def git(*args: str, cwd: Path | None = None, auth: str | None = None, timeout: float | None = None) -> str:
    cmd = ["git", *_auth_args(auth), *args]
    try:
        result = subprocess.run(
            cmd, cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise PreparationError(redact(f"git {' '.join(args)} timed out after {timeout:.0f}s", auth)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PreparationError(redact(f"git {' '.join(args)} failed: {detail}", auth))
    return result.stdout


@dataclass(frozen=True)
class RemoteRefs:
    branches: list[str]
    default_branch: str | None


def ls_remote(url: str, auth: str | None = None, timeout: float = 30) -> RemoteRefs:
    """Branches and default branch (HEAD) of a remote, without cloning it."""
    output = git("ls-remote", "--symref", url, "HEAD", "refs/heads/*", auth=auth, timeout=timeout)
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
    """Serialise mirror updates across concurrent runs and worker processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh_mirror(data_dir: Path, project_slug: str, repo: RepoSpec) -> Path:
    mirror = mirror_path(data_dir, project_slug, repo.name)
    with _locked(mirror):
        if (mirror / "HEAD").exists():
            git("remote", "set-url", "origin", repo.url, cwd=mirror)
            git("fetch", "--prune", "--force", "origin", cwd=mirror, auth=repo.auth)
        else:
            if mirror.exists():
                shutil.rmtree(mirror)
            git("clone", "--mirror", repo.url, str(mirror), auth=repo.auth)
    return mirror


def clone_from_mirror(mirror: Path, dest: Path, repo: RepoSpec) -> None:
    """``git clone --local`` (hardlinked objects) and point ``origin`` at the real remote."""
    git("clone", "--local", "--branch", repo.branch, str(mirror), str(dest))
    git("remote", "set-url", "origin", repo.url, cwd=dest)


def checkout_repositories(
    data_dir: Path, project_slug: str, paths: RunPaths, repos: list[RepoSpec]
) -> None:
    for repo in repos:
        mirror = refresh_mirror(data_dir, project_slug, repo)
        clone_from_mirror(mirror, paths.project / repo.name, repo)


def write_attachments(directory: Path, files: list[tuple[str, bytes]]) -> list[Path]:
    """Write files and make them read-only. The worker also mounts the directory read-only."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in files:
        if "/" in name or name in {".", ".."}:
            raise PreparationError(f"invalid attachment name {name!r}")
        target = directory / name
        target.write_bytes(data)
        target.chmod(0o444)
        written.append(target)
    return written


def seal_read_only(directory: Path) -> None:
    for path in [directory, *directory.rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)


def chown_tree(path: Path, uid: int, gid: int, skip: Path | None = None) -> None:
    """Hand the tree to the container user. Only possible (and needed) when running as root."""
    if os.geteuid() != 0:
        return
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        if skip is not None and root_path.is_relative_to(skip):
            dirs[:] = []
            continue
        os.lchown(root_path, uid, gid)
        for name in files:
            os.lchown(root_path / name, uid, gid)


def remove_run_directory(paths: RunPaths) -> None:
    if not paths.root.exists():
        return

    def _make_writable(func, target, _exc):  # read-only attachment dirs
        parent = Path(target).parent
        parent.chmod(0o755)
        Path(target).chmod(0o755)
        func(target)

    shutil.rmtree(paths.root, onexc=_make_writable)
