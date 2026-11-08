"""Agent routing (docs/agent-routing.md): picking one of a project's agents for a task that
names none, with OpenAI's Decisions API.

``profiles`` builds what the decision knows about each agent (§3), ``build_request`` the
Decisions request (§4), and ``Router`` makes the call with its retries and checks the
answer (§6). Intake decides when to route; the call never runs inside a transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.crypto import SecretBox
from exradius.models import ConnectionTools, RoutingSettings, utcnow
from exradius.schemas.project_config import ProjectConfig

MODEL = "gpt-6-luna"
GUESS_BELOW = 0.6
"""Below this confidence the pick is a guess (§5): a starting point, to be calibrated with
the routing evaluations planned for later."""
ATTEMPTS = 3
WAITS = (2.0, 6.0)
"""Seconds before the second and the third attempt."""
TIMEOUT = 30.0
SETTINGS_ID = 1
MAX_JSON_BYTES = 256 * 1024
MAX_JSON_VALUES = 10_000
MAX_JSON_DEPTH = 32
"""``input_json`` limits of the Decisions API."""

INSTRUCTIONS = """\
You route work to the agents of one software project. Each agent is described by a \
profile: its name, its role (the agent's job description, written by the project's \
developers) and its connections, the MCP servers it can act with, each with its tools. \
Tool annotations such as readOnlyHint and destructiveHint say what a tool can change. \
Every agent also has a shell, the project's repositories as git clones, and file editing; \
those are the same for all of them and are not listed.

After the profiles comes one task, as the agent would receive it. Choose the agent best \
able to do the whole task: the one whose role covers it and whose tools let it act on it. \
When the task asks for a change, prefer an agent whose role is to make changes; when it \
asks only for an investigation or an answer, prefer an agent whose role is to investigate."""

Method = Literal["named", "only_agent", "decided"]


class RoutingFailed(Exception):
    """Routing could not pick an agent; ``str(exc)`` is the rejection's reason."""


# -- profiles (§3) --------------------------------------------------------------------------


def definition_hash(config: ProjectConfig, name: str) -> str:
    """sha256 of the connection's definition as the config writes it (secret references,
    not their values), so a stored tool list is used only for the same definition."""
    data = json.dumps(config.connection_definition(name), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def connection_kind(config: ProjectConfig, name: str) -> str:
    conn = config.connections[name]
    if conn.catalog:
        return f"catalog {conn.catalog}"
    return "custom http" if conn.url else "custom stdio"


async def stored_tools(session: AsyncSession, project_id: uuid.UUID) -> dict[str, ConnectionTools]:
    rows = (await session.scalars(select(ConnectionTools).where(ConnectionTools.project_id == project_id))).all()
    return {row.connection: row for row in rows}


def profiles(config: ProjectConfig, tools: dict[str, ConnectionTools]) -> list[dict[str, Any]]:
    """Every agent's profile, by slug: name and role from ``config``, each connection's
    tools from the last Verify when the connection is unchanged since (§3.1)."""
    out = []
    for slug in sorted(config.agents):
        agent = config.agents[slug]
        connections = []
        for name in agent.connections:
            entry: dict[str, Any] = {"name": name, "kind": connection_kind(config, name)}
            row = tools.get(name)
            if row is not None and row.definition_hash == definition_hash(config, name):
                entry["tools"] = list(row.tools)
                entry["tools_listed_at"] = row.listed_at.isoformat()
            else:
                entry["tools"] = None
                entry["tools_note"] = "not listed yet" if row is None else "not listed since this connection changed"
            connections.append(entry)
        out.append({"slug": slug, "name": config.agent_name(slug), "role": agent.role or None,
                    "connections": connections})
    return out


def _for_decision(profile: dict[str, Any]) -> dict[str, Any]:
    """A profile as the request carries it: without listing times, so the cached prefix
    changes only when a role, a connection or a tool does."""
    return {**profile, "connections": [
        {k: v for k, v in c.items() if k != "tools_listed_at"} for c in profile["connections"]
    ]}


# -- the request (§4) -------------------------------------------------------------------------


def _within_json_limits(data: Any) -> bool:
    values = 0

    def walk(value: Any, depth: int) -> bool:
        nonlocal values
        values += 1
        if values > MAX_JSON_VALUES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(value, dict):
            return all(walk(v, depth + 1) for v in value.values())
        if isinstance(value, list):
            return all(walk(v, depth + 1) for v in value)
        return True

    if not walk(data, 1):
        return False
    return len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()) <= MAX_JSON_BYTES


