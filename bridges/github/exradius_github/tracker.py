"""The kit's tracker over GitHub issues, acting as a bot account (design §4)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

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
from exradius_github import uploads
from exradius_github.github import GitHub, GitHubError, parse_time
from exradius_github.store import PullRequestRow, Store, match_task

_MARKER = re.compile(r"\n*<!-- exradius:([A-Za-z0-9_=-]+) -->\s*$")
STARTING = "Exradius is picking an agent for this."
"""The comment that carries the state when the bot has not commented on the issue yet."""
INLINE_LIMIT = 50_000
"""Characters of text files inlined in one comment (GitHub's limit is 65,536)."""
ARTIFACTS_BRANCH = "exradius-artifacts"
MAX_FILE_BYTES = 50 * 1024 * 1024
"""Past this, GitHub's API refuses a blob; it also keeps the branch reasonable."""
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
BRANCH_README = """# Exradius artifacts

Files that Exradius agents produced for this repository's issues, one folder per issue,
committed by the GitHub bridge so the issue's comments can show them (GitHub issues have
no attachments API). Nothing in the repository depends on this branch: it can be deleted
at any time, and the bridge creates it again when it next needs it.
"""


@dataclass(frozen=True)
class _Pending:
    """A file for the item's next comment: text to inline, or a file on the artifacts branch."""

    name: str
    text: str | None = None
    url: str | None = None


def ref_for(repo: str, number: int) -> ItemRef:
    """``id`` is ``owner/repo#12``, the address of the issue; ``key`` is ``repo-12``, the
    base of task ids and so of branch names."""
    return ItemRef(id=f"{repo.lower()}#{number}", key=f"{repo.split('/')[-1].lower()}-{number}")


def ref_for_pull_request(repo: str, number: int) -> ItemRef:
    """A pull request as an item of its own (design §11): its review runs and all their
    comments stay on it. GitHub numbers issues and pull requests together, so the ``id`` is
    as unique as an issue's; the ``key`` (``repo-pr-5``) keeps its task ids apart."""
    return ItemRef(id=f"{repo.lower()}#{number}", key=f"{repo.split('/')[-1].lower()}-pr-{number}")


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

    def __init__(self, github: GitHub, instance: str, *, artifacts_branch: str = ARTIFACTS_BRANCH,
                 store: Store | None = None):
        self.github = github
        self.instance = instance
        self.artifacts_branch = artifacts_branch
        self.store = store
        """Tasks and pull requests (design §10); without it, reviews are not followed."""
        self.bot: Person | None = None
        self._pending: dict[str, list[_Pending]] = {}
        """Files handed to ``attach``, shown in the item's next comment (the result's)."""

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

    async def _issue(self, ref: ItemRef, *, full: bool = False) -> dict:
        repo, number = parse_ref(ref)
        try:
            return await self.github.issue(repo, number, full=full)
        except GitHubError as exc:
            if exc.status in (404, 410):
                raise ItemGone(ref.key) from exc
            raise

    async def item(self, ref: ItemRef) -> Item:
        issue = await self._issue(ref, full=True)
        repo = str(issue.get("repository_url") or "").split("/repos/", 1)[-1] or parse_ref(ref)[0]
        number = int(issue.get("number") or parse_ref(ref)[1])
        is_pull = "pull_request" in issue
        current = ref_for_pull_request(repo, number) if is_pull else ref_for(repo, number)
        repo_name, _ = parse_ref(current)
        reporter = self._person(issue.get("user"))
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
        if is_pull:
            pull = await self.github.pull_request(repo_name, number)
            details["pull_request"] = {
                "branch": (pull.get("head") or {}).get("ref"),
                "base": (pull.get("base") or {}).get("ref"),
                "state": "merged" if pull.get("merged") else pull.get("state"),
            }
            row = await self.store.pull_request(repo_name, number) if self.store else None
            if row is not None:
                details["pull_request"].update({"task": row.task_id, "issue": row.item_id})
                if row.reviewer:
                    # Questions go to whoever asked for the changes, not to the PR's author.
                    reporter = Person(row.reviewer, row.reviewer)
        # Files people dropped into the issue or its comments become the task's attachments,
        # and their links name them (design §4.2).
        used: set[str] = set()
        counter = [0]
        body, files = uploads.find(str(issue.get("body") or ""), issue.get("body_html"),
                                   _time(issue.get("created_at")), self.github.web_host, used, counter)
        comments = []
        for c in await self.github.comments(repo_name, number, full=True):
            created = _time(c.get("created_at"))
            text = strip_state(c.get("body"))
            if not self.is_bot(_login(c.get("user"))):
                text, found = uploads.find(text, c.get("body_html"), created, self.github.web_host, used, counter)
                files += found
            comments.append(Comment(author=_login(c.get("user")), created=created, body=text))
        return Item(
            ref=current,
            scope=repo_name,
            title=str(issue.get("title") or ""),
            body=body,
            url=str(issue.get("html_url") or ""),
            reporter=reporter,
            assigned_to_bot=any(self.is_bot(_login(a)) for a in issue.get("assignees") or []),
            attachments=[f.info for f in files],
            comments=comments,
            details=details,
        )

    async def download(self, attachment: AttachmentInfo) -> bytes:
        """``id`` is the address ``uploads.find`` chose: a signed one, or the link itself."""
        return await self.github.download_upload(attachment.id)

    async def post(self, ref: ItemRef, message: Message) -> datetime | None:
        body = message.plain()
        for file in self._pending.pop(ref.id, []):
            if file.url is not None:
                image = any(file.name.lower().endswith(ext) for ext in IMAGES)
                body += f"\n\n**{file.name}**\n\n" + (f"![{file.name}]({file.url})" if image else f"[Open]({file.url})")
            else:
                text = file.text or ""
                fence = "````" if "```" in text else "```"
                body += f"\n\n<details><summary>{file.name}</summary>\n\n{fence}\n{text.rstrip()}\n{fence}\n\n</details>"
        repo, number = parse_ref(ref)
        comment = await self.github.add_comment(repo, number, body)
        return _time((comment or {}).get("created_at"))

    async def attach(self, ref: ItemRef, name: str, content_type: str, data: bytes) -> None:
        """GitHub issues take no attachments (design §4.1). A text file that fits goes into
        the next comment (the result's); anything else is committed to the artifacts branch
        and shown there: an image as an image, the rest as a link."""
        pending = self._pending.setdefault(ref.id, [])
        text = _is_text(content_type, data)
        inlined = sum(len(f.text) for f in pending if f.text is not None)
        if text is not None and inlined + len(text) <= INLINE_LIMIT:
            pending.append(_Pending(name, text=text))
            return
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        pending.append(_Pending(name, url=await self._commit_artifact(ref, name, data)))

    async def _commit_artifact(self, ref: ItemRef, name: str, data: bytes) -> str:
        """One commit on the artifacts branch, made from the bot's token with the git data API
        (an orphan branch the first time); the link is pinned to that commit, so a later run's
        file of the same name does not change an earlier comment."""
        repo, _ = parse_ref(ref)
        path = f"{ref.key}/{name.replace('/', '_')}"
        blob = await self.github.create_blob(repo, base64.b64encode(data).decode())
        for _attempt in range(3):
            head = await self.github.branch_head(repo, self.artifacts_branch)
            if head is None:
                readme = await self.github.create_blob(repo, base64.b64encode(BRANCH_README.encode()).decode())
                tree = await self.github.create_tree(repo, {"README.md": readme, path: blob}, None)
                commit = await self.github.create_commit(repo, f"{ref.key}: {name}", tree, [])
                try:
                    await self.github.create_branch(repo, self.artifacts_branch, commit)
                except GitHubError as exc:
                    if exc.status == 422:  # created meanwhile: go again on top of it
                        continue
                    raise
            else:
                tree = await self.github.create_tree(repo, {path: blob}, await self.github.commit_tree(repo, head))
                commit = await self.github.create_commit(repo, f"{ref.key}: {name}", tree, [head])
                try:
                    await self.github.move_branch(repo, self.artifacts_branch, commit)
                except GitHubError as exc:
                    if exc.status == 422:  # moved meanwhile: go again on top of it
                        continue
                    raise
            return f"{self.instance}/{repo}/blob/{commit}/{quote(path)}?raw=true"
        raise ValueError(f"the {self.artifacts_branch} branch kept moving while {name} was committed")

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
        if self.store is not None and state.get("state") == "starting" and state.get("task_id"):
            # Remembered so a pull request whose branch starts with the id finds its way back.
            await self.store.record_task(str(state["task_id"]), repo, ref.id, ref.key)
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
        for comment in await self.github.comments(repo, number, full=True):
            author = comment.get("user")
            created = _time(comment.get("created_at"))
            if self.is_bot(_login(author)) or (since is not None and (created is None or created <= since)):
                continue
            body = strip_state(comment.get("body"))
            if not mention.search(body):
                continue
            body, files = uploads.find(body, comment.get("body_html"), created, self.github.web_host, set(), [0])
            text = mention.sub("", body)
            # A Quote reply starts with the quoted question; the answer is what follows.
            unquoted = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
            text = re.sub(r"[ \t]{2,}", " ", unquoted if unquoted.strip() else text).strip()
            replies.append(Reply(author=self._person(author), created=created, body=text,
                                 attachments=tuple(f.info for f in files)))
        return replies

    # -- pull requests (design §11) ---------------------------------------------------------

    async def _trace(self, repo: str, number: int, branch: str | None, head_repo: str | None) -> PullRequestRow | None:
        """The task a pull request's branch belongs to, recorded; ``None`` when it is not one
        of this bridge's (or comes from a fork)."""
        if self.store is None or not branch or (head_repo or "").lower() != repo.lower():
            return None
        tasks = await self.store.tasks_in(repo)
        task_id = match_task(branch, [t.task_id for t in tasks])
        if task_id is None:
            return None
        task = next(t for t in tasks if t.task_id == task_id)
        return await self.store.record_pull_request(repo, number, task, branch)

    async def pull_request_opened(self, repo: str, pull: dict) -> PullRequestRow | None:
        head = pull.get("head") or {}
        return await self._trace(repo, int(pull["number"]), head.get("ref"), (head.get("repo") or {}).get("full_name"))

    async def known_pull_request(self, repo: str, number: int) -> bool:
        return self.store is not None and await self.store.pull_request(repo, number) is not None

    async def review_request(self, repo: str, number: int, review: dict) -> tuple[ItemRef, Reply, str] | None:
        """A review that requested changes on one of the bridge's pull requests, as a request
        for a run on the pull request (kit §6.5): the review's text and its comments on the
        code. ``None`` for a pull request the bridge did not trace to a task."""
        if self.store is None:
            return None
        row = await self.store.pull_request(repo, number)
        if row is None:  # opened before the bridge watched pull requests, or a missed event
            pull = await self.github.pull_request(repo, number)
            head = pull.get("head") or {}
            row = await self._trace(repo, number, head.get("ref"), (head.get("repo") or {}).get("full_name"))
        if row is None:
            return None
        reviewer = _login(review.get("user")) or "a reviewer"
        await self.store.reviewed(repo, number, reviewer)
        lines = [str(review.get("body") or "").strip()]
        comments = await self.github.review_comments(repo, number, int(review["id"])) if review.get("id") else []
        if comments:
            lines.append("Comments on the code:")
            for c in comments:
                line = c.get("line") or c.get("original_line")
                where = f"`{c.get('path')}`" + (f" line {line}" if line else "")
                lines.append(f"- {where}: {str(c.get('body') or '').strip()}")
        body = "\n\n".join(part for part in lines if part) or "(no text)"
        request = Reply(author=Person(reviewer, reviewer), created=_time(review.get("submitted_at")), body=body)
        intro = (f"{reviewer} requested changes on pull request #{number} (branch `{row.head_branch}`), "
                 f"which task {row.task_id} opened for {row.item_key}. Continue on branch `{row.head_branch}`: "
                 "commit there and push it, so the pull request updates; do not open another pull request")
        return ref_for_pull_request(repo, number), request, intro
