from __future__ import annotations

from datetime import timedelta

import pytest
from exradius_bridge_kit.testing import FakeBroker, FakeExradius, make_kit, project_entry

from exradius_github.github import GitHub
from exradius_github.store import Store
from exradius_github.tracker import GitHubTracker
from tests.fakes import API, FakeGitHub

REPO = "acme/plane"
BOT_TOKEN = "ghp_bot"


@pytest.fixture
def fake():
    github = FakeGitHub()
    github.bot = github.add_user("exradius-bot", token=BOT_TOKEN, repos=(REPO,))
    github.ayse = github.add_user("ayse", repos=(REPO,))
    github.mehmet = github.add_user("mehmet", repos=(REPO,))
    github.outsider = github.add_user("outsider")
    return github


@pytest.fixture
async def store(tmp_path):
    store = Store(tmp_path / "data" / "bridge.db", timedelta(days=30))
    await store.open()
    yield store
    await store.close()


@pytest.fixture
async def tracker(fake, store):
    client = GitHub(API, BOT_TOKEN, transport=fake.transport())
    tracker = GitHubTracker(client, "https://github.com", store=store)
    await tracker.start()
    yield tracker
    await client.aclose()


@pytest.fixture
def exradius():
    return FakeExradius(bridge="github", projects=[
        project_entry("plane", name="Plane", bridge_config={"repositories": [REPO]}),
    ])


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
async def kit(tracker, exradius, broker):
    kit = make_kit(tracker, exradius, broker, software="exradius-github test")
    await kit.start()
    await kit.listen()
    yield kit
    await kit.close()