def _json_part(data: dict[str, Any], label: str) -> dict[str, Any]:
    """``input_json`` when the data fits its limits, else the data as text, cut."""
    if _within_json_limits(data):
        return {"type": "input_json", "data": data}
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if len(text.encode()) > MAX_JSON_BYTES:
        text = text.encode()[:MAX_JSON_BYTES].decode(errors="ignore") + f"\n[{label} cut at {MAX_JSON_BYTES // 1024} KiB]"
    return {"type": "input_text", "text": text}


def build_request(
    *,
    project_slug: str,
    project_name: str,
    agent_profiles: list[dict[str, Any]],
    task_id: str,
    instructions: str,
    context: Any,
    attachments: list[tuple[str, str]],
) -> dict[str, Any]:
    """Profiles first, ending at an explicit cache breakpoint; the task last."""
    profiles_part = _json_part({"agents": [_for_decision(p) for p in agent_profiles]}, "profiles")
    ticket: dict[str, Any] = {"context": context, "attachments": [f"{name} ({kind})" for name, kind in attachments]}
    return {
        "model": MODEL,
        "prompt_cache_key": f"exradius-routing-{project_slug}"[:64],
        "prompt_cache_options": {"mode": "explicit"},
        "input": [
            {"role": "developer", "content": INSTRUCTIONS},
            {"role": "user", "content": [
                {"type": "input_text", "text": f"The agents of project {project_name}:"},
                profiles_part,
                # input_json parts cannot carry a breakpoint, so a text part closes the prefix.
                {"type": "input_text", "text": "End of the agent profiles.",
                 "prompt_cache_breakpoint": {"mode": "explicit"}},
            ]},
            {"role": "user", "content": [
                {"type": "input_text", "text": f"# Task {task_id}\n{instructions}"},
                _json_part(ticket, "context"),
            ]},
        ],
        "questions": [{
            "type": "choice",
            "name": "agent",
            "instructions": "Which agent should do this task?",
            "choices": [{"value": p["slug"], "description": p["name"]} for p in agent_profiles],
        }],
    }


# -- the result (§8) --------------------------------------------------------------------------


@dataclass
class Routing:
    method: Method
    agent: str
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    model: str | None = None
    ms: int | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    decided_at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def guess(self) -> bool:
        return self.method == "decided" and self.confidence is not None and self.confidence < GUESS_BELOW

    def record(self) -> dict[str, Any]:
        """``runs.routing``: the decision's fields only for ``decided``."""
        if self.method != "decided":
            return {"method": self.method, "agent": self.agent, "decided_at": self.decided_at}
        return {
            "method": self.method, "agent": self.agent, "confidence": self.confidence, "guess": self.guess,
            "probabilities": self.probabilities, "model": self.model, "ms": self.ms,
            "input_tokens": self.input_tokens, "cached_tokens": self.cached_tokens, "decided_at": self.decided_at,
        }

    def payload(self) -> dict[str, Any]:
        """``payload.routing`` of the ``queued`` event."""
        if self.method != "decided":
            return {"method": self.method}
        return {"method": self.method, "confidence": self.confidence, "guess": self.guess,
                "probabilities": self.probabilities}


# -- the call (§6) ----------------------------------------------------------------------------

_KEYLIKE = re.compile(r"sk-[A-Za-z0-9_\-*.]{4,}")


def _scrub(text: str, key: str) -> str:
    """OpenAI's messages can quote a masked key ("Incorrect API key provided: sk-ab***yz"):
    no reason or log line carries anything key-like."""
    return _KEYLIKE.sub("sk-…", text.replace(key, "sk-…"))


