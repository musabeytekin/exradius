"""Worker crash recovery: startup reconciliation and the orphaned directory sweep."""

from __future__ import annotations

import uuid

import pytest

from exradius.models import InfoRequest, Run, utcnow
from exradius.states import State
from exradius.worker import prepare
from tests.test_run_manager import (  # noqa: F401 - fixtures
    containers,
    manager,
    published,
    results,
    runner,
    states,
    submit_task,
)

DEAD = "dead-worker"
ALIVE = "live-worker"


async def is_alive(worker_id: str) -> bool:
    return worker_id == ALIVE


async def leave_run(sessions, store, published, manager, state: State, task_id="T-1", owner=DEAD,
                    container_id: str | None = "c-old", reported: str | None = None) -> tuple[uuid.UUID, prepare.RunPaths]:
    """A run as a crashed worker leaves it: mid-flight state, directory on disk."""
    run_id = await submit_task(sessions, store, published, task_id=task_id)
    paths = prepare.create_run_directory(manager.settings.data_dir, str(run_id))
    (paths.project / "work.txt").write_text("uncommitted")
    prepare.write_attachments(paths.attachments, [("a.txt", b"x")])
    prepare.seal_read_only(paths.attachments)
    async with sessions() as session:
        run = await session.get(Run, run_id)
        run.state = run.task.state = state
        run.worker_id = owner
        run.container_id = container_id
        run.directory = str(paths.root)
        run.reported_status = reported
        run.token_hash = "abc"
        await session.commit()
    return run_id, paths


async def run_row(sessions, run_id) -> Run:
    async with sessions() as session:
        return await session.get(Run, run_id)


@pytest.fixture(autouse=True)
def as_restarted_worker(manager):
    manager.worker_id = "new-worker"


@pytest.mark.parametrize("state", [State.PREPARING, State.RUNNING, State.RESUMING])
async def test_run_without_container_is_failed_and_cleaned_up(
    manager, sessions, store, containers, published, configured, state
):
    run_id, paths = await leave_run(sessions, store, published, manager, state)
    assert await manager.reconcile(is_alive) == []

    run = await run_row(sessions, run_id)
    assert run.state == State.FAILED and run.ended_at is not None and run.token_hash is None
    assert run.error == f"worker restarted while the run was {state}"
    assert states(published)[-1] == "failed"
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == run.error
    assert not paths.root.exists()


async def test_live_runner_container_is_reattached(manager, sessions, store, containers, published, configured):
    run_id, paths = await leave_run(sessions, store, published, manager, State.RUNNING, container_id="c-old")
    containers.leave_running(run_id, "c-old", runner(sessions, strict=False))

    to_attach = await manager.reconcile(is_alive)
    assert to_attach == [(run_id, "c-old")]
    assert (await run_row(sessions, run_id)).state == State.RUNNING  # not failed
    assert (await run_row(sessions, run_id)).worker_id == "new-worker"  # claimed

    await manager.reattach(run_id, "c-old")
    assert states(published)[-1] == "completed"
    assert [r["state"] for r in results(published)] == ["completed"]
    assert results(published)[0]["summary"] == "All fixed."
    assert not paths.root.exists()


async def test_container_started_but_not_recorded_is_reattached(
    manager, sessions, store, containers, published, configured
):
    """The worker died after starting the runner but before moving the run to running."""
    run_id, _ = await leave_run(sessions, store, published, manager, State.PREPARING, container_id=None)
    containers.leave_running(run_id, "c-new", runner(sessions, strict=False))
    assert await manager.reconcile(is_alive) == [(run_id, "c-new")]
    await manager.reattach(run_id, "c-new")
    assert states(published)[-2:] == ["running", "completed"]
    assert results(published)[0]["state"] == "completed"


async def test_reattached_run_can_go_on_to_wait(manager, sessions, store, containers, published, configured):
    run_id, paths = await leave_run(sessions, store, published, manager, State.RUNNING, container_id="c-old")
    containers.leave_running(run_id, "c-old", runner(sessions, strict=False, status="waiting_for_input", question="Q?"))
    for rid, cid in await manager.reconcile(is_alive):
        await manager.reattach(rid, cid)
    assert states(published)[-1] == "waiting_for_input"
    assert [e.payload["question"] for e in published if e.type == "info_request"] == ["Q?"]
    assert paths.root.exists() and results(published) == []


async def test_setup_containers_of_a_dead_run_are_removed(manager, sessions, store, containers, published, configured):
    run_id, paths = await leave_run(sessions, store, published, manager, State.PREPARING, container_id=None)
    containers.leave_running(run_id, "c-setup", runner(sessions), kind="setup")
    assert await manager.reconcile(is_alive) == []
    assert containers.removed == ["c-setup"]
    assert (await run_row(sessions, run_id)).state == State.FAILED
    assert not paths.root.exists()


