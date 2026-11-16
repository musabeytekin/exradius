"""GitHub's repository or organization webhook: signature, repeats, dispatch to the kit
(design §5)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict

from exradius_bridge_kit import ItemRef, Kit
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from exradius_github.tracker import GitHubTracker, ref_for, ref_for_pull_request

log = logging.getLogger(__name__)


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """``X-Hub-Signature-256: sha256=<hex HMAC of the raw body>``."""
    if not header or "=" not in header:
        return False
    method, _, signature = header.partition("=")
    if method.strip().lower() != "sha256":
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class Webhooks:
    def __init__(self, secret: str, kit: Kit, tracker: GitHubTracker, *, remember: int = 4096):
        self.secret = secret
        self.kit = kit
        self.tracker = tracker
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._remember = remember
        self._work: set[asyncio.Task] = set()

    def _repeat(self, delivery: str) -> bool:
        """GitHub keeps ``X-GitHub-Delivery`` when a delivery is sent again."""
        if delivery in self._seen:
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return False

    async def dispatch(self, event: str, payload: dict) -> None:
        """What a delivery means: on an issue, a hand-over or a comment for the kit (§6); on
        one of the bridge's pull requests, a review that requested changes, or a comment that
        can only answer a question (§11)."""
        if self.tracker.bot is None:
            return
        repo = (payload.get("repository") or {}).get("full_name")
        action = payload.get("action")
        if not repo:
            return
        if event == "pull_request" and action in ("opened", "reopened"):
            row = await self.tracker.pull_request_opened(str(repo), payload.get("pull_request") or {})
            if row is not None:
                log.info("pull request %s#%d belongs to %s", repo, row.number, row.task_id)
            return
        if event == "pull_request_review" and action == "submitted":
            review = payload.get("review") or {}
            number = (payload.get("pull_request") or {}).get("number")
            if str(review.get("state") or "").lower() == "changes_requested" and number:
                self._spawn(self._review(str(repo), int(number), review))
            return
        issue = payload.get("issue") or {}
        if not issue.get("number"):
            return
        number = int(issue["number"])
        if event == "issues" and action == "assigned" and "pull_request" not in issue:
            # Opening an issue with the bot as an assignee sends this too.
            if self.tracker.is_bot((payload.get("assignee") or {}).get("login")):
                await self.kit.handed_over(ref_for(str(repo), number))
        elif event == "issue_comment" and action == "created":
            if self.tracker.is_bot(((payload.get("comment") or {}).get("user") or {}).get("login")):
                return
            if "pull_request" not in issue:
                await self.kit.commented(ref_for(str(repo), number))
            elif await self.tracker.known_pull_request(str(repo), number):
                await self.kit.commented(ref_for_pull_request(str(repo), number), follow_up=False)

    async def _review(self, repo: str, number: int, review: dict) -> None:
        try:
            found = await self.tracker.review_request(repo, number, review)
        except Exception:  # noqa: BLE001 - one review must not stop the listener
            log.exception("reading the review on %s#%d failed", repo, number)
            return
        if found is not None:
            await self.kit.requested(*found)
            log.info("changes requested on %s#%d", repo, number)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    async def idle(self) -> None:
        """Wait until every review being read is handed to the kit (tests, shutdown)."""
        while self._work:
            await asyncio.gather(*list(self._work), return_exceptions=True)
        await self.kit.idle()

    async def receive(self, request: Request) -> Response:
        body = await request.body()
        if not signature_ok(self.secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("refused a webhook delivery with a missing or wrong signature")
            return Response(status_code=401)
        delivery = request.headers.get("X-GitHub-Delivery")
        if delivery and self._repeat(delivery):
            return Response(status_code=200)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return PlainTextResponse("pong")
        await self.dispatch(event, payload)  # the kit queues the work and returns at once
        return Response(status_code=200)


def make_app(webhooks: Webhooks) -> Starlette:
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/webhooks/github", webhooks.receive, methods=["POST"]),
        Route("/healthz", health, methods=["GET"]),
    ])
