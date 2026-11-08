"""The GitHub App's webhook: signature, repeats, dispatch (design §6)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from exradius_github.flow import Bridge, IssueRef

log = logging.getLogger(__name__)

Action = Callable[[], Awaitable[None]]


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """``X-Hub-Signature-256: sha256=<hex HMAC of the raw body>``."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


class Webhooks:
    def __init__(self, secret: str, bridge: Bridge, *, remember: int = 4096):
        self.secret = secret
        self.bridge = bridge
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._remember = remember
        self._tasks: set[asyncio.Task] = set()

    def _repeat(self, delivery: str) -> bool:
        if delivery in self._seen:
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return False

    def actions(self, event: str, payload: dict) -> list[Action]:
        bridge = self.bridge
        if event in ("installation", "installation_repositories"):
            return [bridge.installations_changed]
        if event not in ("issues", "issue_comment"):
            return []
        issue = payload.get("issue") or {}
        repository = (payload.get("repository") or {}).get("full_name")
        installation = (payload.get("installation") or {}).get("id")
        if not repository or not installation or not issue.get("number") or "pull_request" in issue:
            return []
        bridge.app.remember_installation(repository, int(installation))
        ref = IssueRef(str(repository), int(issue["number"]), int(installation))
        sender = (payload.get("sender") or {}).get("login")
        action = payload.get("action")
        if event == "issues" and action == "labeled" and sender != bridge.bot:
            label = payload.get("label") or {}
            return [lambda: bridge.labeled(ref, label, sender)]
        if event == "issue_comment" and action == "created":
            comment = payload.get("comment") or {}
            if comment.get("id") and (comment.get("user") or {}).get("login") != bridge.bot:
                return [lambda: bridge.comment_created(ref, comment)]
        return []

    async def receive(self, request: Request) -> Response:
        body = await request.body()
        if not signature_ok(self.secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("refused a webhook delivery with a missing or wrong signature")
            return Response(status_code=401)
        delivery = request.headers.get("X-GitHub-Delivery")
        if delivery and self._repeat(delivery):
            return Response(status_code=200)
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return PlainTextResponse("pong")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        for action in self.actions(event, payload):
            self._spawn(action)
        return Response(status_code=200)

    def _spawn(self, action: Action) -> None:
        async def run() -> None:
            try:
                await action()
            except Exception:
                log.exception("handling a webhook delivery failed")

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def make_app(webhooks: Webhooks) -> Starlette:
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/webhooks/github", webhooks.receive, methods=["POST"]),
            Route("/healthz", health, methods=["GET"]),
        ]
    )
