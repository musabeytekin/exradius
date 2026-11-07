"""Worker side of the onboarding additions: project verification and heartbeats."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from exradius.models import ProjectVerification, Worker
from exradius.worker import prepare
from exradius.worker.heartbeat import Heartbeat
from exradius.worker.verify import ProjectVerifier
from tests.conftest import BASE_CONFIG, commit_config
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


VERIFY_CONFIG = BASE_CONFIG + """\
workstation:
  image: acme/node:1
  setup: [npm ci, make check]
repositories:
  - {name: app, url: %s, branch: feature}
"""


@pytest.fixture
async def project(sessions, configured, remote_repo):
    commit_config(configured["remote"], {"exradius.yaml": VERIFY_CONFIG % remote_repo})
    return configured["project"]


def names_and_states(verification) -> list[tuple[str, str]]:
    return [(step["name"], step["state"]) for step in verification.steps]


async def test_verification_passes(verifier, sessions, containers, project, settings):
    vid = await new_verification(sessions, project.id)
    assert await verifier.claim(vid, "me") is True
    assert await verifier.claim(vid, "someone-else") is False  # already claimed

    seen = {}

    async def setup(*, image, run_id, paths, env, command, service_network=None, memory=None, caches=()):
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
    assert verification.state == "passed" and verification.error is None and verification.diagnosis is None
    assert verification.started_at and verification.ended_at
    async with sessions() as session:
        head = (await session.get(type(project), project.id)).snapshot_commit
    assert verification.commit == head and len(head) == 40  # recorded by "Read config"
    assert names_and_states(verification) == [
        ("Read config", "passed"),
        ("Create directory", "passed"),
        ("Fetch mirror app", "passed"),
        ("Clone app (feature)", "passed"),
        ("Prepare attachments folder", "passed"),
        ("Setup: npm ci", "passed"),
        ("Setup: make check", "passed"),
        ("Remove directory", "passed"),
    ]
    steps = {s["name"]: s for s in verification.steps}
    assert steps["Read config"]["log"].startswith("commit ") and "(exradius.yaml on main)" in steps["Read config"]["log"]
    assert steps["Read config"]["log"].endswith("missing keys: none")
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
    assert verification.diagnosis is None  # nothing recognised: the panel shows the step and its tail
    assert verification.commit
    assert containers.setup_commands == ["npm ci"]
    assert not (settings.data_dir / "runs" / f"verify-{vid}").exists()


async def test_failure_is_diagnosed(verifier, sessions, containers, project):
    from exradius.worker.containers import ContainerExit

    async def setup(**_):
        return ContainerExit(127, "/bin/sh: 1: corepack: not found\n")

    containers.run_setup_command = setup
    vid = await new_verification(sessions, project.id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    assert verification.diagnosis.startswith("The workstation image has no `corepack`")


async def test_bad_repository_fails_at_the_mirror(verifier, sessions, containers, configured, tmp_path):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + f"repositories:\n  - {{name: app, url: {tmp_path / 'missing.git'}}}\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error.startswith("Fetch mirror app: git clone")
    assert names_and_states(verification)[2:4] == [("Fetch mirror app", "failed"), ("Clone app (default branch)", "skipped")]
    assert verification.diagnosis.startswith("Repository app was not found: check its `url`")


async def test_read_config_fails_with_errors_and_missing_keys(verifier, sessions, containers, configured):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "secret_store: s\nenv: {A: '${secret:A}'}\nnotez: x\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error == "Read config: config error: notez: unknown key"
    assert names_and_states(verification) == [("Read config", "failed")]
    assert verification.commit  # the file was read, with errors
    assert verification.diagnosis == "The config has errors: notez: unknown key."

    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "secret_store: s\nenv: {A: '${secret:A}'}\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verifier.run(vid)
    [step] = (await get(sessions, vid)).steps
    assert step["log"].splitlines()[0] == "secret store s does not exist"
    assert step["log"].splitlines()[-1] == "missing keys: A (store s, which does not exist)"


async def test_builds_come_before_the_directory(verifier, sessions, containers, configured):
    commit_config(configured["remote"], {
        "exradius.yaml": BASE_CONFIG + (
            "workstation:\n  dockerfile: ws/Dockerfile\n  setup: [psql -c 'select 1']\n"
            "services:\n  db:\n    dockerfile: db.Dockerfile\n    context: db\n"
        ),
        "ws/Dockerfile": "FROM ubuntu\n", "db.Dockerfile": "FROM postgres:16\n", "db/init.sql": "select 1;\n",
    })
    vid = await new_verification(sessions, configured["project"].id)
    await verifier.run(vid)
    verification = await get(sessions, vid)
    assert verification.state == "passed", verification.error
    assert [n for n, _ in names_and_states(verification)] == [
        "Read config", "Build workstation", "Build service db", "Create directory", "Prepare attachments folder",
        "Start service db", "Setup: psql -c 'select 1'", "Stop services", "Remove directory",
    ]
    ws, db = containers.builds
    assert db["dockerfile"] == ".exradius.Dockerfile" and db["files"] == {"init.sql", ".exradius.Dockerfile"}
    assert containers.setup_images == [ws["tag"]]
    assert containers.services == {}  # stopped
    steps = {s["name"]: s for s in verification.steps}
    assert steps["Build workstation"]["log"].startswith("Successfully built")
    assert steps["Build workstation"]["log"].splitlines()[-1].startswith(f"built {ws['tag']} in ")

    vid = await new_verification(sessions, configured["project"].id)
    await verifier.run(vid)
    steps = {s["name"]: s for s in (await get(sessions, vid)).steps}
    assert steps["Build service db"]["log"] == f"{db['tag']} is up to date (context unchanged)"
    assert len(containers.builds) == 2


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
