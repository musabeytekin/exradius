"""Bridge files and their problems (design §3), the status report (§9), the directory and
its notice (§10), settings."""

from __future__ import annotations

import os

import pytest

from exradius_bridge_kit import KitSettings, SettingsError, TrackerProblem
from exradius_bridge_kit.exradius import ExradiusError, IntegrationApi
from exradius_bridge_kit.testing import SETTINGS, FakeBroker, FakeExradius, FakeTracker, make_kit, project_entry


def messages(report: dict) -> list[tuple[str | None, str]]:
    return [(p["project"], p["message"]) for p in report["problems"]]


async def test_bridge_file_problems_are_reported():
    exradius = FakeExradius(projects=[
        project_entry("bank", bridge_config={"scopes": []}),
        project_entry("outline", bridge_config={"scopez": ["OUT"]}),
        project_entry("plane", bridge_config={"scopes": ["PLANE"]}),
        project_entry("saleor", bridge_config_error="bridges/fake.yaml: not valid YAML: line 1, column 9: x"),
    ])
    tracker = FakeTracker()
    kit = make_kit(tracker, exradius)
    await kit.start()
    [report] = exradius.reports
    assert report["software"].startswith("fake-bridge 0.0.1 (exradius-bridge-kit ")
    assert report["version"] == 1 and report["started_at"].endswith("Z")
    assert messages(report) == [
        ("bank", "bridges/fake.yaml: scopes: List should have at least 1 item after validation, not 0"),
        ("outline", "bridges/fake.yaml: scopes: Field required"),
        ("outline", "bridges/fake.yaml: scopez: Extra inputs are not permitted"),
        ("saleor", "bridges/fake.yaml: not valid YAML: line 1, column 9: x"),
    ]
    assert kit.projects.resolve("PLANE").project == "plane"
    await kit.close()


async def test_status_is_reported_on_change_only_and_by_heartbeat():
    exradius = FakeExradius(projects=[project_entry("plane", bridge_config={"scopes": ["PLANE"]})])
    broker = FakeBroker()
    kit = make_kit(FakeTracker(), exradius, broker)
    await kit.start()
    await broker.consume(kit.on_event)
    assert len(exradius.reports) == 1
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["plane"], "bridges": ["fake"]}})
    assert exradius.directory_fetches == 2 and len(exradius.reports) == 1  # nothing changed
    exradius.projects.append(project_entry("bank", bridge_config={"scopes": ["PLANE"]}))
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["bank"]}})
    assert len(exradius.reports) == 2 and len(messages(exradius.reports[-1])) == 2
    await kit.report()  # the heartbeat reports anyway
    assert len(exradius.reports) == 3
    await kit.close()


async def test_a_notice_for_other_bridges_is_skipped():
    exradius = FakeExradius(projects=[])
    broker = FakeBroker()
    kit = make_kit(FakeTracker(), exradius, broker)
    await kit.start()
    await broker.consume(kit.on_event)
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["x"], "bridges": ["jira"]}})
    assert exradius.directory_fetches == 1
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["x"]}})  # an older Exradius
    assert exradius.directory_fetches == 2
    await kit.close()


async def test_a_failing_report_does_not_stop_the_kit():
    exradius = FakeExradius(projects=[project_entry("plane", bridge_config={"scopes": ["PLANE"]})])
    exradius.status_code = 500
    tracker = FakeTracker()
    broker = FakeBroker()
    kit = make_kit(tracker, exradius, broker)
    await kit.start()
    ref = tracker.add("PLANE-1")
    await kit.handed_over(ref)
    await kit.idle()
    assert len(broker.tasks) == 1
    exradius.status_code = 204
    await kit.report(only_if_changed=True)  # the failed one was not counted as sent
    assert exradius.reports[-1]["problems"] == []
    await kit.close()


async def test_a_tracker_that_cannot_start_is_reported_then_raised():
    exradius = FakeExradius(projects=[])
    tracker = FakeTracker()
    tracker.problem = "Jira answered 401 for the bot account"
    kit = make_kit(tracker, exradius)
    with pytest.raises(TrackerProblem):
        await kit.start()
    assert messages(exradius.reports[-1]) == [(None, "Jira answered 401 for the bot account")]
    await kit.close()


async def test_the_directory_uses_etags_and_reports_refusals():
    exradius = FakeExradius(projects=[project_entry("plane")])
    api = IntegrationApi(SETTINGS.api_url, SETTINGS.integration_token, transport=exradius.transport())
    first = await api.directory()
    again = await api.directory()
    assert first == again and exradius.directory_fetches == 2
    wrong = IntegrationApi(SETTINGS.api_url, "exr_wrong", transport=exradius.transport())
    with pytest.raises(ExradiusError, match="refused EXRADIUS_INTEGRATION_TOKEN"):
        await wrong.directory()
    await api.aclose()
    await wrong.aclose()


