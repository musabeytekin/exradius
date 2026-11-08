"""Agent routing (docs/agent-routing.md): intake, the Decisions call, the request and the
profiles. OpenAI is an httpx ``MockTransport``."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from exradius import routing
from exradius.models import ConnectionTools, Run, RoutingSettings, Task
from exradius.schemas.project_config import parse_config
from exradius.states import State
from exradius.worker.intake import accept_task
from tests.conftest import BASE_CONFIG, add_project, make_config_repo

KEY = "sk-proj-routing-test-key-0123456789"


def decision(choice="scout", confidence=0.91, probabilities=None, **extra) -> dict:
    probabilities = probabilities or {"bug-fixer": 0.08, "scout": 0.92}
    return {
        "model": "gpt-6-luna",
        "answers": [{"type": "choice", "name": "agent", "choice": choice, "confidence": confidence,
                     "probabilities": [{"value": k, "probability": v} for k, v in probabilities.items()]}],
        "usage": {"input_tokens": 9120, "input_tokens_details": {"cached_tokens": 8960, "cache_write_tokens": 0},
                  "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 9120},
        **extra,
    }


class FakeOpenAI:
    """Answers each request with the next of ``responses`` (an httpx.Response, a dict for
    a 200, or an exception to raise); records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return httpx.Response(200, json=response)
        return response

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


async def _no_sleep(seconds: float) -> None:
    _no_sleep.waits.append(seconds)


_no_sleep.waits = []


@pytest.fixture
def openai():
    _no_sleep.waits.clear()
    return FakeOpenAI()


@pytest.fixture
def router(openai):
    return routing.Router("https://openai.test/v1", transport=httpx.MockTransport(openai), sleep=_no_sleep)


@pytest.fixture
def route(sessions, store, box, settings, router):
    async def _accept(body: dict):
        return await accept_task(sessions, store, json.dumps(body).encode(), box=box, cache_dir=settings.cache_dir,
                                 router=router)
    return _accept


@pytest.fixture
async def with_key(sessions, box):
    async with sessions() as session:
        session.add(RoutingSettings(id=1, openai_key_encrypted=box.encrypt(KEY), key_set_at=datetime.now(UTC)))
        await session.commit()


def task(**fields) -> dict:
    return {"version": 1, "task_id": f"T-{uuid.uuid4().hex[:6]}", "project": "billing-platform",
            "instructions": "Why do invoices show negative totals?", "context": {"issue": {"key": "BILL-7"}},
            **fields}


async def _run(sessions, run_id):
    async with sessions() as session:
        run = await session.get(Run, run_id)
        task_row = await session.get(Task, run.task_id)
        return run, task_row


# Intake (§4, §5, §6) ------------------------------------------------------------------------------


async def test_a_named_agent_makes_no_call(route, configured, openai, sessions):
    intake = await route(task(agent="bug-fixer"))
    run, row = await _run(sessions, intake.run_id)
    assert row.agent == "bug-fixer" and run.routing["method"] == "named"
    assert intake.events[0].envelope.payload["routing"] == {"method": "named"}
    assert openai.requests == []


async def test_the_only_agent_needs_no_key(route, sessions, settings, tmp_path, openai):
    from exradius.models import Provider
    async with sessions() as session:
        session.add(Provider(slug="anthropic", name="Anthropic", kind="anthropic", auth_method="api_key",
                             credential_encrypted="x", settings={}, is_default=True))
        await session.commit()
    single = BASE_CONFIG.split("  scout:")[0]
    await add_project(sessions, settings.cache_dir, make_config_repo(tmp_path / "single", {"exradius.yaml": single}))
    intake = await route(task())
    run, row = await _run(sessions, intake.run_id)
    assert (row.agent, run.routing["method"]) == ("bug-fixer", "only_agent")
    assert intake.events[0].envelope.payload["routing"] == {"method": "only_agent"}
    assert openai.requests == []


async def test_no_key_rejects_at_once(route, configured, openai):
    intake = await route(task())
    [event] = intake.events
    assert event.envelope.payload["state"] == "rejected"
    assert event.envelope.payload["reason"] == (
        "agent routing is not configured (no OpenAI API key), and project billing-platform has 2 agents")
    assert openai.requests == []


