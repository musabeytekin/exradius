"""Webhooks (design §5), the config model (§3), settings (§7), and the flow end to end:
the fake GitHub, the kit's fake broker and fake Exradius."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from exradius_bridge_kit import SettingsError
from exradius_bridge_kit.testing import event, project_entry
from pydantic import ValidationError

from exradius_github.config import GitHubConfig
from exradius_github.settings import Settings, web_url
from exradius_github.tracker import decode_state, ref_for
from exradius_github.webhooks import Webhooks, make_app, signature_ok
from tests.conftest import REPO

SECRET = "hook-secret"
INSTANCE = "https://github.com"


def signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
async def post(kit, tracker):
    app = make_app(Webhooks(SECRET, kit, tracker))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge")

    async def _post(event_name: str, payload: dict, *, delivery: str | None = None, headers: dict | None = None) -> int:
        body, signature = signed(payload)
        extra = {"X-GitHub-Event": event_name} | ({"X-GitHub-Delivery": delivery} if delivery else {})
        response = await client.post("/webhooks/github", content=body, headers={**signature, **extra, **(headers or {})})
        await kit.idle()
        return response.status_code

    yield _post
    await client.aclose()


def assigned(repo: str, number: int, to: str, *, pull_request: bool = False) -> dict:
    issue = {"number": number} | ({"pull_request": {}} if pull_request else {})
    return {"action": "assigned", "issue": issue, "assignee": {"login": to}, "repository": {"full_name": repo}}


def commented(repo: str, number: int, author: str) -> dict:
    return {"action": "created", "issue": {"number": number}, "comment": {"id": 1, "user": {"login": author}},
            "repository": {"full_name": repo}}


def texts(fake, key) -> list[str]:
    """The comments as people see them: without the hidden state."""
    from exradius_github.tracker import strip_state

    return [strip_state(b).strip() for b in fake.bodies(key)]


# Webhooks -------------------------------------------------------------------------------------------


def test_signatures():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert signature_ok(SECRET, body, good)
    assert not signature_ok(SECRET, body, None)
    assert not signature_ok(SECRET, body, "sha1=abc")
    assert not signature_ok("other", body, good)


async def test_ping_refused_and_repeated_deliveries(post, fake, broker):
    assert await post("ping", {"zen": "Keep it logically awesome."}) == 200
    fake.add_issue(REPO, 1, "Fix it", author=fake.ayse, assignees=(fake.bot,))
    assert await post("issues", assigned(REPO, 1, fake.bot), headers={"X-Hub-Signature-256": "sha256=bad"}) == 401
    assert broker.tasks == []
    assert await post("issues", assigned(REPO, 1, fake.bot), delivery="d-1") == 200
    assert await post("issues", assigned(REPO, 1, fake.bot), delivery="d-1") == 200  # GitHub's redelivery
    assert [t["task_id"] for t in broker.tasks] == ["plane-1"]


async def test_only_issue_assignments_to_the_bot_start_work(post, fake, broker):
    fake.add_issue(REPO, 2, "x", author=fake.ayse, assignees=(fake.mehmet,))
    assert await post("issues", assigned(REPO, 2, fake.mehmet)) == 200
    assert await post("issues", {"action": "opened", "issue": {"number": 2}, "repository": {"full_name": REPO}}) == 200
    fake.add_issue(REPO, 3, "pr", author=fake.ayse, assignees=(fake.bot,), pull_request=True)
    assert await post("issues", assigned(REPO, 3, fake.bot, pull_request=True)) == 200
    assert broker.tasks == []
    fake.add_issue(REPO, 4, "x", author=fake.ayse, assignees=(fake.bot,))
    assert await post("issues", assigned(REPO, 4, "Exradius-Bot")) == 200  # logins are case-insensitive
    assert [t["task_id"] for t in broker.tasks] == ["plane-4"]


# The whole flow --------------------------------------------------------------------------------------


async def test_assign_ask_answer_finish(post, fake, broker, exradius):
    key = fake.add_issue(REPO, 10, "Estimates return 404", author=fake.ayse, assignees=(fake.ayse,),
                         body="Since the last deploy.")
    fake.add_comment(key, fake.mehmet, "Seen on staging too.")
    fake.assign(key, fake.bot)
    assert await post("issues", assigned(REPO, 10, fake.bot)) == 200
    [task] = broker.tasks
    assert task["project"] == "plane" and "agent" not in task
    assert task["task_id"] == "plane-10"
    assert task["instructions"] == "Estimates return 404\n\nSince the last deploy."
    assert task["correlation"] == {"bridge": "github", "instance": INSTANCE,
                                   "item": {"id": "acme/plane#10", "key": "plane-10"}}
    assert [c["body"] for c in task["context"]["comments"]] == ["Seen on staging too."]
    assert texts(fake, key)[-1] == "Exradius is picking an agent for this."
    item = ref_for(REPO, 10)

    def deliver(kind: str, payload: dict):
        return broker.deliver(event(kind, item, "plane-10", payload, agent="scout", bridge="github", instance=INSTANCE))

    await deliver("status", {"state": "queued", "previous": None, "reason": None,
                             "routing": {"method": "decided", "confidence": 0.52, "guess": True,
                                         "probabilities": {"scout": 0.55, "developer": 0.45}}})
    assert texts(fake, key)[-1] == "**Scout** is working on this. This was a guess (Scout 55%, Developer 45%)."

    await deliver("info_request", {"request_id": "q-1", "question": "Which branch?"})
    assert texts(fake, key)[-1].startswith("**Scout** asks @ayse:")
    fake.add_comment(key, fake.mehmet, "I think main?")  # no mention: not an answer
    assert await post("issue_comment", commented(REPO, 10, fake.mehmet)) == 200
    assert broker.answers == []
    fake.add_comment(key, fake.ayse, "@exradius-bot preview, please")
    assert await post("issue_comment", commented(REPO, 10, fake.ayse)) == 200
    [answer] = broker.answers
    assert (answer["task_id"], answer["request_id"], answer["answer"]) == ("plane-10", "q-1", "preview, please")

    exradius.artifacts["report.md"] = b"# Report\nAll good.\n"
    exradius.artifacts["shot.png"] = b"\x89PNG\x00"
    await deliver("result", {
        "state": "completed", "summary": "Registered the route.",
        "artifacts": [{"name": n, "content_type": t, "url": exradius.artifact_url(n)}
                      for n, t in (("report.md", "text/markdown"), ("shot.png", "image/png"))],
    })
    result = texts(fake, key)[-1]
    assert result.startswith("**Scout** finished.\n\nRegistered the route.")
    assert "report.md" in result and "shot.png" in result
    assert "<details><summary>report.md</summary>\n\n```\n# Report\nAll good.\n```" in result
    assert fake.assignees(key) == ["ayse"]
    assert decode_state(fake.bodies(key)[-1])["state"] == "ended"

    # Handing it over again starts a second run.
    fake.assign(key, fake.bot)
    assert await post("issues", assigned(REPO, 10, fake.bot)) == 200
    assert [t["task_id"] for t in broker.tasks] == ["plane-10", "plane-10-2"]


async def test_an_unclaimed_repository_is_told_and_handed_back(post, fake, broker):
    other = "acme/ops"
    fake.collaborators[other] |= {fake.bot, fake.ayse}
    key = fake.add_issue(other, 1, "x", author=fake.ayse, assignees=(fake.bot,))
    assert await post("issues", assigned(other, 1, fake.bot)) == 200
    assert broker.tasks == []
    assert texts(fake, key)[0] == "No Exradius project takes work from acme/ops."
    assert fake.assignees(key) == ["ayse"]


async def test_startup_picks_up_issues_assigned_while_away(fake, tracker, exradius, broker):
    from exradius_bridge_kit.testing import make_kit

    fake.add_issue(REPO, 20, "x", author=fake.ayse, assignees=(fake.bot,))
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    await kit.listen()
    await kit.idle()
    assert [t["task_id"] for t in broker.tasks] == ["plane-20"]
    await kit.close()


async def test_bridge_file_problems_reach_the_status(fake, tracker, broker):
    from exradius_bridge_kit.testing import FakeExradius, make_kit

    exradius = FakeExradius(bridge="github", projects=[
        project_entry("plane", bridge_config={"repositories": ["acme/plane", "Acme/Plane"]}),
        project_entry("bank", bridge_config={"repos": ["acme/bank"]}),
    ])
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    messages = [(p["project"], p["message"]) for p in exradius.reports[-1]["problems"]]
    assert messages == [
        ("plane", "bridges/github.yaml: repositories: Value error, listed more than once: acme/plane"),
        ("bank", "bridges/github.yaml: repositories: Field required"),
        ("bank", "bridges/github.yaml: repos: Extra inputs are not permitted"),
    ]
    await kit.close()


# Config and settings ---------------------------------------------------------------------------------


def test_the_config_model():
    config = GitHubConfig.model_validate({"repositories": ["Acme/Plane", "acme/plane.js"]})
    assert config.repositories == ["acme/plane", "acme/plane.js"]
    for bad in ({"repositories": []}, {"repositories": ["plane"]}, {"repositories": ["acme/plane.git"]},
                {"repositories": ["https://github.com/acme/plane"]}, {}, {"repositories": ["a/b"], "x": 1}):
        with pytest.raises(ValidationError):
            GitHubConfig.model_validate(bad)


def test_settings():
    env = {"GITHUB_TOKEN": "ghp_x", "GITHUB_WEBHOOK_SECRET": "s", "EXRADIUS_BROKER_URL": "amqp://rabbitmq:5672/",
           "EXRADIUS_API_URL": "http://api:8000", "EXRADIUS_INTEGRATION_TOKEN": "exr_1"}
    settings = Settings.from_env(env)
    assert settings.github_api_url == "https://api.github.com" and settings.github_url == "https://github.com"
    assert settings.port == 8080 and settings.kit.broker_url == "amqp://rabbitmq:5672/"
    # The chart hands the broker's user over apart from the URL; it is quoted into it.
    chart = env | {"EXRADIUS_BROKER_USERNAME": "user_x", "EXRADIUS_BROKER_PASSWORD": "p@ss/w:rd"}
    assert Settings.from_env(chart).kit.broker_url == "amqp://user_x:p%40ss%2Fw%3Ard@rabbitmq:5672/"
    assert web_url("https://ghe.acme.test/api/v3") == "https://ghe.acme.test"
    with pytest.raises(SettingsError) as missing:
        Settings.from_env({"PORT": "x", "GITHUB_API_URL": "ghe.acme.test"})
    text = str(missing.value)
    for part in ("GITHUB_API_URL must start with https://", "GITHUB_TOKEN is required",
                 "GITHUB_WEBHOOK_SECRET is required", "PORT must be a port number",
                 "EXRADIUS_INTEGRATION_TOKEN is required"):
        assert part in text
