"""HTTP client for the internal run API, authenticated with the per-run token."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import httpx

from exradius.schemas.internal import (
    ArtifactUpload,
    InfoRequestCreate,
    InfoRequestCreated,
    OutcomeReport,
    ResultSubmission,
    RunSpecResponse,
    SessionReport,
    TranscriptBatch,
    TranscriptEvent,
)

log = logging.getLogger(__name__)


class RunApi(Protocol):
    async def get_spec(self) -> RunSpecResponse: ...
    async def report_session(self, session_id: str) -> None: ...
    async def append_transcript(self, events: list[TranscriptEvent]) -> None: ...
    async def create_info_request(self, question: str, context: str | None) -> str: ...
    async def submit_result(self, output: Any, artifacts: list[ArtifactUpload]) -> None: ...
    async def report_outcome(self, outcome: OutcomeReport) -> None: ...


class HttpRunApi:
    def __init__(self, base_url: str, run_id: str, token: str, timeout: float = 60.0):
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/internal/runs/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, body: Any = None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(5):
            try:
                response = await self._client.request(method, path, json=body)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}: {response.text}", request=response.request, response=response
                    )
                if response.status_code >= 400:
                    raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text}")
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                await asyncio.sleep(min(2**attempt, 10))
        raise RuntimeError(f"{method} {path} failed: {last}")

    async def get_spec(self) -> RunSpecResponse:
        return RunSpecResponse.model_validate((await self._request("GET", "/spec")).json())

    async def report_session(self, session_id: str) -> None:
        await self._request("POST", "/session", SessionReport(session_id=session_id).model_dump())

    async def append_transcript(self, events: list[TranscriptEvent]) -> None:
        await self._request(
            "POST", "/transcript", TranscriptBatch(events=events).model_dump(mode="json")
        )

    async def create_info_request(self, question: str, context: str | None) -> str:
        body = InfoRequestCreate(question=question, context=context).model_dump()
        response = await self._request("POST", "/info-requests", body)
        return InfoRequestCreated.model_validate(response.json()).request_id

    async def submit_result(self, output: Any, artifacts: list[ArtifactUpload]) -> None:
        body = ResultSubmission(output=output, artifacts=artifacts).model_dump(mode="json")
        await self._request("POST", "/result", body)

    async def report_outcome(self, outcome: OutcomeReport) -> None:
        await self._request("POST", "/outcome", outcome.model_dump())
