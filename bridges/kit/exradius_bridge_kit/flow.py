"""The flow (design §6, §7): work handed to the bot becomes a task, Exradius's events become
comments, a reply becomes an answer, and the item goes back to a person.

Every method that touches an item runs under that item's lock (``Kit`` takes it), so an
item's state is read and written by one handler at a time.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from exradius_bridge_kit import texts
from exradius_bridge_kit.exradius import Publisher
from exradius_bridge_kit.projects import Projects
from exradius_bridge_kit.state import ItemState, task_id_for
from exradius_bridge_kit.tracker import AttachmentInfo, Item, ItemGone, ItemRef, Tracker

log = logging.getLogger(__name__)

DUPLICATE = "duplicate task_id"
MAX_RETRIES = 5


def safe_name(name: str) -> str:
    """A plain file name, as Exradius requires for attachments."""
    cleaned = "".join("_" if c in "/\\\x00" else c for c in name).strip()
    if cleaned in {"", ".", ".."}:
        cleaned = "attachment"
    if len(cleaned) > 255:
        stem, ext = os.path.splitext(cleaned)
        cleaned = stem[: 255 - len(ext)] + ext
    return cleaned


def unique(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    stem, ext = os.path.splitext(name)
    while candidate in used:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    used.add(candidate)
    return candidate


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Flow:
    def __init__(
        self,
        *,
        tracker: Tracker,
        projects: Projects,
        publisher: Publisher,
        max_attachment_bytes: int,
        downloads: httpx.AsyncClient,
    ):
        self.tracker = tracker
        self.projects = projects
        self.publisher = publisher
        self.max_attachment_bytes = max_attachment_bytes
        self.downloads = downloads

    async def _load(self, ref: ItemRef) -> ItemState:
        return ItemState.load(await self.tracker.load_state(ref))

    async def _save(self, ref: ItemRef, state: ItemState) -> None:
        await self.tracker.save_state(ref, state.dump())

    # -- the tracker side (§6.1, §6.2) ---------------------------------------------------------

    async def handed_over(self, ref: ItemRef) -> None:
        try:
            item = await self.tracker.item(ref)
        except ItemGone:
            return
        if not item.assigned_to_bot:
            return
        state = await self._load(ref)
        if state.running:
            if not state.told_still_working:
                await self.tracker.post(ref, texts.still_working(state.label))
                state.told_still_working = True
                await self._save(ref, state)
            return
        resolution = self.projects.resolve(item.scope)
        if resolution.project is None:
            if resolution.claimants:
                message = texts.claimed_twice(item.scope, list(resolution.claimants), self.tracker.bridge)
            else:
                message = texts.unclaimed(item.scope)
            await self.tracker.post(ref, message)
            await self._hand_back(ref)
            log.info("%s: no single project takes %s (%s)", ref.key, item.scope, ", ".join(resolution.claimants) or "none")
            return
        await self._start(item, state, resolution.project)

    async def commented(self, ref: ItemRef) -> None:
        state = await self._load(ref)
        if state.state != "waiting" or not state.request_id:
            return
        replies = await self.tracker.replies(ref, state.asked_at)
        if replies:
            await self._answer(ref, state, replies[0])

    async def reconcile(self, ref: ItemRef) -> None:
        """Catch up on an item assigned to the bot while the bridge was down (§10)."""
        state = await self._load(ref)
        if state.state == "waiting":
            await self.commented(ref)
        elif not state.running:
            await self.handed_over(ref)

    async def _start(self, item: Item, state: ItemState, project: str, *, retry: bool = False) -> None:
        previous = state.model_copy()
        state.runs += 1
        state.task_id = task_id_for(item.ref.key, state.runs)
        state.project = project
        state.agent = state.label = None
        state.state = "starting"
        state.request_id = None
        state.asked_at = None
        state.told_still_working = False
        if not retry:
            state.retries = 0
        message = await self._task_message(item, state.task_id, project)
        # The state goes on the item first: Exradius's events for this task id may come
        # back at once, and they are matched against it.
        await self._save(item.ref, state)
        try:
            await self.publisher.publish_task(message)
        except Exception:
            await self._save(item.ref, previous)
            raise
        log.info("published %s for project %s", state.task_id, project)

    async def _task_message(self, item: Item, task_id: str, project: str) -> dict[str, Any]:
        attachments, skipped = await self._attachments(item.attachments)
        context: dict[str, Any] = {"item": {
            "key": item.ref.key,
            "url": item.url,
            **({"reporter": item.reporter.name} if item.reporter else {}),
            **{k: v for k, v in item.details.items() if v not in (None, "", [], {})},
        }}
        if item.comments:
            context["comments"] = [
                {"author": c.author, "created": _iso(c.created), "body": c.body} for c in item.comments
            ]
        if skipped:
            context["skipped_attachments"] = skipped
        title = item.title.strip()
        body = item.body.strip()
        return {
            "version": 1,
            "task_id": task_id,
            "project": project,
            "instructions": title + (f"\n\n{body}" if body else ""),
            "context": context,
            "attachments": attachments,
            "correlation": {
                "bridge": self.tracker.bridge,
                "instance": self.tracker.instance,
                "item": {"id": item.ref.id, "key": item.ref.key},
            },
        }

    async def _attachments(self, items: list[AttachmentInfo]) -> tuple[list[dict], list[dict]]:
        """Inlined as base64, oldest first, while the total stays within the cap (§7)."""
        chosen: list[dict] = []
        skipped: list[dict] = []
        used: set[str] = set()
        total = 0
        oldest = datetime.min.replace(tzinfo=UTC)
        for info in sorted(items, key=lambda a: a.created or oldest):
            if total + info.size > self.max_attachment_bytes:
                skipped.append({"name": info.name, "size": info.size})
                continue
            data = await self.tracker.download(info)
            if total + len(data) > self.max_attachment_bytes:
                skipped.append({"name": info.name, "size": len(data)})
                continue
            total += len(data)
            chosen.append({
                "name": unique(safe_name(info.name), used),
                "content_type": info.content_type or "application/octet-stream",
                "content_base64": base64.b64encode(data).decode(),
            })
        return chosen, skipped

    async def _answer(self, ref: ItemRef, state: ItemState, reply) -> None:
        attachments, skipped = await self._attachments(list(reply.attachments))
        answer = reply.body
        if skipped:
            answer += texts.too_large([s["name"] for s in skipped])
        await self.publisher.publish_answer({
            "version": 1,
            "task_id": state.task_id,
            "request_id": state.request_id,
            "answer": answer,
            "attachments": attachments,
        })
        # After publishing: if saving fails, a later reply answers again, and Exradius
        # keeps only the first answer to a request.
        state.state = "active"
        state.request_id = None
        state.asked_at = None
        await self._save(ref, state)
        log.info("answered for %s", state.task_id)

    # -- the Exradius side (§6) ------------------------------------------------------------------

    def matches(self, event: dict[str, Any]) -> ItemRef | None:
        """The item an event is about, when it is this bridge's and this tracker's."""
        correlation = event.get("correlation")
        if not isinstance(correlation, dict):
            return None
        item = correlation.get("item")
        if (correlation.get("bridge") != self.tracker.bridge or correlation.get("instance") != self.tracker.instance
                or not isinstance(item, dict) or not item.get("id") or not item.get("key")):
            return None
        return ItemRef(id=str(item["id"]), key=str(item["key"]))

    async def on_task_event(self, ref: ItemRef, event: dict[str, Any]) -> None:
        kind = event.get("type")
        payload = event.get("payload") or {}
        state = await self._load(ref)
        if state.task_id != event.get("task_id") or not state.running:
            log.debug("ignoring %s for %s: not the item's current run", kind, event.get("task_id"))
            return
        if kind == "info_request":
            await self._question(ref, state, payload)
        elif kind == "result":
            await self._result(ref, state, event, payload)
        elif kind == "status":
            if payload.get("state") == "rejected":
                await self._rejected(ref, state, str(payload.get("reason") or ""))
            elif payload.get("state") == "queued":
                await self._queued(ref, state, event, payload)
            elif state.state == "starting":  # the queued event was missed
                self._take_agent(state, event)
                state.state = "active"
                await self._save(ref, state)

    def _take_agent(self, state: ItemState, event: dict[str, Any]) -> None:
        agent = event.get("agent")
        if isinstance(agent, str) and agent:
            state.agent = agent
            state.label = self.projects.agent_name(state.project, agent)

    async def _queued(self, ref: ItemRef, state: ItemState, event: dict[str, Any], payload: dict[str, Any]) -> None:
        self._take_agent(state, event)
        state.state = "active"
        # The state first: a failed comment must not leave the run looking unstarted.
        await self._save(ref, state)
        names = self.projects.agents.get(state.project or "", {})
        await self.tracker.post(ref, texts.start(state.label or "The agent", payload.get("routing"), names))
        log.info("%s started with %s", state.task_id, state.agent)

    async def _question(self, ref: ItemRef, state: ItemState, payload: dict[str, Any]) -> None:
        try:
            reporter = (await self.tracker.item(ref)).reporter
        except ItemGone:
            reporter = None
        asked = await self.tracker.post(ref, texts.question(
            state.label, reporter, str(payload.get("question") or ""), payload.get("context")))
        state.state = "waiting"
        state.request_id = str(payload.get("request_id") or "")
        state.asked_at = asked or datetime.now(UTC)
        await self._save(ref, state)
        log.info("asked on behalf of %s", state.task_id)

    async def _result(self, ref: ItemRef, state: ItemState, event: dict[str, Any], payload: dict[str, Any]) -> None:
        if state.label is None:
            self._take_agent(state, event)
        attached, failed = await self._upload_artifacts(ref, payload.get("artifacts") or [])
        await self.tracker.post(ref, texts.result(state.label, payload, attached, failed))
        await self._end(ref, state)
        log.info("%s ended: %s", state.task_id, payload.get("state"))

    async def _rejected(self, ref: ItemRef, state: ItemState, reason: str) -> None:
        if reason.startswith(DUPLICATE) and state.retries < MAX_RETRIES and state.project:
            state.retries += 1
            log.info("%s was used before; trying the next task id", state.task_id)
            try:
                item = await self.tracker.item(ref)
            except ItemGone:
                return
            await self._start(item, state, state.project, retry=True)
            return
        await self.tracker.post(ref, texts.rejected(reason))
        await self._end(ref, state)
        log.info("%s was rejected: %s", state.task_id, reason)

    async def _upload_artifacts(self, ref: ItemRef, artifacts: list[dict]) -> tuple[list[str], list[str]]:
        attached: list[str] = []
        failed: list[str] = []
        for artifact in artifacts:
            name = str(artifact.get("name") or "artifact")
            try:
                response = await self.downloads.get(str(artifact.get("url")))
                response.raise_for_status()
                await self.tracker.attach(ref, name, artifact.get("content_type") or "application/octet-stream",
                                          response.content)
                attached.append(name)
            except Exception as exc:  # noqa: BLE001 - named in the comment, the rest go on
                log.warning("could not attach %s to %s: %s", name, ref.key, exc or type(exc).__name__)
                failed.append(name)
        return attached, failed

    async def _end(self, ref: ItemRef, state: ItemState) -> None:
        state.state = "ended"
        state.request_id = None
        state.asked_at = None
        state.retries = 0
        await self._save(ref, state)
        await self._hand_back(ref)

    async def _hand_back(self, ref: ItemRef) -> None:
        try:
            await self.tracker.hand_back(ref)
        except Exception as exc:  # noqa: BLE001 - the run's outcome is already posted
            log.warning("could not hand %s back: %s", ref.key, exc or type(exc).__name__)
