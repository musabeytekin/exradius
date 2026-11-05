"""Provider tests that need a run container (docs/providers.md §2.3).

There is no cheap API check for a Claude subscription token, so the worker starts a
container from the runner image with the provider injected and runs a one-line session
(``exradius-runner --probe``). The result is recorded in ``provider_tests`` and, when the
provider is unchanged since the test was requested, as the provider's ``last_test``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from exradius.models import Provider, ProviderTest, utcnow
from exradius.providers import fingerprint, last_test, runtime_environment
from exradius.runtimes import RuntimeName, runtime_class
from exradius.runtimes.base import PROBE_PREFIX
from exradius.worker.runs import RunManager

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 180
OPEN_STATES = ("pending", "running")
LABEL_PREFIX = "provider-test-"


def parse_probe(exit_code: int, logs: str) -> tuple[bool, str | None, str | None]:
    """The probe's last ``EXRADIUS_PROBE`` line, or the log tail when there is none."""
    for line in reversed(logs.splitlines()):
        if line.startswith(PROBE_PREFIX):
            try:
                report = json.loads(line[len(PROBE_PREFIX):])
                return bool(report.get("ok")), report.get("detail"), report.get("error")
            except ValueError:
                break
    tail = logs.strip()[-1000:]
    return False, None, f"the probe exited with {exit_code} without a result:\n{tail}"


class ProviderTester:
    def __init__(self, runs: RunManager):
        self.runs = runs
        self.settings = runs.settings
        self.sessions = runs.sessions
        self.containers = runs.containers
        self.box = runs.box

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
        async with self.sessions() as session:
            test = await session.get(ProviderTest, test_id)
            provider = await session.get(Provider, test.provider_id) if test else None
            if test is None or test.state != "pending":
                return
            test.state, test.started_at = "running", utcnow()
            await session.commit()
            if provider is None:
                ok, detail, error = False, None, "the provider was deleted"
                env = None
            else:
                runtime = self.runtime_for(provider)
                env = runtime_environment(runtime, self.box, provider)

        if env is not None:
            try:
                # Like a run: the default project image with the runtime bundle mounted.
                result = await self.containers.run_probe(
                    image=self.settings.project_default_image,
                    label=f"{LABEL_PREFIX}{test_id}",
                    env=env,
                    timeout=PROBE_TIMEOUT,
                    runtime=await self.containers.ensure_runtime(runtime),
                )
                ok, detail, error = parse_probe(result.exit_code, result.logs)
            except Exception as exc:  # recorded on the test
                log.exception("provider test %s crashed", test_id)
                ok, detail, error = False, None, f"{type(exc).__name__}: {exc}"
        await self._finish(test_id, ok, detail, error)
        log.info("provider test %s: %s", test_id, "passed" if ok else "failed")

    async def _finish(self, test_id: uuid.UUID, ok: bool, detail: str | None, error: str | None) -> None:
        async with self.sessions() as session:
            test = await session.get(ProviderTest, test_id, with_for_update=True, populate_existing=True)
            if test is None:
                return
            test.state = "passed" if ok else "failed"
            test.ended_at = utcnow()
            test.detail, test.error = detail, error
            provider = await session.get(Provider, test.provider_id)
            if provider is not None and fingerprint(provider) == test.fingerprint:
                provider.last_test = last_test(ok, detail, error, at=test.ended_at)
            await session.commit()

    async def reconcile(self, is_alive: Callable[[str], Awaitable[bool]]) -> list[uuid.UUID]:
        """Fail tests held by a worker that is no longer alive, and remove their containers."""
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
            if owner == self.runs.worker_id or await is_alive(owner):
                continue
            async with self.sessions() as session:
                test = await session.get(ProviderTest, test_id, with_for_update=True, populate_existing=True)
                if test is None or test.state not in OPEN_STATES or test.worker_id != owner:
                    continue
                test.state, test.ended_at = "failed", utcnow()
                test.error = "worker restarted during the test"
                await session.commit()
            label = f"{LABEL_PREFIX}{test_id}"
            await self.containers.remove_containers([c.id for c in await self.containers.list_run_containers(label)])
            failed.append(test_id)
        return failed