async def test_a_decided_agent_runs(route, configured, with_key, openai, sessions):
    openai.responses.append(decision())
    intake = await route(task())
    run, row = await _run(sessions, intake.run_id)
    assert row.agent == "scout" and row.state == State.QUEUED
    assert run.routing == {
        "method": "decided", "agent": "scout", "confidence": 0.91, "guess": False,
        "probabilities": {"bug-fixer": 0.08, "scout": 0.92}, "model": "gpt-6-luna", "ms": run.routing["ms"],
        "input_tokens": 9120, "cached_tokens": 8960, "decided_at": run.routing["decided_at"],
    }
    envelope = intake.events[0].envelope
    assert envelope.agent == "scout"
    assert envelope.payload["routing"] == {"method": "decided", "confidence": 0.91, "guess": False,
                                           "probabilities": {"bug-fixer": 0.08, "scout": 0.92}}
    [request] = openai.requests
    assert request.url == "https://openai.test/v1/decisions"
    assert request.headers["authorization"] == f"Bearer {KEY}"


async def test_low_confidence_is_a_guess_and_still_runs(route, configured, with_key, openai, sessions):
    openai.responses.append(decision(choice="bug-fixer", confidence=0.48,
                                     probabilities={"bug-fixer": 0.55, "scout": 0.45}))
    intake = await route(task())
    run, row = await _run(sessions, intake.run_id)
    assert row.agent == "bug-fixer" and run.routing["guess"] is True
    assert intake.events[0].envelope.payload["routing"]["guess"] is True


async def test_an_unknown_named_agent_is_still_rejected(route, configured, with_key, openai):
    intake = await route(task(agent="nobody"))
    assert intake.events[0].envelope.payload["reason"].startswith("unknown agent 'nobody'")
    assert openai.requests == []


# The call (§6) -------------------------------------------------------------------------------------


async def test_retries_then_rejects(route, configured, with_key, openai):
    openai.responses += [httpx.Response(503), httpx.Response(503), httpx.Response(503)]
    intake = await route(task())
    assert intake.run_id is None
    assert intake.events[0].envelope.payload["reason"] == "agent routing failed: OpenAI answered 503 three times"
    assert _no_sleep.waits == [2.0, 6.0]


async def test_a_retry_that_succeeds(route, configured, with_key, openai, sessions):
    openai.responses += [httpx.ReadTimeout("slow"), httpx.Response(429), decision()]
    intake = await route(task())
    run, _ = await _run(sessions, intake.run_id)
    assert run.routing["agent"] == "scout" and len(openai.requests) == 3


async def test_mixed_failures_are_listed(router, openai):
    openai.responses += [httpx.ConnectError("down"), httpx.Response(500), httpx.ReadTimeout("slow")]
    with pytest.raises(routing.RoutingFailed) as failed:
        await router.decide(KEY, _request())
    assert str(failed.value) == ("agent routing failed: OpenAI could not be reached (ConnectError), "
                                 "then answered 500, then timed out")


async def test_no_retry_on_401_and_no_key_in_the_reason(router, openai, caplog):
    openai.responses.append(httpx.Response(401, json={"error": {
        "message": "Incorrect API key provided: sk-proj-ro*****6789. You can find your API key at ..."}}))
    with pytest.raises(routing.RoutingFailed) as failed:
        await router.decide(KEY, _request())
    reason = str(failed.value)
    assert reason.startswith("agent routing failed: OpenAI answered 401: Incorrect API key provided: sk-…")
    assert "ro*****" not in reason and KEY not in reason
    assert len(openai.requests) == 1 and _no_sleep.waits == []


@pytest.mark.parametrize(("answer", "reason"), [
    ({"answers": [{"type": "refusal", "name": "agent"}]}, "agent routing failed: the decision refused to choose"),
    (decision(choice="nobody"), "agent routing failed: the decision answered 'nobody', not an agent"),
    ({"answers": []}, "agent routing failed: the decision answered 'None', not an agent"),
])
async def test_answers_that_are_not_an_agent(router, openai, answer, reason):
    openai.responses.append(answer)
    with pytest.raises(routing.RoutingFailed) as failed:
        await router.decide(KEY, _request())
    assert str(failed.value) == reason


async def test_a_refusal_rejects_the_task(route, configured, with_key, openai):
    openai.responses.append({"answers": [{"type": "refusal", "name": "agent"}]})
    intake = await route(task())
    assert intake.run_id is None
    assert intake.events[0].envelope.payload["reason"] == "agent routing failed: the decision refused to choose"


# The request (§4) ---------------------------------------------------------------------------------


