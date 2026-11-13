"""Access (design §2), tasks and assignment (§5), comments and mentions (§8), and their mail (§10)."""

from __future__ import annotations

from exradius_bridge_kit.testing import project_entry

from tests.conftest import entry


async def new_task(client, **fields) -> str:
    data = {"project": "plane", "title": "Fix invoice totals", "description": "Negative since Monday.", **fields}
    files = fields.pop("files", None)
    response = await client.post("/api/tasks", data={k: v for k, v in data.items() if k != "files"}, files=files)
    assert response.status_code == 201, response.text
    return response.json()["key"]


async def test_bridge_files_decide_who_sees_which_project(browser, desk, exradius):
    exradius.projects += [
        project_entry("broken", bridge_config_error="bridges/desk.yaml: not valid YAML: line 1"),
        project_entry("loose", bridge_config={"groups": ["team-a", "team-a"], "teams": []}),
        project_entry("empty", bridge_config={"groups": []}),
        entry("idle", ["team-a"], runnable=False),
    ]
    await desk.refresh_directory()
    problems = [(p["project"], p["message"]) for p in exradius.reports[-1]["problems"]]
    assert ("broken", "bridges/desk.yaml: not valid YAML: line 1") in problems
    assert ("loose", "bridges/desk.yaml: groups: Value error, listed more than once: team-a") in problems
    assert ("loose", "bridges/desk.yaml: teams: Extra inputs are not permitted") in problems
    assert ("empty", "bridges/desk.yaml: groups: List should have at least 1 item after validation, not 0") in problems

    ada = await browser("ada", groups=["team-a"])
    assert (await ada.get("/api/projects")).json() == [
        {"slug": "idle", "name": "Idle", "runnable": False}, {"slug": "plane", "name": "Plane", "runnable": True}]
    cem = await browser("cem", groups=["team-b"])
    assert [p["slug"] for p in (await cem.get("/api/projects")).json()] == ["bank"]
    nobody = await browser("dan", groups=[])
    assert (await nobody.get("/api/projects")).json() == []

    key = await new_task(ada)
    assert key == "plane-1"
    assert (await cem.get(f"/api/tasks/{key}")).status_code == 404
    assert (await cem.post("/api/tasks", data={"project": "plane", "title": "x"})).status_code == 404
    assert (await cem.get("/api/tasks")).json() == []
    assert (await cem.get("/api/projects/plane/people")).status_code == 404

    # A broken file hides its project, tasks included, until it is fixed.
    exradius.projects = [project_entry("plane", bridge_config={"groupz": ["team-a"]})]
    await desk.refresh_directory()
    assert (await ada.get(f"/api/tasks/{key}")).status_code == 404
    exradius.projects = [entry("plane", ["team-a"], name="Plane")]
    await desk.refresh_directory()
    assert (await ada.get(f"/api/tasks/{key}")).status_code == 200


async def test_a_directory_notice_refreshes_the_projects(browser, desk, broker, exradius):
    exradius.projects.append(entry("mail", ["team-a"]))
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["mail"], "bridges": ["jira"]}})
    assert "mail" not in desk.directory.projects
    await broker.deliver({"version": 1, "type": "directory_changed", "payload": {"projects": ["mail"], "bridges": ["desk"]}})
    assert "mail" in desk.directory.projects


async def test_tasks_keys_edits_history_and_filters(browser):
    ada = await browser("ada")
    ben = await browser("ben")
    first = await new_task(ada, files=[("files", ("trace.txt", b"stack", "text/plain"))])
    second = await new_task(ben, title="Second")
    assert (first, second) == ("plane-1", "plane-2")

    task = (await ada.get(f"/api/tasks/{first}")).json()
    assert task["title"] == "Fix invoice totals" and task["status"] == "todo"
    assert task["reporter"]["name"] == "Ada" and task["assignee"] == {"kind": "none", "id": None, "name": None}
    assert [f["name"] for f in task["files"]] == ["trace.txt"]
    download = await ben.get(f"/api/attachments/{task['files'][0]['id']}")
    assert download.content == b"stack" and "attachment" in download.headers["content-disposition"]
    assert task["exradius"] == "start" and task["run"] is None

    assert (await ben.patch(f"/api/tasks/{first}", json={"status": "done", "title": "Fix totals"})).status_code == 204
    assert (await ben.patch(f"/api/tasks/{first}", json={"status": "closed"})).status_code == 422
    assert (await ben.patch(f"/api/tasks/{first}", json={"title": "  "})).status_code == 422
    assert (await ben.patch(f"/api/tasks/{first}", json={"labels": ["x"]})).status_code == 422
    task = (await ada.get(f"/api/tasks/{first}")).json()
    changes = [(e["field"], e["old"], e["new"], e["person"]["name"]) for e in task["timeline"] if e["type"] == "change"]
    assert changes == [("title", "Fix invoice totals", "Fix totals", "Ben"), ("status", "todo", "done", "Ben")]

    await ada.post(f"/api/tasks/{second}/assignee", json={"assignee": f"person:{ada.person['id']}"})
    assert [t["key"] for t in (await ada.get("/api/tasks")).json()] == [second, first]
    assert [t["key"] for t in (await ada.get("/api/tasks", params={"assignee": "me"})).json()] == [second]
    assert [t["key"] for t in (await ben.get("/api/tasks", params={"assignee": "me"})).json()] == []
    assert [t["key"] for t in (await ada.get("/api/tasks", params={"assignee": "none"})).json()] == [first]
    assert [t["key"] for t in (await ada.get("/api/tasks", params={"status": "done"})).json()] == [first]
    assert [t["key"] for t in (await ada.get("/api/tasks", params={"project": "bank"})).json()] == []
    assert (await ada.get("/api/tasks", params={"assignee": "everyone"})).status_code == 422


