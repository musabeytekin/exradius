"""HTTP clients for the internal API (docs/kubernetes.md §9), authenticated with the Run's
token: ``HttpRunApi`` for task Runs, ``HttpVerifyApi`` and ``HttpProbeApi`` for verify and
probe Runs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import httpx

from exradius.schemas.internal import (
    AnswersResponse,
    ArtifactUpload,
    AttachmentItem,
    InfoRequestCreate,
    InfoRequestCreated,
    OutcomeReport,
    ProbeReport,
    Reported,
    ResultSubmission,
    RunSpecResponse,
    SessionReport,
    ToolReport,
    TranscriptBatch,
    TranscriptEvent,
    VerifySpec,
)

log = logging.getLogger(__name__)


class RunEnded(Exception):
    """The API answered 401: the run has been finalised (or the token is wrong)."""


class RunApi(Protocol):
    async def get_spec(self) -> RunSpecResponse: ...
    async def report_session(self, session_id: str) -> None: ...
    async def append_transcript(self, events: list[TranscriptEvent]) -> None: ...
    async def create_info_request(self, question: str, context: str | None) -> str: ...
    async def submit_result(self, output: Any, artifacts: list[ArtifactUpload]) -> None: ...
    async def report_outcome(self, outcome: OutcomeReport) -> None: ...
    async def report(self, status: str) -> None: ...
    async def wait_answers(self, wait: float) -> AnswersResponse: ...


class _Http:
    def __init__(self, base_url: str, prefix: str, token: str, timeout: float = 60.0):
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}{prefix}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, body: Any = None, *, params: dict | None = None,
                       timeout: float | None = None, attempts: int = 5) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, path, json=body, params=params,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}: {response.text}", request=response.request, response=response
                    )
                if response.status_code == 401:
                    raise RunEnded(f"{method} {path} -> 401: {response.text}")
                if response.status_code >= 400:
                    raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text}")
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                await asyncio.sleep(min(2**attempt, 10))
        raise RuntimeError(f"{method} {path} failed: {last}")


class HttpRunApi(_Http):
    def __init__(self, base_url: str, run_id: str, token: str, timeout: float = 60.0):
        super().__init__(base_url, f"/internal/runs/{run_id}", token, timeout)

    async def get_spec(self) -> RunSpecResponse:
        return RunSpecResponse.model_validate((await self._request("GET", "/spec")).json())

    async def report_session(self, session_id: str) -> None:
        await self._request("POST", "/session", SessionReport(session_id=session_id).model_dump())

    async def append_transcript(self, events: list[TranscriptEvent]) -> None:
        await self._request("POST", "/transcript", TranscriptBatch(events=events).model_dump(mode="json"))

    async def create_info_request(self, question: str, context: str | None) -> str:
        body = InfoRequestCreate(question=question, context=context).model_dump()
        response = await self._request("POST", "/info-requests", body)
        return InfoRequestCreated.model_validate(response.json()).request_id

    async def submit_result(self, output: Any, artifacts: list[ArtifactUpload]) -> None:
        body = ResultSubmission(output=output, artifacts=artifacts).model_dump(mode="json")
        await self._request("POST", "/result", body)

    async def report_outcome(self, outcome: OutcomeReport) -> None:
        await self._request("POST", "/outcome", outcome.model_dump())

    async def report(self, status: str) -> None:
        """``waiting`` or ``resumed`` (docs/kubernetes.md §8.2)."""
        await self._request("POST", "/reported", Reported(status=status).model_dump())

    async def wait_answers(self, wait: float) -> AnswersResponse:
        response = await self._request("GET", "/answers", params={"wait": wait}, timeout=wait + 30)
        return AnswersResponse.model_validate(response.json())

    async def list_attachments(self) -> list[AttachmentItem]:
        return [AttachmentItem.model_validate(a) for a in (await self._request("GET", "/attachments")).json()]

    async def get_attachment(self, attachment_id: str) -> bytes:
        return (await self._request("GET", f"/attachments/{attachment_id}", timeout=300)).content


class HttpVerifyApi(_Http):
    def __init__(self, base_url: str, verification_id: str, token: str):
        super().__init__(base_url, f"/internal/verifications/{verification_id}", token)

    async def get_spec(self) -> VerifySpec:
        return VerifySpec.model_validate((await self._request("GET", "/spec")).json())

    async def report_tools(self, report: ToolReport) -> None:
        await self._request("POST", "/tools", report.model_dump())


class HttpProbeApi(_Http):
    def __init__(self, base_url: str, test_id: str, token: str):
        super().__init__(base_url, f"/internal/probes/{test_id}", token)

    async def report(self, report: ProbeReport) -> None:
        await self._request("POST", "/result", report.model_dump())
