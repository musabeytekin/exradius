"""The kit's tracker over Jira Cloud REST v3 (design §4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from exradius_bridge_kit import (
    AttachmentInfo,
    Comment,
    Item,
    ItemGone,
    ItemRef,
    Markdown,
    Mention,
    Message,
    Paragraph,
    Person,
    Reply,
    Span,
    TrackerProblem,
)

from exradius_jira import adf
from exradius_jira.config import JiraConfig
from exradius_jira.jira import Jira, JiraError, parse_time

PROPERTY = "exradius"
"""The issue property holding the kit's state: hidden from people, moves with the issue."""
ISSUE_FIELDS = (
    "summary", "description", "issuetype", "priority", "status", "labels", "components", "project",
    "reporter", "assignee", "created", "parent", "issuelinks", "attachment",
)


def _named(value: Any) -> str | None:
    return value.get("name") if isinstance(value, dict) else None


def _account(value: Any) -> str | None:
    return value.get("accountId") if isinstance(value, dict) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def render(message: Message) -> adf.Node:
    """The kit's message as an ADF document; mentions become ADF mention nodes."""
    blocks: list[adf.Node] = []
    for block in message.blocks:
        if isinstance(block, Markdown):
            blocks += adf.from_markdown(block.text)
            continue
        inline: list[adf.Node | None] = []
        for part in block.parts:
            if isinstance(part, Mention):
                inline.append(adf.mention(part.person.id, part.person.name))
            else:
                marks = ([{"type": "strong"}] if part.bold else []) + ([{"type": "em"}] if part.italic else [])
                inline.append(adf.text(part.text, *marks))
        blocks.append(adf.paragraph(*[n for n in inline if n]))
    return adf.doc(*blocks)


def _links(items: list[dict]) -> list[dict[str, Any]]:
    links = []
    for link in items:
        kind = link.get("type") or {}
        if "outwardIssue" in link:
            other, relation = link["outwardIssue"], kind.get("outward")
        elif "inwardIssue" in link:
            other, relation = link["inwardIssue"], kind.get("inward")
        else:
            continue
        fields = other.get("fields") or {}
        links.append({"relation": relation, "key": other.get("key"), "summary": fields.get("summary"),
                      "status": _named(fields.get("status"))})
    return links


