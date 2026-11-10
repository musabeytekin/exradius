"""Starting tasks, questions, answers and results (design §3, §4, §6).

Every change to one issue happens under that issue's lock, whether it comes from a
webhook, an Exradius event or the startup reconcile, so the state marker is read and
written by one handler at a time.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from dataclasses import dataclass
from typing import Any

import httpx

from exradius_github import replies
from exradius_github.exradius import Directory, DirectoryClient, Publisher
from exradius_github.github import GitHubApp, GitHubError, Repo
from exradius_github.labels import Choice, Labels, marker, resolve
from exradius_github.settings import Settings
from exradius_github.state import IssueState, latest, strip, task_id_for, with_state

log = logging.getLogger(__name__)

STRIPES = 256
DUPLICATE = "duplicate task_id"
MAX_RETRIES = 5
COMMENT_LIMIT = 65_000  # GitHub takes 65 536 characters; keep room for the marker
INLINE_LIMIT = 60_000
TEXT_TYPES = ("application/json", "application/yaml", "application/x-yaml", "application/x-patch", "application/x-diff")
TEXT_SUFFIXES = (".md", ".txt", ".patch", ".diff", ".json", ".yaml", ".yml", ".log", ".csv")


@dataclass(frozen=True)
class IssueRef:
    repo: str
    number: int
    installation: int

    @property
    def key(self) -> str:
        return f"{self.repo.lower()}#{self.number}"


def _is_text(name: str, content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in TEXT_TYPES or name.lower().endswith(TEXT_SUFFIXES)


def _fenced(text: str) -> str:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text.rstrip()}\n{fence}"


class Bridge:
    def __init__(
        self,
        *,
        settings: Settings,
        app: GitHubApp,
        labels: Labels,
        directory_client: DirectoryClient,
        publisher: Publisher,
        bot: str,
        downloads: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.app = app
        self.labels = labels
        self.directory_client = directory_client
        self.publisher = publisher
        self.bot = bot
        """The App's login, ``<slug>[bot]``."""
        self.directory = Directory()
        self._downloads = downloads or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self._locks = [asyncio.Lock() for _ in range(STRIPES)]
        self._directory_lock = asyncio.Lock()

    def lock(self, ref: IssueRef) -> asyncio.Lock:
        return self._locks[zlib.crc32(ref.key.encode()) % STRIPES]

    def repo(self, ref: IssueRef) -> Repo:
        return self.app.repo(ref.repo, ref.installation)

    async def refresh_directory(self) -> None:
        async with self._directory_lock:
            self.directory = await self.directory_client.fetch()
            await self.labels.sync(self.directory)

    async def installations_changed(self) -> None:
        self.app.forget_installations()
        await self.refresh_directory()

    # -- GitHub side ----------------------------------------------------------------------

    async def labeled(self, ref: IssueRef, label: dict, sender: str | None) -> None:
        """Someone added a label; one of the bridge's starts a task (design §3.1)."""
        resolution = resolve(label, ref.repo, self.directory, self.settings.github_host)
        if resolution is None:
            return
        async with self.lock(ref):
            repo = self.repo(ref)
            comments = await repo.comments(ref.number)
            state, _ = latest(comments, self.bot)
            name = str(label.get("name") or "")
            if state.running:
                if name != state.agent_label:
                    await repo.comment(ref.number, f"**{state.label or 'The agent'}** is still working on this.")
                    await repo.remove_label(ref.number, name)
                return
            if resolution.kind == "unavailable" or resolution.choice is None:
                await repo.comment(ref.number, f"The label `{name}` no longer stands for an available agent, so it was removed.")
                await repo.remove_label(ref.number, name)
                return
            await self._start(ref, state, resolution.choice, sender, comments)

    async def comment_created(self, ref: IssueRef, comment: dict) -> None:
        if (comment.get("user") or {}).get("login") == self.bot:
            return
        async with self.lock(ref):
            comments = await self.repo(ref).comments(ref.number)
            state, question = latest(comments, self.bot)
            if state.state != "waiting" or question is None or int(comment["id"]) <= int(question["id"]):
                return
            if replies.quotes(comment.get("body") or "", question.get("body") or ""):
                await self._answer(ref, state, question, comment)

    async def reconcile(self) -> None:
        """Catch up on what webhooks missed while the bridge was down (design §6)."""
        from exradius_github.labels import wanted

        for repo_name, labels in wanted(self.directory, self.settings.github_host).items():
            installation = await self.app.installation_for(repo_name)
            if installation is None:
                continue
            repo = self.app.repo(repo_name, installation)
            seen: set[int] = set()
            for label in labels:
                try:
                    issues = await repo.issues_with_label(label.name)
                except GitHubError as exc:
                    log.warning("cannot list issues labelled %r in %s: %s", label.name, repo_name, exc)
                    continue
                for issue in issues:
                    if issue["number"] in seen:
                        continue
                    seen.add(issue["number"])
                    ref = IssueRef(repo_name, int(issue["number"]), installation)
                    try:
                        await self._reconcile_issue(ref, issue, label.name)
                    except Exception:
                        log.exception("reconciling %s failed", ref.key)

    async def _reconcile_issue(self, ref: IssueRef, issue: dict, label_name: str) -> None:
        async with self.lock(ref):
            repo = self.repo(ref)
            comments = await repo.comments(ref.number)
            state, question = latest(comments, self.bot)
            if state.state == "waiting" and question is not None:
                for comment in comments:
                    if (comment.get("user") or {}).get("login") == self.bot or int(comment["id"]) <= int(question["id"]):
                        continue
                    if replies.quotes(comment.get("body") or "", question.get("body") or ""):
                        await self._answer(ref, state, question, comment)
                        return
                return
            if state.running:
                return
            label = next((l for l in issue.get("labels") or [] if l.get("name") == label_name), {"name": label_name})
            resolution = resolve(label, ref.repo, self.directory, self.settings.github_host)
            if resolution is None or resolution.choice is None:
                return
            events = await repo.label_events(ref.number)
            adder = next((e for e in reversed(events) if (e.get("label") or {}).get("name") == label_name), None)
            requester = ((adder or {}).get("actor") or {}).get("login")
            await self._start(ref, state, resolution.choice, requester, comments)

    async def _start(
        self,
        ref: IssueRef,
        state: IssueState,
        choice: Choice,
        requester: str | None,
        comments: list[dict],
        *,
        retry_comment: dict | None = None,
    ) -> None:
        repo = self.repo(ref)
        issue = await repo.issue(ref.number)
        state.runs += 1
        state.task_id = task_id_for(ref.repo, ref.number, state.runs)
        state.project, state.agent, state.label = choice.project, choice.agent, choice.shown
        state.agent_label = choice.label
        state.requester = requester
        state.state = "active"
        state.request_id = None
        if retry_comment is None:
            state.retries = 0
        message = self._task_message(ref, issue, comments, state.task_id, choice)
        # The state goes on the issue first: Exradius's events for this task id may come
        # back at once, and they are matched against it.
        if retry_comment is not None:
            carrier = await repo.edit_comment(retry_comment["id"], with_state(retry_comment.get("body") or "", state))
        else:
            carrier = await repo.comment(ref.number, with_state(f"**{choice.shown}** is working on this.", state))
        try:
            await self.publisher.publish_task(message)
        except Exception:
            state.state = "ended"
            text = f"**{choice.shown}** could not start: Exradius could not be reached. Add the label again to retry."
            await repo.edit_comment(carrier["id"], with_state(text, state))
            await repo.remove_label(ref.number, choice.label)
            raise
        log.info("started %s with %s/%s", state.task_id, choice.project, choice.agent)

    def _task_message(self, ref: IssueRef, issue: dict, comments: list[dict], task_id: str, choice: Choice) -> dict[str, Any]:
        title = str(issue.get("title") or "").strip()
        body = str(issue.get("body") or "").strip()
        other_labels = [l["name"] for l in issue.get("labels") or [] if isinstance(l, dict) and not marker(l.get("description"))]
        details = {
            "repository": ref.repo,
            "number": ref.number,
            "url": issue.get("html_url"),
            "author": (issue.get("user") or {}).get("login"),
            "state": issue.get("state"),
            "labels": other_labels,
            "created": issue.get("created_at"),
        }
        context: dict[str, Any] = {"issue": {k: v for k, v in details.items() if v not in (None, "", [])}}
        thread = [
            {"author": (c.get("user") or {}).get("login"), "created": c.get("created_at"), "body": strip(c.get("body")).strip()}
            for c in comments
        ]
        if thread:
            context["comments"] = thread
        return {
            "version": 1,
            "task_id": task_id,
            "project": choice.project,
            "agent": choice.agent,
            "instructions": title + (f"\n\n{body}" if body else ""),
            "context": context,
            "attachments": [],
            "correlation": {
                "github": {"host": self.settings.github_host, "repository": ref.repo, "issue": ref.number, "installation": ref.installation}
            },
        }

    async def _answer(self, ref: IssueRef, state: IssueState, question: dict, comment: dict) -> None:
        await self.publisher.publish_answer({
            "version": 1,
            "task_id": state.task_id,
            "request_id": state.request_id,
            "answer": replies.without_quotes(comment.get("body") or ""),
            "attachments": [],
        })
        # After publishing: if the edit fails, a later reply answers again, and Exradius
        # keeps only the first answer to a request.
        state.state = "active"
        state.request_id = None
        await self.repo(ref).edit_comment(question["id"], with_state(question.get("body") or "", state))
        log.info("answered %s", state.task_id)

    # -- Exradius side ----------------------------------------------------------------------

    async def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "directory_changed":
            await self.refresh_directory()
            return
        if kind not in ("status", "info_request", "result"):
            return
        correlation = event.get("correlation")
        github = correlation.get("github") if isinstance(correlation, dict) else None
        if not isinstance(github, dict) or github.get("host") != self.settings.github_host:
            return  # another integration's task, or another host's
        try:
            ref = IssueRef(str(github["repository"]), int(github["issue"]), int(github["installation"]))
        except (KeyError, TypeError, ValueError):
            return
        payload = event.get("payload") or {}
        async with self.lock(ref):
            repo = self.repo(ref)
            state, carrier = latest(await repo.comments(ref.number), self.bot)
            if state.task_id != event.get("task_id") or not state.running:
                log.debug("ignoring %s for %s: not the issue's current run", kind, event.get("task_id"))
                return
            if kind == "info_request":
                await self._question(ref, state, payload)
            elif kind == "result":
                await self._result(ref, state, payload)
            elif payload.get("state") == "rejected":
                await self._rejected(ref, state, str(payload.get("reason") or ""), carrier)

    def _mention(self, state: IssueState) -> str:
        return f" @{state.requester}" if state.requester else ""

    async def _question(self, ref: IssueRef, state: IssueState, payload: dict) -> None:
        who = f" @{state.requester}" if state.requester else ""
        parts = [f"**{state.label or 'The agent'}** asks{who}:", str(payload.get("question") or "").strip()]
        if payload.get("context"):
            parts.append(str(payload["context"]).strip())
        parts.append("_Use Quote reply on this comment to answer._")
        state.state = "waiting"
        state.request_id = str(payload.get("request_id") or "")
        await self.repo(ref).comment(ref.number, with_state("\n\n".join(p for p in parts if p), state))
        log.info("asked on behalf of %s", state.task_id)

    async def _result(self, ref: IssueRef, state: IssueState, payload: dict) -> None:
        label = f"**{state.label or 'The agent'}**"
        outcome = payload.get("state")
        if outcome == "completed":
            head = f"{label} finished.{self._mention(state)}"
        elif outcome == "cancelled":
            head = f"{label} was stopped.{self._mention(state)}"
        else:
            head = f"{label} could not finish: {payload.get('error') or 'no reason was given'}.{self._mention(state)}"
        parts = [head]
        if payload.get("summary"):
            parts.append(str(payload["summary"]).strip())
        warnings = [str(w) for w in payload.get("warnings") or []]
        if warnings:
            parts.append("**Warnings**\n\n" + "\n".join(f"- {w}" for w in warnings))
        parts += await self._artifacts(payload.get("artifacts") or [], used=sum(len(p) for p in parts))
        state.state = "ended"
        state.request_id = None
        state.retries = 0
        body = "\n\n".join(parts)
        if len(body) > COMMENT_LIMIT:
            body = body[: COMMENT_LIMIT - 20] + "\n\n… (cut to fit)"
        await self.repo(ref).comment(ref.number, with_state(body, state))
        await self._remove_label(ref, state)
        log.info("%s ended: %s", state.task_id, outcome)

    async def _artifacts(self, artifacts: list[dict], *, used: int) -> list[str]:
        """Text artifacts inline in collapsed blocks; GitHub cannot hold files on issues."""
        blocks: list[str] = []
        left_out: list[str] = []
        for artifact in artifacts:
            name = str(artifact.get("name") or "artifact")
            content_type = str(artifact.get("content_type") or "application/octet-stream")
            if not _is_text(name, content_type):
                left_out.append(name)
                continue
            try:
                response = await self._downloads.get(str(artifact.get("url")))
                response.raise_for_status()
                text = response.content.decode("utf-8")
            except (httpx.HTTPError, UnicodeDecodeError) as exc:
                log.warning("could not inline %s: %s", name, exc)
                left_out.append(name)
                continue
            block = f"<details><summary>{name}</summary>\n\n{_fenced(text)}\n\n</details>"
            if len(text) > INLINE_LIMIT or used + len(block) > COMMENT_LIMIT - 500:
                left_out.append(name)
                continue
            blocks.append(block)
            used += len(block)
        if left_out:
            blocks.append("Not attached (GitHub cannot hold files on issues): " + ", ".join(left_out))
        return blocks

    async def _rejected(self, ref: IssueRef, state: IssueState, reason: str, carrier: dict | None) -> None:
        if reason.startswith(DUPLICATE) and state.retries < MAX_RETRIES and state.project and state.agent and carrier:
            state.retries += 1
            choice = Choice(state.project, state.agent, state.label or f"{state.project} · {state.agent}", state.agent_label or "")
            log.info("%s was used before; trying the next task id", state.task_id)
            comments = await self.repo(ref).comments(ref.number)
            await self._start(ref, state, choice, state.requester, comments, retry_comment=carrier)
            return
        state.state = "ended"
        state.request_id = None
        state.retries = 0
        text = f"**{state.label or 'The agent'}** could not start: {reason or 'no reason was given'}.{self._mention(state)}"
        await self.repo(ref).comment(ref.number, with_state(text, state))
        await self._remove_label(ref, state)
        log.info("%s was rejected: %s", state.task_id, reason)

    async def _remove_label(self, ref: IssueRef, state: IssueState) -> None:
        if not state.agent_label:
            return
        try:
            await self.repo(ref).remove_label(ref.number, state.agent_label)
        except GitHubError as exc:
            log.warning("cannot remove %r from %s: %s", state.agent_label, ref.key, exc)
