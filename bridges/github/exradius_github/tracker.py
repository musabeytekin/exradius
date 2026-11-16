"""The kit's tracker over GitHub issues, acting as a bot account (design §4)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from exradius_bridge_kit import (
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

from exradius_github.config import GitHubConfig
from exradius_github.github import GitHub, GitHubError, parse_time

_MARKER = re.compile(r"\n*<!-- exradius:([A-Za-z0-9_=-]+) -->\s*$")
STARTING = "Exradius is picking an agent for this."
"""The comment that carries the state when the bot has not commented on the issue yet."""
INLINE_LIMIT = 50_000
"""Characters of text files inlined in one comment (GitHub's limit is 65,536)."""


def ref_for(repo: str, number: int) -> ItemRef:
    """``id`` is ``owner/repo#12``, the address of the issue; ``key`` is ``repo-12``, the
    base of task ids and so of branch names."""
    return ItemRef(id=f"{repo.lower()}#{number}", key=f"{repo.split('/')[-1].lower()}-{number}")


def parse_ref(ref: ItemRef) -> tuple[str, int]:
    repo, _, number = ref.id.rpartition("#")
    return repo, int(number)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def _login(user: Any) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def strip_state(body: str | None) -> str:
    return _MARKER.sub("", body or "")


def encode_state(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), default=str).encode()
    return f"<!-- exradius:{base64.urlsafe_b64encode(raw).decode()} -->"


