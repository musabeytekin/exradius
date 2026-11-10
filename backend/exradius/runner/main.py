"""The runner in a run pod (docs/kubernetes.md §7, §8):

    exradius-runner <run_id>                                  the agent's session (workstation)
    exradius-runner --prepare <purpose> <id>                  init container ``prepare``
    exradius-runner --clone                                   init containers ``clone-<n>``
    exradius-runner --files <run_id>                          the ``files`` sidecar
    exradius-runner --verify <verification_id>                a verify Run's workstation
    exradius-runner --probe <runtime> [--report <id>] [--model <id>]   a provider test
    exradius-runner --list-tools                              one connection (the skill's check)

The session mode fetches the run spec, hosts the ``exradius`` MCP server, drives the runtime
session and streams every event to the internal API. When the agent asks a question it
reports ``waiting`` and stays alive, long-polling for the answer, then resumes the same
session in the same pod and reports ``resumed`` (§8.2). After the session it runs the
delivery check and reports the outcome with its warnings. On SIGTERM (cancel) it stops the
session, runs the check and reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from exradius.runner import delivery, egress, gitconfig, workspace
from exradius.runner.client import HttpProbeApi, HttpRunApi, HttpVerifyApi, RunApi, RunEnded
from exradius.runner.environment import RunnerEnvironment, isolate
from exradius.runner.prompt import build_resume_message, build_system_prompt, build_task_message
from exradius.runner.tools import SERVER_NAME, ExradiusTools
from exradius.runtimes import ConnectionSpec, RunSpec, get_runtime
from exradius.runtimes.base import PROBE_PREFIX
from exradius.schemas.internal import DeliveredAnswer, OutcomeReport, ProbeReport, ToolReport, TranscriptEvent

log = logging.getLogger("exradius.runner")

ANSWER_POLL = 30.0
"""Seconds of one long-poll for answers."""
ATTACHMENT_WAIT = 120.0
"""How long the runner waits for the ``files`` sidecar to write an answer's attachments."""
ENDED = ("completed", "failed", "cancelled", "rejected")


class ApiEventSink:
    def __init__(self, api: RunApi, tools: ExradiusTools):
        self.api = api
        self.tools = tools
        self.seq = 0
        self.session_id: str | None = None

    async def event(self, payload: dict[str, Any]) -> None:
        self.seq += 1
        try:
            await self.api.append_transcript([TranscriptEvent(seq=self.seq, payload=payload)])
        except Exception:  # the transcript must never break the session
            log.exception("could not store transcript event %s", self.seq)

    async def session_started(self, session_id: str) -> None:
        self.session_id = session_id
        await self.api.report_session(session_id)

    def suspend_requested(self) -> bool:
        return self.tools.suspend_requested


