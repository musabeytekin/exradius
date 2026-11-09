"""A fake Kubernetes API with a stand-in operator and runner, for the worker's tests.

``FakeCluster`` keeps Runs and Secrets in memory. With ``auto`` on (the default), creating a
task Run starts a simulated pod: prepare steps pass (or fail as ``prepare_failure`` says),
the workstation starts, and each item of ``behaviours`` plays the runner, writing what the
real runner reports into Postgres. A behaviour that reports ``waiting_for_input`` keeps the
pod alive until the run is resuming, then the next behaviour runs in the same pod, as the
real runner resumes in-process. The pod's exit code decides its final phase. Every status
change is followed by ``manager.settle`` (the watch). Without ``auto`` the test moves Runs
itself with ``set_status``.

Verify and probe Runs are simulated too when ``auto`` is on: a verify pod passes its steps
(or fails at ``verify_failure``), writes ``tool_reports`` as the api stores the runner's
reports, and succeeds; a probe pod writes ``probe_report`` (unless ``None``) and ends.

Builds: creating a Build starts a simulated build pod (``auto``) that waits for the Build's
Secret, as a real pod waits for its token, reads the context the worker stored (kept in
``contexts``), runs for a moment with a log, and succeeds with a digest or fails as
``build_failure`` says. ``build_pods`` lists every Build that got a pod.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from exradius.models import ImageBuild, InfoRequest, ProjectVerification, ProviderTest, Run, TranscriptChunk
from exradius.states import State, is_terminal
from exradius.worker.run_objects import parse_name

Behaviour = Callable[[uuid.UUID, dict], Awaitable[int]]


def _merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


class FakeCluster:
    def __init__(self):
        self.runs: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.patches: list[tuple[str, dict]] = []
        self.created: list[dict[str, Any]] = []
        self.lease: dict | None = None
        self.auto = True
        self.behaviours: list[Behaviour] = []
        self.prepare_failure: dict | None = None
        """A failed step for the next simulated pod: ``{"container", "name", "exitCode", "reason", "log"}``."""
        self.workstation_log = "runner log line"
        self.tool_reports: dict[str, dict] = {}
        """What a simulated verify pod's runner reports, by connection."""
        self.verify_failure: dict | None = None
        """``{"container", "exitCode", "log", "reason"?}``: the step a verify pod fails at."""
        self.probe_report: dict | None = {"ok": True, "detail": "claude-sonnet-5-5 replied 'OK'", "error": None}
        self.probe_exit = 0
        self.builds: dict[str, dict[str, Any]] = {}
        self.build_pods: list[str] = []
        self.contexts: dict[str, dict[str, bytes]] = {}
        """The files of each Build's context, as its pod fetched them."""
        self.build_failure: dict | None = None
        """``{"log", "message"?, "reason"?}``: how the next simulated build fails."""
        self.build_log = "#5 [2/2] RUN make tools\n#5 DONE 0.4s"
        self.build_seconds = 0.05
        self.manager = None
        self.sessions = None
        self.store = None
        self.pods: list[asyncio.Task] = []
        self.envs: list[dict[str, str]] = []
        """The workstation environment of every simulated pod, in order."""
        self._stop: dict[str, asyncio.Event] = {}

    # -- the API the worker uses ------------------------------------------------------------

    async def create_run(self, body):
        name = body["metadata"]["name"]
        if name in self.runs:
            return copy.deepcopy(self.runs[name])
        obj = copy.deepcopy(body)
        obj["metadata"]["uid"] = str(uuid.uuid4())
        obj["status"] = {}
        self.runs[name] = obj
        self.created.append(copy.deepcopy(obj))
        purpose = obj["spec"]["purpose"]
        if self.auto and purpose == "task":
            self.pods.append(asyncio.create_task(self._pod(name)))
        elif self.auto and purpose == "verify":
            self.pods.append(asyncio.create_task(self._verify_pod(name)))
        elif self.auto and purpose == "probe":
            self.pods.append(asyncio.create_task(self._probe_pod(name)))
        return copy.deepcopy(obj)

    async def get_run(self, name):
        obj = self.runs.get(name)
        return copy.deepcopy(obj) if obj is not None else None

    async def list_runs(self):
        return [copy.deepcopy(o) for o in self.runs.values()], "1"

    async def patch_run(self, name, spec):
        if name not in self.runs:
            return None
        self.patches.append((name, dict(spec)))
        _merge(self.runs[name]["spec"], copy.deepcopy(spec))
        if spec.get("cancel"):
            self._operator_cancel(name)
        if "podGeneration" in spec and self.auto:
            self.pods.append(asyncio.create_task(self._pod(name, resume=True)))
        return copy.deepcopy(self.runs[name])

    async def delete_run(self, name):
        self.deleted.append(name)
        found = self.runs.pop(name, None) is not None
        if name in self._stop:
            self._stop[name].set()
        self.secrets = {k: v for k, v in self.secrets.items()
                        if not any(o.get("name") == name for o in v["metadata"].get("ownerReferences") or [])}
        return found

    async def create_secret(self, body):
        self.secrets[body["metadata"]["name"]] = copy.deepcopy(body)

    async def delete_secret(self, name):
        self.secrets.pop(name, None)

    async def watch_runs(self, resource_version):
        if False:  # pragma: no cover - an empty async generator
            yield

    async def get_lease(self, namespace, name):
        return self.lease

    async def create_build(self, body):
        name = body["metadata"]["name"]
        if name in self.builds:
            return copy.deepcopy(self.builds[name]), False
        obj = copy.deepcopy(body)
        obj["metadata"]["uid"] = str(uuid.uuid4())
        obj["status"] = {}
        self.builds[name] = obj
        self.created.append(copy.deepcopy(obj))
        if self.auto:
            self.pods.append(asyncio.create_task(self._build_pod(name, obj["metadata"]["uid"])))
        return copy.deepcopy(obj), True

    async def get_build(self, name):
        obj = self.builds.get(name)
        return copy.deepcopy(obj) if obj is not None else None

    async def delete_build(self, name, uid=None):
        obj = self.builds.get(name)
        if obj is None or (uid and obj["metadata"]["uid"] != uid):
            return False
        self.deleted.append(name)
        del self.builds[name]
        self.secrets = {k: v for k, v in self.secrets.items()
                        if not any(o.get("name") == name and o.get("kind") == "Build"
                                   for o in v["metadata"].get("ownerReferences") or [])}
        return True

    # -- the operator, by hand ----------------------------------------------------------------

    def set_status(self, name: str, **status) -> None:
        obj = self.runs[name]
        status.setdefault("podGeneration", int(obj["spec"].get("podGeneration") or 0))
        obj["status"] = {**obj.get("status", {}), **status}

    def secret_data(self, name: str) -> dict[str, str]:
        data = self.secrets[name]["data"]
        return {k: base64.b64decode(v).decode() for k, v in data.items()}

    def workstation_env(self, name: str) -> dict[str, str]:
        """The workstation container's environment as the operator builds it (contract §3)."""
        obj = self.runs[name]
        spec = obj["spec"]
        secret = self.secret_data(spec["secretName"])
        env: dict[str, str] = {}
        entries = [
            *spec["workstation"].get("env", []),
            {"name": "HOME", "value": "/home/runner"},
            {"name": "EXRADIUS_STATE_DIR", "value": "/exradius/state"},
            {"name": "EXRADIUS_WORK_DIR", "value": "/work"},
            *spec.get("git", {}).get("env", []),
            *spec["runtime"].get("env", []),
            {"name": "EXRADIUS_API_URL", "value": "http://exradius-api-internal.exradius:8001"},
            {"name": "EXRADIUS_RUN_TOKEN", "secretKey": "token"},
            {"name": "EXRADIUS_PURPOSE", "value": spec["purpose"]},
            {"name": "EXRADIUS_ID", "value": spec["id"]},
        ]
        for entry in entries:
            env[entry["name"]] = entry["value"] if "value" in entry else secret[entry["secretKey"]]
        return env

    def _operator_cancel(self, name: str) -> None:
        """The operator deletes the pod; once it is gone the Run is Failed/Cancelled."""
        if name in self._stop:
            self._stop[name].set()
        else:
            self.set_status(name, phase="Failed", reason="Cancelled", message="cancelled")
            self._settle_later(name)

    def _settle_later(self, name: str) -> None:
        if self.manager is None:
            return
        parsed = parse_name(name)
        if parsed and parsed[0] == "task":
            self.pods.append(asyncio.create_task(self.manager.settle(parsed[1])))

    # -- the simulated pod ---------------------------------------------------------------------

    async def _settle(self, run_id: uuid.UUID) -> None:
        if self.manager is not None:
            await self.manager.settle(run_id)

    async def _state(self, run_id: uuid.UUID) -> tuple[str, str | None]:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            return run.state, run.reported_status

    async def _pod(self, name: str, resume: bool = False) -> None:
        await asyncio.sleep(0)  # the worker creates the Secret right after the Run
        stop = self._stop.setdefault(name, asyncio.Event())
        stop.clear()
        if name not in self.runs:
            return
        run_id = uuid.UUID(self.runs[name]["spec"]["id"])
        if self.prepare_failure is not None:
            failure, self.prepare_failure = self.prepare_failure, None
            self.set_status(name, phase="Failed", reason="StepFailed", steps=[{**failure, "state": "failed"}],
                            message=f"{failure['name']} failed")
            await self._settle(run_id)
            return
        self.set_status(name, phase="Running", workstation={"state": "running", "startedAt": "2026-11-08T00:00:00Z"})
        env = self.workstation_env(name)
        self.envs.append(env)
        await self._settle(run_id)
        exit_code = 0
        cancelled = False
        if resume:
            await self._report(run_id, "resumed")
        while self.behaviours:
            behaviour = self.behaviours.pop(0)
            exit_code = await behaviour(run_id, env)
            if stop.is_set():
                cancelled = True
                break
            state, reported = await self._state(run_id)
            if reported != "waiting_for_input" or is_terminal(state):
                break
            await self._settle(run_id)  # the api's run_reported nudge
            while not stop.is_set():
                state, _ = await self._state(run_id)
                if state == State.RESUMING or is_terminal(state):
                    break
                await asyncio.sleep(0.01)
            if stop.is_set() or is_terminal(state):
                cancelled = stop.is_set()
                break
            await self._report(run_id, "resumed")
        if name not in self.runs:
            return
        if cancelled or self.runs[name]["spec"].get("cancel"):
            self.set_status(name, phase="Failed", reason="Cancelled", message="cancelled",
                            workstation={"state": "terminated", "exitCode": 143, "reason": "Error"})
        else:
            self.set_status(
                name, phase="Succeeded" if exit_code == 0 else "Failed", reason="" if exit_code == 0 else "Error",
                workstation={"state": "terminated", "exitCode": exit_code,
                             "reason": "OOMKilled" if exit_code == 137 else ("Completed" if exit_code == 0 else "Error"),
                             "startedAt": "2026-11-08T00:00:00Z", "log": self.workstation_log},
            )
        await self._settle(run_id)

    @staticmethod
    def steps_of(spec: dict) -> list[dict]:
        """The pod's steps as the operator names them (contract §4)."""
        steps = [{"name": "Prepare workspace", "container": "prepare"}]
        for index, repo in enumerate(spec.get("repositories") or []):
            steps.append({"name": f"Clone {repo['folder']} ({repo.get('branch') or 'default branch'})",
                          "container": f"clone-{index}"})
        for index, command in enumerate(spec["workstation"].get("setup") or []):
            steps.append({"name": f"Setup: {command}", "container": f"setup-{index}"})
        return steps

    async def _verify_pod(self, name: str) -> None:
        await asyncio.sleep(0)
        if name not in self.runs:
            return
        spec = self.runs[name]["spec"]
        steps, failed = [], None
        for step in self.steps_of(spec):
            if failed is not None:
                steps.append({**step, "state": "pending"})
            elif self.verify_failure and self.verify_failure["container"] == step["container"]:
                failed = self.verify_failure
                steps.append({**step, "state": "failed", "startedAt": "2026-11-08T00:00:00Z",
                              "finishedAt": "2026-11-08T00:00:01Z", "exitCode": failed.get("exitCode", 1),
                              "reason": failed.get("reason", "Error"), "log": failed.get("log", "")})
            else:
                log = f"ran {step['name'].removeprefix('Setup: ')}" if step["container"].startswith("setup") else "ok"
                steps.append({**step, "state": "passed", "startedAt": "2026-11-08T00:00:00Z",
                              "finishedAt": "2026-11-08T00:00:01Z", "exitCode": 0, "log": log})
        if failed is not None:
            self.verify_failure = None
            self.set_status(name, phase="Failed", reason="StepFailed", steps=steps, message="step failed")
            return
        self.set_status(name, phase="Running", steps=steps, workstation={"state": "running"})
        await asyncio.sleep(0.02)
        verification_id = uuid.UUID(spec["id"])
        async with self.sessions() as session:
            verification = await session.get(ProjectVerification, verification_id)
            if verification is not None:
                verification.tool_reports = {**(verification.tool_reports or {}), **copy.deepcopy(self.tool_reports)}
                await session.commit()
        await asyncio.sleep(0.02)
        if name in self.runs:
            self.set_status(name, phase="Succeeded", workstation={"state": "terminated", "exitCode": 0,
                                                                  "log": "verify log"})

    def set_build_status(self, name: str, **status) -> None:
        obj = self.builds[name]
        obj["status"] = {**obj.get("status", {}), **status}

    async def _build_pod(self, name: str, uid: str) -> None:
        def current() -> bool:
            return name in self.builds and self.builds[name]["metadata"]["uid"] == uid

        for _ in range(500):  # the pod's token comes last (CreateContainerConfigError until then)
            if not current():
                return
            if name in self.secrets:
                break
            await asyncio.sleep(0.01)
        else:
            self.set_build_status(name, phase="Failed", reason="CreateContainerConfigError",
                                  message="context could not be created: secret not found")
            return
        self.build_pods.append(name)
        spec = self.builds[name]["spec"]
        if self.sessions is not None and self.store is not None:
            async with self.sessions() as session:
                row = await session.get(ImageBuild, uuid.UUID(spec["id"]))
                key = row.context_key
            with tarfile.open(fileobj=io.BytesIO(await self.store.get(key))) as tar:
                self.contexts[name] = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        self.set_build_status(name, phase="Running", podName=name, startedAt="2026-11-09T00:00:00Z",
                              log="#1 [internal] load build definition from " + spec["dockerfile"])
        await asyncio.sleep(self.build_seconds)
        if not current():
            return
        if self.build_failure is not None:
            failure, self.build_failure = self.build_failure, None
            self.set_build_status(name, phase="Failed", reason=failure.get("reason", "BuildFailed"),
                                  message=failure.get("message", "the build exited with 1"),
                                  finishedAt="2026-11-09T00:00:03Z", log=failure["log"])
            return
        digest = "sha256:" + hashlib.sha256(spec["image"].encode()).hexdigest()
        self.set_build_status(name, phase="Succeeded", digest=digest, finishedAt="2026-11-09T00:00:12Z",
                              log=self.build_log)

    async def _probe_pod(self, name: str) -> None:
        await asyncio.sleep(0)
        if name not in self.runs:
            return
        test_id = uuid.UUID(self.runs[name]["spec"]["id"])
        self.envs.append(self.workstation_env(name))
        self.set_status(name, phase="Running", workstation={"state": "running"})
        if self.probe_report is not None:
            async with self.sessions() as session:
                test = await session.get(ProviderTest, test_id)
                test.report = dict(self.probe_report)
                await session.commit()
        if name in self.runs:
            self.set_status(name, phase="Succeeded" if self.probe_exit == 0 else "Failed",
                            workstation={"state": "terminated", "exitCode": self.probe_exit,
                                         "log": "Error: invalid x-api-key"})

    async def _report(self, run_id: uuid.UUID, status: str) -> None:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            if not is_terminal(run.state):
                run.reported_status = "waiting_for_input" if status == "waiting" else status
            await session.commit()
        await self._settle(run_id)

    async def idle(self) -> None:
        """Wait until every simulated pod and settle has finished."""
        while self.pods:
            pods, self.pods = self.pods, []
            await asyncio.gather(*pods, return_exceptions=False)


def runner(sessions, *, status="completed", summary="All fixed.", question=None, answer_early=False,
           error=None, exit_code=0, check=None, warnings=None) -> Behaviour:
    """What the runner reports over the internal API, written straight into Postgres."""

    async def behaviour(run_id: uuid.UUID, env: dict) -> int:
        async with sessions() as session:
            run = await session.get(Run, run_id)
            if check:
                check(run, env)
            seq = await session.scalar(
                select(TranscriptChunk.seq).where(TranscriptChunk.run_id == run_id).order_by(TranscriptChunk.seq.desc())
            ) or 0
            session.add(TranscriptChunk(run_id=run_id, seq=seq + 1, payload={"type": "AssistantMessage", "n": seq + 1}))
            if question:
                request = InfoRequest(run_id=run_id, question=question)
                session.add(request)
                await session.flush()
                if answer_early:
                    request.answer = "early answer"
                    request.answered_at = request.created_at
            if status:
                run.reported_status = status
                run.runtime_session_id = "sess-1"
                if status != "waiting_for_input":
                    run.summary = summary
                    run.output = {"fixed": True} if status == "completed" else None
                    run.error = error
                    run.delivery_warnings = list(warnings or [])
            await session.commit()
        return exit_code

    return behaviour
