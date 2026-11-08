"""The runner in the workstation container (exradius/runner/main.py, docs/kubernetes.md §8.2):
it waits in-process for answers and resumes the same session in the same pod."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from exradius.runner import main as runner_main
from exradius.runner.environment import RunnerEnvironment
from exradius.runner.prompt import build_resume_message
from exradius.runtimes import RunOutcome
from exradius.schemas.internal import AnswersResponse, AttachmentPath, DeliveredAnswer, OutcomeReport, RunSpecResponse


def answer(request_id: str, *paths: str) -> DeliveredAnswer:
    return DeliveredAnswer(request_id=request_id, question=f"Q {request_id}?", answer=f"A {request_id}",
                           attachments=[AttachmentPath(path=p, content_type="text/plain", id=f"{request_id}.{n}")
                                        for n, p in enumerate(paths)])


class FakeApi:
    def __init__(self, spec: RunSpecResponse, polls: list[AnswersResponse]):
        self.spec = spec
        self.polls = list(polls)
        self.reports: list[str] = []
        self.waits: list[float] = []
        self.outcomes: list[OutcomeReport] = []
        self.events = 0

    async def get_spec(self):
        return self.spec

    async def report_session(self, session_id):
        pass

    async def append_transcript(self, events):
        self.events += len(events)

    async def create_info_request(self, question, context):
        return "req"

    async def submit_result(self, output, artifacts):
        pass

    async def report_outcome(self, outcome):
        self.outcomes.append(outcome)

    async def report(self, status):
        self.reports.append(status)

    async def wait_answers(self, wait):
        self.waits.append(wait)
        await asyncio.sleep(0)
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    async def aclose(self):
        pass


class FakeRuntime:
    """Scripted outcomes; records every call."""

    def __init__(self, outcomes: list[RunOutcome]):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, Any]] = []

    async def start(self, run_spec, events):
        self.calls.append(("start", run_spec.prompt))
        await events.event({"type": "start"})
        return self.outcomes.pop(0)

    async def resume(self, run_spec, session_id, message, events):
        self.calls.append(("resume", session_id, message))
        await events.event({"type": "resume"})
        return self.outcomes.pop(0)


def spec(**fields) -> RunSpecResponse:
    return RunSpecResponse(run_id="r", task_id="T-1", runtime="claude_code", mode=fields.pop("mode", "start"),
                           agent_prompt="", instructions="Fix it.", **fields)


@pytest.fixture
def runtime(monkeypatch):
    holder: dict[str, FakeRuntime] = {}

    def install(outcomes):
        holder["runtime"] = FakeRuntime(outcomes)
        monkeypatch.setattr(runner_main, "get_runtime", lambda name, runtime_dir=None: holder["runtime"])
        return holder["runtime"]

    return install


def session(api, tmp_path) -> runner_main.Session:
    work, state = tmp_path / "work", tmp_path / "state"
    work.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    result = runner_main.Session(api, "r", work, state)
    result.answer_poll = 0.01
    result.attachment_wait = 2
    return result


WAITING = RunOutcome("waiting_for_input", "s1", None, None)
DONE = RunOutcome("completed", "s1", "All fixed.", None)


async def test_it_waits_in_process_and_resumes_the_same_session(tmp_path, runtime):
    first = answer("q1", ".exradius/attachments/q1/log.txt")
    api = FakeApi(spec(), [
        AnswersResponse(state="waiting_for_input"),
        AnswersResponse(state="resuming", answers=[first]),
    ])
    rt = runtime([WAITING, DONE])
    run = session(api, tmp_path)

    async def sidecar():  # the files sidecar writes the attachment a little later
        await asyncio.sleep(0.2)
        target = run.working_dir / ".exradius" / "attachments" / "q1"
        target.mkdir(parents=True)
        (target / "log.txt").write_text("the log")

    writer = asyncio.create_task(sidecar())
    outcome = await asyncio.wait_for(run.drive(), 5)
    await writer
    assert outcome.status == "completed" and outcome.final_message == "All fixed." and outcome.session_id == "s1"
    assert api.reports == ["waiting", "resumed"]
    assert rt.calls[0][0] == "start"
    assert rt.calls[1] == ("resume", "s1", build_resume_message([first]))
    assert all(w == 0.01 for w in api.waits) and len(api.waits) == 2
    assert (run.state_dir / "transcript-seq").read_text() == "2"  # one sequence for both sessions


async def test_answers_already_used_are_not_resumed_with_again(tmp_path, runtime):
    first, second = answer("q1"), answer("q2")
    api = FakeApi(spec(), [
        AnswersResponse(state="resuming", answers=[first]),
        AnswersResponse(state="resuming", answers=[first]),  # the worker has not moved on yet
        AnswersResponse(state="resuming", answers=[first, second]),
    ])
    rt = runtime([WAITING, WAITING, DONE])
    outcome = await asyncio.wait_for(session(api, tmp_path).drive(), 5)
    assert outcome.status == "completed"
    assert [c[2] for c in rt.calls[1:]] == [build_resume_message([first]), build_resume_message([second])]
    assert api.reports == ["waiting", "resumed", "waiting", "resumed"]


async def test_a_recreated_pod_starts_in_resume_mode(tmp_path, runtime):
    delivered = answer("q1")
    api = FakeApi(spec(mode="resume", session_id="s0", answers=[delivered]), [AnswersResponse(state="running")])
    rt = runtime([DONE])
    run = session(api, tmp_path)
    (run.state_dir / "transcript-seq").write_text("7")  # the transcript continues from the old pod
    outcome = await asyncio.wait_for(run.drive(), 5)
    assert outcome.status == "completed"
    assert rt.calls == [("resume", "s0", build_resume_message([delivered]))]
    assert api.reports == ["resumed"]
    assert (run.state_dir / "transcript-seq").read_text() == "8"


async def test_resume_mode_without_a_session_fails(tmp_path, runtime):
    api = FakeApi(spec(mode="resume", answers=[answer("q1")]), [AnswersResponse(state="running")])
    runtime([DONE])
    with pytest.raises(RuntimeError, match="no runtime session id"):
        await session(api, tmp_path).drive()


async def test_a_run_that_ends_while_waiting_is_cancelled(tmp_path, runtime):
    api = FakeApi(spec(), [AnswersResponse(state="waiting_for_input"), AnswersResponse(state="cancelled")])
    rt = runtime([WAITING])
    outcome = await asyncio.wait_for(session(api, tmp_path).drive(), 5)
    assert outcome.status == "cancelled" and outcome.session_id == "s1"
    assert api.reports == ["waiting"] and len(rt.calls) == 1


async def test_missing_answer_attachments_do_not_block_forever(tmp_path, runtime):
    api = FakeApi(spec(), [AnswersResponse(state="resuming", answers=[answer("q1", ".exradius/attachments/q1/x")])])
    runtime([WAITING, DONE])
    run = session(api, tmp_path)
    run.attachment_wait = 0.2
    assert (await asyncio.wait_for(run.drive(), 5)).status == "completed"


async def test_sigterm_outcome_depends_on_the_runs_state():
    class Api:
        def __init__(self, state):
            self.state = state

        async def wait_answers(self, wait):
            assert wait == 0
            return AnswersResponse(state=self.state)

    assert (await runner_main._stopped_outcome(Api("cancelled"))).status == "cancelled"
    stopped = await runner_main._stopped_outcome(Api("running"))
    assert stopped.status == "failed" and "SIGTERM" in stopped.error


async def test_sigterm_stops_the_session_runs_the_check_and_reports(tmp_path, monkeypatch):
    api = FakeApi(spec(), [AnswersResponse(state="cancelled")])
    started = asyncio.Event()

    async def forever(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    checked = []

    async def check(work_dir):
        checked.append(work_dir)
        return ["app: uncommitted changes in 1 file were discarded with the task directory"]

    monkeypatch.setattr(runner_main, "HttpRunApi", lambda url, run_id, token: api)
    monkeypatch.setattr(runner_main, "execute", forever)
    monkeypatch.setattr(runner_main.delivery, "check", check)
    monkeypatch.setenv("EXRADIUS_API_URL", "http://api")
    monkeypatch.setenv("EXRADIUS_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("EXRADIUS_STATE_DIR", str(tmp_path / "state"))
    environment = RunnerEnvironment(container={}, run_token="tok", runtime_dir=None)

    async def terminate():
        await started.wait()
        os.kill(os.getpid(), signal.SIGTERM)

    killer = asyncio.create_task(terminate())
    code = await asyncio.wait_for(runner_main.amain("r", environment), 5)
    await killer
    assert code == 0
    [outcome] = api.outcomes
    assert outcome.status == "cancelled"
    assert outcome.warnings == ["app: uncommitted changes in 1 file were discarded with the task directory"]
    assert checked == [Path(tmp_path)]


async def test_a_finished_session_reports_its_outcome_with_warnings(tmp_path, monkeypatch, runtime):
    api = FakeApi(spec(), [AnswersResponse(state="running")])
    runtime([RunOutcome("failed", "s1", None, "invalid api key")])

    async def check(work_dir):
        return []

    monkeypatch.setattr(runner_main, "HttpRunApi", lambda url, run_id, token: api)
    monkeypatch.setattr(runner_main.delivery, "check", check)
    monkeypatch.setenv("EXRADIUS_API_URL", "http://api")
    monkeypatch.setenv("EXRADIUS_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("EXRADIUS_STATE_DIR", str(tmp_path))
    environment = RunnerEnvironment(container={"GIT_AUTHOR_NAME": "Bot", "GIT_AUTHOR_EMAIL": "bot@x"},
                                    run_token="tok", runtime_dir=None)
    assert await asyncio.wait_for(runner_main.amain("r", environment), 5) == 1
    [outcome] = api.outcomes
    assert (outcome.status, outcome.error, outcome.warnings) == ("failed", "invalid api key", [])
    assert (tmp_path / "git" / "exradius.gitconfig").exists()  # written at start


def test_run_dispatches_the_modes(monkeypatch):
    seen = []

    async def prepare(purpose, object_id, url, token, root):
        seen.append(("prepare", purpose, object_id, url, token, str(root)))
        return 0

    async def files(run_id, url, token, directory):
        seen.append(("files", run_id, str(directory)))
        return 0

    def clone(work, state, environ):
        seen.append(("clone", str(work), str(state)))
        return 0

    async def verify(verification_id, environment):
        seen.append(("verify", verification_id))
        return 0

    monkeypatch.setattr(runner_main.workspace, "prepare", prepare)
    monkeypatch.setattr(runner_main.workspace, "files", files)
    monkeypatch.setattr(runner_main.workspace, "clone", clone)
    monkeypatch.setattr(runner_main, "verify", verify)
    monkeypatch.setenv("EXRADIUS_API_URL", "http://api")
    monkeypatch.setenv("EXRADIUS_RUN_TOKEN", "tok")
    for argv in (["--prepare", "task", "r-1"], ["--files", "r-1"], ["--clone"], ["--verify", "v-1"]):
        monkeypatch.setattr("sys.argv", ["exradius-runner", *argv])
        monkeypatch.setenv("EXRADIUS_RUN_TOKEN", "tok")  # isolate() removes it each time
        with pytest.raises(SystemExit) as exit_info:
            runner_main.run()
        assert exit_info.value.code == 0
    assert seen == [
        ("prepare", "task", "r-1", "http://api", "tok", "/exradius/workspace"),
        ("files", "r-1", "/exradius/attachments"),
        ("clone", "/work", "/exradius/state"),
        ("verify", "v-1"),
    ]
    monkeypatch.setattr("sys.argv", ["exradius-runner", "--nonsense"])
    with pytest.raises(SystemExit) as exit_info:
        runner_main.run()
    assert exit_info.value.code == 2
