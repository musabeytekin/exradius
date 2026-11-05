"""Container entrypoint: ``exradius-runner <run_id>``.

Fetches the run spec, hosts the ``exradius`` MCP server, drives the runtime session and
streams every event to the internal API. Exits after reporting the outcome; on
``waiting_for_input`` the worker starts a new container when the answer arrives.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from exradius.runner.client import HttpRunApi, RunApi
from exradius.runner.prompt import build_prompt, build_resume_message
from exradius.runner.tools import SERVER_NAME, ExradiusTools
from exradius.runtimes import ConnectionSpec, RunSpec, get_runtime
from exradius.schemas.internal import OutcomeReport, TranscriptEvent

log = logging.getLogger("exradius.runner")


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


async def execute(api: RunApi, run_id: str, working_dir: Path, state_dir: Path) -> OutcomeReport:
    spec = await api.get_spec()
    tools = ExradiusTools(api, working_dir)
    connections = [
        ConnectionSpec(
            name=c.name,
            transport=c.transport,
            command=c.command,
            args=c.args,
            url=c.url,
            env=c.env,
            headers=c.headers,
        )
        for c in spec.connections
        if c.name != SERVER_NAME
    ]
    connections.append(ConnectionSpec(name=SERVER_NAME, transport="sdk", server=tools.server()))
    run_spec = RunSpec(
        run_id=run_id,
        working_dir=working_dir,
        state_dir=state_dir,
        prompt=build_prompt(spec),
        connections=connections,
        env={},
    )
    runtime = get_runtime(spec.runtime)
    sink = ApiEventSink(api, tools)
    # Transcript sequence numbers continue across containers of the same run.
    sink.seq = _transcript_offset(state_dir)
    try:
        if spec.mode == "resume":
            if not spec.session_id:
                raise RuntimeError("cannot resume: no runtime session id was recorded")
            outcome = await runtime.resume(
                run_spec, spec.session_id, build_resume_message(spec.answers), sink
            )
        else:
            outcome = await runtime.start(run_spec, sink)
    finally:
        _save_transcript_offset(state_dir, sink.seq)
    return OutcomeReport(
        status=outcome.status,
        session_id=outcome.session_id or sink.session_id,
        final_message=outcome.final_message,
        error=outcome.error,
    )


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


async def amain(run_id: str) -> int:
    api_url = os.environ["EXRADIUS_API_URL"]
    # Read the token and drop it from the environment so the agent's tools and MCP
    # servers do not inherit it.
    token = os.environ.pop("EXRADIUS_RUN_TOKEN")
    working_dir = Path(os.environ.get("EXRADIUS_WORK_DIR", os.getcwd()))
    state_dir = Path(os.environ.get("EXRADIUS_STATE_DIR", "/exradius/state"))
    api = HttpRunApi(api_url, run_id, token)
    try:
        try:
            outcome = await execute(api, run_id, working_dir, state_dir)
        except Exception as exc:
            traceback.print_exc()
            outcome = OutcomeReport(status="failed", error=f"{type(exc).__name__}: {exc}")
        log.info("run %s finished: %s", run_id, outcome.status)
        await api.report_outcome(outcome)
        return 0 if outcome.status != "failed" else 1
    finally:
        await api.aclose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EXRADIUS_RUN_ID", "")
    if not run_id:
        print("usage: exradius-runner <run_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(amain(run_id)))


if __name__ == "__main__":
    run()
