"""Fakes for testing the kit and the bridges built on it (design §11): a tracker, a broker
and the Exradius integration API, all in memory.

```python
tracker = FakeTracker()
exradius = FakeExradius(projects=[project_entry("plane", bridge_config={"projects": ["PLANE"]})])
kit = make_kit(tracker, exradius)
await kit.start()
```
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from exradius_bridge_kit.exradius import IntegrationApi
from exradius_bridge_kit.kit import Kit
from exradius_bridge_kit.settings import KitSettings
from exradius_bridge_kit.tracker import (
    AttachmentInfo,
    Comment,
    Item,
    ItemGone,
    ItemRef,
    Message,
    Person,
    Reply,
    TrackerProblem,
)

BOT = Person("bot", "AI Agent")
REPORTER = Person("u-ayse", "Ayşe Yılmaz")
SETTINGS = KitSettings(broker_url="amqp://fake/", api_url="https://exradius.test", integration_token="exr_test")


class ScopesConfig(BaseModel):
    """The fake tracker's bridge file: ``scopes: [PLANE]``."""

    model_config = ConfigDict(extra="forbid")
    scopes: list[str] = Field(min_length=1)


class Clock:
    """Tracker time: each call is a second later than the previous."""

    def __init__(self, start: datetime = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)):
        self._ticks = itertools.count()
        self.start = start

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=next(self._ticks))


@dataclass
class FakeItem:
    ref: ItemRef
    scope: str
    title: str = "Fix the estimate route"
    body: str = "The estimate endpoint returns 404."
    reporter: Person | None = REPORTER
    assignee: Person | None = None
    attachments: list[tuple[AttachmentInfo, bytes]] = field(default_factory=list)
    comments: list[tuple[Person, datetime, str, bool]] = field(default_factory=list)
    """(author, created, Markdown, addressed to the bot)."""
    details: dict[str, Any] = field(default_factory=dict)
    posted: list[Message] = field(default_factory=list)
    attached: list[tuple[str, str, bytes]] = field(default_factory=list)
    handed_back: int = 0
    state: dict[str, Any] | None = None


class FakeTracker:
    bridge = "fake"
    instance = "https://tracker.test"
    config_model = ScopesConfig

    def __init__(self, *, clock: Clock | None = None):
        self.items: dict[str, FakeItem] = {}
        self.clock = clock or Clock()
        self.problem: str | None = None
        """Set to make ``start`` raise ``TrackerProblem``."""
        self.failing: set[str] = set()
        """Method names that raise ``RuntimeError``."""
        self.calls: list[str] = []

    def claims(self, config: ScopesConfig) -> Iterable[str]:
        return config.scopes

    # -- building the world -------------------------------------------------------------------

    def add(self, key: str, *, scope: str | None = None, assigned: bool = True, **fields: Any) -> ItemRef:
        ref = ItemRef(id=str(10000 + len(self.items) + 1), key=key)
        self.items[ref.id] = FakeItem(ref=ref, scope=scope or key.split("-")[0],
                                      assignee=BOT if assigned else None, **fields)
        return ref

    def comment(self, ref: ItemRef, body: str, *, author: Person = REPORTER, to_bot: bool = True) -> datetime:
        created = self.clock()
        self.items[ref.id].comments.append((author, created, body, to_bot))
        return created

    def attach_file(self, ref: ItemRef, name: str, data: bytes, content_type: str = "text/plain") -> None:
        item = self.items[ref.id]
        info = AttachmentInfo(id=f"a{len(item.attachments) + 1}-{ref.id}", name=name, content_type=content_type,
                              size=len(data), created=self.clock())
        item.attachments.append((info, data))

    def texts(self, ref: ItemRef) -> list[str]:
        """What the bot posted on the item, as plain text."""
        return [m.plain() for m in self.items[ref.id].posted]

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failing:
            raise RuntimeError(f"{name} failed")

    def _item(self, ref: ItemRef) -> FakeItem:
        if ref.id not in self.items:
            raise ItemGone(ref.key)
        return self.items[ref.id]

    # -- the interface ------------------------------------------------------------------------

    async def start(self) -> None:
        self._check("start")
        if self.problem:
            raise TrackerProblem(self.problem)

    async def item(self, ref: ItemRef) -> Item:
        self._check("item")
        item = self._item(ref)
        return Item(
            ref=item.ref, scope=item.scope, title=item.title, body=item.body,
            url=f"{self.instance}/browse/{item.ref.key}", reporter=item.reporter,
            assigned_to_bot=item.assignee == BOT,
            attachments=[info for info, _ in item.attachments],
            comments=[Comment(author=a.name, created=c, body=b) for a, c, b, _ in item.comments],
            details=dict(item.details),
        )

    async def download(self, attachment: AttachmentInfo) -> bytes:
        self._check("download")
        for item in self.items.values():
            for info, data in item.attachments:
                if info.id == attachment.id:
                    return data
        raise ItemGone(attachment.id)

    async def post(self, ref: ItemRef, message: Message) -> datetime:
        self._check("post")
        item = self._item(ref)
        item.posted.append(message)
        created = self.clock()
        item.comments.append((BOT, created, message.plain(), False))
        return created

    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None:
        self._check("attach")
        self._item(ref).attached.append((name, content_type, data))

    async def hand_back(self, ref: ItemRef) -> None:
        self._check("hand_back")
        item = self._item(ref)
        if item.assignee == BOT:
            item.assignee = item.reporter
            item.handed_back += 1

    async def load_state(self, ref: ItemRef) -> dict[str, Any] | None:
        self._check("load_state")
        state = self._item(ref).state
        return json.loads(json.dumps(state)) if state is not None else None

    async def save_state(self, ref: ItemRef, state: dict[str, Any]) -> None:
        self._check("save_state")
        self._item(ref).state = json.loads(json.dumps(state))

    async def assigned_to_bot(self) -> list[ItemRef]:
        self._check("assigned_to_bot")
        return [i.ref for i in self.items.values() if i.assignee == BOT]

    async def replies(self, ref: ItemRef, since: datetime | None) -> list[Reply]:
        self._check("replies")
        item = self._item(ref)
        out = []
        for author, created, body, to_bot in item.comments:
            if author == BOT or not to_bot or (since is not None and created <= since):
                continue
            later = tuple(info for info, _ in item.attachments if since is None or (info.created and info.created > since))
            out.append(Reply(author=author, created=created, body=body, attachments=later))
        return out


