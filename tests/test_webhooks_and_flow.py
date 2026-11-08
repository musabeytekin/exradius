"""Webhooks (design §6), the config model (§3), settings (§7), and the flow end to end:
the fake Jira, the kit's fake broker and fake Exradius."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from exradius_bridge_kit import SettingsError
from exradius_bridge_kit.testing import event, project_entry
from pydantic import ValidationError

from exradius_jira import adf
from exradius_jira.config import JiraConfig
from exradius_jira.settings import Settings
from exradius_jira.webhooks import Webhooks, make_app, signature_ok

SECRET = "hook-secret"


def signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
async def post(kit, tracker):
    app = make_app(Webhooks(SECRET, kit, tracker))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge")

    async def _post(payload: dict, *, delivery: str | None = None, headers: dict | None = None) -> int:
        body, signature = signed(payload)
        extra = {"X-Atlassian-Webhook-Identifier": delivery} if delivery else {}
        response = await client.post("/webhooks/jira", content=body, headers={**signature, **extra, **(headers or {})})
        await kit.idle()
        return response.status_code

    yield _post
    await client.aclose()


def assigned(fake, issue_id: str, to) -> dict:
    issue = fake.issues[issue_id]
    return {"webhookEvent": "jira:issue_updated", "issue": {"id": issue_id, "key": issue["key"], "fields": {}},
            "changelog": {"items": [{"field": "assignee", "fieldId": "assignee", "to": to.account_id}]}}


def commented(fake, issue_id: str, author) -> dict:
    issue = fake.issues[issue_id]
    return {"webhookEvent": "comment_created", "issue": {"id": issue_id, "key": issue["key"]},
            "comment": {"id": "1", "author": {"accountId": author.account_id}}}


# Webhooks -------------------------------------------------------------------------------------------


def test_signatures():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert signature_ok(SECRET, body, good)
    assert not signature_ok(SECRET, body, None)
    assert not signature_ok(SECRET, body, "sha1=abc")
    assert not signature_ok("other", body, good)


async def test_refused_and_repeated_deliveries(post, fake, broker):
    issue_id = fake.add_issue("PLANE-1", "Fix it", reporter=fake.ayse, assignee=fake.bot)
    assert await post(assigned(fake, issue_id, fake.bot), headers={"X-Hub-Signature": "sha256=bad"}) == 401
    assert broker.tasks == []
    assert await post(assigned(fake, issue_id, fake.bot), delivery="d-1") == 200
    assert await post(assigned(fake, issue_id, fake.bot), delivery="d-1") == 200  # Jira's retry
    assert [t["task_id"] for t in broker.tasks] == ["PLANE-1"]


async def test_only_assignments_to_the_bot_start_work(post, fake, broker):
    issue_id = fake.add_issue("PLANE-2", "x", reporter=fake.ayse, assignee=fake.mehmet)
    assert await post(assigned(fake, issue_id, fake.mehmet)) == 200
    edit = {"webhookEvent": "jira:issue_updated", "issue": {"id": issue_id, "key": "PLANE-2"},
            "changelog": {"items": [{"field": "summary", "fieldId": "summary", "to": None}]}}
    assert await post(edit) == 200
    assert broker.tasks == []
    created = fake.add_issue("PLANE-3", "x", reporter=fake.ayse, assignee=fake.bot)
    assert await post({"webhookEvent": "jira:issue_created", "issue": {
        "id": created, "key": "PLANE-3", "fields": {"assignee": {"accountId": fake.bot.account_id}}}}) == 200
    assert [t["task_id"] for t in broker.tasks] == ["PLANE-3"]


# The whole flow --------------------------------------------------------------------------------------


async def test_assign_ask_answer_finish(post, fake, broker, exradius):
    issue_id = fake.add_issue("PLANE-10", "Estimates return 404", reporter=fake.ayse, assignee=fake.ayse,
                              description=adf.doc(adf.paragraph(adf.text("Since the last deploy."))))
    fake.assign(issue_id, fake.bot)
    assert await post(assigned(fake, issue_id, fake.bot)) == 200
    [task] = broker.tasks
    assert task["project"] == "plane" and "agent" not in task
    assert task["instructions"] == "Estimates return 404\n\nSince the last deploy."
    assert task["correlation"] == {"bridge": "jira", "instance": "https://acme.atlassian.net",
                                   "item": {"id": issue_id, "key": "PLANE-10"}}
    ref = {"id": issue_id, "key": "PLANE-10"}

    from exradius_bridge_kit import ItemRef

    item = ItemRef(**ref)
    await broker.deliver(event("status", item, "PLANE-10", {
        "state": "queued", "previous": None, "reason": None,
        "routing": {"method": "decided", "confidence": 0.52, "guess": True, "probabilities": {"scout": 0.55, "developer": 0.45}},
    }, agent="scout", bridge="jira", instance="https://acme.atlassian.net"))
    assert fake.comment_texts(issue_id) == ["Scout is working on this. This was a guess (Scout 55%, Developer 45%)."]

    await broker.deliver(event("info_request", item, "PLANE-10", {"request_id": "q-1", "question": "Which branch?"},
                               agent="scout", bridge="jira", instance="https://acme.atlassian.net"))
    assert fake.comment_texts(issue_id)[-1] == "Scout asks @Ayşe Yılmaz:\nWhich branch?\nMention me in your reply to answer."
    fake.add_comment(issue_id, fake.ayse, adf.doc(adf.paragraph(
        adf.mention(fake.bot.account_id, "AI Agent"), adf.text(" preview, please"))))
    assert await post(commented(fake, issue_id, fake.ayse)) == 200
    [answer] = broker.answers
    assert (answer["task_id"], answer["request_id"], answer["answer"]) == ("PLANE-10", "q-1", "preview, please")

    exradius.artifacts["report.docx"] = b"PK"
    await broker.deliver(event("result", item, "PLANE-10", {
        "state": "completed", "summary": "Registered the route.",
        "artifacts": [{"name": "report.docx", "content_type": "application/octet-stream",
                       "url": exradius.artifact_url("report.docx")}],
    }, agent="scout", bridge="jira", instance="https://acme.atlassian.net"))
    assert fake.comment_texts(issue_id)[-1] == "Scout finished.\nRegistered the route.\nAttached: report.docx"
    assert fake.uploads == [(issue_id, "report.docx", b"PK")]
    assert fake.assignee(issue_id) == fake.ayse.account_id
    assert fake.properties[(issue_id, "exradius")]["state"] == "ended"


async def test_an_unclaimed_space_is_told_and_handed_back(post, fake, broker):
    issue_id = fake.add_issue("OPS-1", "x", reporter=fake.ayse, assignee=fake.bot)
    assert await post(assigned(fake, issue_id, fake.bot)) == 200
    assert broker.tasks == []
    assert fake.comment_texts(issue_id) == ["No Exradius project takes work from OPS."]
    assert fake.assignee(issue_id) == fake.ayse.account_id


async def test_bridge_file_problems_reach_the_status(fake, tracker, broker):
    from exradius_bridge_kit.testing import FakeExradius, make_kit

    exradius = FakeExradius(bridge="jira", projects=[
        project_entry("plane", bridge_config={"projects": ["PLANE", "plane"]}),
        project_entry("bank", bridge_config={"project": ["BANK"]}),
    ])
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    messages = [(p["project"], p["message"]) for p in exradius.reports[-1]["problems"]]
    assert messages == [
        ("plane", "bridges/jira.yaml: projects.1: Value error, 'plane' is not a Jira space key (like PLANE)"),
        ("bank", "bridges/jira.yaml: projects: Field required"),
        ("bank", "bridges/jira.yaml: project: Extra inputs are not permitted"),
    ]
    await kit.close()


# Config and settings ---------------------------------------------------------------------------------


def test_the_config_model():
    assert JiraConfig.model_validate({"projects": ["PLANE", "OPS_2"]}).projects == ["PLANE", "OPS_2"]
    for bad in ({"projects": []}, {"projects": ["PLANE", "PLANE"]}, {"projects": ["1X"]}, {}, {"projects": ["P"], "site": "x"}):
        with pytest.raises(ValidationError):
            JiraConfig.model_validate(bad)


def test_settings():
    env = {"JIRA_SITE": "https://acme.atlassian.net/", "JIRA_BOT_EMAIL": "a@b", "JIRA_BOT_TOKEN": "t",
           "JIRA_WEBHOOK_SECRET": "s", "EXRADIUS_BROKER_URL": "amqp://x/", "EXRADIUS_API_URL": "http://api:8000",
           "EXRADIUS_INTEGRATION_TOKEN": "exr_1"}
    settings = Settings.from_env(env)
    assert settings.jira_site == "https://acme.atlassian.net" and settings.port == 8080
    assert settings.kit.api_url == "http://api:8000"
    with pytest.raises(SettingsError) as missing:
        Settings.from_env({"PORT": "x", "JIRA_SITE": "acme.atlassian.net"})
    text = str(missing.value)
    for part in ("JIRA_SITE must start with https://", "JIRA_BOT_TOKEN is required", "PORT must be a port number",
                 "EXRADIUS_INTEGRATION_TOKEN is required"):
        assert part in text
    assert "JIRA_ADMIN" not in text
