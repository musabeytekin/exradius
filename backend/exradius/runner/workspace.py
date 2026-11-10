"""The runner's helper modes in a run pod (docs/kubernetes.md §7): ``--prepare`` (init 1),
``--clone`` (one init container per repository) and ``--files`` (the sidecar that writes
answer attachments).

The workspace is one PVC per Run. ``prepare`` mounts it whole and lays it out; the other
containers mount its sub-directories:

    work/                          /work                         0755
    work/.exradius/attachments/    read-only in setup and the workstation; 0775
    home/                          /home/runner                  0755
    tmp/                           /tmp                          1777
    state/                         /exradius/state               session files, git config, markers

A step that finds its marker in ``state/steps/`` has nothing to do: the pod was recreated
on the same workspace for a resume (§8.3).

With a project volume (§7.6), ``prepare`` also creates its ``mirrors/`` and each
``caches/<name>/`` (without one, each cache is ``caches/<name>/`` of the workspace), and
the mount point of every cache below the home or ``/tmp``: all as the run user, so the
kubelet never makes one as root (a root-owned ``~/.cache`` would stop other tools writing
there). ``clone`` refreshes the repository's mirror under a file lock and clones from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
from pathlib import Path, PurePosixPath

from exradius import git
from exradius.runner.client import HttpRunApi, RunEnded

log = logging.getLogger(__name__)

WORKSPACE = Path("/exradius/workspace")
ATTACHMENTS = Path(".exradius") / "attachments"
LAYOUT = {"work": 0o755, "home": 0o755, "tmp": 0o1777, "state": 0o755}
MOUNTED = {"/home/runner": "home", "/tmp": "tmp"}
"""Container paths that are workspace sub-directories, where cache mount points are made."""
FILES_POLL = 30.0
ENDED = ("completed", "failed", "cancelled", "rejected")


def marker(state_dir: Path, step: str) -> Path:
    return state_dir / "steps" / f"{step}.done"


def _mark(state_dir: Path, step: str) -> None:
    path = marker(state_dir, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def write_file(directory: Path, name: str, data: bytes) -> Path:
    """Write ``name`` read-only and atomically (a reader never sees it half written)."""
    if "/" in name or name in {".", ".."} or "\x00" in name:
        raise ValueError(f"invalid attachment name {name!r}")
    target = directory / name
    partial = directory / f".{name}.partial"
    partial.write_bytes(data)
    partial.chmod(0o444)
    os.replace(partial, target)
    return target


def layout(root: Path) -> None:
    for name, mode in LAYOUT.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(mode)  # explicit: mkdir applies the umask and never sets the sticky bit
    attachments = root / "work" / ATTACHMENTS
    attachments.mkdir(parents=True, exist_ok=True)
    attachments.chmod(0o775)  # the files sidecar runs as another uid in the run's group


def cache_directories(root: Path, caches: list[dict[str, str]], project: Path | None) -> list[str]:
    """Each cache's directory (on the project volume, else the workspace) and its mount
    point below the home or ``/tmp``; returns what it made, for the step's log."""
    made = []
    base = project if project is not None else root
    if project is not None:
        (project / "mirrors").mkdir(parents=True, exist_ok=True)
    for cache in caches:
        directory = base / "caches" / cache["name"]
        if not directory.exists():
            directory.mkdir(parents=True)
            made.append(cache["path"])
        path = PurePosixPath(cache["path"])
        for prefix, sub in MOUNTED.items():
            if path.is_relative_to(prefix) and path != PurePosixPath(prefix):
                (root / sub / path.relative_to(prefix)).mkdir(parents=True, exist_ok=True)
    return made