class Session:
    """One task run, from start to outcome, waiting in-process for answers."""

    def __init__(self, api: RunApi, run_id: str, working_dir: Path, state_dir: Path,
                 environment: RunnerEnvironment | None = None):
        self.api = api
        self.run_id = run_id
        self.working_dir = working_dir
        self.state_dir = state_dir
        self.environment = environment or RunnerEnvironment(container={}, run_token=None, runtime_dir=None)
        self.consumed: set[str] = set()
        """Info requests whose answers this process resumed with."""
        self.answer_poll = ANSWER_POLL
        self.attachment_wait = ATTACHMENT_WAIT

    async def drive(self) -> OutcomeReport:
        spec = await self.api.get_spec()
        tools = ExradiusTools(self.api, self.working_dir)
        connections = [
            ConnectionSpec(name=c.name, transport=c.transport, command=c.command, args=c.args, url=c.url, env=c.env,
                           headers=c.headers)
            for c in spec.connections
            if c.name != SERVER_NAME
        ]
        connections.append(ConnectionSpec(name=SERVER_NAME, transport="sdk", server=tools.server()))
        run_spec = RunSpec(
            run_id=self.run_id,
            working_dir=self.working_dir,
            state_dir=self.state_dir,
            prompt=build_task_message(spec),
            system_prompt=build_system_prompt(spec),
            connections=connections,
            env=self.environment.container,  # the agent gets the project's environment, not the runner's
            model=spec.model,
        )
        runtime = get_runtime(spec.runtime, self.environment.runtime_dir)
        sink = ApiEventSink(self.api, tools)
        # Transcript sequence numbers continue across sessions and recreated pods.
        sink.seq = _transcript_offset(self.state_dir)
        try:
            if spec.mode == "resume":
                if not spec.session_id:
                    raise RuntimeError("cannot resume: no runtime session id was recorded")
                outcome = await self._resume(runtime, run_spec, sink, spec.session_id, spec.answers)
            else:
                outcome = await runtime.start(run_spec, sink)
            while outcome.status == "waiting_for_input":
                _save_transcript_offset(self.state_dir, sink.seq)
                await self.api.report("waiting")
                answers = await self._wait_for_answers()
                if answers is None:  # the run ended while it waited (cancelled)
                    return OutcomeReport(status="cancelled", session_id=outcome.session_id or sink.session_id)
                tools.clear_suspend()
                session_id = outcome.session_id or sink.session_id
                if not session_id:
                    raise RuntimeError("cannot resume: no runtime session id was recorded")
                outcome = await self._resume(runtime, run_spec, sink, session_id, answers)
        finally:
            _save_transcript_offset(self.state_dir, sink.seq)
        return OutcomeReport(
            status=outcome.status,
            session_id=outcome.session_id or sink.session_id,
            final_message=outcome.final_message,
            error=outcome.error,
        )

    async def _resume(self, runtime, run_spec: RunSpec, sink: ApiEventSink, session_id: str,
                      answers: list[DeliveredAnswer]):
        await self._wait_for_attachments(answers)
        self.consumed |= {a.request_id for a in answers}
        await self.api.report("resumed")
        return await runtime.resume(run_spec, session_id, build_resume_message(answers), sink)

    async def _wait_for_answers(self) -> list[DeliveredAnswer] | None:
        """Long-poll until the run is resuming with answers this process has not used."""
        while True:
            response = await self.api.wait_answers(self.answer_poll)
            if response.state in ENDED:
                return None
            fresh = [a for a in response.answers if a.request_id not in self.consumed]
            if response.state == "resuming" and fresh:
                return fresh

    async def _wait_for_attachments(self, answers: list[DeliveredAnswer]) -> None:
        """The ``files`` sidecar writes them; resume once they are there."""
        paths = [self.working_dir / a.path for answer in answers for a in answer.attachments]
        deadline = time.monotonic() + self.attachment_wait
        while not all(p.exists() for p in paths):
            if time.monotonic() >= deadline:
                missing = ", ".join(str(p) for p in paths if not p.exists())
                log.warning("answer attachments not there after %.0fs: %s", self.attachment_wait, missing)
                return
            await asyncio.sleep(0.5)


async def execute(api: RunApi, run_id: str, working_dir: Path, state_dir: Path,
                  environment: RunnerEnvironment | None = None) -> OutcomeReport:
    """The session to its outcome (without the delivery check), once the egress proxy sees
    the agent phase."""
    if environment is not None:
        await egress.wait_for_agent_phase(environment.egress)
    return await Session(api, run_id, working_dir, state_dir, environment).drive()


def _offset_file(state_dir: Path) -> Path:
    return state_dir / "transcript-seq"


