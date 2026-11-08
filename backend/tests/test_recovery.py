"""Crash recovery on Kubernetes (docs/kubernetes.md §4): a starting worker compares the Runs
with Postgres; the sweep settles every unfinished run from its Run as it is now."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from exradius.models import InfoRequest, ProjectVerification, ProviderTest, Run, utcnow
from exradius.states import State
from exradius.worker import run_objects
from exradius.worker.runs import ABANDONED_AFTER
from tests.fakes import runner
from tests.test_run_manager import (  # noqa: F401 - fixtures
    cluster,
    manager,
    published,
    results,
    states,
    submit_task,
)


async def leave_running(manager, cluster, sessions, run_id, status="running", **status_fields) -> str:
    """A run in ``status`` whose Run exists, as a dead worker left it (no pod simulation)."""
    cluster.auto = False
    task = asyncio.create_task(manager.execute(run_id))
    name = f"run-{run_id}"
    while name not in cluster.secrets:
        await asyncio.sleep(0.01)
    if status == "running":
        cluster.set_status(name, phase="Running", workstation={"state": "running"}, **status_fields)
        await manager.settle(run_id)
    task.cancel()  # the worker dies
    await asyncio.gather(task, return_exceptions=True)
    manager._released.clear()
    return name


async def run_state(sessions, run_id) -> Run:
    async with sessions() as session:
        return await session.get(Run, run_id)


async def test_a_run_with_its_run_object_is_settled(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    await runner(sessions)(run_id, {})  # the runner reported while no worker was there
    cluster.set_status(name, phase="Succeeded", workstation={"state": "terminated", "exitCode": 0})

    await manager.recover()
    assert states(published)[-1] == "completed"
    assert results(published)[0]["state"] == "completed"
    assert name in cluster.deleted


async def test_recover_holds_no_slot_for_a_run_it_finished(manager, sessions, store, cluster, published, configured,
                                                          accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    await runner(sessions)(run_id, {})
    cluster.set_status(name, phase="Succeeded", workstation={"state": "terminated", "exitCode": 0})
    assert await manager.recover() == []


async def test_a_live_run_is_kept_and_holds_a_slot(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    held = await manager.recover()
    assert held == [run_id]
    assert (await run_state(sessions, run_id)).state == State.RUNNING
    assert name in cluster.runs and cluster.deleted == []


async def test_a_run_whose_object_is_gone_fails(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    del cluster.runs[name]
    await manager.recover()
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "the run's Kubernetes object disappeared"


async def preparing_without_run(manager, sessions, run_id, started_ago: float = 0) -> None:
    """A run moved to preparing by a worker that died before it created the Run."""
    await manager._move(run_id, State.PREPARING, expect={State.QUEUED})
    async with sessions() as session:
        run = await session.get(Run, run_id)
        run.started_at = utcnow() - timedelta(seconds=started_ago)
        await session.commit()


async def test_preparing_without_a_run_fails_when_no_other_worker_lives(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    await preparing_without_run(manager, sessions, run_id)
    await manager.recover()
    [result] = results(published)
    assert result["error"] == "the worker restarted while preparing the run"


async def test_preparing_without_a_run_is_left_to_a_live_worker_while_young(
        manager, sessions, store, cluster, published, configured, accept):
    async def alive():
        return True

    manager.other_workers_alive = alive
    run_id = await submit_task(accept, published)
    await preparing_without_run(manager, sessions, run_id, started_ago=60)
    await manager.recover()
    await manager.sweep()
    assert (await run_state(sessions, run_id)).state == State.PREPARING
    assert results(published) == []

    async with sessions() as session:  # older than any preparation takes
        run = await session.get(Run, run_id)
        run.started_at = utcnow() - timedelta(seconds=ABANDONED_AFTER + 60)
        await session.commit()
    await manager.sweep()
    assert results(published)[0]["error"] == "the worker restarted while preparing the run"


async def test_this_workers_own_preparation_is_not_abandoned(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    await preparing_without_run(manager, sessions, run_id)
    manager._hold_from_now(run_id)  # this process is preparing it
    await manager.sweep()
    assert (await run_state(sessions, run_id)).state == State.PREPARING
    manager._released.clear()


async def test_a_run_object_of_a_finished_run_is_deleted(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await manager.execute(run_id)
    await cluster.idle()
    name = f"run-{run_id}"
    stray = {"apiVersion": "exradius.dev/v1alpha1", "kind": "Run", "metadata": {"name": name}, "spec": {}, "status": {}}
    cluster.runs[name] = stray  # finalize could not delete it
    unknown = f"run-{uuid.uuid4()}"
    cluster.runs[unknown] = {**stray, "metadata": {"name": unknown}}
    await manager.recover()
    assert name not in cluster.runs and unknown not in cluster.runs


async def test_waiting_runs_are_left_alone(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await manager.execute(run_id)
    name = f"run-{run_id}"
    cluster._stop[name].set()  # the pod dies while the run waits
    await cluster.idle()
    held = await manager.recover()
    assert held == []  # a waiting run holds no slot
    assert (await run_state(sessions, run_id)).state == State.WAITING_FOR_INPUT
    assert name in cluster.runs


async def test_a_cancelled_run_that_was_not_finalised_is_finalised(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    await manager._move(run_id, State.CANCELLED, reason="cancelled by admin")  # then the worker died
    cluster.set_status(name, phase="Failed", reason="Cancelled")
    await manager.recover()
    assert [r["state"] for r in results(published)] == ["cancelled"]
    assert name in cluster.deleted


async def test_double_settle_is_harmless(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    await runner(sessions)(run_id, {})
    cluster.set_status(name, phase="Succeeded", workstation={"state": "terminated", "exitCode": 0})
    obj = await cluster.get_run(name)
    await asyncio.gather(manager.settle(run_id, obj), manager.settle(run_id, obj), manager.settle(run_id))
    await manager.sweep()
    assert states(published).count("completed") == 1
    assert len(results(published)) == 1


async def test_sweep_picks_up_a_lost_nudge(manager, sessions, store, cluster, published, configured, accept):
    """The runner reported waiting; the run_reported message never arrived."""
    run_id = await submit_task(accept, published)
    await leave_running(manager, cluster, sessions, run_id)
    await runner(sessions, status="waiting_for_input", question="Q?")(run_id, {})
    await manager.sweep()
    assert states(published)[-1] == "waiting_for_input"
    assert [e.type for e in published].count("info_request") == 1


async def test_waiting_run_with_answer_is_still_resumable(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await manager.execute(run_id)
    async with sessions() as session:
        request = await session.scalar(select(InfoRequest))
        request.answer, request.answered_at = "A", utcnow()
        await session.commit()
    await manager.recover()
    assert await manager.resumable_runs() == [run_id]


async def test_stray_verify_and_probe_runs_are_deleted(settings, sessions, store, cluster, configured):
    from exradius.worker.main import Worker

    worker = Worker(settings, cluster=cluster)
    try:
        async with sessions() as session:
            open_one = ProjectVerification(project_id=configured["project"].id, state="running", steps=[])
            done_one = ProjectVerification(project_id=configured["project"].id, state="passed", steps=[])
            test = ProviderTest(provider_id=configured["provider"].id, state="failed", fingerprint="x")
            session.add_all([open_one, done_one, test])
            await session.commit()
        names = [run_objects.run_name("verify", open_one.id), run_objects.run_name("verify", done_one.id),
                 run_objects.run_name("probe", test.id)]
        for name in names:
            cluster.runs[name] = {"metadata": {"name": name}, "spec": {}, "status": {}}
        await worker._delete_stray_runs()
        assert list(cluster.runs) == [names[0]]  # only the open verification keeps its Run
    finally:
        await worker.engine.dispose()


@pytest.mark.parametrize("kind", ["DELETED", "MODIFIED"])
async def test_watch_events_settle_task_runs_only(manager, sessions, store, cluster, published, configured, accept, kind):
    run_id = await submit_task(accept, published)
    name = await leave_running(manager, cluster, sessions, run_id)
    await manager.on_event(kind, {"metadata": {"name": f"verify-{uuid.uuid4()}"}})  # not a task Run: ignored
    assert (await run_state(sessions, run_id)).state == State.RUNNING
    if kind == "DELETED":
        del cluster.runs[name]
        await manager.on_event(kind, {"metadata": {"name": name}})
        assert results(published)[0]["error"] == "the run's Kubernetes object disappeared"
    else:
        await runner(sessions)(run_id, {})
        cluster.set_status(name, phase="Succeeded", workstation={"state": "terminated", "exitCode": 0})
        await manager.on_event(kind, await cluster.get_run(name))
        assert results(published)[0]["state"] == "completed"