class JiraTracker:
    bridge = "jira"
    config_model = JiraConfig

    def __init__(self, jira: Jira):
        self.jira = jira
        self.instance = jira.site
        self.bot: Person | None = None

    def claims(self, config: JiraConfig) -> Iterable[str]:
        return config.projects

    @property
    def _bot(self) -> Person:
        assert self.bot is not None, "start() first"
        return self.bot

    async def start(self) -> None:
        try:
            me = await self.jira.myself()
        except JiraError as exc:
            if exc.status in (401, 403):
                raise TrackerProblem(f"Jira refused the bot account (JIRA_BOT_EMAIL, JIRA_BOT_TOKEN): "
                                     f"{exc.status} {exc.detail}") from exc
            raise TrackerProblem(f"cannot reach Jira at {self.jira.site}: {exc}") from exc
        self.bot = Person(str(me["accountId"]), str(me.get("displayName") or "the bot account"))

    def _person(self, value: Any) -> Person | None:
        account = _account(value)
        if not account or account == self._bot.id:
            return None
        return Person(account, str(value.get("displayName") or "someone"))

    @staticmethod
    def _attachments(fields: dict) -> list[AttachmentInfo]:
        return [
            AttachmentInfo(id=str(a["id"]), name=str(a.get("filename") or "attachment"),
                           content_type=a.get("mimeType") or "application/octet-stream",
                           size=int(a.get("size") or 0), created=_time(a.get("created")))
            for a in fields.get("attachment") or [] if isinstance(a, dict) and a.get("id")
        ]

    async def _issue(self, ref: ItemRef, fields: Iterable[str]) -> dict:
        try:
            return await self.jira.issue(ref.id, fields)
        except JiraError as exc:
            if exc.status == 404:
                raise ItemGone(ref.key) from exc
            raise

    async def item(self, ref: ItemRef) -> Item:
        issue = await self._issue(ref, ISSUE_FIELDS)
        fields = issue.get("fields") or {}
        key = str(issue.get("key") or ref.key)
        project = (fields.get("project") or {}).get("key") or key.rsplit("-", 1)[0]
        details: dict[str, Any] = {
            "type": _named(fields.get("issuetype")),
            "priority": _named(fields.get("priority")),
            "status": _named(fields.get("status")),
            "labels": list(fields.get("labels") or []),
            "components": [c.get("name") for c in fields.get("components") or [] if isinstance(c, dict)],
            "created": fields.get("created"),
        }
        parent = fields.get("parent")
        if isinstance(parent, dict) and parent.get("key"):
            details["parent"] = {"key": parent["key"], "summary": (parent.get("fields") or {}).get("summary")}
        links = _links(fields.get("issuelinks") or [])
        if links:
            details["links"] = links
        comments = [
            Comment(author=(c.get("author") or {}).get("displayName"), created=_time(c.get("created")),
                    body=adf.to_markdown(c.get("body")))
            for c in await self.jira.comments(str(issue.get("id") or ref.id))
        ]
        return Item(
            ref=ItemRef(id=str(issue.get("id") or ref.id), key=key),
            scope=str(project),
            title=str(fields.get("summary") or ""),
            body=adf.to_markdown(fields.get("description")),
            url=f"{self.jira.site}/browse/{key}",
            reporter=self._person(fields.get("reporter")),
            assigned_to_bot=_account(fields.get("assignee")) == self._bot.id,
            attachments=self._attachments(fields),
            comments=comments,
            details=details,
        )

    async def download(self, attachment: AttachmentInfo) -> bytes:
        return await self.jira.download(attachment.id)

    async def post(self, ref: ItemRef, message: Message) -> datetime | None:
        comment = await self.jira.add_comment(ref.id, render(message))
        return _time((comment or {}).get("created"))

    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None:
        await self.jira.upload(ref.id, name, content_type, data)

    async def hand_back(self, ref: ItemRef) -> None:
        """To the reporter, unless someone else took the issue meanwhile; unassigned when
        the reporter cannot be assigned."""
        issue = await self._issue(ref, ("assignee", "reporter"))
        fields = issue.get("fields") or {}
        if _account(fields.get("assignee")) != self._bot.id:
            return
        reporter = self._person(fields.get("reporter"))
        if reporter is None:
            await self.jira.assign(ref.id, None)
            return
        try:
            await self.jira.assign(ref.id, reporter.id)
        except JiraError:
            await self.jira.assign(ref.id, None)

    async def load_state(self, ref: ItemRef) -> dict[str, Any] | None:
        value = await self.jira.get_property(ref.id, PROPERTY)
        return value if isinstance(value, dict) else None

    async def save_state(self, ref: ItemRef, state: dict[str, Any]) -> None:
        await self.jira.set_property(ref.id, PROPERTY, state)

    async def assigned_to_bot(self) -> list[ItemRef]:
        issues = await self.jira.search(f'assignee = "{self._bot.id}" ORDER BY updated DESC', ("key",))
        return [ItemRef(id=str(i["id"]), key=str(i["key"])) for i in issues]

    async def replies(self, ref: ItemRef, since: datetime | None) -> list[Reply]:
        """Comments by people after ``since`` whose ADF mentions the bot; fetched through
        REST, never trusted from the webhook."""
        replies = []
        attachments: list[AttachmentInfo] | None = None
        for comment in await self.jira.comments(ref.id):
            author = comment.get("author")
            created = _time(comment.get("created"))
            if _account(author) == self._bot.id or (since is not None and (created is None or created <= since)):
                continue
            if not adf.mentions(comment.get("body"), self._bot.id):
                continue
            if attachments is None:
                fields = (await self._issue(ref, ("attachment",))).get("fields") or {}
                attachments = [a for a in self._attachments(fields)
                               if since is None or (a.created is not None and a.created > since)]
            replies.append(Reply(
                author=self._person(author), created=created,
                body=adf.to_markdown(comment.get("body"), skip_mention=self._bot.id),
                attachments=tuple(attachments),
            ))
        return replies
