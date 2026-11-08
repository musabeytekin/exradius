"""Provider tests that need a run pod (docs/providers.md §2.3, docs/kubernetes.md §13).

There is no cheap API check for a Claude subscription token, so the worker creates a Run
with ``purpose: probe``: the default project image with the runtime bundle, no workspace,
the provider's variables from the per-run Secret, and a one-line session
(``exradius-runner --probe``) that reports through the internal API. The result is recorded
in ``provider_tests`` and, when the provider is unchanged since the test was requested, as
the provider's ``last_test``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from exradius.crypto import hash_token, new_run_token
from exradius.models import Provider, ProviderTest, utcnow
from exradius.providers import fingerprint, last_test, runtime_environment
from exradius.runtimes import RuntimeName, runtime_class
from exradius.worker import run_objects
from exradius.worker.run_objects import RunView, run_name, tail
from exradius.worker.runs import RunManager

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 180
OPEN_STATES = ("pending", "running")
POLL_SECONDS = 2.0


def probe_failure(view: RunView) -> str:
    """Why a probe pod ended without a report."""
    workstation = view.workstation
    if workstation.get("state") == "terminated":
        return (f"the probe exited with {workstation.get('exitCode')} without a result:\n"
                f"{tail(workstation.get('log') or '', 1000)}").strip()
    return view.message or f"the probe pod ended ({view.reason or view.phase}) without a result"


class ProviderTester:
    def __init__(self, runs: RunManager):
        self.runs = runs
        self.settings = runs.settings
        self.sessions = runs.sessions
        self.cluster = runs.cluster
        self.box = runs.box
        self.poll = POLL_SECONDS
        self.timeout = PROBE_TIMEOUT

    async def claim(self, test_id: uuid.UUID, worker_id: str | None) -> bool:
        async with self.sessions() as session:
            test = await session.get(ProviderTest, test_id, with_for_update=True, populate_existing=True)
            if test is None or test.state != "pending" or test.worker_id:
                return False
            test.worker_id = worker_id
            await session.commit()
            return True

    def runtime_for(self, provider: Provider) -> str:
        """The first runtime that accepts this provider (only Claude Code in v1)."""
        for name in RuntimeName:
            if runtime_class(name).capabilities.accepts(provider.kind, provider.auth_method):
                return name.value
        raise ValueError(f"no runtime accepts {provider.kind}/{provider.auth_method}")

    async def run(self, test_id: uuid.UUID) -> None:
        token = new_run_token()
        objects = None
        async with self.sessions() as session:
            test = await session.get(ProviderTest, test_id)
            provider = await session.get(Provider, test.provider_id) if test else None
            if test is None or test.state != "pending":
                return
            test.state, test.started_at = "running", utcnow()
            if provider is None:
                ok, detail, error = False, None, "the provider was deleted"
            else:
                runtime = self.runtime_for(provider)
                objects = run_objects.probe_run(
                    test_id=test_id, namespace=self.runs.namespace, runtime=runtime,
                    runtime_image=self.settings.runtime_image(runtime), image=self.settings.project_default_image,
                    provider_env=runtime_environment(runtime, self.box, provider), model=test.model, token=token,
                )
                test.token_hash = hash_token(token)
            await session.commit()

        if objects is not None:
            try:
                ok, detail, error = await self._probe(test_id, objects)
            except Exception as exc:  # recorded on the test
                log.exception("provider test %s crashed", test_id)
                ok, detail, error = False, None, f"{type(exc).__name__}: {exc}"
            finally:
                await self.runs._delete_run(objects.name)
        await self._finish(test_id, ok, detail, error)
        log.info("provider test %s: %s", test_id, "passed" if ok else "failed")

    async def _probe(self, test_id: uuid.UUID, objects) -> tuple[bool, str | None, str | None]:
        """Like a run: the default project image with the runtime bundle mounted."""
        await self.runs.create(objects)
        deadline = time.monotonic() + self.timeout
        while True:
            async with self.sessions() as session:
                report = await session.scalar(select(ProviderTest.report).where(ProviderTest.id == test_id))
            if report is not None:
                return bool(report.get("ok")), report.get("detail"), report.get("error")
            view = RunView(await self.cluster.get_run(objects.name))
            if not view.exists:
                return False, None, "the probe Run disappeared"
            if view.final:
                # The report may have landed between the two reads.
                async with self.sessions() as session:
                    report = await session.scalar(select(ProviderTest.report).where(ProviderTest.id == test_id))
                if report is not None:
                    return bool(report.get("ok")), report.get("detail"), report.get("error")
                return False, None, probe_failure(view)
            if time.monotonic() >= deadline:
                waiting = f" ({message})" if (message := view.unschedulable) else ""
                return False, None, f"the probe did not finish within {self.timeout:.0f}s{waiting}"
            await asyncio.sleep(self.poll)

    async def _finish(self, test_id: uuid.UUID, ok: bool, detail: str | None, error: str | None) -> None:
        async with self.sessions() as session:
            test = await session.get(ProviderTest, test_id, with_for_update=True, populate_existing=True)
            if test is None:
                return
            test.state = "passed" if ok else "failed"
            test.ended_at = utcnow()
            test.token_hash = None
            test.detail, test.error = detail, error
            provider = await session.get(Provider, test.provider_id)
            # A failed test of a chosen model says nothing against the credential.
            if provider is not None and fingerprint(provider) == test.fingerprint and (ok or test.model is None):
                provider.last_test = last_test(ok, detail, error, at=test.ended_at)
            await session.commit()

    async def reconcile(self, is_alive: Callable[[str], Awaitable[bool]], worker_id: str) -> list[uuid.UUID]:
        """Fail tests held by a worker that is no longer alive, and delete their Runs."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ProviderTest.id, ProviderTest.worker_id).where(
                        ProviderTest.state.in_(OPEN_STATES), ProviderTest.worker_id.is_not(None)
                    )
                )
            ).all()
        failed = []
        for test_id, owner in rows:
            if owner == worker_id or await is_alive(owner):
                continue
            async with self.sessions() as session:
                test = await session.get(ProviderTest, test_id, with_for_update=True, populate_existing=True)
                if test is None or test.state not in OPEN_STATES or test.worker_id != owner:
                    continue
                test.state, test.ended_at = "failed", utcnow()
                test.error = "worker restarted during the test"
                test.token_hash = None
                await session.commit()
            await self.runs._delete_run(run_name("probe", test_id))
            failed.append(test_id)
        return failed

    async def open_ids(self) -> set[uuid.UUID]:
        async with self.sessions() as session:
            return set((await session.scalars(
                select(ProviderTest.id).where(ProviderTest.state.in_(OPEN_STATES))
            )).all())