class FakeBroker:
    """Records what the kit publishes; ``deliver`` hands it an event as the broker would."""

    def __init__(self):
        self.tasks: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.handler = None
        self.connected = False
        self.down = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def publish_task(self, message: dict[str, Any]) -> None:
        if self.down:
            raise ConnectionError("broker down")
        self.tasks.append(json.loads(json.dumps(message)))

    async def publish_answer(self, message: dict[str, Any]) -> None:
        if self.down:
            raise ConnectionError("broker down")
        self.answers.append(json.loads(json.dumps(message)))

    async def consume(self, handler) -> None:
        self.handler = handler

    async def deliver(self, event: dict[str, Any]) -> None:
        assert self.handler is not None, "consume() first"
        await self.handler(json.loads(json.dumps(event)))


def project_entry(
    slug: str,
    *,
    name: str | None = None,
    agents: dict[str, str] | None = None,
    bridge_config: dict[str, Any] | None = None,
    bridge_config_error: str | None = None,
    runnable: bool = True,
) -> dict[str, Any]:
    """One project of a bridge's directory (integration-api.md §4)."""
    agents = agents if agents is not None else {"developer": "Developer", "scout": "Scout"}
    return {
        "slug": slug, "name": name or slug.capitalize(), "runnable": runnable, "repositories": [],
        "agents": [{"slug": s, "name": n} for s, n in sorted(agents.items())],
        "bridge_config": None if bridge_config_error else (bridge_config or {}),
        "bridge_config_error": bridge_config_error,
    }


class FakeExradius:
    """The integration API: the directory with ETags, and the status reports; plus the
    artifact downloads of ``task.result`` under ``/artifacts/<name>``."""

    def __init__(self, projects: list[dict[str, Any]] | None = None, bridge: str = "fake"):
        self.bridge = bridge
        self.projects = list(projects or [])
        self.reports: list[dict[str, Any]] = []
        self.artifacts: dict[str, bytes] = {}
        self.directory_fetches = 0
        self.status_code = 204
        """What the status report answers."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {SETTINGS.integration_token}" and \
                not request.url.path.startswith("/artifacts/"):
            return httpx.Response(401, json={"detail": "unknown integration token"})
        if request.url.path == "/api/integration/v1/directory":
            self.directory_fetches += 1
            body = json.dumps({"bridge": self.bridge, "projects": self.projects}).encode()
            tag = f'"{hashlib.sha256(body).hexdigest()}"'
            if request.headers.get("if-none-match") == tag:
                return httpx.Response(304, headers={"ETag": tag})
            return httpx.Response(200, content=body, headers={"ETag": tag, "Content-Type": "application/json"})
        if request.url.path == "/api/integration/v1/status" and request.method == "PUT":
            self.reports.append(json.loads(request.content))
            return httpx.Response(self.status_code)
        if request.url.path.startswith("/artifacts/"):
            name = request.url.path.removeprefix("/artifacts/")
            if name in self.artifacts:
                return httpx.Response(200, content=self.artifacts[name])
            return httpx.Response(404)
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def artifact_url(self, name: str) -> str:
        return f"{SETTINGS.api_url}/artifacts/{name}?sig=x"


def make_kit(tracker: Any, exradius: FakeExradius, broker: FakeBroker | None = None, **kwargs: Any) -> Kit:
    """A ``Kit`` wired to the fakes."""
    transport = exradius.transport()
    return Kit(
        tracker,
        kwargs.pop("settings", SETTINGS),
        software=kwargs.pop("software", "fake-bridge 0.0.1"),
        broker=broker or FakeBroker(),
        api=IntegrationApi(SETTINGS.api_url, SETTINGS.integration_token, transport=transport),
        downloads=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


def event(type_: str, item: ItemRef, task_id: str, payload: dict[str, Any], *, agent: str | None = None,
          bridge: str = "fake", instance: str = "https://tracker.test", project: str = "plane") -> dict[str, Any]:
    """A task event as Exradius publishes it (architecture §4.3)."""
    return {
        "version": 1, "task_id": task_id, "run_id": "r-1", "project": project, "agent": agent,
        "correlation": {"bridge": bridge, "instance": instance, "item": {"id": item.id, "key": item.key}},
        "timestamp": "2026-11-08T10:00:00Z", "type": type_, "payload": payload,
    }
