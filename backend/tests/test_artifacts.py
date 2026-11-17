"""Independent artifact storage, selection and access boundaries."""

import uuid

import pytest

from exradius.crypto import hash_token
from exradius.models import Artifact, Run, Task
from tests.test_api import admin, client, control, make_run, sync_db  # noqa: F401

AUTH = {"Authorization": "Bearer tok"}


def upload(client, run_id, name="report.pdf", content=b"report"):
    return client.post(f"/internal/runs/{run_id}/artifacts", params={"name": name},
                       content=content, headers=AUTH)


def test_upload_survives_result_replacement_and_requires_explicit_selection(client, admin, sync_db, store, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    response = upload(client, run_id)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["size"] == 6 and saved["selected"] is False
    assert upload(client, run_id).json()["id"] == saved["id"]
    base = f"/internal/runs/{run_id}"
    assert client.post(f"{base}/result", json={"output": "done", "artifact_ids": [saved["id"]]},
                       headers=AUTH).status_code == 204
    assert client.get(f"{base}/artifacts/{saved['id']}", headers=AUTH).json()["selected"] is True
    assert client.post(f"{base}/result", json={"output": "without files"}, headers=AUTH).status_code == 204
    listed = client.get(f"{base}/artifacts", headers=AUTH).json()
    assert len(listed) == 1 and listed[0]["selected"] is False
    sync_db.expire_all()
    artifact = sync_db.get(Artifact, uuid.UUID(saved["id"]))
    assert store.objects[artifact.object_key][0] == b"report"


def test_cross_run_artifacts_cannot_be_read_or_selected(client, admin, sync_db, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
    saved = upload(client, run_id).json()
    task = sync_db.get(Task, task_id)
    other = Run(task_id="OTHER", state="running", token_hash=hash_token("other"))
    sync_db.add(Task(id="OTHER", project_id=task.project_id, agent=task.agent, instructions="x", state="running"))
    sync_db.add(other)
    sync_db.commit()
    headers = {"Authorization": "Bearer other"}
    base = f"/internal/runs/{other.id}"
    assert client.get(f"{base}/artifacts/{saved['id']}", headers=headers).status_code == 404
    assert client.post(f"{base}/result", json={"output": "x", "artifact_ids": [saved["id"]]},
                       headers=headers).status_code == 404
    assert client.post(f"{base}/result", json={"output": "no files"}, headers=headers).status_code == 204


def test_upload_limits_and_unsafe_names(client, admin, sync_db, tmp_path, monkeypatch):
    from exradius import artifacts

    _, run_id = make_run(admin, sync_db, tmp_path)
    monkeypatch.setattr(artifacts, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(artifacts, "MAX_RUN_BYTES", 10)
    assert upload(client, run_id, content=b"x" * 9).status_code == 413
    assert upload(client, run_id, content=b"x" * 6).status_code == 201
    assert upload(client, run_id, "second.zip", b"y" * 5).status_code == 413
    for name in ("../secret", "bad\r\nheader", ".", "..", "folder/file"):
        assert upload(client, run_id, name).status_code == 422


def test_download_is_private_and_never_renders_active_content(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    saved = upload(client, run_id, 'rapor "ö".html', b"<script>alert(1)</script>").json()
    path = f"/api/artifacts/{saved['id']}/content"
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    admin.post("/api/auth/logout")
    assert client.get(path).status_code == 401


def test_bridge_can_only_download_selected_files_of_its_configured_project(client, admin, sync_db, tmp_path):
    from tests.test_integration_api import new_token

    task_id, run_id = make_run(admin, sync_db, tmp_path)
    task = sync_db.get(Task, task_id)
    task.correlation = {"bridge": "github"}
    project = task.project
    snapshot = dict(project.snapshot)
    snapshot["bridges"] = {"github": {"config": {}, "error": None}}
    project.snapshot = snapshot
    sync_db.commit()
    github = {"Authorization": f"Bearer {new_token(admin, 'github')}"}
    jira = {"Authorization": f"Bearer {new_token(admin, 'jira')}"}
    saved = upload(client, run_id).json()
    path = f"/api/integration/v1/artifacts/{saved['id']}/content"
    assert client.get(path, headers=github).status_code == 404

    client.post(f"/internal/runs/{run_id}/result", json={"artifact_ids": [saved["id"]]}, headers=AUTH)
    assert client.get(path, headers=github).content == b"report"
    assert client.get(path, headers=jira).status_code == 404
    assert client.get(path).status_code == 401
    project.snapshot = {**snapshot, "bridges": {}}
    sync_db.commit()
    assert client.get(path, headers=github).status_code == 404
    project.snapshot = snapshot
    sync_db.commit()
    token = next(t for t in admin.get("/api/integration-tokens").json() if t["bridge"] == "github")
    assert admin.delete(f"/api/integration-tokens/{token['id']}").status_code == 204
    assert client.get(path, headers=github).status_code == 401


def test_failed_uploads_are_rate_limited_but_do_not_block_results(client, admin, sync_db, tmp_path, monkeypatch):
    from exradius import artifacts

    _, run_id = make_run(admin, sync_db, tmp_path)
    monkeypatch.setattr(artifacts, "UPLOADS_PER_MINUTE", 2)
    assert upload(client, run_id, "../bad").status_code == 422
    assert upload(client, run_id, "../bad").status_code == 422
    assert upload(client, run_id).status_code == 429
    assert client.post(f"/internal/runs/{run_id}/result", json={"output": "could not upload"},
                       headers=AUTH).status_code == 204


async def test_runner_http_client_uploads_binary_then_submits_ids(client, admin, sync_db, tmp_path):
    import httpx
    from exradius.runner.client import HttpRunApi

    _, run_id = make_run(admin, sync_db, tmp_path)
    api = HttpRunApi("http://testserver", run_id, "tok")
    await api.aclose()
    api._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                  base_url=f"http://testserver/internal/runs/{run_id}", headers=AUTH)
    try:
        saved = await api.upload_artifact("arbitrary.bin", b"\x00\xff\xfe")
        assert saved.size == 3 and saved.selected is False
        assert (await api.list_artifacts())[0].id == saved.id
        await api.submit_result("done", [str(saved.id)])
        assert (await api.get_artifact(str(saved.id))).selected is True
    finally:
        await api.aclose()


def test_chunked_uploads_cannot_bypass_limits(client, admin, sync_db, tmp_path, monkeypatch):
    from exradius import artifacts

    _, run_id = make_run(admin, sync_db, tmp_path)
    monkeypatch.setattr(artifacts, "MAX_FILE_BYTES", 4)
    response = client.post(f"/internal/runs/{run_id}/artifacts?name=data.bin",
                           content=iter([b"123", b"456"]), headers=AUTH)
    assert response.status_code == 413
    assert client.get(f"/internal/runs/{run_id}/artifacts", headers=AUTH).json() == []


def test_storage_failure_does_not_record_a_file_or_block_empty_result(client, admin, sync_db, store, tmp_path,
                                                                   monkeypatch):
    from exradius.storage import ObjectStoreError

    _, run_id = make_run(admin, sync_db, tmp_path)

    async def unavailable(*args):
        raise ObjectStoreError("unavailable")

    monkeypatch.setattr(store, "put", unavailable)
    with pytest.raises(ObjectStoreError):
        upload(client, run_id)
    assert client.get(f"/internal/runs/{run_id}/artifacts", headers=AUTH).json() == []
    assert client.post(f"/internal/runs/{run_id}/result", json={"output": "upload failed"}, headers=AUTH).status_code == 204


def test_invalid_selection_does_not_replace_previous_result(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    saved = upload(client, run_id).json()
    base = f"/internal/runs/{run_id}"
    assert client.post(f"{base}/result", json={"output": "original", "artifact_ids": [saved["id"]]},
                       headers=AUTH).status_code == 204
    assert client.post(f"{base}/result", json={"output": "wrong", "artifact_ids": [str(uuid.uuid4())]},
                       headers=AUTH).status_code == 404
    assert client.get(f"{base}/artifacts/{saved['id']}", headers=AUTH).json()["selected"] is True
    sync_db.expire_all()
    assert sync_db.get(Run, uuid.UUID(run_id)).output == "original"


def test_ended_run_can_no_longer_upload_or_list(client, admin, sync_db, tmp_path):
    from exradius.models import utcnow

    _, run_id = make_run(admin, sync_db, tmp_path)
    assert upload(client, run_id).status_code == 201
    sync_db.get(Run, uuid.UUID(run_id)).ended_at = utcnow()
    sync_db.commit()
    assert upload(client, run_id).status_code == 401
    assert client.get(f"/internal/runs/{run_id}/artifacts", headers=AUTH).status_code == 401
