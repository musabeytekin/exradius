"""Tasks, assignment, comments, runs and Exradius's events (design §5–§8, §10).

Everything that changes a task runs under that task's lock, so a person's action and an
Exradius event for the same task never interleave. Publishing to the broker happens under
the lock and before the commit: an event that comes back at once waits for the lock and
then finds the rows (design §7.1).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from exradius_bridge_kit import Problem
from exradius_bridge_kit.exradius import ExradiusError, IntegrationApi
from exradius_bridge_kit.flow import safe_name, unique
from exradius_bridge_kit.status import StatusReporter
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from exradius_desk import mentions
from exradius_desk.access import Directory
from exradius_desk.files import download_url
from exradius_desk.live import Hub
from exradius_desk.mail import Mail, Mailer, cut
from exradius_desk.models import (
    Answerer,
    Artifact,
    Attachment,
    Comment,
    Counter,
    History,
    Meta,
    Mention,
    Person,
    Run,
    SessionFactory,
    Task,
    utcnow,
)

log = logging.getLogger(__name__)

BRIDGE = "desk"
STATUSES = ("todo", "in_progress", "done")
LIVE = ("sent", "queued", "preparing", "running", "waiting_for_input", "resuming")
ENDED = ("completed", "failed", "cancelled", "rejected")
DUPLICATE = "duplicate task_id"
MAX_RETRIES = 5
STRIPES = 256
FAILURES = 5
FAILURES_FOR = 900.0
TITLE_MAX = 300
TEXT_MAX = 100_000


class Refused(Exception):
    """An action the person may not take now; the API answers ``status`` with the message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Upload:
    name: str
    content_type: str
    data: bytes


def exradius_task_id(key: str, install: str, run: int) -> str:
    """``plane-12-kqxz``, then ``plane-12-2-kqxz`` (design §6). The last part is the install
    code, letters only, so the id never ends in ``-<digits>`` as every Jira and GitHub id does."""
    return f"{key}-{install}" if run <= 1 else f"{key}-{run}-{install}"


def parse_key(key: str) -> tuple[str, int] | None:
    project, _, number = key.rpartition("-")
    if not project or not number.isdigit():
        return None
    return project, int(number)


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def guess_text(routing: dict | None, names: dict[str, str]) -> str | None:
    """"a guess: Scout 59%, Developer 41%" for a decided guess (kit §6.1)."""
    if not isinstance(routing, dict) or routing.get("method") != "decided" or not routing.get("guess"):
        return None
    probabilities = routing.get("probabilities") or {}
    ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])[:3]
    return "a guess: " + ", ".join(f"{names.get(a, a)} {round(p * 100)}%" for a, p in ranked)