def _config(text: str = BASE_CONFIG):
    result = parse_config(text)
    assert result.config is not None, result.errors
    return result.config


def _request(**overrides):
    config = _config()
    fields = {
        "project_slug": "billing-platform", "project_name": "Billing",
        "agent_profiles": routing.profiles(config, {}), "task_id": "BILL-7",
        "instructions": "Why do invoices show negative totals?", "context": {"k": "v"},
        "attachments": [("trace.txt", "text/plain")],
    }
    return routing.build_request(**{**fields, **overrides})


def test_profiles_first_the_task_last():
    request = _request()
    assert request["model"] == "gpt-6-luna"
    assert request["prompt_cache_key"] == "exradius-routing-billing-platform"
    assert request["prompt_cache_options"] == {"mode": "explicit"}
    developer, profiles, ticket = request["input"]
    assert developer["role"] == "developer"
    head, data, closing = profiles["content"]
    assert head["text"] == "The agents of project Billing:"
    assert [a["slug"] for a in data["data"]["agents"]] == ["bug-fixer", "scout"]  # sorted
    assert closing["prompt_cache_breakpoint"] == {"mode": "explicit"}
    text, extra = ticket["content"]
    assert text["text"] == "# Task BILL-7\nWhy do invoices show negative totals?"
    assert extra == {"type": "input_json", "data": {"context": {"k": "v"}, "attachments": ["trace.txt (text/plain)"]}}
    [question] = request["questions"]
    assert question["type"] == "choice" and question["name"] == "agent"
    assert question["choices"] == [{"value": "bug-fixer", "description": "Bug fixer"},
                                   {"value": "scout", "description": "Scout"}]
    assert "prompt_cache_breakpoint" not in json.dumps(request["questions"])


def test_the_prefix_does_not_depend_on_the_task():
    one, two = _request(task_id="A-1", instructions="x"), _request(task_id="B-2", instructions="y", context=[1])
    assert one["input"][:2] == two["input"][:2]


def test_a_large_context_is_cut_as_text():
    request = _request(context={"log": "x" * (300 * 1024)})
    part = request["input"][2]["content"][1]
    assert part["type"] == "input_text"
    assert part["text"].endswith("[context cut at 256 KiB]")
    assert len(part["text"].encode()) < 257 * 1024


def test_too_many_values_go_as_text():
    request = _request(context=list(range(routing.MAX_JSON_VALUES + 1)))
    assert request["input"][2]["content"][1]["type"] == "input_text"


# Profiles (§3) --------------------------------------------------------------------------------------


def _row(config, name: str, *, changed: bool = False) -> ConnectionTools:
    return ConnectionTools(
        project_id=uuid.uuid4(), connection=name,
        definition_hash="0" * 64 if changed else routing.definition_hash(config, name),
        tools=[{"name": "search", "description": "Search the docs.", "annotations": {"readOnlyHint": True}}],
        listed_at=datetime(2026, 11, 8, 17, 2, tzinfo=UTC),
    )


def test_profiles_use_stored_tools_only_for_the_same_definition():
    config = _config()
    [fixer, scout] = routing.profiles(config, {"docs": _row(config, "docs")})
    assert fixer == {
        "slug": "bug-fixer", "name": "Bug fixer", "role": "You fix bugs.",
        "connections": [{"name": "docs", "kind": "custom http",
                         "tools": [{"name": "search", "description": "Search the docs.",
                                    "annotations": {"readOnlyHint": True}}],
                         "tools_listed_at": "2026-11-08T17:02:00+00:00"}],
    }
    assert scout == {"slug": "scout", "name": "Scout", "role": "You investigate.", "connections": []}

    [fixer, _] = routing.profiles(config, {"docs": _row(config, "docs", changed=True)})
    assert fixer["connections"][0]["tools"] is None
    assert fixer["connections"][0]["tools_note"] == "not listed since this connection changed"
    [fixer, _] = routing.profiles(config, {})
    assert fixer["connections"][0]["tools_note"] == "not listed yet"


def test_listing_times_stay_out_of_the_request():
    config = _config()
    request = _request(agent_profiles=routing.profiles(config, {"docs": _row(config, "docs")}))
    assert "tools_listed_at" not in json.dumps(request)
    assert "Search the docs." in json.dumps(request)


