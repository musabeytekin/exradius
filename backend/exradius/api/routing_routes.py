"""Agent routing settings (docs/agent-routing.md §7): the OpenAI key, write-only, and the
panel's Test; and the agent profiles routing sees (§3.2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from exradius import routing
from exradius.api.deps import DB, Admin, State, get_or_404
from exradius.config_repo import ProjectConfigSnapshot
from exradius.models import Project, RoutingSettings, utcnow
from exradius.project_state import cached
from exradius.schemas import api as s

router = APIRouter(prefix="/api")


async def _settings(session: AsyncSession) -> RoutingSettings:
    """The single row; created when missing (the migration inserts it, tests that create
    the schema from the models do not)."""
    row = await session.get(RoutingSettings, routing.SETTINGS_ID, with_for_update=True)
    if row is None:
        row = RoutingSettings(id=routing.SETTINGS_ID)
        session.add(row)
    return row


def _out(row: RoutingSettings | None) -> s.RoutingOut:
    return s.RoutingOut(
        key_set=bool(row and row.openai_key_encrypted),
        key_set_at=row.key_set_at if row and row.openai_key_encrypted else None,
        last_test=s.RoutingTestOut.model_validate(row.last_test) if row and row.last_test else None,
        model=routing.MODEL,
        guess_below=routing.GUESS_BELOW,
    )


@router.get("/routing", response_model=s.RoutingOut)
async def get_routing(session: DB, _: Admin):
    return _out(await session.get(RoutingSettings, routing.SETTINGS_ID))


@router.put("/routing/key", response_model=s.RoutingOut)
async def set_key(body: s.RoutingKeyIn, session: DB, state: State, _: Admin):
    """Set or replace the key; it is never returned. A new key clears the last test."""
    row = await _settings(session)
    row.openai_key_encrypted = state.box.encrypt(body.key)
    row.key_set_at = utcnow()
    row.last_test = None
    await session.commit()
    return _out(row)


@router.delete("/routing/key", status_code=204)
async def delete_key(session: DB, _: Admin):
    row = await _settings(session)
    row.openai_key_encrypted = None
    row.key_set_at = None
    row.last_test = None
    await session.commit()
    return Response(status_code=204)


@router.post("/routing/test", response_model=s.RoutingTestOut)
async def test_key(session: DB, state: State, _: Admin):
    """A two-choice decision on a fixed sentence, from the API process."""
    key = await routing.Router.key(session, state.box)
    if key is None:
        raise HTTPException(409, "no OpenAI API key is set")
    await session.commit()  # no transaction is held during the call
    router = state.router or routing.Router(state.settings.openai_base_url)
    try:
        picked = await router.test(key)
        result = s.RoutingTestOut(ok=True, at=utcnow(), ms=picked.ms,
                                  detail=f"answered {picked.agent}, confidence {picked.confidence}")
    except routing.RoutingFailed as exc:
        result = s.RoutingTestOut(ok=False, at=utcnow(), detail=str(exc).removeprefix("agent routing failed: "))
    row = await _settings(session)
    row.last_test = result.model_dump(mode="json")
    await session.commit()
    return result


@router.get("/projects/{project_id}/agents", response_model=list[s.AgentProfileOut])
async def agent_profiles(project_id: uuid.UUID, session: DB, _: Admin):
    """Each agent's profile as routing would build it from the latest read of the config
    and the tools the last Verify listed (§3.2)."""
    project = await get_or_404(session, Project, project_id)
    snapshot = cached(project) or ProjectConfigSnapshot.unreadable("the config has not been read yet")
    if snapshot.config is None:
        return []
    return routing.profiles(snapshot.config, await routing.stored_tools(session, project.id))
