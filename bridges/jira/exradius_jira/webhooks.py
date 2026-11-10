"""Jira's admin webhook: signature, repeats, dispatch to the kit (design §6)."""

from __future__ import annotations

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

from exradius_jira.tracker import JiraTracker

log = logging.getLogger(__name__)


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """``X-Hub-Signature: sha256=<hex HMAC of the raw body>``."""
    if not header or "=" not in header:
        return False
    method, _, signature = header.partition("=")
    if method.strip().lower() != "sha256":
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class Webhooks:
    def __init__(self, secret: str, kit: Kit, tracker: JiraTracker, *, remember: int = 4096):
        self.secret = secret
        self.kit = kit
        self.tracker = tracker
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._remember = remember

    def _repeat(self, delivery: str) -> bool:
        """Jira keeps ``X-Atlassian-Webhook-Identifier`` across its retries."""
        if delivery in self._seen:
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return False

    def dispatch(self, payload: dict) -> list[tuple[str, ItemRef]]:
        """What a delivery means for the kit: ``handed_over`` or ``commented``, per issue."""
        issue = payload.get("issue") or {}
        if not issue.get("id") or not issue.get("key") or self.tracker.bot is None:
            return []
        ref = ItemRef(id=str(issue["id"]), key=str(issue["key"]))
        bot = self.tracker.bot.id
        event = payload.get("webhookEvent")
        if event == "jira:issue_created":
            if ((issue.get("fields") or {}).get("assignee") or {}).get("accountId") == bot:
                return [("handed_over", ref)]
        elif event == "jira:issue_updated":
            items = (payload.get("changelog") or {}).get("items") or []
            if any(_is_assignee(i) and i.get("to") == bot for i in items):
                return [("handed_over", ref)]
        elif event == "comment_created":
            comment = payload.get("comment") or {}
            if (comment.get("author") or {}).get("accountId") != bot:
                return [("commented", ref)]
        return []

    async def receive(self, request: Request) -> Response:
        body = await request.body()
        if not signature_ok(self.secret, body, request.headers.get("X-Hub-Signature")):
            log.warning("refused a webhook delivery with a missing or wrong signature")
            return Response(status_code=401)
        delivery = request.headers.get("X-Atlassian-Webhook-Identifier")
        if delivery and self._repeat(delivery):
            return Response(status_code=200)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        for action, ref in self.dispatch(payload):
            await getattr(self.kit, action)(ref)  # returns at once; the kit queues it per issue
        return Response(status_code=200)


def _is_assignee(item: dict) -> bool:
    return item.get("fieldId") == "assignee" if item.get("fieldId") else item.get("field") == "assignee"


def make_app(webhooks: Webhooks) -> Starlette:
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/webhooks/jira", webhooks.receive, methods=["POST"]),
        Route("/healthz", health, methods=["GET"]),
    ])