def _transcript_offset(state_dir: Path) -> int:
    try:
        return int(_offset_file(state_dir).read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _save_transcript_offset(state_dir: Path, seq: int) -> None:
    try:
        _offset_file(state_dir).write_text(str(seq))
    except OSError:
        log.warning("could not persist transcript offset")


def _git_config(state_dir: Path, environ: dict[str, str]) -> None:
    """The run's global git config, rewritten at every start (docs/kubernetes.md §7.2)."""
    name, email = environ.get("GIT_AUTHOR_NAME"), environ.get("GIT_AUTHOR_EMAIL")
    if not name or not email:
        return
    try:
        gitconfig.write_git_config(state_dir, name, email)
    except OSError:
        log.exception("could not write the git config")


async def _stopped_outcome(api: RunApi) -> OutcomeReport:
    """SIGTERM: cancelled when the run was cancelled, else the pod was stopped under it."""
    with contextlib.suppress(Exception):
        if (await api.wait_answers(0)).state == "cancelled":
            return OutcomeReport(status="cancelled")
    return OutcomeReport(status="failed", error="the run's pod was stopped (SIGTERM) before the run ended")


async def amain(run_id: str, environment: RunnerEnvironment) -> int:
    api_url = os.environ["EXRADIUS_API_URL"]
    token = environment.run_token
    if not token:
        raise SystemExit("EXRADIUS_RUN_TOKEN is not set")
    working_dir = Path(os.environ.get("EXRADIUS_WORK_DIR", os.getcwd()))
    state_dir = Path(os.environ.get("EXRADIUS_STATE_DIR", "/exradius/state"))
    _git_config(state_dir, environment.container)
    api = HttpRunApi(api_url, run_id, token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        session = asyncio.create_task(execute(api, run_id, working_dir, state_dir, environment))
        stopped = asyncio.create_task(stop.wait())
        await asyncio.wait({session, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if session.done():
            stopped.cancel()
            try:
                outcome = session.result()
            except RunEnded:
                log.info("run %s has ended", run_id)
                return 0
            except Exception as exc:
                traceback.print_exception(exc)
                outcome = OutcomeReport(status="failed", error=f"{type(exc).__name__}: {exc}")
        else:
            log.info("stopping: SIGTERM")
            session.cancel()
            with contextlib.suppress(BaseException):
                await session
            outcome = await _stopped_outcome(api)
        outcome.warnings = await delivery.check(working_dir)
        log.info("run %s finished: %s", run_id, outcome.status)
        try:
            await api.report_outcome(outcome)
        except RunEnded:
            pass
        return 1 if outcome.status == "failed" else 0
    finally:
        await api.aclose()


async def probe(runtime_name: str, environment: RunnerEnvironment, model: str | None = None,
                report_id: str | None = None) -> int:
    """Provider test (providers.md §2.3): a one-line session with the provider injected. With
    ``--report`` the result also goes to the internal API (a probe Run)."""
    try:
        await egress.wait_for_agent_phase(environment.egress)
        result = await get_runtime(runtime_name, environment.runtime_dir).probe(environment.container, model)
        report = {"ok": result.ok, "detail": result.detail, "error": result.error}
    except Exception as exc:
        traceback.print_exc()
        report = {"ok": False, "detail": None, "error": f"{type(exc).__name__}: {exc}"}
    print(PROBE_PREFIX + json.dumps(report), flush=True)
    if report_id:
        api = HttpProbeApi(os.environ["EXRADIUS_API_URL"], report_id, environment.run_token or "")
        try:
            await api.report(ProbeReport(**report))
        finally:
            await api.aclose()
    return 0 if report["ok"] else 1


async def verify(verification_id: str, environment: RunnerEnvironment) -> int:
    """A verify Run's workstation: list every used connection's tools, as the agent's
    session would start them, and report each (docs/agent-routing.md §3.1)."""
    from exradius.runner import list_tools

    api = HttpVerifyApi(os.environ["EXRADIUS_API_URL"], verification_id, environment.run_token or "")
    try:
        await egress.wait_for_agent_phase(environment.egress)
        spec = await api.get_spec()
        for connection in spec.connections:
            result = await list_tools.report(connection.model_dump(), dict(environment.container))
            await api.report_tools(ToolReport(name=connection.name, **result))
            print(f"{connection.name}: {'ok' if result['ok'] else result['error']}", flush=True)
    finally:
        await api.aclose()
    return 0


def _arg(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args[:-1] else None


def run() -> None:
    # First of all: keep the container's environment for the agent, clean our own.
    environment = isolate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = sys.argv[1:]
    mode = args[0] if args else ""
    api_url = os.environ.get("EXRADIUS_API_URL", "")
    token = environment.run_token or ""
    if mode == "--probe":
        # exradius-runner --probe <runtime> [--report <id>] [--model <id>]
        rest = args[1:]
        runtime_name = rest[0] if rest and not rest[0].startswith("--") else "claude_code"
        raise SystemExit(asyncio.run(probe(runtime_name, environment, _arg(rest, "--model"), _arg(rest, "--report"))))
    if mode == "--list-tools":
        from exradius.runner import list_tools

        raise SystemExit(asyncio.run(list_tools.main(dict(environment.container))))
    if mode == "--verify" and len(args) > 1:
        raise SystemExit(asyncio.run(verify(args[1], environment)))
    if mode == "--prepare" and len(args) > 2:
        root = Path(os.environ.get("EXRADIUS_WORKSPACE", str(workspace.WORKSPACE)))
        raise SystemExit(asyncio.run(workspace.prepare(args[1], args[2], api_url, token, root,
                                                       dict(environment.container))))
    if mode == "--clone":
        work = Path(os.environ.get("EXRADIUS_WORK_DIR", "/work"))
        state = Path(os.environ.get("EXRADIUS_STATE_DIR", "/exradius/state"))
        raise SystemExit(workspace.clone(work, state, dict(environment.container),
                                         environment.egress.get("EXRADIUS_EGRESS_URL")))
    if mode == "--files" and len(args) > 1:
        directory = Path(os.environ.get("EXRADIUS_ATTACHMENTS_DIR", "/exradius/attachments"))
        raise SystemExit(asyncio.run(workspace.files(args[1], api_url, token, directory)))
    run_id = mode or os.environ.get("EXRADIUS_RUN_ID", "")
    if not run_id or run_id.startswith("--"):
        print("usage: exradius-runner <run_id> | --prepare <purpose> <id> | --clone | --files <run_id> | "
              "--verify <id> | --probe <runtime> [--report <id>] [--model <id>] | --list-tools", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(amain(run_id, environment)))


if __name__ == "__main__":
    run()
