"""Worker side of the onboarding additions: project verification as a verify Run
(docs/kubernetes.md §13) and heartbeats."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from exradius.models import ConnectionTools, ProjectVerification, Worker
from exradius.worker.heartbeat import Heartbeat, lease_status, operator_status
from exradius.worker.verify import ProjectVerifier
from tests.conftest import BASE_CONFIG, commit_config
from tests.test_run_manager import cluster, manager, published  # noqa: F401 - fixtures


@pytest.fixture
def verifier(manager):
    verifier = ProjectVerifier(manager)
    verifier.poll = 0.01
    return verifier


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

TOOLS = [{"name": "search", "description": "Search."}]


@pytest.fixture
async def project(sessions, configured, remote_repo):
    commit_config(configured["remote"], {"exradius.yaml": VERIFY_CONFIG % remote_repo})
    return configured["project"]


def names_and_states(verification) -> list[tuple[str, str]]:
    return [(step["name"], step["state"]) for step in verification.steps]


async def verify(verifier, vid):
    await asyncio.wait_for(verifier.run(vid), 10)


async def test_verification_passes(verifier, sessions, cluster, project, settings):
    cluster.tool_reports = {"docs": {"ok": True, "tools": TOOLS, "error": None, "log": ""}}
    vid = await new_verification(sessions, project.id)
    assert await verifier.claim(vid, "me") is True
    assert await verifier.claim(vid, "someone-else") is False  # already claimed
    await verify(verifier, vid)

    verification = await get(sessions, vid)
    assert verification.state == "passed" and verification.error is None and verification.diagnosis is None
    assert verification.started_at and verification.ended_at
    async with sessions() as session:
        head = (await session.get(type(project), project.id)).snapshot_commit
    assert verification.commit == head and len(head) == 40  # recorded by "Read config"
    assert names_and_states(verification) == [
        ("Read config", "passed"),
        ("Prepare workspace", "passed"),
        ("Clone app (feature)", "passed"),
        ("Setup: npm ci", "passed"),
        ("Setup: make check", "passed"),
        ("List tools docs", "passed"),
    ]
    steps = {s["name"]: s for s in verification.steps}
    assert steps["List tools docs"]["log"] == "1 tool: search"
    assert steps["Read config"]["log"].startswith("commit ") and "(exradius.yaml on main)" in steps["Read config"]["log"]
    assert steps["Read config"]["log"].endswith("missing keys: none")
    assert steps["Setup: npm ci"]["log"] == "ran npm ci"
    assert all(s["started_at"] and s["ended_at"] for s in verification.steps)
    assert verification.token_hash is None  # revoked at the end

    [created] = cluster.created
    name = f"verify-{vid}"
    assert created["metadata"]["name"] == name
    assert created["metadata"]["labels"]["exradius.dev/purpose"] == "verify"
    spec = created["spec"]
    assert (spec["purpose"], spec["id"], spec["project"]) == ("verify", str(vid), "billing-platform")
    assert spec["workstation"]["image"] == "acme/node:1" and spec["workstation"]["setup"] == ["npm ci", "make check"]
    assert spec["runtime"]["env"] == []  # no provider in a verify pod
    assert cluster.deleted == [name]  # deleting the Run removes everything Verify made


async def test_failing_setup_skips_the_rest_and_the_run_is_deleted(verifier, sessions, cluster, project):
    cluster.verify_failure = {"container": "setup-0", "exitCode": 2, "log": "setup output\nboom"}
    vid = await new_verification(sessions, project.id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    assert verification.error == "Setup: npm ci: exited with 2"
    assert names_and_states(verification)[-3:] == [
        ("Setup: npm ci", "failed"),
        ("Setup: make check", "skipped"),
        ("List tools docs", "skipped"),
    ]
    assert "boom" in verification.steps[-3]["log"]
    assert verification.diagnosis is None  # nothing recognised: the panel shows the step and its tail
    assert verification.commit
    assert cluster.deleted == [f"verify-{vid}"]


async def test_setup_killed_under_a_declared_limit(verifier, sessions, cluster, configured, remote_repo):
    commit_config(configured["remote"], {"exradius.yaml": VERIFY_CONFIG % remote_repo + "resources: {memory: 2g}\n"})
    cluster.verify_failure = {"container": "setup-0", "exitCode": 137, "reason": "OOMKilled", "log": "Killed"}
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.steps[3]["log"] == "exited with 137: exceeded its declared memory (2g)\nKilled"
    assert verification.diagnosis.startswith("`npm ci` exceeded the workstation's declared memory (2g)")


async def test_failure_is_diagnosed(verifier, sessions, cluster, project):
    cluster.verify_failure = {"container": "setup-0", "exitCode": 127, "log": "/bin/sh: 1: corepack: not found\n"}
    vid = await new_verification(sessions, project.id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    assert verification.diagnosis.startswith("The workstation image has no `corepack`")


async def test_bad_repository_fails_at_its_clone(verifier, sessions, cluster, configured, tmp_path):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + f"repositories:\n  - {{name: app, url: {tmp_path / 'missing.git'}}}\n"})
    cluster.verify_failure = {"container": "clone-0", "exitCode": 1,
                              "log": "git clone failed: fatal: repository '/x/missing.git' does not exist"}
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error.startswith("Clone app (default branch): git clone")
    assert names_and_states(verification)[1:] == [
        ("Prepare workspace", "passed"), ("Clone app (default branch)", "failed"), ("List tools docs", "skipped"),
    ]
    assert verification.diagnosis.startswith("Repository app was not found: check its `url`")


async def test_an_image_that_cannot_be_pulled(verifier, sessions, cluster, project):
    cluster.auto = False
    vid = await new_verification(sessions, project.id)
    task = asyncio.create_task(verifier.run(vid))
    name = f"verify-{vid}"
    while name not in cluster.runs:
        await asyncio.sleep(0.01)
    cluster.set_status(name, phase="Failed", reason="ImagePullFailed",
                       message='ImagePullFailed: image "acme/node:1" could not be pulled: failed to resolve reference')
    await asyncio.wait_for(task, 5)
    verification = await get(sessions, vid)
    assert names_and_states(verification)[1:3] == [("Prepare workspace", "failed"), ("Clone app (feature)", "skipped")]
    assert verification.diagnosis.startswith("The image `acme/node:1` could not be pulled")


async def test_waiting_for_cluster_capacity_shows_on_the_first_step(verifier, sessions, cluster, project):
    cluster.auto = False
    vid = await new_verification(sessions, project.id)
    task = asyncio.create_task(verifier.run(vid))
    name = f"verify-{vid}"
    while name not in cluster.runs:
        await asyncio.sleep(0.01)
    cluster.set_status(name, phase="Pending", conditions=[{
        "type": "Scheduled", "status": "False", "reason": "Unschedulable",
        "message": "0/1 nodes are available: 1 Insufficient memory."}])
    for _ in range(100):
        step = (await get(sessions, vid)).steps[1]
        if step["state"] == "running":
            break
        await asyncio.sleep(0.01)
    assert step["log"] == "waiting for cluster capacity: 0/1 nodes are available: 1 Insufficient memory."
    del cluster.runs[name]  # removed by hand: the verification ends
    await asyncio.wait_for(task, 5)
    verification = await get(sessions, vid)
    assert verification.state == "failed"


async def test_read_config_fails_with_errors_and_missing_keys(verifier, sessions, cluster, configured):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "secret_store: s\nenv: {A: '${secret:A}'}\nnotez: x\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error == "Read config: config error: notez: unknown key"
    assert names_and_states(verification) == [("Read config", "failed")]
    assert verification.commit  # the file was read, with errors
    assert verification.diagnosis == "The config has errors: notez: unknown key."
    assert cluster.created == []

    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "secret_store: s\nenv: {A: '${secret:A}'}\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    [step] = (await get(sessions, vid)).steps
    assert step["log"].splitlines()[0] == "secret store s does not exist"
    assert step["log"].splitlines()[-1] == "missing keys: A (store s, which does not exist)"


@pytest.mark.parametrize("extra, reason", [
    ("workstation:\n  dockerfile: ws/Dockerfile\n", "workstation.dockerfile needs image builds, not available yet"),
    ("services:\n  db: {image: 'postgres:16'}\n", "services.db: services are not available yet"),
])
async def test_what_this_version_cannot_run_stops_at_read_config(verifier, sessions, cluster, configured, extra, reason):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + extra, "ws/Dockerfile": "FROM ubuntu\n"})
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert names_and_states(verification) == [("Read config", "failed")]
    assert verification.error == f"Read config: {reason}"
    assert f"not available yet: {reason}" in verification.steps[0]["log"]
    assert cluster.created == []


async def test_only_pending_verifications_run(verifier, sessions, project):
    vid = await new_verification(sessions, project.id)
    async with sessions() as session:
        (await session.get(ProjectVerification, vid)).state = "passed"
        await session.commit()
    await verifier.run(vid)
    assert (await get(sessions, vid)).steps == []


async def test_dead_workers_verifications_are_failed_and_their_runs_deleted(verifier, sessions, cluster, project):
    async def is_alive(worker_id):
        return worker_id == "live"

    dead = await new_verification(sessions, project.id, worker_id="dead")
    live = await new_verification(sessions, project.id, worker_id="live")
    queued = await new_verification(sessions, project.id)  # message still on the queue
    async with sessions() as session:
        verification = await session.get(ProjectVerification, dead)
        verification.state = "running"
        verification.token_hash = "x"
        verification.steps = [{"name": "Read config", "state": "passed"}, {"name": "Clone app (main)", "state": "running"},
                              {"name": "List tools docs", "state": "pending"}]
        await session.commit()
    cluster.runs[f"verify-{dead}"] = {"metadata": {"name": f"verify-{dead}"}, "spec": {}, "status": {}}

    assert await verifier.reconcile(is_alive, "me") == [dead]
    failed = await get(sessions, dead)
    assert failed.state == "failed" and failed.error == "worker restarted during verification"
    assert [s["state"] for s in failed.steps] == ["passed", "failed", "skipped"]
    assert failed.token_hash is None
    assert cluster.deleted == [f"verify-{dead}"]
    assert (await get(sessions, live)).state == "pending"
    assert (await get(sessions, queued)).state == "pending"
    assert await verifier.open_ids() == {dead, live, queued} - {dead}


# Tools for agent routing (docs/agent-routing.md §3.1) ------------------------------------------


async def test_tools_are_listed_and_stored(verifier, sessions, cluster, project):
    from exradius.project_state import cached
    from exradius.routing import definition_hash

    tools = [{"name": "search", "description": "Search the docs.", "annotations": {"readOnlyHint": True}},
             {"name": "write_page", "description": "Write a page."}]
    cluster.tool_reports = {"docs": {"ok": True, "tools": tools, "error": None, "log": ""}}
    vid = await new_verification(sessions, project.id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "passed"
    assert {s["name"]: s for s in verification.steps}["List tools docs"]["log"] == "2 tools: search, write_page"

    async with sessions() as session:
        [row] = (await session.scalars(select(ConnectionTools))).all()
        current = cached(await session.get(type(project), project.id)).config
    assert (row.connection, row.tools, row.verification_id) == ("docs", tools, vid)
    assert row.definition_hash == definition_hash(current, "docs") and row.commit == verification.commit

    cluster.tool_reports = {"docs": {"ok": True, "tools": tools[:1], "error": None, "log": ""}}  # a second Verify replaces it
    vid2 = await new_verification(sessions, project.id)
    await verify(verifier, vid2)
    async with sessions() as session:
        [row] = (await session.scalars(select(ConnectionTools))).all()
    assert row.tools == tools[:1] and row.verification_id == vid2


async def test_a_connection_that_cannot_list_fails_verify(verifier, sessions, cluster, project):
    cluster.tool_reports = {"docs": {"ok": False, "tools": None, "error": "McpError: Connection closed",
                                     "log": "npm ERR! 404 @acme/docs-mcp"}}
    vid = await new_verification(sessions, project.id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    assert verification.error == "List tools docs: McpError: Connection closed"
    steps = {s["name"]: s for s in verification.steps}
    assert steps["List tools docs"]["state"] == "failed"
    assert steps["List tools docs"]["log"] == "McpError: Connection closed\nnpm ERR! 404 @acme/docs-mcp"
    async with sessions() as session:
        assert (await session.scalars(select(ConnectionTools))).all() == []


async def test_a_runner_that_ends_without_listing_fails_the_step(verifier, sessions, cluster, project):
    cluster.tool_reports = {}
    vid = await new_verification(sessions, project.id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    steps = {s["name"]: s for s in verification.steps}
    assert steps["List tools docs"]["state"] == "failed"
    assert steps["List tools docs"]["log"] == "the runner ended without listing this connection's tools"


async def test_unused_connections_are_not_listed(verifier, sessions, cluster, configured, remote_repo):
    unused = (VERIFY_CONFIG % remote_repo).replace("    connections: [docs]\n", "")
    commit_config(configured["remote"], {"exradius.yaml": unused})
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "passed"
    assert not [s for s in verification.steps if s["name"].startswith("List tools")]


# Heartbeats ------------------------------------------------------------------------------------


RUNTIMES = [{"name": "claude_code", "version": "0.2.0", "ready": True, "image": "exradius-runtime-claude-code:0.2.0",
             "error": None}]


async def test_heartbeat_upserts_and_removes_its_row(sessions):
    report = {"runtimes": list(RUNTIMES), "operator": {"alive": True, "holder": "op-1", "renewed_at": None,
                                                       "detail": None}}

    async def reporter():
        return dict(report)

    heartbeat = Heartbeat(sessions, worker_id="w1", concurrency=3, report=reporter)
    other = Heartbeat(sessions, worker_id="w2", concurrency=1, report=reporter)
    await heartbeat.beat()
    assert await heartbeat.others_alive() is False
    async with sessions() as session:
        first = await session.get(Worker, "w1")
        assert (first.concurrency, first.runtimes, first.operator["alive"]) == (3, RUNTIMES, True)
        assert first.hostname
    report["operator"] = {"alive": False, "holder": None, "renewed_at": None, "detail": "no leader Lease"}
    await heartbeat.beat()
    await other.beat()
    assert await heartbeat.others_alive() is True
    async with sessions() as session:
        row = await session.get(Worker, "w1")
        assert row.operator["alive"] is False
        assert row.last_seen_at >= first.last_seen_at and row.started_at == first.started_at
    await heartbeat.stop()
    async with sessions() as session:
        assert await session.get(Worker, "w1") is None
    assert await other.others_alive() is False


def test_lease_status():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def lease(renewed: datetime, holder="exradius-operator-abc_1"):
        return {"spec": {"holderIdentity": holder, "leaseDurationSeconds": 15,
                         "renewTime": renewed.isoformat().replace("+00:00", "Z")}}

    alive = lease_status(lease(now - timedelta(seconds=5)), now)
    assert alive == {"alive": True, "holder": "exradius-operator-abc_1", "renewed_at": "2026-11-08T11:59:55+00:00",
                     "detail": None}
    stale = lease_status(lease(now - timedelta(minutes=5)), now)
    assert stale["alive"] is False and stale["detail"] == "the leader Lease has not been renewed"
    assert lease_status(lease(now, holder=""), now)["alive"] is False  # released
    missing = lease_status(None, now)
    assert missing["alive"] is False and "not running" in missing["detail"]


async def test_operator_status_reads_the_lease(settings, cluster):
    assert (await operator_status(cluster, settings))["alive"] is False
    cluster.lease = {"spec": {"holderIdentity": "op", "leaseDurationSeconds": 15,
                              "renewTime": datetime.now(UTC).isoformat()}}
    assert (await operator_status(cluster, settings))["alive"] is True

    async def broken(namespace, name):
        raise RuntimeError("forbidden")

    cluster.get_lease = broken
    status = await operator_status(cluster, settings)
    assert status["alive"] is False and status["detail"] == "RuntimeError: forbidden"
