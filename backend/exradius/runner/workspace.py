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
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path

from exradius import git
from exradius.runner.client import HttpRunApi, RunEnded

log = logging.getLogger(__name__)

WORKSPACE = Path("/exradius/workspace")
ATTACHMENTS = Path(".exradius") / "attachments"
LAYOUT = {"work": 0o755, "home": 0o755, "tmp": 0o1777, "state": 0o755}
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


async def prepare(purpose: str, object_id: str, api_url: str, token: str, root: Path = WORKSPACE) -> int:
    """Lay out the workspace and, for a task, write its attachments (read-only)."""
    state = root / "state"
    if marker(state, "prepare").exists():
        print("the workspace is already prepared")
        return 0
    layout(root)
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


def clone(work_dir: Path, state_dir: Path, environ: dict[str, str]) -> int:
    """Clone one repository into ``/work/<folder>`` (docs/kubernetes.md §7.5): straight from
    the remote until the project volume holds mirrors (phase 3). The credential is used for
    this command only."""
    folder = environ["EXRADIUS_CLONE_FOLDER"]
    url = environ["EXRADIUS_CLONE_URL"]
    branch = environ.get("EXRADIUS_CLONE_BRANCH") or None
    auth = environ.get("EXRADIUS_GIT_AUTH") or None
    step = f"clone-{environ.get('EXRADIUS_CLONE_INDEX') or folder}"
    if marker(state_dir, step).exists():
        print(f"{folder} is already cloned")
        return 0
    dest = work_dir / folder
    if dest.exists():  # left by an attempt that did not finish
        shutil.rmtree(dest)
    try:
        git.clone(url, dest, branch, auth)
        head = git.git("log", "-1", "--format=%h %s", cwd=dest).strip()
    except git.GitError as exc:
        print(git.redact(str(exc), auth), flush=True)
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
