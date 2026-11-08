"""What a bridge implements: the tracker interface and its data types (design §4)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class TrackerProblem(Exception):
    """The tracker cannot be used (bad credentials, missing permissions). ``start`` raises
    it; its message is reported in the bridge's status."""


class ItemGone(Exception):
    """The work item no longer exists, or the bot can no longer see it."""


@dataclass(frozen=True)
class ItemRef:
    id: str
    """Stable: survives renames and moves (Jira's issue id)."""
    key: str
    """What people see, and the base of task ids (``PLANE-101``)."""


@dataclass(frozen=True)
class Person:
    id: str
    name: str


@dataclass(frozen=True)
class Comment:
    author: str | None
    created: datetime | None
    body: str
    """Markdown."""


@dataclass(frozen=True)
class AttachmentInfo:
    id: str
    name: str
    content_type: str
    size: int
    created: datetime | None = None


@dataclass(frozen=True)
class Reply:
    """A comment that answers the bot's question: written by a person after the question
    and addressed to the bot the tracker's way."""

    author: Person | None
    created: datetime | None
    body: str
    """Markdown, the bot's mention removed."""
    attachments: tuple[AttachmentInfo, ...] = ()
    """Added to the item since the question."""


@dataclass
class Item:
    ref: ItemRef
    scope: str
    """How the tracker groups work (a Jira space key): what bridge files claim."""
    title: str
    body: str
    """Markdown."""
    url: str
    reporter: Person | None
    """``None`` when unknown, or when it is the bot itself."""
    assigned_to_bot: bool
    attachments: list[AttachmentInfo] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    """Tracker-specific facts for the task's context: type, priority, labels, links, ..."""


# -- messages the kit posts -----------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class Mention:
    person: Person


@dataclass(frozen=True)
class Paragraph:
    """One line of the kit's own words, with emphasis and mentions."""

    parts: tuple[Span | Mention, ...]


@dataclass(frozen=True)
class Markdown:
    """Text written by the agent (or a list the kit builds), as Markdown blocks."""

    text: str


@dataclass(frozen=True)
class Message:
    """What the kit posts; each tracker renders it in its own format."""

    blocks: tuple[Paragraph | Markdown, ...]

    def plain(self) -> str:
        """Markdown-ish text, for logs, tests and trackers that speak Markdown:
        ``**bold**``, ``*italic*``, ``@Name`` for a mention, blocks apart by a blank line."""
        out = []
        for block in self.blocks:
            if isinstance(block, Markdown):
                out.append(block.text.strip())
                continue
            line = ""
            for part in block.parts:
                if isinstance(part, Mention):
                    line += f"@{part.person.name}"
                elif part.bold:
                    line += f"**{part.text}**"
                elif part.italic:
                    line += f"*{part.text}*"
                else:
                    line += part.text
            out.append(line)
        return "\n\n".join(b for b in out if b)


# -- the interface ----------------------------------------------------------------------------


@runtime_checkable
class Tracker(Protocol):
    bridge: str
    """The bridge's name, as in ``bridges/<name>.yaml`` and the token's bridge."""
    instance: str
    """Which tracker (``https://acme.atlassian.net``), carried in every task's correlation."""
    config_model: type[BaseModel]
    """The schema of ``bridges/<bridge>.yaml``."""

    def claims(self, config: BaseModel) -> Iterable[str]:
        """The scopes a valid bridge file claims."""

    async def start(self) -> None:
        """Check credentials and learn the bot's identity; raise ``TrackerProblem``."""

    async def item(self, ref: ItemRef) -> Item: ...

    async def download(self, attachment: AttachmentInfo) -> bytes: ...

    async def post(self, ref: ItemRef, message: Message) -> datetime | None:
        """Post a comment; the time the tracker gave it, when it says."""

    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None: ...

    async def hand_back(self, ref: ItemRef) -> None:
        """Give the item back to a person (its reporter), if it is still with the bot."""

    async def load_state(self, ref: ItemRef) -> dict[str, Any] | None: ...

    async def save_state(self, ref: ItemRef, state: dict[str, Any]) -> None: ...

    async def assigned_to_bot(self) -> list[ItemRef]: ...

    async def replies(self, ref: ItemRef, since: datetime | None) -> list[Reply]:
        """Comments that answer the bot, written after ``since``, oldest first."""
