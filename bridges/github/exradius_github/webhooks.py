"""GitHub's repository or organization webhook: signature, repeats, dispatch to the kit
(design §5)."""

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

from exradius_github.tracker import GitHubTracker, ref_for

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

    def _repeat(self, delivery: str) -> bool:
        """GitHub keeps ``X-GitHub-Delivery`` when a delivery is sent again."""
        if delivery in self._seen:
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        return False

    def dispatch(self, event: str, payload: dict) -> list[tuple[str, ItemRef]]:
        """What a delivery means for the kit: ``handed_over`` or ``commented``, per issue.
        Pull requests are left out."""
        issue = payload.get("issue") or {}
        repo = (payload.get("repository") or {}).get("full_name")
        if not repo or not issue.get("number") or "pull_request" in issue or self.tracker.bot is None:
            return []
        ref = ref_for(str(repo), int(issue["number"]))
        action = payload.get("action")
        if event == "issues" and action == "assigned":
            # Opening an issue with the bot as an assignee sends this too.
            if self.tracker.is_bot((payload.get("assignee") or {}).get("login")):
                return [("handed_over", ref)]
        elif event == "issue_comment" and action == "created":
            if not self.tracker.is_bot(((payload.get("comment") or {}).get("user") or {}).get("login")):
                return [("commented", ref)]
        return []

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
        for action, ref in self.dispatch(event, payload):
            await getattr(self.kit, action)(ref)  # returns at once; the kit queues it per issue
        return Response(status_code=200)


def make_app(webhooks: Webhooks) -> Starlette:
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/webhooks/github", webhooks.receive, methods=["POST"]),
        Route("/healthz", health, methods=["GET"]),
    ])
