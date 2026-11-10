"""The webhook endpoint, end to end through the ASGI app."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from exradius_github.webhooks import Webhooks, make_app, signature_ok
from tests.conftest import INSTALLATION, World
from tests.fakes import BOT

SECRET = "hook-secret"


@pytest.fixture
async def hooks(world: World):
    webhooks = Webhooks(SECRET, world.bridge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(webhooks)), base_url="http://bridge") as client:
        yield webhooks, client


async def post(hooks, event: str, payload: dict, *, delivery: str = "d-1") -> httpx.Response:
    webhooks, client = hooks
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
    response = await client.post("/webhooks/github", content=body, headers=headers)
    await webhooks.drain()
    return response


def labeled(world: World, number: int, label: dict, *, sender: str = "octocat", pull: bool = False) -> dict:
    issue = dict(world.github.repos["acme/plane"]["issues"][number])
    if pull:
        issue["pull_request"] = {}
    return {
        "action": "labeled", "issue": issue, "label": label, "sender": {"login": sender},
        "repository": {"full_name": "acme/plane"}, "installation": {"id": INSTALLATION},
    }


def test_signatures():
    body = b"{}"
    good = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert signature_ok("s", body, good)
    assert not signature_ok("s", b"{ }", good) and not signature_ok("s", body, None) and not signature_ok("s", body, "sha1=00")


async def test_unsigned_deliveries_are_refused_and_pings_answered(world: World, hooks):
    _, client = hooks
    assert (await client.post("/webhooks/github", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"})).status_code == 401
    assert (await post(hooks, "ping", {"zen": "Keep it logically awesome."})).text == "pong"


async def test_adding_an_agent_label_starts_a_task_once(world: World, hooks):
    world.issue(7)
    label = world.github.label_issue("acme/plane", 7, "agent: Developer")
    assert (await post(hooks, "issues", labeled(world, 7, label))).status_code == 200
    await post(hooks, "issues", labeled(world, 7, label))  # the same delivery id again
    assert [t["task_id"] for t in world.publisher.tasks] == ["plane-7"]


async def test_pull_requests_and_the_bots_own_labels_are_ignored(world: World, hooks):
    world.issue(7)
    label = world.github.label_issue("acme/plane", 7, "agent: Developer")
    await post(hooks, "issues", labeled(world, 7, label, pull=True), delivery="d-2")
    await post(hooks, "issues", labeled(world, 7, label, sender=BOT), delivery="d-3")
    assert world.publisher.tasks == []


async def test_a_quote_reply_answers_through_the_webhook(world: World, hooks):
    ref = world.issue(7)
    await world.label(ref)
    await world.bridge.on_event(world.event(ref, "info_request", {"request_id": "rq-1", "question": "Which version?"}))
    comment = world.github.add_comment("acme/plane", 7, "octocat", "> Which version?\n\n2.4")
    payload = {"action": "created", "issue": world.github.repos["acme/plane"]["issues"][7], "comment": comment,
               "sender": {"login": "octocat"}, "repository": {"full_name": "acme/plane"}, "installation": {"id": INSTALLATION}}
    await post(hooks, "issue_comment", payload)
    assert [a["answer"] for a in world.publisher.answers] == ["2.4"]


async def test_installation_changes_resync_the_labels(world: World, hooks):
    world.github.installations[INSTALLATION].discard("acme/plane")
    world.github.add_repo("acme/plane", 99)
    world.github.repos["acme/plane"]["labels"] = []
    await post(hooks, "installation_repositories", {"action": "added", "installation": {"id": 99}})
    assert "agent: Developer" in world.github.label_names("acme/plane")
