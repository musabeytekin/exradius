"""The integration API (docs/integration-api.md §4, §5): the directory, read with an
integration token, and the admin's management of those tokens.

``/api/integration/v1/*`` accepts integration tokens only (``Authorization: Bearer``):
not the admin session, not download signatures. Admin routes never accept a token.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import or_, select, update

from exradius import directory
from exradius.api.deps import DB, Admin, get_or_404
from exradius.crypto import hash_token, new_integration_token
from exradius.models import IntegrationToken, utcnow
from exradius.schemas import api as s

LAST_USED_EVERY = timedelta(minutes=1)

router = APIRouter(prefix="/api/integration/v1")
admin_router = APIRouter(prefix="/api/integration-tokens")


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
async def get_directory(request: Request, session: DB, _: Token):
    """Projects and their agents, built from the latest read of each config. The ``ETag``
    is the sha256 of the body; ``If-None-Match`` with it answers ``304``."""
    body = directory.encode(await directory.build(session))
    tag = directory.etag(body)
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if _matches(request.headers.get("if-none-match"), tag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


# Token management (admin) -----------------------------------------------------------------


@admin_router.get("", response_model=list[s.IntegrationTokenOut])
async def list_tokens(session: DB, _: Admin):
    return (await session.scalars(select(IntegrationToken).order_by(IntegrationToken.created_at))).all()


@admin_router.post("", response_model=s.IntegrationTokenCreated, status_code=201)
async def create_token(body: s.IntegrationTokenIn, session: DB, _: Admin):
    """The token is in this answer only; Exradius keeps its sha256."""
    if await session.scalar(select(IntegrationToken.id).where(IntegrationToken.name == body.name)):
        raise HTTPException(409, f"a token named {body.name!r} already exists")
    value = new_integration_token()
    token = IntegrationToken(name=body.name, token_hash=hash_token(value), created_at=utcnow())
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
