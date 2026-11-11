"""The egress proxy's routes on the internal port (docs/kubernetes.md §9, §11).

``GET /internal/egress/runs/{name}`` is what a run (a task's ``run-<id>``, a
verification's ``verify-<id>``, a provider probe's ``probe-<id>``) may reach and with which
credentials: the agent phase's host list, the model route with the provider's credential,
each repository with its credential and push permission, and each HTTP connection whose
headers hold a secret. ``POST /internal/egress/records`` stores the proxy's batches of
requests in ``egress_records``.

Only the proxy may call them. It sends its ServiceAccount token, issued for the audience
``exradius-api``; the api asks the Kubernetes API who it belongs to (a TokenReview) and
remembers the answer for a few minutes.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from exradius.api.deps import DB, AppState, State
from exradius.git import basic_header
from exradius.models import EgressRecord, Project, ProjectVerification, Provider, ProviderTest, Run
from exradius.network import agent_connections, allowed_hosts, proxied
from exradius.project_state import cached, store_values
from exradius.providers import model_route
from exradius.resolve import StoreError, StoreValues, connection_spec, repo_specs
from exradius.runtimes import RuntimeName, runtime_class
from exradius.schemas import internal as i
from exradius.schemas.project_config import ProjectConfig
from exradius.worker.run_objects import parse_name

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/egress")

REVIEW_CACHE_SECONDS = 300.0
"""How long a reviewed token is taken for the proxy's without asking again."""

Review = Callable[[str, list[str]], Awaitable[dict[str, Any]]]


class EgressAuth:
    """Authenticates the egress proxy's ServiceAccount token with a TokenReview."""

    def __init__(self, review: Review, username: str, audience: str):
        self.review = review
        self.username = username
        self.audience = audience
        self._known: dict[str, float] = {}

    async def check(self, token: str) -> None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        if self._known.get(key, 0) > now:
            return
        try:
            status = await self.review(token, [self.audience])
        except Exception as exc:  # noqa: BLE001 - the proxy retries
            log.warning("TokenReview failed: %s", exc)
            raise HTTPException(503, "cannot review the token now") from exc
        user = (status.get("user") or {}).get("username")
        if not status.get("authenticated") or self.audience not in (status.get("audiences") or [self.audience]):
            raise HTTPException(401, "invalid token")
        if user != self.username:
            raise HTTPException(403, f"{user} is not the egress proxy")
        if len(self._known) > 1000:
            self._known = {k: v for k, v in self._known.items() if v > now}
        self._known[key] = now + REVIEW_CACHE_SECONDS


def egress_auth(state: AppState) -> EgressAuth:
    """The app's EgressAuth, made on first use with the in-cluster Kubernetes client."""
    auth = state.egress_auth
    if auth is None:
        from exradius.worker.kube import KubeCluster

        cluster = KubeCluster(state.settings.runs_namespace, state.settings.kube_api_url)
        auth = EgressAuth(cluster.review_token, state.settings.egress_service_account, state.settings.egress_audience)
        state.egress_auth = auth
    return auth


async def authenticated_egress(state: State, authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    await egress_auth(state).check(authorization.removeprefix("Bearer ").strip())


Egress = Annotated[None, Depends(authenticated_egress)]


def _repositories(config: ProjectConfig, secrets: StoreValues) -> list[i.EgressRepository]:
    out = []
    for repo in repo_specs(config, secrets):
        headers = {"Authorization": basic_header(repo.auth)} if repo.auth else {}
        out.append(i.EgressRepository(url=repo.url, push=repo.push_allowed, headers=headers))
    return out


def _connections(config: ProjectConfig, names: list[str], secrets: StoreValues) -> list[i.EgressConnection]:
    out = []
    for name in names:
        definition = config.connection_definition(name)
        if proxied(definition):
            spec = connection_spec(definition, secrets)
            out.append(i.EgressConnection(name=name, url=spec["url"], headers=spec["headers"]))
    return out


def _model(runtime: str, state: AppState, provider: Provider | None) -> i.EgressRoute | None:
    if provider is None:
        return None
    return i.EgressRoute(**model_route(runtime, state.box, provider))


def _runtime_for(provider: Provider) -> str:
    for name in RuntimeName:
        if runtime_class(name).capabilities.accepts(provider.kind, provider.auth_method):
            return name.value
    raise HTTPException(409, f"no runtime accepts {provider.kind}/{provider.auth_method}")


@router.get("/runs/{name}", response_model=i.EgressPolicy)
async def policy(name: str, session: DB, state: State, _: Egress):
    """The policy of an active run, verification or probe; 404 for anything else."""
    parsed = parse_name(name)
    if parsed is None:
        raise HTTPException(404, "not a run")
    purpose, object_id = parsed
    install = state.settings.egress_allow_hosts
    try:
        if purpose == "task":
            run = await session.get(Run, object_id)
            if run is None or run.ended_at is not None or run.config is None:
                raise HTTPException(404, "the run is not active")
            config = ProjectConfig.model_validate(run.config, strict=False)
            agent = run.task.agent
            if agent not in config.agents:
                raise HTTPException(404, "the run's agent is gone from its config")
            secrets = await store_values(session, state.box, run.task.project_id, config)
            provider = await session.get(Provider, run.provider_id) if run.provider_id else None
            return i.EgressPolicy(
                run=name, allow=allowed_hosts(config, agent, install),
                model=_model(config.agents[agent].runtime, state, provider),
                repositories=_repositories(config, secrets),
                connections=_connections(config, agent_connections(config, agent), secrets),
            )
        if purpose == "verify":
            verification = await session.get(ProjectVerification, object_id)
            if verification is None or verification.state not in ("pending", "running"):
                raise HTTPException(404, "the verification is not open")
            project = await session.get(Project, verification.project_id)
            snapshot = cached(project) if project else None
            if snapshot is None or snapshot.config is None:
                raise HTTPException(404, "the project's config is not readable")
            config = snapshot.config
            secrets = await store_values(session, state.box, verification.project_id, config)
            return i.EgressPolicy(
                run=name, allow=allowed_hosts(config, None, install),
                repositories=_repositories(config, secrets),
                connections=_connections(config, agent_connections(config, None), secrets),
            )
        test = await session.get(ProviderTest, object_id)
        if test is None or test.state != "running":
            raise HTTPException(404, "the provider test is not running")
        provider = await session.get(Provider, test.provider_id)
        if provider is None:
            raise HTTPException(404, "the provider was deleted")
        return i.EgressPolicy(run=name, model=_model(_runtime_for(provider), state, provider))
    except StoreError as exc:  # the proxy keeps the policy it has
        raise HTTPException(409, str(exc)) from exc


def _clip(value: str | None, limit: int) -> str | None:
    return value if value is None or len(value) <= limit else value[: limit - 1] + "…"


@router.post("/records", status_code=204)
async def records(body: i.EgressBatch, session: DB, _: Egress):
    """A batch of requests; a record whose run name is not one of ours is skipped."""
    for record in body.records:
        parsed = parse_name(record.run)
        if parsed is None:
            continue
        purpose, object_id = parsed
        session.add(EgressRecord(
            kind=purpose, object_id=object_id, at=record.at, phase=record.phase[:10], route=record.route[:10],
            method=_clip(record.method, 16), host=_clip(record.host, 255) or "", port=record.port,
            path=_clip(record.path, 2000), allowed=record.allowed, reason=_clip(record.reason, 1000),
            status=record.status or None, bytes_sent=record.bytes_sent, bytes_received=record.bytes_received,
            duration_ms=record.duration_ms,
        ))
    await session.commit()
    return Response(status_code=204)
