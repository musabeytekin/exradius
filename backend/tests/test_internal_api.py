"""The internal API on the api's internal port (docs/kubernetes.md §9): what run, verify and
probe pods report, and the split of the two ports."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from exradius.crypto import hash_token
from exradius.models import Attachment, InfoRequest, ProjectVerification, ProviderTest, Run, Task, utcnow
from tests.test_api import admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401 - fixtures

AUTH = {"Authorization": "Bearer tok"}


def set_run(sync_db, run_id: str, **fields) -> None:
    run = sync_db.get(Run, uuid.UUID(run_id))
    for key, value in fields.items():
        setattr(run, key, value)
    sync_db.commit()


def fresh_run(sync_db, run_id: str) -> Run:
    sync_db.expire_all()
    return sync_db.get(Run, uuid.UUID(run_id))


# Runs ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status, stored", [("waiting", "waiting_for_input"), ("resumed", "resumed")])
def test_reported_is_stored_and_nudges_the_worker(client, admin, sync_db, control, tmp_path, status, stored):
    _, run_id = make_run(admin, sync_db, tmp_path)
    response = client.post(f"/internal/runs/{run_id}/reported", json={"status": status}, headers=AUTH)
    assert response.status_code == 204
    assert fresh_run(sync_db, run_id).reported_status == stored
    assert control.reported == [run_id]


def test_reported_survives_a_broker_outage_and_ignores_ended_runs(client, admin, sync_db, control, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    control.healthy = False
    assert client.post(f"/internal/runs/{run_id}/reported", json={"status": "waiting"}, headers=AUTH).status_code == 204
    assert fresh_run(sync_db, run_id).reported_status == "waiting_for_input"  # the sweep picks it up
    control.healthy = True
    set_run(sync_db, run_id, state="cancelled", reported_status=None)
    assert client.post(f"/internal/runs/{run_id}/reported", json={"status": "resumed"}, headers=AUTH).status_code == 204
    assert fresh_run(sync_db, run_id).reported_status is None
    assert client.post(f"/internal/runs/{run_id}/reported", json={"status": "nope"}, headers=AUTH).status_code == 422


def add_answered_request(sync_db, run_id: str, *, delivered=True) -> InfoRequest:
    request = InfoRequest(run_id=uuid.UUID(run_id), question="Logs?", answer="here", answered_at=utcnow(),
                          delivered_at=utcnow() if delivered else None,
                          answer_attachments=[{"name": "log.txt", "content_type": "text/plain", "object_key": "k/log",
                                               "source_url": None}])
    sync_db.add(request)
    sync_db.commit()
    return request


def test_answers_return_at_once_when_resuming(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    request = add_answered_request(sync_db, run_id)
    set_run(sync_db, run_id, state="resuming", resume_request_ids=[str(request.id)])
    started = time.monotonic()
    body = client.get(f"/internal/runs/{run_id}/answers", params={"wait": 30}, headers=AUTH).json()
    assert time.monotonic() - started < 5
    assert body["state"] == "resuming"
    [answer] = body["answers"]
    assert (answer["request_id"], answer["question"], answer["answer"]) == (str(request.id), "Logs?", "here")
    assert answer["attachments"] == [{"path": f".exradius/attachments/{request.id}/log.txt", "content_type": "text/plain",
                                      "id": f"{request.id}.0"}]


def test_answers_wait_then_return_nothing(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path, state="waiting_for_input")
    started = time.monotonic()
    body = client.get(f"/internal/runs/{run_id}/answers", params={"wait": 1}, headers=AUTH).json()
    assert time.monotonic() - started >= 0.9
    assert body == {"state": "waiting_for_input", "answers": []}
    assert client.get(f"/internal/runs/{run_id}/answers", params={"wait": 61}, headers=AUTH).status_code == 422


def test_answers_return_when_the_run_resumes_during_the_wait(client, admin, sync_db, tmp_path, db_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    _, run_id = make_run(admin, sync_db, tmp_path, state="waiting_for_input")
    request = add_answered_request(sync_db, run_id)

    def resume_later():
        time.sleep(0.5)
        engine = create_engine(f"sqlite:///{db_path}")
        with Session(engine) as session:
            run = session.get(Run, uuid.UUID(run_id))
            run.state, run.resume_request_ids = "resuming", [str(request.id)]
            session.commit()
        engine.dispose()

    thread = threading.Thread(target=resume_later)
    thread.start()
    started = time.monotonic()
    body = client.get(f"/internal/runs/{run_id}/answers", params={"wait": 20}, headers=AUTH).json()
    thread.join()
    assert time.monotonic() - started < 10
    assert body["state"] == "resuming" and len(body["answers"]) == 1


def test_answers_say_when_the_run_ended(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path, state="waiting_for_input")
    set_run(sync_db, run_id, state="cancelled")
    body = client.get(f"/internal/runs/{run_id}/answers", params={"wait": 30}, headers=AUTH).json()
    assert body == {"state": "cancelled", "answers": []}


def test_attachments_of_the_task_and_its_answers(client, admin, sync_db, store, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
    attachment = Attachment(task_id=task_id, name="stack.txt", content_type="text/plain", object_key="k/stack")
    sync_db.add(attachment)
    sync_db.commit()
    store.objects["k/stack"] = (b"trace", "text/plain")
    store.objects["k/log"] = (b"the log", "text/plain")
    request = add_answered_request(sync_db, run_id)

    listed = client.get(f"/internal/runs/{run_id}/attachments", headers=AUTH).json()
    assert listed == [{"id": str(attachment.id), "name": "stack.txt", "content_type": "text/plain"}]
    task_file = client.get(f"/internal/runs/{run_id}/attachments/{attachment.id}", headers=AUTH)
    assert task_file.status_code == 200 and task_file.content == b"trace"
    assert task_file.headers["content-type"].startswith("text/plain")
    answer_file = client.get(f"/internal/runs/{run_id}/attachments/{request.id}.0", headers=AUTH)
    assert answer_file.status_code == 200 and answer_file.content == b"the log"
    for missing in (f"{request.id}.1", f"{request.id}.x", str(uuid.uuid4()), "not-an-id"):
        assert client.get(f"/internal/runs/{run_id}/attachments/{missing}", headers=AUTH).status_code == 404
    store.objects.pop("k/stack")
    assert client.get(f"/internal/runs/{run_id}/attachments/{attachment.id}", headers=AUTH).status_code == 404


def test_another_runs_attachments_are_not_served(client, admin, sync_db, store, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
    other_task = Task(id="OTHER-1", project_id=sync_db.get(Task, task_id).project_id, agent="bug-fixer",
                      instructions="x", state="running")
    other_run = Run(id=uuid.uuid4(), task_id="OTHER-1", state="running", token_hash=hash_token("other"))
    attachment = Attachment(task_id="OTHER-1", name="secret.txt", content_type="text/plain", object_key="k/s")
    sync_db.add_all([other_task, other_run, attachment])
    sync_db.commit()
    request = add_answered_request(sync_db, str(other_run.id))
    store.objects["k/s"] = store.objects["k/log"] = (b"x", "text/plain")
    assert client.get(f"/internal/runs/{run_id}/attachments/{attachment.id}", headers=AUTH).status_code == 404
    assert client.get(f"/internal/runs/{run_id}/attachments/{request.id}.0", headers=AUTH).status_code == 404
    unanswered = add_answered_request(sync_db, run_id)
    unanswered.answered_at = None
    sync_db.commit()
    assert client.get(f"/internal/runs/{run_id}/attachments/{unanswered.id}.0", headers=AUTH).status_code == 404


def test_a_cancelled_runner_still_reports_until_finalize(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    set_run(sync_db, run_id, state="cancelled", error=None)
    outcome = {"status": "cancelled", "final_message": "stopped", "error": "interrupted",
               "warnings": ["api: uncommitted changes in 1 file were discarded with the task directory"]}
    assert client.post(f"/internal/runs/{run_id}/outcome", json=outcome, headers=AUTH).status_code == 204
    run = fresh_run(sync_db, run_id)
    assert run.state == "cancelled" and run.reported_status is None and run.error is None  # kept as they were
    assert run.delivery_warnings == outcome["warnings"] and run.summary == "stopped"

    set_run(sync_db, run_id, ended_at=utcnow())
    assert client.post(f"/internal/runs/{run_id}/outcome", json=outcome, headers=AUTH).status_code == 401


def test_outcome_of_a_live_run(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    outcome = {"status": "failed", "session_id": "s-1", "error": "invalid api key", "warnings": []}
    assert client.post(f"/internal/runs/{run_id}/outcome", json=outcome, headers=AUTH).status_code == 204
    run = fresh_run(sync_db, run_id)
    assert (run.reported_status, run.error, run.runtime_session_id, run.delivery_warnings) == (
        "failed", "invalid api key", "s-1", [])


def test_a_wrong_token_is_refused(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "tok"}):
        assert client.get(f"/internal/runs/{run_id}/answers", headers=headers).status_code == 401


# Verify and probe Runs ---------------------------------------------------------------------------


def test_verify_spec_and_tool_reports(client, admin, sync_db, tmp_path):
    project = setup_configuration(admin, tmp_path)["project"]
    verification = ProjectVerification(project_id=uuid.UUID(project["id"]), state="running", steps=[],
                                       token_hash=hash_token("vtok"))
    sync_db.add(verification)
    sync_db.commit()
    auth = {"Authorization": "Bearer vtok"}
    base = f"/internal/verifications/{verification.id}"
    spec = client.get(f"{base}/spec", headers=auth).json()
    by_name = {c["name"]: c for c in spec["connections"]}
    assert list(by_name) == ["docs", "sentry"]  # used by an agent, in file order
    assert by_name["docs"]["headers"] == {"Authorization": "Bearer docs-key"}  # substituted for the pod
    assert by_name["sentry"]["env"] == {"SENTRY_TOKEN": "sentry"}

    tools = [{"name": "search", "description": "Search."}]
    assert client.post(f"{base}/tools", json={"name": "docs", "ok": True, "tools": tools}, headers=auth).status_code == 204
    assert client.post(f"{base}/tools", json={"name": "sentry", "ok": False, "error": "McpError", "log": "npm ERR!"},
                       headers=auth).status_code == 204
    sync_db.expire_all()
    reports = sync_db.get(ProjectVerification, verification.id).tool_reports
    assert reports == {"docs": {"ok": True, "tools": tools, "error": None, "log": ""},
                       "sentry": {"ok": False, "tools": None, "error": "McpError", "log": "npm ERR!"}}

    assert client.get(f"{base}/spec", headers={"Authorization": "Bearer tok"}).status_code == 401
    row = sync_db.get(ProjectVerification, verification.id)
    row.state = "passed"
    sync_db.commit()
    assert client.get(f"{base}/spec", headers=auth).status_code == 401


def test_probe_result(client, admin, sync_db):
    provider = admin.post("/api/providers", json={"slug": "sub", "name": "Sub", "kind": "anthropic",
                                                   "auth_method": "oauth_token", "credential": "oat"}).json()
    test = ProviderTest(provider_id=uuid.UUID(provider["id"]), state="running", fingerprint="f",
                        token_hash=hash_token("ptok"))
    sync_db.add(test)
    sync_db.commit()
    body = {"ok": True, "detail": "claude-x replied 'OK'"}
    url = f"/internal/probes/{test.id}/result"
    assert client.post(url, json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer ptok"}).status_code == 204
    sync_db.expire_all()
    assert sync_db.get(ProviderTest, test.id).report == {"ok": True, "detail": "claude-x replied 'OK'", "error": None}


# The two ports -----------------------------------------------------------------------------------


def test_each_port_serves_its_own_routes(client, admin, sync_db, settings, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    internal = f"http://testserver:{settings.internal_port}"
    api = f"http://testserver:{settings.api_port}"
    spec_path = f"/internal/runs/{run_id}/spec"
    assert client.get(internal + spec_path, headers=AUTH).status_code == 200
    assert client.get(api + spec_path, headers=AUTH).status_code == 404  # never on the admin port
    assert client.get(internal + "/api/projects").status_code == 404  # run pods reach only /internal
    assert client.post(internal + "/api/auth/login", json={"username": "admin", "password": "correct horse"}).status_code == 404
    assert client.get(api + "/api/projects").status_code == 200
    for base in (internal, api):
        assert client.get(base + "/healthz").json() == {"ok": True}
    assert client.get(spec_path, headers=AUTH).status_code == 200  # other ports (tests) see everything