async def test_reported_outcome_is_honoured_when_container_is_gone(
    manager, sessions, store, containers, published, configured
):
    """The worker died after the container exited but before acting on its outcome."""
    run_id, _ = await leave_run(sessions, store, published, manager, State.RUNNING, reported="completed")
    assert await manager.reconcile(is_alive) == []
    assert (await run_row(sessions, run_id)).state == State.COMPLETED
    assert [r["state"] for r in results(published)] == ["completed"]


async def test_runs_of_live_workers_are_left_alone(manager, sessions, store, containers, published, configured):
    run_id, paths = await leave_run(sessions, store, published, manager, State.PREPARING, owner=ALIVE, container_id=None)
    assert await manager.reconcile(is_alive) == []
    run = await run_row(sessions, run_id)
    assert run.state == State.PREPARING and run.worker_id == ALIVE
    assert paths.root.exists() and results(published) == []


async def test_two_restarting_workers_recover_a_run_once(manager, sessions, store, containers, published, configured):
    run_id, _ = await leave_run(sessions, store, published, manager, State.RUNNING, container_id="c-old")
    containers.leave_running(run_id, "c-old", runner(sessions, strict=False))
    assert await manager.reconcile(is_alive) == [(run_id, "c-old")]

    # A second worker restarting now sees the run claimed by the first, which is alive.
    async def first_worker_is_alive(worker_id: str) -> bool:
        return worker_id == "new-worker"

    manager.worker_id = "other-new-worker"
    assert await manager.reconcile(first_worker_is_alive) == []
    assert (await run_row(sessions, run_id)).worker_id == "new-worker"


async def test_double_settle_is_harmless(manager, sessions, store, containers, published, configured):
    """Two workers waiting on the same container: only the first acts on its exit."""
    run_id, _ = await leave_run(sessions, store, published, manager, State.RUNNING, container_id="c-old",
                                reported="waiting_for_input")
    await manager._settle(run_id, "c-old", wait=False)
    await manager._settle(run_id, "c-old", wait=False)
    assert states(published).count("waiting_for_input") == 1


async def test_waiting_runs_are_left_alone(manager, sessions, store, containers, published, configured):
    run_id, paths = await leave_run(sessions, store, published, manager, State.WAITING_FOR_INPUT, container_id=None)
    assert await manager.reconcile(is_alive) == []
    assert await manager.sweep_orphans() == []
    assert (await run_row(sessions, run_id)).state == State.WAITING_FOR_INPUT
    assert paths.root.exists() and (paths.project / "work.txt").read_text() == "uncommitted"


async def test_sweep_removes_orphans_and_keeps_live_runs(manager, sessions, store, containers, published, configured):
    waiting_id, waiting = await leave_run(
        sessions, store, published, manager, State.WAITING_FOR_INPUT, task_id="W", container_id=None
    )
    finished_id, finished = await leave_run(sessions, store, published, manager, State.RUNNING, task_id="F")
    async with sessions() as session:
        run = await session.get(Run, finished_id)
        run.state = run.task.state = State.COMPLETED
        await session.commit()
    data = manager.settings.data_dir
    stray = prepare.create_run_directory(data, str(uuid.uuid4()))  # no row at all
    prepare.write_attachments(stray.attachments, [("a.txt", b"x")])
    prepare.seal_read_only(stray.attachments)  # read-only dirs must not stop the sweep
    junk = data / "runs" / "not-a-run-id"
    junk.mkdir()
    (data / "runs" / "stray-file").write_text("left alone")
    mirror = data / "mirrors" / "billing" / "app.git"
    mirror.mkdir(parents=True)

    removed = await manager.sweep_orphans()
    assert sorted(removed) == sorted([str(finished_id), stray.root.name, "not-a-run-id"])
    assert waiting.root.exists()
    assert not finished.root.exists() and not stray.root.exists() and not junk.exists()
    assert (data / "runs" / "stray-file").exists() and mirror.exists()


async def test_sweep_without_runs_directory(manager):
    assert await manager.sweep_orphans() == []


async def test_waiting_run_with_answer_is_still_resumable(manager, sessions, store, containers, published, configured):
    run_id, _ = await leave_run(sessions, store, published, manager, State.WAITING_FOR_INPUT, container_id=None)
    async with sessions() as session:
        session.add(InfoRequest(run_id=run_id, question="Q?", answer="A", answered_at=utcnow()))
        await session.commit()
    assert await manager.reconcile(is_alive) == []
    assert await manager.resumable_runs() == [run_id]


