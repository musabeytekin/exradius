"""Runs (design §6, §7): starting by assignment or mention, Exradius's events, answers by
mention, cancel, results, hand-back and their mail."""

from __future__ import annotations

import base64

import httpx

from exradius_desk.desk import exradius_task_id, guess_text
from exradius_desk.files import download_url
from tests.conftest import event


async def new_task(client, **fields) -> str:
    files = fields.pop("files", None)
    data = {"project": "plane", "title": "Fix invoice totals", "description": "Negative since Monday.", **fields}
    response = await client.post("/api/tasks", data=data, files=files)
    assert response.status_code == 201, response.text
    return response.json()["key"]


def comments(task: dict) -> list[dict]:
    return [e for e in task["timeline"] if e["type"] == "comment"]


async def test_a_run_from_assignment_to_result(browser, desk, broker, smtp):
    ada = await browser("ada")
    ben = await browser("ben")
    key = await new_task(ada, files=[("files", ("trace.txt", b"stack", "text/plain"))])
    await ada.post(f"/api/tasks/{key}/comments", data={"body": f"@[person:{ben.person['id']}] see the trace"})

    assert (await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": "exradius"})).status_code == 204
    [message] = broker.tasks
    task_id = f"plane-1-{desk.install}"
    assert message["task_id"] == task_id and "agent" not in message and message["project"] == "plane"
    assert message["instructions"] == "Fix invoice totals\n\nNegative since Monday."
    assert message["correlation"] == {"bridge": "desk", "instance": "http://desk.test", "install": desk.install,
                                      "task": key}
    context = message["context"]
    assert context["task"]["handed_over_by"] == "Ada" and context["task"]["url"] == "http://desk.test/tasks/plane-1"
    assert context["comments"][0]["text"] == "@Ben see the trace"
    assert [a["name"] for a in message["attachments"]] == ["trace.txt"]
    assert base64.b64decode(message["attachments"][0]["content_base64"]) == b"stack"

    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["assignee"]["kind"] == "exradius" and task["status"] == "in_progress"
    assert task["run"]["state"] == "sent" and task["exradius"] is None
    response = await ben.post(f"/api/tasks/{key}/assignee", json={"assignee": "none"})
    assert response.status_code == 409 and "cancel the run" in response.json()["detail"]

    await broker.deliver(event(desk, "status", task_id, {"state": "queued", "previous": None, "routing": {
        "method": "decided", "confidence": 0.48, "guess": True, "probabilities": {"developer": 0.41, "scout": 0.59}}},
        agent="scout"))
    await broker.deliver(event(desk, "status", task_id, {"state": "running", "previous": "preparing"}, agent="scout"))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    started = comments(task)[-1]
    assert started["kind"] == "started" and started["author"]["kind"] == "exradius"
    assert started["data"]["agent_name"] == "Scout" and started["data"]["guess"] == "a guess: Scout 59%, Developer 41%"
    assert task["run"]["state"] == "running" and task["run"]["agent_name"] == "Scout"
    working = await ben.post(f"/api/tasks/{key}/comments", data={"body": "@[exradius] also check refunds"})
    assert working.status_code == 409 and "it reads comments when it asks a question" in working.json()["detail"]
    assert len(comments((await ada.get(f"/api/tasks/{key}")).json())) == 2  # the refused comment is not stored

    await broker.deliver(event(desk, "info_request", task_id, {
        "request_id": "q-1", "question": "Which region?", "context": "Both have the bug."}, agent="scout"))
    await broker.deliver(event(desk, "status", task_id, {"state": "waiting_for_input", "previous": "running"}))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    question = comments(task)[-1]
    assert question["kind"] == "question" and question["body"] == "Which region?" and question["answered_by"] is None
    assert task["exradius"] == "answer"

    plain = await ben.post(f"/api/tasks/{key}/comments", data={"body": "probably eu"})
    assert plain.json()["effect"] is None and broker.answers == []  # no mention: not for the agent
    answer = await ben.post(f"/api/tasks/{key}/comments", data={"body": f"@[exradius] eu-west-1, ask @[person:{ada.person['id']}]"},
                            files=[("files", ("eu.log", b"eu", "text/plain"))])
    assert answer.json()["effect"] == "answered"
    [published] = broker.answers
    assert published["task_id"] == task_id and published["request_id"] == "q-1"
    assert published["answer"] == "@Exradius eu-west-1, ask @Ada"
    assert [a["name"] for a in published["attachments"]] == ["eu.log"]
    again = await ada.post(f"/api/tasks/{key}/comments", data={"body": "@[exradius] us-east-1"})
    assert again.status_code == 409 and len(broker.answers) == 1  # the question is answered
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert [c for c in comments(task) if c["kind"] == "question"][0]["answered_by"] == answer.json()["id"]
    assert comments(task)[-1]["answers"] == question["id"]

    calls = []

    def exradius_api(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"# Report")

    desk.downloads = httpx.AsyncClient(transport=httpx.MockTransport(exradius_api))
    await broker.deliver(event(desk, "status", task_id, {"state": "resuming", "previous": "waiting_for_input"}))
    await broker.deliver(event(desk, "result", task_id, {
        "state": "completed", "summary": "Fixed the rounding.", "error": None, "warnings": ["1 commit not pushed"],
        "artifacts": [{"name": "report.md", "content_type": "text/markdown",
                       "url": "https://exradius.example.com/prefix/api/artifacts/a1/content?sig=abc"}],
        "transcript_url": "https://exradius.example.com/api/runs/r/transcript?sig=t"}, agent="scout"))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    result = comments(task)[-1]
    assert result["kind"] == "result" and result["body"] == "Fixed the rounding."
    assert result["data"] == {"state": "completed", "error": None, "warnings": ["1 commit not pushed"]}
    assert "transcript" not in str(task)
    assert task["assignee"] == {"kind": "person", "id": ada.person["id"], "name": "Ada"}  # handed back
    assert task["status"] == "in_progress"  # never moved to Done by Desk
    assert task["run"] is None and task["runs"][0]["state"] == "completed" and task["exradius"] == "start"
    download = await ben.get(f"/api/artifacts/{result['artifacts'][0]['id']}")
    assert download.content == b"# Report"
    assert calls == ["http://exradius-api.internal:8000/api/artifacts/a1/content?sig=abc"]

    await desk.idle()
    # Ada handed it over and reported it; Ben's answer mentioned her.
    assert smtp.subjects("ada@acme.test") == ["plane-1 Fix invoice totals: Scout asks a question",
                                              "plane-1 Fix invoice totals: you were mentioned",
                                              "plane-1 Fix invoice totals: finished"]
    # Ben was mentioned first, and is a participant only since he answered.
    assert smtp.subjects("ben@acme.test") == ["plane-1 Fix invoice totals: you were mentioned",
                                              "plane-1 Fix invoice totals: finished"]
    assert "Fixed the rounding." in smtp.to("ben@acme.test")[-1]


async def test_a_mention_starts_the_next_run_with_the_thread(browser, desk, broker):
    ada = await browser("ada")
    ben = await browser("ben")
    key = await new_task(ada, assignee="exradius")  # creating assigned to Exradius starts a run
    first = f"plane-1-{desk.install}"
    assert [t["task_id"] for t in broker.tasks] == [first]
    await broker.deliver(event(desk, "status", first, {"state": "queued", "previous": None,
                                                       "routing": {"method": "only_agent"}}, agent="developer"))
    await broker.deliver(event(desk, "result", first, {"state": "failed", "summary": None,
                                                       "error": "tests failed", "artifacts": []}, agent="developer"))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["assignee"]["name"] == "Ada" and comments(task)[-1]["data"]["state"] == "failed"

    response = await ben.post(f"/api/tasks/{key}/comments", data={"body": "@[exradius] try again with the fixtures"},
                              files=[("files", ("fixtures.sql", b"insert", "text/plain"))])
    assert response.json()["effect"] == "started"
    second = broker.tasks[-1]
    assert second["task_id"] == f"plane-1-2-{desk.install}"
    texts = [(c["author"], c["kind"], c["text"]) for c in second["context"]["comments"]]
    assert texts[0] == ("Exradius", "started", "Developer started working on this (run 1).")
    assert texts[1] == ("Exradius", "result", "Developer could not finish.\n\ntests failed")
    assert texts[-1] == ("Ben", "comment", "@Exradius try again with the fixtures")
    assert second["attachments"][0]["name"] == "fixtures.sql"
    assert second["context"]["task"]["handed_over_by"] == "Ben"

    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert comments(task)[-1]["started_run"] == 2 and task["run"]["seq"] == 2
    await broker.deliver(event(desk, "result", f"plane-1-2-{desk.install}", {"state": "completed", "summary": "Done."}))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["assignee"]["name"] == "Ben"  # back to whoever wrote the mention


async def test_cancel_before_and_after_exradius_knows_the_task(browser, desk, broker):
    ada = await browser("ada")
    ben = await browser("ben")
    key = await new_task(ada)
    assert (await ada.post(f"/api/tasks/{key}/cancel")).status_code == 409  # nothing live
    await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": "exradius"})
    task_id = broker.tasks[0]["task_id"]

    assert (await ben.post(f"/api/tasks/{key}/cancel")).status_code == 204
    assert broker.cancels == [{"version": 1, "task_id": task_id, "reason": "cancelled by Ben in Desk"}]
    assert (await ada.post(f"/api/tasks/{key}/cancel")).status_code == 204  # twice: nothing more
    assert len(broker.cancels) == 1
    # Still in the broker's queue: Exradius dropped the cancel, so it goes again on queued.
    await broker.deliver(event(desk, "status", task_id, {"state": "queued", "previous": None}, agent="scout"))
    assert len(broker.cancels) == 2
    await broker.deliver(event(desk, "status", task_id, {"state": "cancelled", "previous": "queued",
                                                         "reason": "cancelled by Ben in Desk"}))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["assignee"]["kind"] == "exradius"  # the result ends the run
    await broker.deliver(event(desk, "result", task_id, {"state": "cancelled", "summary": None, "error": None}))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    kinds = [c["kind"] for c in comments(task)]
    assert kinds == ["cancel", "started", "result"]
    assert task["assignee"]["name"] == "Ada" and task["runs"][0]["cancel_requested_by"]["name"] == "Ben"


async def test_rejections_and_the_duplicate_id_retry(browser, desk, broker, smtp):
    ada = await browser("ada")
    key = await new_task(ada, assignee="exradius")
    first = broker.tasks[0]["task_id"]
    await broker.deliver(event(desk, "status", first, {"state": "rejected", "previous": None,
                                                       "reason": f"duplicate task_id '{first}'"}))
    assert broker.tasks[-1]["task_id"] == f"plane-1-2-{desk.install}"
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["run"]["task_id"] == f"plane-1-2-{desk.install}" and comments(task) == []

    await broker.deliver(event(desk, "status", f"plane-1-2-{desk.install}", {
        "state": "rejected", "previous": None, "reason": "missing secret DB_PASSWORD in store plane"}))
    task = (await ada.get(f"/api/tasks/{key}")).json()
    [rejected] = comments(task)
    assert rejected["kind"] == "rejected" and rejected["body"] == "missing secret DB_PASSWORD in store plane"
    assert task["assignee"]["name"] == "Ada" and task["run"] is None
    await desk.idle()
    assert smtp.subjects("ada@acme.test") == ["plane-1 Fix invoice totals: could not start"]

    # The next run takes the next number, past the retried one.
    await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": "exradius"})
    assert broker.tasks[-1]["task_id"] == f"plane-1-3-{desk.install}"
    assert (await ada.get(f"/api/tasks/{key}")).json()["run"]["seq"] == 2


async def test_events_of_others_are_ignored(browser, desk, broker):
    ada = await browser("ada")
    key = await new_task(ada, assignee="exradius")
    task_id = broker.tasks[0]["task_id"]
    for other in (event(desk, "status", task_id, {"state": "queued"}, install="zzzz"),
                  event(desk, "status", task_id, {"state": "queued"}, bridge="jira"),
                  event(desk, "status", f"plane-9-{desk.install}", {"state": "queued"}),
                  {"version": 1, "type": "status", "task_id": task_id, "payload": {"state": "queued"}}):
        await broker.deliver(other)
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["run"]["state"] == "sent" and comments(task) == []


async def test_a_broker_that_is_down(browser, desk, broker):
    ada = await browser("ada")
    key = await new_task(ada)
    broker.down = True
    response = await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": "exradius"})
    assert response.status_code == 503 and response.json()["detail"] == "Exradius's broker cannot be reached"
    response = await ada.post(f"/api/tasks/{key}/comments", data={"body": "@[exradius] go"})
    assert response.status_code == 503
    task = (await ada.get(f"/api/tasks/{key}")).json()
    assert task["assignee"]["kind"] == "none" and task["status"] == "todo" and task["runs"] == []
    assert comments(task) == []
    created = await ada.post("/api/tasks", data={"project": "plane", "title": "Two", "assignee": "exradius"})
    assert created.status_code == 503 and created.json()["detail"].startswith("plane-2 was created, but")


async def test_files_beyond_the_cap_are_listed_not_sent(browser, desk, broker):
    ada = await browser("ada")
    key = await new_task(ada, files=[("files", ("old.bin", b"o" * 700_000, "application/octet-stream"))])
    await ada.post(f"/api/tasks/{key}/files", files=[("files", ("new.bin", b"n" * 700_000, "application/octet-stream"))])
    await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": "exradius"})
    message = broker.tasks[-1]
    assert [a["name"] for a in message["attachments"]] == ["new.bin"]  # newest first
    assert message["context"]["skipped_attachments"] == [{"name": "old.bin", "size": 700_000}]


def test_ids_guesses_and_download_urls():
    assert exradius_task_id("plane-12", "kqxz", 1) == "plane-12-kqxz"
    assert exradius_task_id("plane-12", "kqxz", 3) == "plane-12-3-kqxz"
    assert guess_text({"method": "decided", "guess": False}, {}) is None
    assert guess_text({"method": "decided", "guess": True, "probabilities": {"a": 0.2, "b": 0.5, "c": 0.2,
                                                                             "d": 0.1}}, {"b": "Bee"}) == \
        "a guess: Bee 50%, a 20%, c 20%"
    assert download_url("https://x.test/p/api/artifacts/1/content?sig=s", "http://api:8000/") == \
        "http://api:8000/api/artifacts/1/content?sig=s"
    assert download_url("https://files.test/report.md", "http://api:8000") == "https://files.test/report.md"


async def test_no_id_ends_in_digits(desk):
    """Every Jira and GitHub id ends in -<digits>; Desk's never do (design §6)."""
    import re

    assert re.fullmatch(r"[a-z]{4}", desk.install)
    for key in ("plane-1", "billing-platform-12", "a-1-2-3"):
        for run in range(1, 8):
            task_id = exradius_task_id(key, desk.install, run)
            assert not re.search(r"-[0-9]+$", task_id), task_id
            assert re.fullmatch(r"[a-z0-9-]+", task_id) and task_id.startswith(f"{key}-")
    assert len({exradius_task_id("plane-1", desk.install, run) for run in range(1, 8)}) == 7