def decode_state(body: str | None) -> dict[str, Any] | None:
    found = _MARKER.search(body or "")
    if not found:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(found[1]))
    except (binascii.Error, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _is_text(content_type: str, data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class GitHubTracker:
    bridge = "github"
    config_model = GitHubConfig

    def __init__(self, github: GitHub, instance: str):
        self.github = github
        self.instance = instance
        self.bot: Person | None = None
        self._inline: dict[str, list[tuple[str, str]]] = {}
        """Text files handed to ``attach``, posted with the item's next comment."""

    def claims(self, config: GitHubConfig) -> Iterable[str]:
        return config.repositories

    @property
    def _bot(self) -> Person:
        assert self.bot is not None, "start() first"
        return self.bot

    def is_bot(self, login: str | None) -> bool:
        return login is not None and login.lower() == self._bot.id.lower()

    async def start(self) -> None:
        try:
            me = await self.github.me()
        except GitHubError as exc:
            if exc.status in (401, 403):
                raise TrackerProblem(f"GitHub refused the bot account's token (GITHUB_TOKEN): "
                                     f"{exc.status} {exc.detail}") from exc
            raise TrackerProblem(f"cannot reach GitHub at {self.github.api_url}: {exc}") from exc
        login = str(me["login"])
        self.bot = Person(login, login)

    def _person(self, user: Any) -> Person | None:
        login = _login(user)
        if not login or self.is_bot(login):
            return None
        return Person(login, login)

    async def _issue(self, ref: ItemRef) -> dict:
        repo, number = parse_ref(ref)
        try:
            return await self.github.issue(repo, number)
        except GitHubError as exc:
            if exc.status in (404, 410):
                raise ItemGone(ref.key) from exc
            raise

    async def item(self, ref: ItemRef) -> Item:
        issue = await self._issue(ref)
        repo = str(issue.get("repository_url") or "").split("/repos/", 1)[-1] or parse_ref(ref)[0]
        number = int(issue.get("number") or parse_ref(ref)[1])
        current = ref_for(repo, number)
        repo_name, _ = parse_ref(current)
        details: dict[str, Any] = {
            "repository": repo_name,
            "number": number,
            "state": issue.get("state"),
            "labels": [label.get("name") for label in issue.get("labels") or [] if isinstance(label, dict)],
            "assignees": [_login(a) for a in issue.get("assignees") or [] if _login(a)],
            "created": issue.get("created_at"),
        }
        if isinstance(issue.get("milestone"), dict):
            details["milestone"] = issue["milestone"].get("title")
        comments = [
            Comment(author=_login(c.get("user")), created=_time(c.get("created_at")), body=strip_state(c.get("body")))
            for c in await self.github.comments(repo_name, number)
        ]
        return Item(
            ref=current,
            scope=repo_name,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            url=str(issue.get("html_url") or ""),
            reporter=self._person(issue.get("user")),
            assigned_to_bot=any(self.is_bot(_login(a)) for a in issue.get("assignees") or []),
            # GitHub has no issue attachments: files people add are links in the Markdown.
            attachments=[],
            comments=comments,
            details=details,
        )

    async def download(self, attachment: AttachmentInfo) -> bytes:
        raise ValueError(f"GitHub issues have no attachments to download ({attachment.name})")

    async def post(self, ref: ItemRef, message: Message) -> datetime | None:
        body = message.plain()
        for name, text in self._inline.pop(ref.id, []):
            fence = "````" if "```" in text else "```"
            body += f"\n\n<details><summary>{name}</summary>\n\n{fence}\n{text.rstrip()}\n{fence}\n\n</details>"
        repo, number = parse_ref(ref)
        comment = await self.github.add_comment(repo, number, body)
        return _time((comment or {}).get("created_at"))

    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None:
        """GitHub issues take no attachments: a text file goes into the next comment (the
        result's), anything else is named there as not attached."""
        text = _is_text(content_type, data)
        if text is None:
            raise ValueError(f"{name} is not a text file, and GitHub issues take no attachments")
        pending = self._inline.setdefault(ref.id, [])
        if sum(len(t) for _, t in pending) + len(text) > INLINE_LIMIT:
            raise ValueError(f"{name} is too long to put in a comment")
        pending.append((name, text))

    async def hand_back(self, ref: ItemRef) -> None:
        """To the issue's author, unless the bot no longer has the issue; GitHub leaves the
        author out when they cannot be assigned, and the issue is then unassigned."""
        issue = await self._issue(ref)
        assignees = [_login(a) for a in issue.get("assignees") or []]
        bot = next((a for a in assignees if self.is_bot(a)), None)
        if bot is None:
            return
        repo, number = parse_ref(ref)
        author = self._person(issue.get("user"))
        if author is not None and author.id not in assignees:
            try:
                await self.github.add_assignees(repo, number, [author.id])
            except GitHubError:
                pass
        await self.github.remove_assignees(repo, number, [bot])

    async def _bot_comments(self, ref: ItemRef) -> list[dict]:
        repo, number = parse_ref(ref)
        return [c for c in await self.github.comments(repo, number) if self.is_bot(_login(c.get("user")))]

    async def load_state(self, ref: ItemRef) -> dict[str, Any] | None:
        """The state in the bot's latest comment that carries one."""
        for comment in reversed(await self._bot_comments(ref)):
            state = decode_state(comment.get("body"))
            if state is not None:
                return state
        return None

    async def save_state(self, ref: ItemRef, state: dict[str, Any]) -> None:
        """Into the bot's latest comment, as a hidden marker that never renders; a first
        comment is posted when the bot has none on the issue yet."""
        repo, number = parse_ref(ref)
        mine = await self._bot_comments(ref)
        if mine:
            latest = mine[-1]
            body = f"{strip_state(latest.get('body')).rstrip()}\n\n{encode_state(state)}"
            await self.github.edit_comment(repo, int(latest["id"]), body)
        else:
            await self.github.add_comment(repo, number, f"{STARTING}\n\n{encode_state(state)}")

    async def assigned_to_bot(self) -> list[ItemRef]:
        refs = []
        for issue in await self.github.assigned():
            if "pull_request" in issue:
                continue
            repo = (issue.get("repository") or {}).get("full_name") or \
                str(issue.get("repository_url") or "").split("/repos/", 1)[-1]
            if repo and issue.get("number"):
                refs.append(ref_for(repo, int(issue["number"])))
        return refs

    def _mention(self) -> re.Pattern[str]:
        return re.compile(rf"(?<![\w@/-])@{re.escape(self._bot.id)}(?![\w-])", re.IGNORECASE)

    async def replies(self, ref: ItemRef, since: datetime | None) -> list[Reply]:
        """Comments by people after ``since`` that mention the bot, read through the API
        (never trusted from the webhook); the mention is taken out of the text."""
        repo, number = parse_ref(ref)
        mention = self._mention()
        replies = []
        for comment in await self.github.comments(repo, number):
            author = comment.get("user")
            created = _time(comment.get("created_at"))
            if self.is_bot(_login(author)) or (since is not None and (created is None or created <= since)):
                continue
            body = strip_state(comment.get("body"))
            if not mention.search(body):
                continue
            text = mention.sub("", body)
            # A Quote reply starts with the quoted question; the answer is what follows.
            unquoted = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
            text = re.sub(r"[ \t]{2,}", " ", unquoted if unquoted.strip() else text).strip()
            replies.append(Reply(author=self._person(author), created=created, body=text))
        return replies
