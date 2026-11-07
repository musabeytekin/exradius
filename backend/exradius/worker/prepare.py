"""Run directory preparation (architecture §5.1) and cleanup (§5.5).

Layout under the data directory::

    runs/<run_id>/project/                      container working directory (/work)
    runs/<run_id>/project/.exradius/attachments read-only attachments
    runs/<run_id>/state/                        runtime state kept across suspend/resume
    runs/<run_id>/home/                         the run user's home (/home/runner): caches, build output
    runs/<run_id>/tmp/                          the run's /tmp, on disk for the same reason
    mirrors/<project>/<repo>.git                per-project bare mirrors (shared, kept)

Everything here is synchronous; the worker calls it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import base64
import fcntl
import os
import re
import shutil
import stat
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
    home: Path
    """Mounted at ``/home/runner`` in setup, run and resume containers. On disk, not a tmpfs:
    package manager caches there reach hundreds of MiB per run (§5.1)."""
    tmp: Path
    """Mounted at ``/tmp`` in the same containers, mode 1777: Yarn Berry stages every package
    archive there before copying it into its cache, as much again as the cache itself."""

    @property
    def etc(self) -> Path:
        """Generated ``passwd`` and ``group`` for the run's containers (not mounted as a whole)."""
        return self.root / "etc"


@dataclass(frozen=True)
class RepoSpec:
    name: str
    url: str
    branch: str | None
    """``None``: the remote's default branch."""
    auth: str | None = None
    """Secret value for HTTPS auth: ``user:password`` or a bare token."""
    push_allowed: bool = False
    """Runs get a credential helper for this remote (docs/agent-identity.md §4)."""


def run_paths(data_dir: Path, run_id: str) -> RunPaths:
    root = data_dir / "runs" / run_id
    project = root / "project"
    return RunPaths(
        root=root,
        project=project,
        state=root / "state",
        attachments=project / ATTACHMENTS_REL,
        home=root / "home",
        tmp=root / "tmp",
    )


def create_run_directory(data_dir: Path, run_id: str) -> RunPaths:
    paths = run_paths(data_dir, run_id)
    paths.attachments.mkdir(parents=True, exist_ok=True)
    paths.state.mkdir(parents=True, exist_ok=True)
    _make_user_dirs(paths)
    return paths


USER_DIR_MODES = {"home": 0o755, "tmp": 0o1777}
"""The run user's directories on disk and their modes (owner: the run user, see ``chown_tree``)."""


def _make_user_dirs(paths: RunPaths) -> None:
    for name, mode in USER_DIR_MODES.items():
        directory: Path = getattr(paths, name)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(mode)  # explicit: mkdir applies the umask and never sets the sticky bit


def ensure_user_dirs(paths: RunPaths, uid: int, gid: int) -> None:
    """The run's ``home`` and ``tmp``, created for the run user if missing: a run directory
    made before they were on disk has neither, and its resume must not fail on the bind
    mounts. Existing ones are left as they are."""
    for directory in (paths.home, paths.tmp):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(USER_DIR_MODES[directory.name])
            chown_tree(directory, uid, gid)


def identity_files(uid: int, gid: int, home: str) -> tuple[str, str]:
    """``/etc/passwd`` and ``/etc/group`` for run containers: ``root`` and ``runner`` with
    the run uid/gid and ``home``, whatever users the project image defines."""
    passwd = ["root:x:0:0:root:/root:/bin/sh"]
    group = ["root:x:0:"]
    if uid != 0:
        passwd.append(f"runner:x:{uid}:{gid}::{home}:/bin/bash")
    else:
        passwd[0] = f"root:x:0:{gid}:root:{home}:/bin/bash"
    if gid != 0:
        group.append(f"runner:x:{gid}:")
    return "\n".join(passwd) + "\n", "\n".join(group) + "\n"


def write_identity_files(paths: RunPaths, uid: int, gid: int, home: str) -> tuple[Path, Path]:
    """Write them under ``<run>/etc`` (world-readable, owned by the worker); idempotent."""
    passwd_text, group_text = identity_files(uid, gid, home)
    paths.etc.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in (("passwd", passwd_text), ("group", group_text)):
        target = paths.etc / name
        if not target.exists() or target.read_text() != text:
            target.write_text(text)
            target.chmod(0o644)
        written.append(target)
    return written[0], written[1]


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
    """Serialise mirror updates across concurrent runs and worker processes."""
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
    except PreparationError:
        return
    if refs.default_branch and refs.default_branch in refs.branches:
        git("symbolic-ref", "HEAD", f"refs/heads/{refs.default_branch}", cwd=mirror)


def refresh_mirror(data_dir: Path, project_slug: str, repo: RepoSpec) -> Path:
    return sync_mirror(mirror_path(data_dir, project_slug, repo.name), repo.url, repo.auth)


def clone_from_mirror(mirror: Path, dest: Path, repo: RepoSpec) -> None:
    """``git clone --local`` (hardlinked objects) and point ``origin`` at the real remote.
    Without a branch the mirror's HEAD (the remote's default branch) is checked out."""
    git("clone", "--local", *(["--branch", repo.branch] if repo.branch else []), str(mirror), str(dest))
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
    """Remove the run directory, whatever the run left in it: read-only files and
    directories (sealed attachments, Go's module cache, ``chmod -w`` build output), deep
    ``node_modules`` trees, sockets and FIFOs, symlinks pointing anywhere. Symlinks are
    removed, never followed."""
    if not paths.root.exists():
        return
    try:
        shutil.rmtree(paths.root)
        return
    except OSError:
        pass  # permissions: open up the tree (only the parts that refused) and try again
    _make_tree_removable(paths.root)
    shutil.rmtree(paths.root, onexc=_removal_failed)


def _make_tree_removable(root: Path) -> None:
    """Give the owner rwx on every directory below ``root``, top-down so that unreadable
    directories become listable before the walk enters them. Files need nothing: removing
    one takes write permission on its directory only. ``fwalk`` works relative to directory
    descriptors, so trees longer than ``PATH_MAX`` are no problem."""
    def _open_up(name: str, dir_fd: int | None = None) -> None:
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode) and mode & 0o700 != 0o700:  # never a symlink's target
                os.chmod(name, stat.S_IMODE(mode) | 0o700, dir_fd=dir_fd)
        except OSError:
            pass

    _open_up(str(root))
    for _current, dirs, _files, dir_fd in os.fwalk(root, onerror=lambda _exc: None):
        for name in dirs:
            _open_up(name, dir_fd)


def _removal_failed(func, target, exc: BaseException) -> None:
    if isinstance(exc, FileNotFoundError):  # gone meanwhile
        return
    raise exc