async def test_assignable_people_and_the_assigned_mail(browser, desk, smtp):
    ada = await browser("ada")
    ben = await browser("ben")
    cem = await browser("cem", groups=["team-b"])
    people = (await ada.get("/api/projects/plane/people")).json()
    assert [p["name"] for p in people] == ["Ada", "Ben"]

    key = await new_task(ada, assignee=f"person:{ben.person['id']}")
    response = await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": f"person:{cem.person['id']}"})
    assert response.status_code == 422  # not allowed on plane
    await ada.post(f"/api/tasks/{key}/assignee", json={"assignee": f"person:{ada.person['id']}"})  # herself: no mail
    await desk.idle()
    assert smtp.subjects("ben@acme.test") == ["plane-1 Fix invoice totals: assigned to you"]
    assert smtp.to("ada@acme.test") == []
    assert "http://desk.test/tasks/plane-1" in smtp.to("ben@acme.test")[0]


async def test_mentions_are_references_and_send_one_mail(browser, desk, smtp):
    ada = await browser("ada")
    ben = await browser("ben", email="ben@acme.test")
    cem = await browser("cem", groups=["team-b"])
    key = await new_task(ada)
    body = f"@[person:{ben.person['id']}] can you look? cc @[person:{ada.person['id']}]"
    response = await ada.post(f"/api/tasks/{key}/comments", data={"body": body},
                              files=[("files", ("log.txt", b"log", "text/plain"))])
    assert response.status_code == 201 and response.json()["effect"] is None
    stranger = await ada.post(f"/api/tasks/{key}/comments", data={"body": f"@[person:{cem.person['id']}] hi"})
    assert stranger.status_code == 422
    assert (await ada.post(f"/api/tasks/{key}/comments", data={"body": "  "})).status_code == 422

    task = (await ada.get(f"/api/tasks/{key}")).json()
    [comment] = [e for e in task["timeline"] if e["type"] == "comment"]
    assert comment["body"] == body and [f["name"] for f in comment["files"]] == ["log.txt"]
    assert task["names"][str(ben.person["id"])] == "Ben"
    await desk.idle()
    [mail] = smtp.to("ben@acme.test")
    assert "Subject: plane-1 Fix invoice totals: you were mentioned" in mail
    assert "@Ben can you look? cc @Ada" in mail and f"#comment-{comment['id']}" in mail
    assert smtp.to("ada@acme.test") == []  # mentioning yourself sends nothing


async def test_no_mail_without_smtp_or_to_unverified_addresses(browser, desk, smtp):
    ada = await browser("ada")
    ben = await browser("ben", extra={"email_verified": False})
    key = await new_task(ada, assignee=f"person:{ben.person['id']}")
    await desk.idle()
    assert smtp.received == []
    desk.mailer.smtp = None
    await ada.post(f"/api/tasks/{key}/comments", data={"body": f"@[person:{ben.person['id']}]"})
    await desk.idle()
    assert smtp.received == []


async def test_a_failed_mail_is_reported(browser, desk, exradius, smtp):
    ada = await browser("ada")
    ben = await browser("ben")
    await smtp.stop()
    await new_task(ada, assignee=f"person:{ben.person['id']}")
    await desk.idle()
    problems = [p["message"] for p in exradius.reports[-1]["problems"]]
    assert any(m.startswith("mail could not be sent through 127.0.0.1") for m in problems)
    await smtp.start()  # for the fixture's teardown


async def test_upload_cap(browser):
    ada = await browser("ada")
    big = b"x" * (1024 * 1024 + 1)
    response = await ada.post("/api/tasks", data={"project": "plane", "title": "x"},
                              files=[("files", ("big.bin", big, "application/octet-stream"))])
    assert response.status_code == 413
    key = await new_task(ada)
    halves = [("files", ("a.bin", b"x" * 600_000, "application/octet-stream")),
              ("files", ("b.bin", b"x" * 600_000, "application/octet-stream"))]
    assert (await ada.post(f"/api/tasks/{key}/files", files=halves)).status_code == 413
    assert (await ada.post(f"/api/tasks/{key}/files", files=halves[:1])).status_code == 204


async def test_live_updates_go_to_people_who_may_see_the_project(desk):
    seen_a, seen_b = [], []
    a = desk.hub.subscribe(lambda project: project == "plane")
    b = desk.hub.subscribe(lambda project: project == "bank")
    desk.hub.changed("plane", "plane-1")
    while not a.queue.empty():
        seen_a.append(a.queue.get_nowait())
    while not b.queue.empty():
        seen_b.append(b.queue.get_nowait())
    assert seen_a == [{"key": "plane-1", "project": "plane"}] and seen_b == []
    desk.hub.unsubscribe(a)
    desk.hub.unsubscribe(b)
    assert desk.hub.listeners == set()