def _openai_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        return response.text[:300].strip()
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return response.text[:300].strip()


class _Retry(Exception):
    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class Router:
    """Makes decisions; ``transport`` and ``sleep`` are for tests."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.sleep = sleep

    @staticmethod
    async def key(session: AsyncSession, box: SecretBox) -> str | None:
        row = await session.get(RoutingSettings, SETTINGS_ID)
        if row is None or not row.openai_key_encrypted:
            return None
        return box.decrypt(row.openai_key_encrypted)

    async def _post(self, key: str, request: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT) as client:
            return await client.post(
                f"{self.base_url}/decisions", json=request,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )

    async def _attempt(self, key: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._post(key, request)
        except httpx.TimeoutException as exc:
            raise _Retry("timed out") from exc
        except httpx.TransportError as exc:
            raise _Retry(f"could not be reached ({type(exc).__name__})") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _Retry(f"answered {response.status_code}")
        if response.status_code != 200:
            raise RoutingFailed(_scrub(
                f"agent routing failed: OpenAI answered {response.status_code}: {_openai_message(response)}", key))
        try:
            return response.json()
        except ValueError as exc:
            raise RoutingFailed("agent routing failed: OpenAI's answer is not JSON") from exc

    async def call(self, key: str, request: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """The Decisions answer and the milliseconds the successful attempt took; retries
        network errors, timeouts, ``429`` and ``5xx`` (§6)."""
        problems: list[str] = []
        for attempt in range(ATTEMPTS):
            if attempt:
                await self.sleep(WAITS[attempt - 1])
            started = time.monotonic()
            try:
                answer = await self._attempt(key, request)
            except _Retry as exc:
                problems.append(exc.what)
                continue
            return answer, int((time.monotonic() - started) * 1000)
        if len(set(problems)) == 1:
            summary = f"OpenAI {problems[0]} {_times(ATTEMPTS)}"
        else:
            summary = "OpenAI " + ", then ".join(problems)
        raise RoutingFailed(_scrub(f"agent routing failed: {summary}", key))

    async def decide(self, key: str, request: dict[str, Any]) -> Routing:
        answer, ms = await self.call(key, request)
        slugs = [c["value"] for c in request["questions"][0]["choices"]]
        answers = answer.get("answers") if isinstance(answer, dict) else None
        first = answers[0] if isinstance(answers, list) and answers and isinstance(answers[0], dict) else {}
        if first.get("type") == "refusal":
            raise RoutingFailed("agent routing failed: the decision refused to choose")
        choice = first.get("choice")
        if first.get("type") != "choice" or choice not in slugs:
            raise RoutingFailed(f"agent routing failed: the decision answered {str(choice)[:100]!r}, not an agent")
        probabilities = {
            p["value"]: round(float(p["probability"]), 4)
            for p in first.get("probabilities") or []
            if isinstance(p, dict) and p.get("value") in slugs and isinstance(p.get("probability"), (int, float))
        }
        confidence = first.get("confidence")
        usage = answer.get("usage") or {}
        return Routing(
            method="decided",
            agent=choice,
            confidence=round(float(confidence), 4) if isinstance(confidence, (int, float)) else None,
            probabilities=probabilities or None,
            model=answer.get("model") or MODEL,
            ms=ms,
            input_tokens=usage.get("input_tokens"),
            cached_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens"),
        )

    async def test(self, key: str) -> Routing:
        """The panel's Test (§7): a two-choice decision on a fixed sentence."""
        request = {
            "model": MODEL,
            "input": "The checkout page shows a blank screen after the last deploy.",
            "questions": [{
                "type": "choice", "name": "agent", "instructions": "Which team should look at this?",
                "choices": [{"value": "web", "description": "The web frontend."},
                            {"value": "billing", "description": "Invoices and payments."}],
            }],
        }
        return await self.decide(key, request)


def _times(n: int) -> str:
    return {1: "once", 2: "twice", 3: "three times"}.get(n, f"{n} times")
