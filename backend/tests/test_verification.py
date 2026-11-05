"""Worker side of the onboarding additions: project verification and heartbeats."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from exradius.models import Project, ProjectVerification, Repository, Worker
from exradius.worker import prepare
from exradius.worker.heartbeat import Heartbeat
from exradius.worker.verify import ProjectVerifier
from tests.test_run_manager import containers, manager, published  # noqa: F401 - fixtures


@pytest.fixture
def verifier(manager):
    manager.worker_id = "me"
    return ProjectVerifier(manager)


async def new_verification(sessions, project_id, **fields) -> uuid.UUID:
    async with sessions() as session:
        verification = ProjectVerification(project_id=project_id, state="pending", steps=[], **fields)
        session.add(verification)
        await session.commit()
        return verification.id


async def get(sessions, vid) -> ProjectVerification:
    async with sessions() as session:
        return await session.get(ProjectVerification, vid)


@pytest.fixture
async def project(sessions, configured, remote_repo):
    async with sessions() as session:
        project = await session.scalar(select(Project))
        project.setup_commands = ["npm ci", "make check"]
        project.image = "acme/node:1"
        session.add(Repository(project_id=project.id, name="app", url=str(remote_repo), branch="feature"))
        await session.commit()
        return project


def names_and_states(verification) -> list[tuple[str, str]]:
    return [(step["name"], step["state"]) for step in verification.steps]


async def test_verification_passes(verifier, sessions, containers, project, settings):
    vid = await new_verification(sessions, project.id)
    assert await verifier.claim(vid, "me") is True
    assert await verifier.claim(vid, "someone-else") is False  # already claimed

    seen = {}

    async def setup(*, image, run_id, paths, env, command):
        seen.setdefault("image", image)
        seen.setdefault("label", run_id)
        seen["feature_checked_out"] = (paths.project / "app" / "feature.txt").exists()
        seen["attachments_dir"] = paths.attachments.is_dir()
        containers.setup_commands.append(command)
        from exradius.worker.containers import ContainerExit

        return ContainerExit(0, f"ran {command}\n")

    containers.run_setup_command = setup
    await verifier.run(vid)

    verification = await get(sessions, vid)
    assert verification.state == "passed" and verification.error is None
    assert verification.started_at and verification.ended_at
    assert names_and_states(verification) == [
        ("Create directory", "passed"),
        ("Fetch mirror app", "passed"),
        ("Clone app (feature)", "passed"),
        ("Prepare attachments folder", "passed"),
        ("Setup: npm ci", "passed"),
        ("Setup: make check", "passed"),
        ("Remove directory", "passed"),
    ]
    steps = {s["name"]: s for s in verification.steps}
    assert steps["Setup: npm ci"]["log"] == "ran npm ci"
    assert steps["Clone app (feature)"]["log"].startswith("checked out feature at ")
    assert all(s["started_at"] and s["ended_at"] for s in verification.steps)
    assert seen == {"image": "acme/node:1", "label": f"verify-{vid}", "feature_checked_out": True, "attachments_dir": True}
    assert not (settings.data_dir / "runs" / f"verify-{vid}").exists()  # throwaway directory removed
    assert (prepare.mirror_path(settings.data_dir, project.slug, "app") / "HEAD").exists()  # mirror kept


async def test_failing_setup_skips_the_rest_and_cleans_up(verifier, sessions, containers, project, settings):
    containers.setup_exit = 2
    vid = await new_verification(sessions, project.id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    assert verification.error.startswith("Setup: npm ci: exited with 2")
    assert names_and_states(verification)[-3:] == [
        ("Setup: npm ci", "failed"),
        ("Setup: make check", "skipped"),
        ("Remove directory", "passed"),
    ]
    assert "boom" in verification.steps[-3]["log"]
    assert containers.setup_commands == ["npm ci"]
    assert not (settings.data_dir / "runs" / f"verify-{vid}").exists()


async def test_bad_repository_fails_at_the_mirror(verifier, sessions, containers, configured, tmp_path):
    async with sessions() as session:
        project = await session.scalar(select(Project))
        session.add(Repository(project_id=project.id, name="app", url=str(tmp_path / "missing.git"), branch="main"))
        await session.commit()
    vid = await new_verification(sessions, project.id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error.startswith("Fetch mirror app: git clone")
    assert names_and_states(verification)[1:3] == [("Fetch mirror app", "failed"), ("Clone app (main)", "skipped")]


async def test_only_pending_verifications_run(verifier, sessions, project):
    vid = await new_verification(sessions, project.id)
    async with sessions() as session:
        (await session.get(ProjectVerification, vid)).state = "passed"
        await session.commit()
    await verifier.run(vid)
    assert (await get(sessions, vid)).steps == []


async def test_dead_workers_verifications_are_failed(verifier, sessions, containers, project, settings):
    async def is_alive(worker_id):
        return worker_id == "live"

    dead = await new_verification(sessions, project.id, worker_id="dead")
    live = await new_verification(sessions, project.id, worker_id="live")
    queued = await new_verification(sessions, project.id)  # message still on the queue
    async with sessions() as session:
        verification = await session.get(ProjectVerification, dead)
        verification.state = "running"
        verification.steps = [{"name": "Create directory", "state": "passed"}, {"name": "Fetch mirror app", "state": "running"},
                              {"name": "Remove directory", "state": "pending"}]
        await session.commit()
    directory = prepare.create_run_directory(settings.data_dir, f"verify-{dead}")

    assert await verifier.reconcile(is_alive) == [dead]
    failed = await get(sessions, dead)
    assert failed.state == "failed" and failed.error == "worker restarted during verification"
    assert [s["state"] for s in failed.steps] == ["passed", "failed", "skipped"]
    assert not directory.root.exists()
    assert (await get(sessions, live)).state == "pending"
    assert (await get(sessions, queued)).state == "pending"


async def test_sweep_keeps_verifications_in_progress(manager, sessions, project, settings):
    running = await new_verification(sessions, project.id)
    finished = await new_verification(sessions, project.id)
    async with sessions() as session:
        (await session.get(ProjectVerification, running)).state = "running"
        (await session.get(ProjectVerification, finished)).state = "passed"
        await session.commit()
    keep = prepare.create_run_directory(settings.data_dir, f"verify-{running}")
    drop = prepare.create_run_directory(settings.data_dir, f"verify-{finished}")
    junk = prepare.create_run_directory(settings.data_dir, "verify-not-a-uuid")
    removed = await manager.sweep_orphans()
    assert sorted(removed) == sorted([f"verify-{finished}", "verify-not-a-uuid"])
    assert keep.root.exists() and not drop.root.exists() and not junk.root.exists()


# Heartbeats ------------------------------------------------------------------------------------


RUNTIMES = [{"name": "claude_code", "version": "0.1.0", "ready": True, "image": "exradius-runtime-claude-code:0.1.0", "error": None}]


class FakeReporter:
    def __init__(self):
        self.report = {"docker_ok": True, "docker_detail": "reachable", "runtimes": list(RUNTIMES),
                       "images": [{"tag": "python:3.14", "created_at": "2026-11-05T10:00:00Z", "size": 1}]}

    async def docker_report(self):
        return dict(self.report)


async def test_heartbeat_upserts_and_removes_its_row(sessions):
    reporter = FakeReporter()
    heartbeat = Heartbeat(sessions, reporter, worker_id="w1", concurrency=3)
    await heartbeat.beat()
    async with sessions() as session:
        first = await session.get(Worker, "w1")
        assert (first.concurrency, first.docker_ok, first.runtimes) == (3, True, RUNTIMES)
        assert first.images[0]["tag"] == "python:3.14" and first.hostname
    reporter.report["docker_ok"] = False
    reporter.report["runtimes"] = [{**RUNTIMES[0], "ready": False, "error": "Docker is not reachable"}]
    await heartbeat.beat()
    async with sessions() as session:
        rows = (await session.scalars(select(Worker))).all()
        assert len(rows) == 1 and rows[0].docker_ok is False and rows[0].runtimes[0]["ready"] is False
        assert rows[0].last_seen_at >= first.last_seen_at and rows[0].started_at == first.started_at
    await heartbeat.stop()
    async with sessions() as session:
        assert await session.get(Worker, "w1") is None