def test_a_definition_change_changes_the_hash():
    config = _config()
    other = _config(BASE_CONFIG.replace("https://mcp.example/mcp", "https://mcp.example/v2"))
    assert routing.definition_hash(config, "docs") != routing.definition_hash(other, "docs")
    assert routing.definition_hash(config, "docs") == routing.definition_hash(_config(), "docs")


async def test_the_panel_test_decision(router, openai):
    openai.responses.append(decision(choice="web", probabilities={"web": 0.97, "billing": 0.03}))
    picked = await router.test(KEY)
    assert picked.agent == "web"
    [body] = openai.bodies()
    assert [c["value"] for c in body["questions"][0]["choices"]] == ["web", "billing"]


# The panel's routing settings (§7) and the agents tab (§3.2) ---------------------------------------

from tests.test_api import admin, client, control, sync_db  # noqa: E402,F401 - fixtures


def test_the_key_is_write_only_and_encrypted(admin, sync_db, openai, router):
    admin.app.state.exradius.router = router
    assert admin.get("/api/routing").json() == {"key_set": False, "key_set_at": None, "last_test": None,
                                                "model": "gpt-6-luna", "guess_below": 0.6}
    assert admin.post("/api/routing/test").status_code == 409  # no key
    set_ = admin.put("/api/routing/key", json={"key": f"  {KEY} "})
    assert set_.status_code == 200 and set_.json()["key_set"] is True and KEY not in set_.text
    row = sync_db.get(RoutingSettings, 1)
    assert row.openai_key_encrypted and KEY not in row.openai_key_encrypted
    assert KEY not in admin.get("/api/routing").text

    openai.responses.append(decision(choice="web", confidence=0.97, probabilities={"web": 0.97, "billing": 0.03}))
    tested = admin.post("/api/routing/test").json()
    assert tested["ok"] is True and tested["detail"] == "answered web, confidence 0.97"
    assert openai.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert admin.get("/api/routing").json()["last_test"]["ok"] is True

    openai.responses.append(httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-pr***89."}}))
    failed = admin.post("/api/routing/test").json()
    assert failed["ok"] is False and failed["detail"] == "OpenAI answered 401: Incorrect API key provided: sk-…"

    assert admin.delete("/api/routing/key").status_code == 204
    assert admin.get("/api/routing").json()["key_set"] is False
    assert admin.put("/api/routing/key", json={"key": " "}).status_code == 422


def test_routing_routes_need_the_admin(client):
    for method, url in (("get", "/api/routing"), ("put", "/api/routing/key"), ("delete", "/api/routing/key"),
                        ("post", "/api/routing/test")):
        assert getattr(client, method)(url).status_code == 401


def test_the_agents_tab_shows_profiles(admin, sync_db, tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": BASE_CONFIG})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    profiles = admin.get(f"/api/projects/{project['id']}/agents").json()
    assert [p["slug"] for p in profiles] == ["bug-fixer", "scout"]
    assert profiles[0]["connections"] == [{"name": "docs", "kind": "custom http", "tools": None,
                                           "tools_listed_at": None, "tools_note": "not listed yet"}]

    config = _config()
    sync_db.add(ConnectionTools(project_id=uuid.UUID(project["id"]), connection="docs",
                                definition_hash=routing.definition_hash(config, "docs"),
                                tools=[{"name": "search", "description": "Search the docs.",
                                        "annotations": {"readOnlyHint": True}}],
                                listed_at=datetime(2026, 11, 8, 17, 2, tzinfo=UTC)))
    sync_db.commit()
    [fixer, _] = admin.get(f"/api/projects/{project['id']}/agents").json()
    [docs] = fixer["connections"]
    assert docs["tools"] == [{"name": "search", "description": "Search the docs.", "annotations": {"readOnlyHint": True}}]
    assert docs["tools_listed_at"].startswith("2026-11-08T17:02") and docs["tools_note"] is None


async def test_the_task_view_shows_routing(route, configured, with_key, openai, sessions, store, settings):
    from fastapi.testclient import TestClient
    from exradius.api.app import create_app
    from tests.test_api import FakeControl

    openai.responses.append(decision())
    intake = await route(task(task_id="BILL-9"))
    assert intake.run_id is not None
    app = create_app(settings, store=store, broker=FakeControl())
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "correct horse"}).status_code == 200
        run = c.get("/api/tasks/BILL-9").json()["run"]
    assert run["routing"]["method"] == "decided" and run["routing"]["agent"] == "scout"
