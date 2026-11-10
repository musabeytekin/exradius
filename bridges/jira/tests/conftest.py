from __future__ import annotations

import pytest
from exradius_bridge_kit.testing import FakeBroker, FakeExradius, make_kit, project_entry

from exradius_jira.jira import Jira
from exradius_jira.tracker import JiraTracker
from tests.fakes import SITE, FakeJira


@pytest.fixture
def fake():
    jira = FakeJira()
    jira.bot = jira.add_user("agent@acme.test", "AI Agent")
    jira.ayse = jira.add_user("ayse@acme.test", "Ayşe Yılmaz")
    jira.mehmet = jira.add_user("mehmet@acme.test", "Mehmet Kaya")
    return jira


@pytest.fixture
async def tracker(fake):
    client = Jira(SITE, "agent@acme.test", "token", transport=fake.transport())
    tracker = JiraTracker(client)
    await tracker.start()
    yield tracker
    await client.aclose()


@pytest.fixture
def exradius():
    return FakeExradius(bridge="jira", projects=[project_entry("plane", name="Plane", bridge_config={"projects": ["PLANE"]})])


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
async def kit(tracker, exradius, broker):
    kit = make_kit(tracker, exradius, broker, software="exradius-jira test")
    await kit.start()
    await kit.listen()
    yield kit
    await kit.close()