async def prepare(purpose: str, object_id: str, api_url: str, token: str, root: Path = WORKSPACE,
                  environ: dict[str, str] | None = None, handoff: Path | None = None) -> int:
    """Hand the run token to the workstation's runner, lay out the workspace and the cache
    directories and, for a task, write its attachments (read-only)."""
    environ = dict(os.environ if environ is None else environ)
    state = root / "state"
    if handoff is not None and token:
        hand_over(handoff, token)
    layout(root)
    caches = json.loads(environ.get("EXRADIUS_CACHES") or "[]")
    project = Path(environ["EXRADIUS_PROJECT_DIR"]) if environ.get("EXRADIUS_PROJECT_DIR") else None
    if caches or project is not None:
        made = cache_directories(root, caches, project)
        where = "on the project volume" if project is not None else "on this run's workspace (no project volume)"
        print(f"caches {where}: {', '.join(c['path'] for c in caches) or 'none'}"
              + (f" (new: {', '.join(made)})" if made else ""))
    if marker(state, "prepare").exists():
        print("the workspace is already prepared")
        return 0
    attachments = root / "work" / ATTACHMENTS
    if purpose == "task":
        api = HttpRunApi(api_url, object_id, token)
        try:
            for item in await api.list_attachments():
                write_file(attachments, item.name, await api.get_attachment(item.id))
                print(f"attachment {item.name}")
        finally:
            await api.aclose()
    _mark(state, "prepare")
    print("prepared /work, /home/runner, /tmp and the attachments folder (read-only)")
    return 0


def hand_over(path: Path, token: str) -> None:
    """Write the token where the workstation's runner reads and deletes it (an in-memory
    volume of this pod, so every new pod gets it again, even when the rest is prepared)."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)


def clone(work_dir: Path, state_dir: Path, environ: dict[str, str], egress_url: str | None = None) -> int:
    """Clone one repository into ``/work/<folder>`` (docs/kubernetes.md §7.5): from its
    mirror on the project volume, refreshed first under a file lock (``EXRADIUS_MIRROR_DIR``),
    or straight from the remote without one. Each fetch goes to the egress proxy's git route
    (``egress_url``), which adds the repository's credential; ``origin`` is the plain URL."""
    folder = environ["EXRADIUS_CLONE_FOLDER"]
    url = environ["EXRADIUS_CLONE_URL"]
    branch = environ.get("EXRADIUS_CLONE_BRANCH") or None
    config = git.insteadof(egress_url, url)
    mirrors = environ.get("EXRADIUS_MIRROR_DIR") or None
    step = f"clone-{environ.get('EXRADIUS_CLONE_INDEX') or folder}"
    if marker(state_dir, step).exists():
        print(f"{folder} is already cloned")
        return 0
    dest = work_dir / folder
    if dest.exists():  # left by an attempt that did not finish
        shutil.rmtree(dest)
    try:
        if mirrors:
            mirror = Path(mirrors) / f"{folder}.git"
            fresh = not (mirror / "HEAD").exists()
            git.sync_mirror(mirror, url, None, config=config)
            print(f"{'created' if fresh else 'fetched'} the mirror of {git.redact(url)}", flush=True)
            git.clone_from_mirror(mirror, dest, url, branch)
        else:
            git.clone(url, dest, branch, None, config=config)
        head = git.git("log", "-1", "--format=%h %s", cwd=dest).strip()
    except git.GitError as exc:
        print(git.redact(str(exc)), flush=True)
        return 1
    _mark(state_dir, step)
    print(f"checked out {branch or 'the default branch'} at {head}")
    return 0


async def files(run_id: str, api_url: str, token: str, directory: Path) -> int:
    """The sidecar: write each delivered answer's attachments into
    ``<attachments>/<request id>/`` (files 0444, directory 0555) as they arrive. The
    workstation mounts the folder read-only, and the runner resumes once they are there."""
    api = HttpRunApi(api_url, run_id, token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            poll = asyncio.create_task(api.wait_answers(FILES_POLL))
            stopped = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({poll, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if poll not in done:
                poll.cancel()
                break
            stopped.cancel()
            try:
                answers = poll.result()
            except RunEnded:
                break
            except Exception:  # noqa: BLE001 - retried at the next poll
                log.exception("could not read the answers")
                await asyncio.sleep(5)
                continue
            if answers.state in ENDED:
                break
            for answer in answers.answers:
                await _write_answer(api, directory, answer)
    finally:
        await api.aclose()
    return 0


async def _write_answer(api: HttpRunApi, directory: Path, answer) -> None:
    target = directory / answer.request_id
    pending = [a for a in answer.attachments if a.id and not (target / Path(a.path).name).exists()]
    if not pending:
        return
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)
    for attachment in pending:
        write_file(target, Path(attachment.path).name, await api.get_attachment(attachment.id))
        print(f"answer attachment {answer.request_id}/{Path(attachment.path).name}", flush=True)
    target.chmod(0o555)
