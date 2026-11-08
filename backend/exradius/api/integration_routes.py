"""The integration API (docs/integration-api.md §4, §5, §7): the directory and the bridge
status, used with an integration token bound to a bridge, and the admin's management of
those tokens and view of the bridges.

``/api/integration/v1/*`` accepts integration tokens only (``Authorization: Bearer``):
not the admin session, not download signatures. Admin routes never accept a token.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from exradius import directory
from exradius.api.deps import DB, Admin, get_or_404
from exradius.crypto import hash_token, new_integration_token
from exradius.models import BridgeStatus, IntegrationToken, utcnow
from exradius.schemas import api as s

LAST_USED_EVERY = timedelta(minutes=1)
REPORTING_WITHIN = timedelta(minutes=15)
MAX_STATUS_BODY = 64 * 1024

router = APIRouter(prefix="/api/integration/v1")
admin_router = APIRouter(prefix="/api/integration-tokens")
bridges_router = APIRouter(prefix="/api/bridges")


def _refused(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


async def require_token(request: Request, session: DB) -> IntegrationToken:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        raise _refused("an integration token is required (Authorization: Bearer <token>)")
    token = await session.scalar(select(IntegrationToken).where(IntegrationToken.token_hash == hash_token(value)))
    if token is None:
        raise _refused("unknown integration token")
    if token.revoked_at is not None:
        raise _refused("the integration token was revoked")
    now = utcnow()
    # At most one write a minute per token, decided by the database (§5).
    await session.execute(
        update(IntegrationToken)
        .where(
            IntegrationToken.id == token.id,
            or_(IntegrationToken.last_used_at.is_(None), IntegrationToken.last_used_at < now - LAST_USED_EVERY),
        )
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return token


Token = Annotated[IntegrationToken, Depends(require_token)]


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    tags = [t.strip().removeprefix("W/") for t in if_none_match.split(",")]
    return "*" in tags or etag in tags


@router.get(
    "/directory",
    response_class=Response,
    responses={200: {"model": s.DirectoryOut}, 304: {"description": "Not modified (If-None-Match)"}},
)
async def get_directory(request: Request, session: DB, token: Token):
    """The projects whose config has the token's bridge file, with that file, built from
    the latest read of each config. The ``ETag`` is the sha256 of the body;
    ``If-None-Match`` with it answers ``304``."""
    body = directory.encode(directory.view(await directory.build(session), token.bridge))
    tag = directory.etag(body)
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if _matches(request.headers.get("if-none-match"), tag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.put("/status", status_code=204)
async def put_status(request: Request, session: DB, token: Token):
    """The bridge's latest status (§7); it replaces the previous report of the same bridge."""
    body = await request.body()
    if len(body) > MAX_STATUS_BODY:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"a status report is at most {MAX_STATUS_BODY // 1024} KiB")
    try:
        report = s.BridgeStatusIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    if len(report.problems) > s.MAX_PROBLEMS:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"at most {s.MAX_PROBLEMS} problems")
    if any(len(p.message) > s.MAX_PROBLEM_MESSAGE for p in report.problems):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"a problem's message is at most {s.MAX_PROBLEM_MESSAGE} characters")
    data = report.model_dump(mode="json")
    row = await session.get(BridgeStatus, token.bridge, with_for_update=True)
    if row is None:
        session.add(BridgeStatus(bridge=token.bridge, report=data, reported_at=utcnow(), token_id=token.id))
    else:
        row.report, row.reported_at, row.token_id = data, utcnow(), token.id
    await session.commit()
    return Response(status_code=204)


# Token management (admin) -----------------------------------------------------------------


@admin_router.get("", response_model=list[s.IntegrationTokenOut])
async def list_tokens(session: DB, _: Admin):
    return (await session.scalars(select(IntegrationToken).order_by(IntegrationToken.created_at))).all()


@admin_router.post("", response_model=s.IntegrationTokenCreated, status_code=201)
async def create_token(body: s.IntegrationTokenIn, session: DB, _: Admin):
    """The token is in this answer only; Exradius keeps its sha256."""
    name = body.name or body.bridge
    if await session.scalar(select(IntegrationToken.id).where(IntegrationToken.name == name)):
        raise HTTPException(409, f"a token named {name!r} already exists")
    value = new_integration_token()
    token = IntegrationToken(name=name, bridge=body.bridge, token_hash=hash_token(value), created_at=utcnow())
    session.add(token)
    await session.commit()
    return s.IntegrationTokenCreated(**s.IntegrationTokenOut.model_validate(token).model_dump(), token=value)


@admin_router.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, session: DB, _: Admin):
    """Revoke the token; it is refused from the next request on."""
    token = await get_or_404(session, IntegrationToken, token_id)
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        await session.commit()
    return Response(status_code=204)


# Bridges (admin) ----------------------------------------------------------------------------


def _reporting(row: BridgeStatus | None) -> bool:
    if row is None:
        return False
    reported = row.reported_at if row.reported_at.tzinfo else row.reported_at.replace(tzinfo=utcnow().tzinfo)
    return reported >= utcnow() - REPORTING_WITHIN


async def bridge_problems(session: AsyncSession, slug: str) -> list[s.BridgeProblemOut]:
    """What every bridge last reported about the project ``slug``, for its page."""
    rows = (await session.scalars(select(BridgeStatus).order_by(BridgeStatus.bridge))).all()
    return [
        s.BridgeProblemOut(bridge=row.bridge, message=problem["message"], reported_at=row.reported_at)
        for row in rows
        for problem in (row.report or {}).get("problems", [])
        if problem.get("project") == slug
    ]


@bridges_router.get("", response_model=list[s.BridgeOut])
async def list_bridges(session: DB, _: Admin):
    """Every bridge Exradius knows of: from active tokens, bridge files and reports. For
    the New token form, the names found in configs are those with ``projects``."""
    full = await directory.build(session)
    tokens = (await session.scalars(select(IntegrationToken).where(IntegrationToken.revoked_at.is_(None)))).all()
    reports = {row.bridge: row for row in (await session.scalars(select(BridgeStatus))).all()}
    names = set(directory.bridge_names(full)) | {t.bridge for t in tokens} | set(reports)
    out = []
    for name in sorted(names):
        row = reports.get(name)
        report = (row.report or {}) if row else {}
        out.append(s.BridgeOut(
            bridge=name,
            tokens=sum(1 for t in tokens if t.bridge == name),
            projects=[p["slug"] for p in full["projects"] if name in (p.get("bridges") or {})],
            reported_at=row.reported_at if row else None,
            reporting=_reporting(row),
            software=report.get("software") if row else None,
            started_at=report.get("started_at") if row else None,
            problems=[s.BridgeProblem.model_validate(p) for p in report.get("problems", [])],
        ))
    return out