def test_settings_from_the_environment():
    settings = KitSettings.from_env({"EXRADIUS_BROKER_URL": "amqp://x/", "EXRADIUS_API_URL": "http://api:8000/",
                                     "EXRADIUS_INTEGRATION_TOKEN": "exr_1", "MAX_ATTACHMENT_MB": "2.5"})
    assert settings.api_url == "http://api:8000" and settings.max_attachment_bytes == int(2.5 * 1024 * 1024)
    with pytest.raises(SettingsError) as missing:
        KitSettings.from_env({"MAX_ATTACHMENT_MB": "lots"})
    assert str(missing.value) == ("EXRADIUS_BROKER_URL is required; EXRADIUS_API_URL is required; "
                                  "EXRADIUS_INTEGRATION_TOKEN is required; "
                                  "MAX_ATTACHMENT_MB must be a positive number, not 'lots'")


@pytest.mark.skipif(not os.environ.get("EXRADIUS_KIT_TEST_BROKER_URL"), reason="needs EXRADIUS_KIT_TEST_BROKER_URL")
async def test_the_real_broker_round_trip():
    import asyncio
    import json
    import uuid

    import aio_pika

    from exradius_bridge_kit.exradius import EVENTS_EXCHANGE, Broker

    url = os.environ["EXRADIUS_KIT_TEST_BROKER_URL"]
    bridge = f"test-{uuid.uuid4().hex[:6]}"
    broker = Broker(url, bridge, retry_after=0.1)
    await broker.connect()
    got: list[dict] = []
    failed_once: list[bool] = []

    async def handler(event):
        if event.get("fail") and not failed_once:
            failed_once.append(True)
            raise RuntimeError("first try fails")
        got.append(event)

    await broker.consume(handler)
    connection = await aio_pika.connect_robust(url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    for body in ({"n": 1}, {"n": 2, "fail": True}):
        await exchange.publish(aio_pika.Message(json.dumps(body).encode()), routing_key="task.status")
    await exchange.publish(aio_pika.Message(b'{"type": "directory_changed"}'), routing_key="directory.changed")
    for _ in range(100):
        if len(got) == 3:
            break
        await asyncio.sleep(0.05)
    assert [e.get("n") for e in got] == [1, 2, None]  # in order, the failed one retried

    # mandatory publishing: an answer no queue takes would raise. Nothing reads it back,
    # since a running Exradius worker consumes the queue (and drops an unknown request).
    await broker.publish_answer({"version": 1, "task_id": "T-none", "request_id": str(uuid.uuid4()), "answer": "x"})
    await broker.publish_cancel({"version": 1, "task_id": "T-none"})  # the queue exists: declared on connect
    events_queue = await channel.declare_queue(broker.queue_name, durable=True)
    await events_queue.delete(if_unused=False, if_empty=False)
    await connection.close()
    await broker.close()


async def test_status_reporter_on_its_own():
    """A bridge without the kit's flow (Desk) reports through ``StatusReporter``."""
    from exradius_bridge_kit import Problem, StatusReporter

    exradius = FakeExradius()
    api = IntegrationApi(SETTINGS.api_url, SETTINGS.integration_token, transport=exradius.transport())
    problems = [Problem("plane", "x" * 1500)] + [Problem(None, f"p{n}") for n in range(150)]
    reporter = StatusReporter(api, "exradius-desk 0.1.0", lambda: problems, heartbeat=0.01)
    await reporter.report()
    [report] = exradius.reports
    assert report["software"] == "exradius-desk 0.1.0" and report["started_at"].endswith("Z")
    assert len(report["problems"]) == 100 and len(report["problems"][0]["message"]) == 1000
    await reporter.report(only_if_changed=True)
    assert len(exradius.reports) == 1
    reporter.start()
    import asyncio

    while len(exradius.reports) < 3:
        await asyncio.sleep(0.01)
    await reporter.stop()
    exradius.status_code = 500
    await reporter.report()  # logged, not raised
    await api.aclose()


async def test_fake_broker_records_cancels():
    broker = FakeBroker()
    await broker.publish_cancel({"version": 1, "task_id": "T-1", "reason": "stop"})
    assert broker.cancels == [{"version": 1, "task_id": "T-1", "reason": "stop"}]
    broker.down = True
    with pytest.raises(ConnectionError):
        await broker.publish_cancel({"version": 1, "task_id": "T-2"})