class Desk:
    def __init__(
        self,
        *,
        sessions: SessionFactory,
        broker: Any,
        api: IntegrationApi,
        public_url: str,
        max_attachment_bytes: int,
        software: str,
        mailer: Mailer | None = None,
        hub: Hub | None = None,
        downloads: httpx.AsyncClient | None = None,
        api_url: str = "",
        provider_problem: Callable[[], str | None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.sessions = sessions
        self.broker = broker
        self.api = api
        self.public_url = public_url.rstrip("/")
        self.max_attachment_bytes = max_attachment_bytes
        self.mailer = mailer or Mailer(None)
        if self.mailer.on_failure is None:
            self.mailer.on_failure = lambda reason: self._failed(Problem(None, reason))
        self.hub = hub or Hub()
        self.downloads = downloads or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self.api_url = api_url.rstrip("/")
        self.provider_problem = provider_problem
        self.directory = Directory()
        self.status = StatusReporter(api, software, self.problems)
        self.install = ""
        self._clock = clock
        self._failures: deque[tuple[float, Problem]] = deque(maxlen=FAILURES)
        self._locks = [asyncio.Lock() for _ in range(STRIPES)]
        self._directory_lock = asyncio.Lock()
        self._work: set[asyncio.Task] = set()

    # -- running --------------------------------------------------------------------------------

    async def start(self) -> None:
        async with self.sessions() as session:
            install = await session.get(Meta, "install")
        if install is None:
            raise RuntimeError("Desk's database has no install code: run `exradius-desk migrate`")
        self.install = install.value
        await self.refresh_directory()
        await self.broker.connect()

    async def listen(self) -> None:
        await self.broker.consume(self.on_event)
        self.status.start()

    async def close(self) -> None:
        await self.status.stop()
        await self.idle()
        await self.broker.close()
        await self.api.aclose()
        await self.downloads.aclose()

    async def idle(self) -> None:
        """Wait for background work: mail, re-reported status (tests, shutdown)."""
        while self._work:
            await asyncio.gather(*list(self._work), return_exceptions=True)
        await self.mailer.idle()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks[zlib.crc32(key.encode()) % STRIPES]

    # -- Exradius side: directory and status (design §2, §11) -------------------------------------

    async def refresh_directory(self) -> None:
        async with self._directory_lock:
            self.directory.update(await self.api.directory())
        await self.status.report(only_if_changed=True)

    def problems(self) -> list[Problem]:
        now = self._clock()
        recent = [p for at, p in self._failures if now - at <= FAILURES_FOR]
        provider = self.provider_problem()
        return [*([Problem(None, provider)] if provider else []), *self.directory.problems, *recent]

    def _failed(self, problem: Problem) -> None:
        self._failures.append((self._clock(), problem))
        self._spawn(self.status.report(only_if_changed=True))

    # -- access ----------------------------------------------------------------------------------

    def allowed(self, person: Person, project: str) -> bool:
        return self.directory.allows(person.groups or [], project)

    def projects(self, person: Person) -> list[dict[str, Any]]:
        return [{"slug": p.slug, "name": p.name, "runnable": p.runnable}
                for p in self.directory.visible(person.groups or [])]

    async def people(self, project: str) -> list[Person]:
        """Who can be assigned or mentioned on a project (design §2.1)."""
        async with self.sessions() as session:
            everyone = (await session.scalars(select(Person).order_by(Person.name))).all()
        return [p for p in everyone if self.directory.allows(p.groups or [], project)]

    async def _task(self, session: AsyncSession, person: Person, key: str) -> Task:
        parsed = parse_key(key)
        task = None
        if parsed is not None and self.allowed(person, parsed[0]):
            task = await session.scalar(select(Task).where(Task.project == parsed[0], Task.number == parsed[1]))
        if task is None:
            raise Refused(404, f"no task {key}")
        return task

    # -- reading ---------------------------------------------------------------------------------

    async def list_tasks(self, person: Person, *, project: str | None = None, assignee: str | None = None,
                         status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        visible = [p.slug for p in self.directory.visible(person.groups or [])]
        if project:
            visible = [p for p in visible if p == project]
        if not visible:
            return []
        query: Select = select(Task).where(Task.project.in_(visible))
        if status:
            if status not in STATUSES:
                raise Refused(422, f"status must be one of {', '.join(STATUSES)}")
            query = query.where(Task.status == status)
        if assignee:
            query = query.where(*self._assignee_filter(person, assignee))
        query = query.order_by(Task.updated_at.desc(), Task.id.desc()).limit(min(limit, 200)).offset(max(offset, 0))
        async with self.sessions() as session:
            tasks = (await session.scalars(query)).all()
            if not tasks:
                return []
            ids = [t.id for t in tasks]
            live = {r.task_id: r for r in (await session.scalars(
                select(Run).where(Run.task_id.in_(ids), Run.state.in_(LIVE)))).all()}
            names = await self._names(session, {t.assignee_id for t in tasks if t.assignee_id})
        return [{
            "key": t.key, "project": t.project, "title": t.title, "status": t.status,
            "assignee": self._assignee(t, names), "updated_at": iso(t.updated_at),
            "run_state": live[t.id].state if t.id in live else None,
        } for t in tasks]

    def _assignee_filter(self, person: Person, assignee: str) -> list:
        if assignee == "me":
            return [Task.assignee_kind == "person", Task.assignee_id == person.id]
        if assignee in ("exradius", "none"):
            return [Task.assignee_kind == assignee]
        if assignee.startswith("person:") and assignee[7:].isdigit():
            return [Task.assignee_kind == "person", Task.assignee_id == int(assignee[7:])]
        raise Refused(422, "assignee must be me, exradius, none or person:<id>")

    @staticmethod
    async def _names(session: AsyncSession, ids: Iterable[int | None]) -> dict[int, str]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {p.id: p.name for p in (await session.scalars(select(Person).where(Person.id.in_(wanted)))).all()}

    @staticmethod
    def _assignee(task: Task, names: dict[int, str]) -> dict[str, Any]:
        if task.assignee_kind == "exradius":
            return {"kind": "exradius", "id": None, "name": mentions.EXRADIUS}
        if task.assignee_kind == "person" and task.assignee_id:
            return {"kind": "person", "id": task.assignee_id, "name": names.get(task.assignee_id, "someone")}
        return {"kind": "none", "id": None, "name": None}

    async def get_task(self, person: Person, key: str) -> dict[str, Any]:
        async with self.sessions() as session:
            task = await self._task(session, person, key)
            return await self._view(session, task)

    async def _view(self, session: AsyncSession, task: Task) -> dict[str, Any]:
        runs = (await session.scalars(select(Run).where(Run.task_id == task.id).order_by(Run.seq, Run.id))).all()
        comments = (await session.scalars(select(Comment).where(Comment.task_id == task.id)
                                          .order_by(Comment.created_at, Comment.id))).all()
        history = (await session.scalars(select(History).where(History.task_id == task.id)
                                         .order_by(History.created_at, History.id))).all()
        attachments = (await session.scalars(select(Attachment).where(Attachment.task_id == task.id)
                                             .order_by(Attachment.id))).all()
        artifacts = (await session.scalars(select(Artifact).join(Run).where(Run.task_id == task.id)
                                           .order_by(Artifact.id))).all()
        mention_rows = (await session.scalars(select(Mention).join(Comment).where(Comment.task_id == task.id))).all()
        ids = {task.reporter_id, task.assignee_id, task.handed_over_by, *(c.author_id for c in comments),
               *(h.person_id for h in history), *(a.uploaded_by for a in attachments),
               *(m.person_id for m in mention_rows), *(r.cancel_requested_by for r in runs),
               *(r.handed_over_by for r in runs)}
        for h in history:
            if h.field == "assignee":
                for value in (h.old, h.new):
                    if value and value.startswith("person:"):
                        ids.add(int(value[7:]))
        names = await self._names(session, ids)
        run_by_id = {r.id: r for r in runs}
        question_by_request = {c.request_id: c for c in comments if c.kind == "question" and c.request_id}
        live = next((r for r in runs if r.state in LIVE), None)

        def person_ref(pid: int | None) -> dict | None:
            return {"id": pid, "name": names.get(pid, "someone")} if pid else None

        def files(comment_id: int | None) -> list[dict]:
            return [{"id": a.id, "name": a.name, "content_type": a.content_type, "size": a.size,
                     "uploaded_by": person_ref(a.uploaded_by), "created_at": iso(a.created_at)}
                    for a in attachments if a.comment_id == comment_id]

        timeline: list[dict[str, Any]] = []
        for c in comments:
            run = run_by_id.get(c.run_id) if c.run_id else None
            entry: dict[str, Any] = {
                "type": "comment", "id": c.id, "kind": c.kind, "body": c.body, "data": c.data or {},
                "author": ({"kind": "exradius", "id": None, "name": mentions.EXRADIUS} if c.author_kind == "exradius"
                           else {"kind": "person", **(person_ref(c.author_id) or {"id": None, "name": "someone"})}),
                "created_at": iso(c.created_at), "run": run.seq if run else None,
                "agent_name": run.agent_name if run else None,
                "files": files(c.id),
                "artifacts": [{"id": a.id, "name": a.name, "content_type": a.content_type}
                              for a in artifacts if a.comment_id == c.id],
            }
            if c.kind == "question":
                entry["request_id"] = c.request_id
                entry["answered_by"] = c.answered_by_id
            if c.answers_request_id:
                question = question_by_request.get(c.answers_request_id)
                entry["answers"] = question.id if question else None
            if c.started_run_id:
                started = run_by_id.get(c.started_run_id)
                entry["started_run"] = started.seq if started else None
            timeline.append(entry)
        for h in history:
            timeline.append({"type": "change", "id": h.id, "field": h.field, "old": h.old, "new": h.new,
                             "person": person_ref(h.person_id), "created_at": iso(h.created_at)})
        timeline.sort(key=lambda e: (e["created_at"] or "", 0 if e["type"] == "change" else 1, e["id"]))

        open_question = self._open_question(live, comments)
        return {
            "key": task.key, "project": task.project,
            "project_name": (self.directory.projects.get(task.project).name
                             if task.project in self.directory.projects else task.project),
            "title": task.title, "description": task.description, "status": task.status,
            "reporter": person_ref(task.reporter_id), "assignee": self._assignee(task, names),
            "handed_over_by": person_ref(task.handed_over_by),
            "created_at": iso(task.created_at), "updated_at": iso(task.updated_at),
            "files": files(None),
            "run": self._run_view(live, names) if live else None,
            "runs": [self._run_view(r, names) for r in runs],
            "timeline": timeline,
            "names": {str(k): v for k, v in names.items()},
            "exradius": "answer" if open_question else ("start" if live is None else None),
        }

    @staticmethod
    def _run_view(run: Run, names: dict[int, str]) -> dict[str, Any]:
        return {
            "seq": run.seq, "task_id": run.exradius_task_id, "state": run.state, "agent": run.agent,
            "agent_name": run.agent_name, "routing": run.routing, "created_at": iso(run.created_at),
            "ended_at": iso(run.ended_at),
            "handed_over_by": {"id": run.handed_over_by, "name": names.get(run.handed_over_by, "someone")},
            "cancel_requested_by": ({"id": run.cancel_requested_by, "name": names.get(run.cancel_requested_by)}
                                    if run.cancel_requested_by else None),
        }

    @staticmethod
    def _open_question(live: Run | None, comments: Iterable[Comment]) -> Comment | None:
        """The oldest unanswered question of the run, while it waits (design §7.3)."""
        if live is None or live.state != "waiting_for_input":
            return None
        return next((c for c in comments if c.kind == "question" and c.run_id == live.id
                     and c.answered_by_id is None), None)

    # -- files (design §8) -----------------------------------------------------------------------

    def _check_uploads(self, uploads: list[Upload]) -> None:
        total = sum(len(u.data) for u in uploads)
        if total > self.max_attachment_bytes:
            mb = self.max_attachment_bytes / (1024 * 1024)
            raise Refused(413, f"the files total {total / (1024 * 1024):.1f} MB; at most {mb:g} MB at once")
        for upload in uploads:
            if not upload.name.strip():
                raise Refused(422, "a file has no name")

    @staticmethod
    def _store_uploads(session: AsyncSession, task: Task, person: Person, uploads: list[Upload],
                       comment: Comment | None = None) -> None:
        for upload in uploads:
            session.add(Attachment(task_id=task.id, comment_id=comment.id if comment else None,
                                   name=upload.name.strip()[:255],
                                   content_type=(upload.content_type or "application/octet-stream")[:255],
                                   size=len(upload.data), data=upload.data, uploaded_by=person.id))

    async def attachment(self, person: Person, attachment_id: int) -> Attachment:
        async with self.sessions() as session:
            row = await session.get(Attachment, attachment_id, options=[undefer(Attachment.data)])
            task = await session.get(Task, row.task_id) if row else None
        if row is None or task is None or not self.allowed(person, task.project):
            raise Refused(404, "no such file")
        return row

    async def artifact(self, person: Person, artifact_id: int) -> tuple[Artifact, httpx.Response]:
        async with self.sessions() as session:
            row = await session.get(Artifact, artifact_id)
            run = await session.get(Run, row.run_id) if row else None
            task = await session.get(Task, run.task_id) if run else None
        if row is None or task is None or not self.allowed(person, task.project):
            raise Refused(404, "no such file")
        url = download_url(row.url, self.api_url)
        try:
            request = self.downloads.build_request("GET", url)
            response = await self.downloads.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise Refused(502, f"Exradius cannot be reached for {row.name}: {exc or type(exc).__name__}") from exc
        if response.status_code != 200:
            await response.aclose()
            raise Refused(502, f"Exradius answered {response.status_code} for {row.name}")
        return row, response

    # -- tasks (design §5) -----------------------------------------------------------------------

    @staticmethod
    def _text(value: str | None, what: str, *, required: bool = False, limit: int = TEXT_MAX) -> str:
        value = (value or "").strip()
        if required and not value:
            raise Refused(422, f"{what} is required")
        if len(value) > limit:
            raise Refused(422, f"{what} is longer than {limit} characters")
        return value

    async def create_task(self, person: Person, *, project: str, title: str, description: str = "",
                          assignee: str = "none", uploads: list[Upload] | None = None) -> dict[str, Any]:
        uploads = uploads or []
        if not self.allowed(person, project):
            raise Refused(404, f"no project {project}")
        title = self._text(title, "the title", required=True, limit=TITLE_MAX)
        description = self._text(description, "the description")
        self._check_uploads(uploads)
        target = await self._assignee_target(project, assignee)
        for attempt in (1, 2):
            async with self.sessions() as session:
                counter = await session.get(Counter, project, with_for_update=True)
                if counter is None:
                    counter = Counter(project=project, last=0)
                    session.add(counter)
                counter.last += 1
                task = Task(project=project, number=counter.last, title=title, description=description,
                            reporter_id=person.id)
                session.add(task)
                try:
                    await session.flush()
                except IntegrityError:  # the project's first task, created twice at once
                    await session.rollback()
                    if attempt == 2:
                        raise
                    continue
                self._store_uploads(session, task, person, uploads)
                if target is not None and target[0] == "person":
                    self._set_assignee(session, task, person, "person", target[1])
                await session.commit()
                key, number = task.key, task.number
                break
        log.info("%s created %s", person.name, key)
        mails = []
        if target is not None and target[0] == "person" and target[1] != person.id:
            mails = await self._assigned_mail(key, title, target[1])
        self._after(project, key, mails)
        if target is not None and target[0] == "exradius":
            try:
                await self.assign(person, key, "exradius")
            except Refused as exc:
                raise Refused(exc.status, f"{key} was created, but {exc.message}") from exc
        return {"key": key, "number": number}

    async def _assignee_target(self, project: str, assignee: str | None) -> tuple[str, int | None] | None:
        """``none``, ``exradius`` or ``person:<id>`` of someone of design §2.1."""
        if not assignee or assignee == "none":
            return None
        if assignee == "exradius":
            return ("exradius", None)
        if assignee.startswith("person:") and assignee[7:].isdigit():
            pid = int(assignee[7:])
            if any(p.id == pid for p in await self.people(project)):
                return ("person", pid)
            raise Refused(422, "that person cannot be assigned on this project")
        raise Refused(422, "assignee must be none, exradius or person:<id>")

    @staticmethod
    def _set_assignee(session: AsyncSession, task: Task, person: Person | None, kind: str, pid: int | None) -> None:
        old = "exradius" if task.assignee_kind == "exradius" else (
            f"person:{task.assignee_id}" if task.assignee_kind == "person" else "none")
        new = "exradius" if kind == "exradius" else (f"person:{pid}" if kind == "person" else "none")
        if old == new:
            return
        task.assignee_kind, task.assignee_id = kind, pid if kind == "person" else None
        task.updated_at = utcnow()
        session.add(History(task_id=task.id, person_id=person.id if person else None, field="assignee",
                            old=old, new=new))

    async def edit_task(self, person: Person, key: str, *, title: str | None = None,
                        description: str | None = None, status: str | None = None) -> None:
        async with self.lock(key):
            async with self.sessions() as session:
                task = await self._task(session, person, key)
                changes: list[tuple[str, str, str]] = []
                if title is not None:
                    value = self._text(title, "the title", required=True, limit=TITLE_MAX)
                    if value != task.title:
                        changes.append(("title", task.title, value))
                        task.title = value
                if description is not None:
                    value = self._text(description, "the description")
                    if value != task.description:
                        changes.append(("description", task.description, value))
                        task.description = value
                if status is not None:
                    if status not in STATUSES:
                        raise Refused(422, f"status must be one of {', '.join(STATUSES)}")
                    if status != task.status:
                        changes.append(("status", task.status, status))
                        task.status = status
                for field, old, new in changes:
                    session.add(History(task_id=task.id, person_id=person.id, field=field, old=old, new=new))
                if changes:
                    task.updated_at = utcnow()
                await session.commit()
                project = task.project
        if changes:
            self._after(project, key)

    async def add_files(self, person: Person, key: str, uploads: list[Upload]) -> None:
        self._check_uploads(uploads)
        async with self.lock(key):
            async with self.sessions() as session:
                task = await self._task(session, person, key)
                self._store_uploads(session, task, person, uploads)
                task.updated_at = utcnow()
                await session.commit()
                project = task.project
        self._after(project, key)

    async def assign(self, person: Person, key: str, assignee: str) -> None:
        mails: list[Mail] = []
        async with self.lock(key):
            async with self.sessions() as session:
                task = await self._task(session, person, key)
                if task.assignee_kind == "exradius":
                    raise Refused(409, f"Exradius is working on {key}; cancel the run to reassign it")
                target = await self._assignee_target(task.project, assignee)
                if target is not None and target[0] == "exradius":
                    await self._start_run(session, task, person)
                    project, title = task.project, task.title
                else:
                    kind, pid = target if target else ("none", None)
                    self._set_assignee(session, task, person, kind, pid)
                    await session.commit()
                    project, title = task.project, task.title
                    if kind == "person" and pid != person.id:
                        mails = await self._assigned_mail(key, title, pid)
        self._after(project, key, mails)

    async def _assigned_mail(self, key: str, title: str, pid: int | None) -> list[Mail]:
        async with self.sessions() as session:
            assignee = await session.get(Person, pid) if pid else None
        project = parse_key(key)[0]
        if assignee is None or not self._mailable(assignee, project):
            return []
        return [Mail(assignee.email, assignee.name, f"{key} {title}: assigned to you",
                     f"{title}\n\n{self.public_url}/tasks/{key}\n")]

    # -- runs (design §7) -----------------------------------------------------------------------------

    async def _start_run(self, session: AsyncSession, task: Task, person: Person,
                         comment: Comment | None = None) -> Run:
        """The one way a run starts (design §7.1). Under the task's lock; commits, or rolls
        back and raises 503 when the broker cannot take the task."""
        task.runs += 1
        seq = (await session.scalar(select(func.count()).select_from(Run).where(Run.task_id == task.id))) + 1
        run = Run(task_id=task.id, seq=seq, exradius_task_id=exradius_task_id(task.key, self.install, task.runs),
                  handed_over_by=person.id, started_by_comment_id=comment.id if comment else None)
        session.add(run)
        self._set_assignee(session, task, person, "exradius", None)
        task.handed_over_by = person.id
        if task.status == "todo":
            session.add(History(task_id=task.id, person_id=None, field="status", old="todo", new="in_progress"))
            task.status = "in_progress"
        await session.flush()
        if comment is not None:
            comment.started_run_id = run.id
        message = await self._task_message(session, task, run, comment)
        try:
            await self.broker.publish_task(message)
        except Exception as exc:
            await session.rollback()
            log.warning("could not publish %s: %s", run.exradius_task_id, exc or type(exc).__name__)
            raise Refused(503, "Exradius's broker cannot be reached") from exc
        await session.commit()
        log.info("%s handed %s to Exradius as %s", person.name, task.key, run.exradius_task_id)
        return run

    async def _task_message(self, session: AsyncSession, task: Task, run: Run,
                            starting: Comment | None) -> dict[str, Any]:
        comments = (await session.scalars(select(Comment).where(Comment.task_id == task.id)
                                          .order_by(Comment.created_at, Comment.id))).all()
        if starting is not None and starting not in comments:
            comments = [*comments, starting]
        elif starting is not None:  # last, whatever its timestamp
            comments = [c for c in comments if c is not starting] + [starting]
        runs = {r.id: r for r in (await session.scalars(select(Run).where(Run.task_id == task.id))).all()}
        artifacts = (await session.scalars(select(Artifact).join(Run).where(Run.task_id == task.id))).all()
        names = await self._names(session, {task.reporter_id, task.handed_over_by,
                                            *(c.author_id for c in comments)})
        mentioned = (await session.scalars(select(Mention.person_id).join(Comment)
                                           .where(Comment.task_id == task.id, Mention.person_id.is_not(None)))).all()
        if starting is not None:
            mentioned = [*mentioned, *mentions.parse(starting.body)[0]]
        names.update(await self._names(session, mentioned))
        rows = (await session.scalars(select(Attachment).where(Attachment.task_id == task.id)
                                      .options(undefer(Attachment.data)))).all()
        attachments, skipped = self._pack(rows, starting)
        context: dict[str, Any] = {"task": {
            "key": task.key, "url": f"{self.public_url}/tasks/{task.key}", "project": task.project,
            "status": task.status, "reporter": names.get(task.reporter_id),
            "handed_over_by": names.get(task.handed_over_by),
        }}
        if comments:
            context["comments"] = [self._context_comment(c, runs, artifacts, names) for c in comments]
        if skipped:
            context["skipped_attachments"] = skipped
        description = task.description.strip()
        return {
            "version": 1,
            "task_id": run.exradius_task_id,
            "project": task.project,
            "instructions": task.title + (f"\n\n{description}" if description else ""),
            "context": context,
            "attachments": attachments,
            "correlation": {"bridge": BRIDGE, "instance": self.public_url, "install": self.install,
                            "task": task.key},
        }

    @staticmethod
    def _context_comment(c: Comment, runs: dict[int, Run], artifacts: list[Artifact],
                         names: dict[int, str]) -> dict[str, Any]:
        data = c.data or {}
        run = runs.get(c.run_id) if c.run_id else None
        agent = (run.agent_name if run else None) or mentions.EXRADIUS
        if c.kind == "question":
            text = c.body + (f"\n\n{data['context']}" if data.get("context") else "")
        elif c.kind == "started":
            text = f"{agent} started working on this (run {run.seq if run else '?'})."
        elif c.kind == "result":
            state = data.get("state")
            heading = {"completed": f"{agent} finished.", "failed": f"{agent} could not finish.",
                       "cancelled": f"{agent} was stopped."}.get(state, f"{agent} ended.")
            parts = [heading, data.get("error") or "", c.body or ""]
            files = [a.name for a in artifacts if a.comment_id == c.id]
            if files:
                parts.append("Files: " + ", ".join(files))
            text = "\n\n".join(p for p in parts if p)
        elif c.kind == "rejected":
            text = f"The run could not start: {c.body}"
        elif c.kind == "cancel":
            text = f"{names.get(c.author_id, 'Someone')} asked Exradius to stop."
        else:
            text = mentions.plain(c.body, names)
        author = mentions.EXRADIUS if c.author_kind == "exradius" else names.get(c.author_id, "someone")
        return {"author": author, "at": iso(c.created_at), "kind": c.kind, "text": text}

    def _pack(self, rows: list[Attachment], starting: Comment | None) -> tuple[list[dict], list[dict]]:
        """The starting comment's files, then the rest newest first, within the cap (design §7.1)."""
        first = [a for a in rows if starting is not None and a.comment_id == starting.id]
        rest = sorted((a for a in rows if a not in first), key=lambda a: (a.created_at, a.id), reverse=True)
        chosen, skipped, used, total = [], [], set(), 0
        for row in [*first, *rest]:
            if total + row.size > self.max_attachment_bytes:
                skipped.append({"name": row.name, "size": row.size})
                continue
            total += row.size
            chosen.append({"name": unique(safe_name(row.name), used), "content_type": row.content_type,
                           "content_base64": base64.b64encode(row.data).decode()})
        return chosen, skipped

    async def cancel(self, person: Person, key: str) -> None:
        async with self.lock(key):
            async with self.sessions() as session:
                task = await self._task(session, person, key)
                run = await self._live(session, task)
                if run is None:
                    raise Refused(409, f"no run of {key} is live")
                if run.cancel_requested_by is None:
                    await self._publish_cancel(run, person.name)
                    run.cancel_requested_by, run.cancel_requested_at = person.id, utcnow()
                    session.add(Comment(task_id=task.id, author_kind="person", author_id=person.id, kind="cancel",
                                        run_id=run.id))
                    task.updated_at = utcnow()
                await session.commit()
                project = task.project
        self._after(project, key)

    async def _publish_cancel(self, run: Run, name: str) -> None:
        try:
            await self.broker.publish_cancel({"version": 1, "task_id": run.exradius_task_id,
                                              "reason": f"cancelled by {name} in Desk"[:500]})
        except Exception as exc:
            raise Refused(503, "Exradius's broker cannot be reached") from exc

    @staticmethod
    async def _live(session: AsyncSession, task: Task) -> Run | None:
        return await session.scalar(select(Run).where(Run.task_id == task.id, Run.state.in_(LIVE))
                                    .order_by(Run.id.desc()).limit(1))

    # -- comments and mentions (design §8) -------------------------------------------------------------

    async def comment(self, person: Person, key: str, body: str, uploads: list[Upload] | None = None) -> dict:
        uploads = uploads or []
        body = self._text(body, "the comment")
        if not body and not uploads:
            raise Refused(422, "the comment is empty")
        self._check_uploads(uploads)
        people_ids, exradius = mentions.parse(body)
        mails: list[Mail] = []
        async with self.lock(key):
            async with self.sessions() as session:
                task = await self._task(session, person, key)
                allowed = {p.id: p for p in await self.people(task.project)}
                strangers = people_ids - set(allowed)
                if strangers:
                    raise Refused(422, "a mentioned person cannot be mentioned on this project")
                comment = Comment(task_id=task.id, author_kind="person", author_id=person.id, kind="comment",
                                  body=body)
                session.add(comment)
                await session.flush()
                self._store_uploads(session, task, person, uploads, comment)
                for pid in sorted(people_ids):
                    session.add(Mention(comment_id=comment.id, person_id=pid))
                if exradius:
                    session.add(Mention(comment_id=comment.id, exradius=True))
                task.updated_at = utcnow()
                effect = None
                if exradius:
                    effect = await self._exradius_mention(session, task, person, comment, uploads)
                else:
                    await session.commit()
                project, title, comment_id = task.project, task.title, comment.id
        names = {p.id: p.name for p in allowed.values()}
        text = mentions.plain(body, names)
        for pid in sorted(people_ids - {person.id}):
            target = allowed[pid]
            if self._mailable(target, project):
                mails.append(Mail(target.email, target.name, f"{key} {title}: you were mentioned",
                                  f"{person.name}:\n\n{cut(text)}\n\n{self.public_url}/tasks/{key}#comment-{comment_id}\n"))
        self._after(project, key, mails)
        return {"id": comment_id, "effect": effect}

    async def _exradius_mention(self, session: AsyncSession, task: Task, person: Person, comment: Comment,
                                uploads: list[Upload]) -> str:
        """Answer the open question, or start a run, or refuse (design §7.3, §8)."""
        live = await self._live(session, task)
        if live is None:
            await self._start_run(session, task, person, comment)
            return "started"
        comments = (await session.scalars(select(Comment).where(Comment.task_id == task.id)
                                          .order_by(Comment.created_at, Comment.id))).all()
        question = self._open_question(live, comments)
        if question is None:
            key = task.key
            await session.rollback()
            raise Refused(409, f"Exradius is working on {key}; it reads comments when it asks a question")
        names = {p.id: p.name for p in await self.people(task.project)}
        attachments, skipped = self._pack([Attachment(name=u.name, content_type=u.content_type or
                                                      "application/octet-stream", size=len(u.data), data=u.data,
                                                      created_at=utcnow(), id=0, comment_id=comment.id)
                                           for u in uploads], comment)
        answer = mentions.plain(comment.body, names)
        try:
            await self.broker.publish_answer({"version": 1, "task_id": live.exradius_task_id,
                                              "request_id": question.request_id, "answer": answer,
                                              "attachments": attachments})
        except Exception as exc:
            await session.rollback()
            raise Refused(503, "Exradius's broker cannot be reached") from exc
        comment.answers_request_id = question.request_id
        question.answered_by_id = comment.id
        if await session.get(Answerer, (task.id, person.id)) is None:
            session.add(Answerer(task_id=task.id, person_id=person.id))
        await session.commit()
        log.info("%s answered a question on %s", person.name, task.key)
        return "answered"

    # -- Exradius's events (design §7.2) ------------------------------------------------------------

    def matches(self, event: dict[str, Any]) -> str | None:
        correlation = event.get("correlation")
        if not isinstance(correlation, dict) or correlation.get("bridge") != BRIDGE \
                or correlation.get("install") != self.install or not isinstance(correlation.get("task"), str):
            return None
        return correlation["task"]

    async def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "directory_changed":
            bridges = (event.get("payload") or {}).get("bridges")
            if not bridges or BRIDGE in bridges:
                try:
                    await self.refresh_directory()
                except ExradiusError as exc:
                    self._failed(Problem(None, f"the directory could not be fetched: {exc}"))
                    raise
            return
        if kind not in ("status", "info_request", "result"):
            return
        key = self.matches(event)
        if key is None:
            return
        try:
            async with self.lock(key):
                outcome = await self._task_event(event)
        except Exception as exc:
            self._failed(Problem(None, f"{key}: handling the {kind} event failed: {exc or type(exc).__name__}"))
            raise  # the broker retries once
        if outcome is not None:
            project, mails = outcome
            self._after(project, key, mails)

    async def _task_event(self, event: dict[str, Any]) -> tuple[str, list[Mail]] | None:
        kind = event["type"]
        payload = event.get("payload") or {}
        async with self.sessions() as session:
            run = await session.scalar(select(Run).where(Run.exradius_task_id == event.get("task_id")))
            if run is None:
                return None
            task = await session.get(Task, run.task_id)
            assert task is not None
            mails: list[Mail] = []
            if kind == "status":
                state = payload.get("state")
                if state == "queued":
                    await self._queued(session, task, run, event, payload)
                elif state == "rejected":
                    if await self._rejected(session, task, run, str(payload.get("reason") or "")):
                        mails = await self._run_mails(session, task, run, "could not start",
                                                      str(payload.get("reason") or ""))
                elif run.state in LIVE and state in LIVE:
                    if run.agent is None:
                        self._take_agent(task, run, event)
                    run.state = state
                # cancelled, failed, completed: the result follows and ends the run.
            elif kind == "info_request" and run.state in LIVE:
                session.add(Comment(task_id=task.id, author_kind="exradius", kind="question", run_id=run.id,
                                    body=str(payload.get("question") or ""),
                                    request_id=str(payload.get("request_id") or ""),
                                    data={"context": payload.get("context")} if payload.get("context") else None))
                task.updated_at = utcnow()
                label = run.agent_name or mentions.EXRADIUS
                text = str(payload.get("question") or "")
                if payload.get("context"):
                    text += f"\n\n{payload['context']}"
                mails = await self._run_mails(session, task, run, f"{label} asks a question", text)
            elif kind == "result" and run.ended_at is None:
                mails = await self._result(session, task, run, event, payload)
            await session.commit()
            return task.project, mails

    def _take_agent(self, task: Task, run: Run, event: dict[str, Any]) -> None:
        agent = event.get("agent")
        if isinstance(agent, str) and agent:
            run.agent = agent
            run.agent_name = self.directory.agent_name(task.project, agent)

    async def _queued(self, session: AsyncSession, task: Task, run: Run, event: dict, payload: dict) -> None:
        if run.state != "sent":
            return
        run.state = "queued"
        self._take_agent(task, run, event)
        run.routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else None
        project = self.directory.projects.get(task.project)
        guess = guess_text(run.routing, project.agents if project else {})
        session.add(Comment(task_id=task.id, author_kind="exradius", kind="started", run_id=run.id,
                            data={"agent": run.agent, "agent_name": run.agent_name, "guess": guess}))
        task.updated_at = utcnow()
        if run.cancel_requested_by is not None:
            # Exradius did not know the task when the first cancel came (design §7.3).
            name = (await session.get(Person, run.cancel_requested_by)).name
            await self._publish_cancel(run, name)

    async def _rejected(self, session: AsyncSession, task: Task, run: Run, reason: str) -> bool:
        """True when the run ended; False when a duplicate id was retried."""
        if run.ended_at is not None:
            return False
        if reason.startswith(DUPLICATE) and run.retries < MAX_RETRIES and run.cancel_requested_by is None:
            task.runs += 1
            run.retries += 1
            run.exradius_task_id = exradius_task_id(task.key, self.install, task.runs)
            run.state = "sent"
            starting = await session.get(Comment, run.started_by_comment_id) if run.started_by_comment_id else None
            message = await self._task_message(session, task, run, starting)
            await self.broker.publish_task(message)
            log.info("a task id was used before; trying %s", message["task_id"])
            return False
        run.state, run.ended_at, run.reason = "rejected", utcnow(), reason
        session.add(Comment(task_id=task.id, author_kind="exradius", kind="rejected", run_id=run.id, body=reason))
        self._hand_back(session, task, run)
        log.info("%s was rejected: %s", run.exradius_task_id, reason)
        return True

    async def _result(self, session: AsyncSession, task: Task, run: Run, event: dict, payload: dict) -> list[Mail]:
        if run.agent is None:
            self._take_agent(task, run, event)
        state = payload.get("state") if payload.get("state") in ("completed", "failed", "cancelled") else "failed"
        run.state, run.ended_at = state, utcnow()
        run.summary = payload.get("summary") or None
        run.error = payload.get("error") or None
        run.warnings = [str(w) for w in payload.get("warnings") or []]
        comment = Comment(task_id=task.id, author_kind="exradius", kind="result", run_id=run.id,
                          body=run.summary or "", data={"state": state, "error": run.error, "warnings": run.warnings})
        session.add(comment)
        await session.flush()
        for artifact in payload.get("artifacts") or []:
            if isinstance(artifact, dict) and isinstance(artifact.get("url"), str):
                session.add(Artifact(run_id=run.id, comment_id=comment.id,
                                     name=str(artifact.get("name") or "file")[:255],
                                     content_type=str(artifact.get("content_type") or "application/octet-stream")[:255],
                                     url=artifact["url"]))
        self._hand_back(session, task, run)
        label = run.agent_name or mentions.EXRADIUS
        heading = {"completed": "finished", "failed": "could not finish", "cancelled": "was stopped"}[state]
        text = "\n\n".join(p for p in (run.error, run.summary) if p) or f"{label} {heading}."
        log.info("%s ended: %s", run.exradius_task_id, state)
        return await self._run_mails(session, task, run, heading, text)

    def _hand_back(self, session: AsyncSession, task: Task, run: Run) -> None:
        """To whoever handed the task over (design §5); no "assigned to you" mail."""
        if task.assignee_kind == "exradius":
            self._set_assignee(session, task, None, "person", run.handed_over_by)
        task.handed_over_by = None
        task.updated_at = utcnow()

    # -- mail (design §10) ----------------------------------------------------------------------------

    def _mailable(self, person: Person, project: str) -> bool:
        return bool(person.email) and person.email_verified and self.directory.allows(person.groups or [], project)

    async def _run_mails(self, session: AsyncSession, task: Task, run: Run, what: str, text: str) -> list[Mail]:
        if not self.mailer.enabled:
            return []
        ids = {run.handed_over_by, task.reporter_id,
               *(await session.scalars(select(Answerer.person_id).where(Answerer.task_id == task.id))).all()}
        people = (await session.scalars(select(Person).where(Person.id.in_(ids)))).all()
        subject = f"{task.key} {task.title}: {what}"
        body = f"{cut(text)}\n\n{self.public_url}/tasks/{task.key}\n"
        return [Mail(p.email, p.name, subject, body) for p in people if self._mailable(p, task.project)]

    def _after(self, project: str, key: str, mails: list[Mail] | None = None) -> None:
        """After a commit: tell the browsers, send the mail."""
        self.hub.changed(project, key)
        if mails:
            self.mailer.send(mails)

