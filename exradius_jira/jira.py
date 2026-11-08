"""A small Jira Cloud REST v3 client: only the calls the bridge makes, as the bot account
(design §2, §4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

PAGE = 100


class JiraError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str):
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {path}: {status} {detail}")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        messages = list(body.get("errorMessages") or [])
        messages += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
        if messages:
            return "; ".join(str(m) for m in messages)
        if "message" in body:
            return str(body["message"])
    return str(body)[:300]


def parse_time(value: str) -> datetime:
    """Jira writes ``2026-11-08T10:02:41.123+0000``."""
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


class Jira:
    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.site = site.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.site,
            auth=(email, token),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, ok: Iterable[int] = (200, 201, 204), **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JiraError(method, path, 0, str(exc) or type(exc).__name__) from exc
        if response.status_code not in tuple(ok):
            raise JiraError(method, path, response.status_code, _detail(response))
        return response

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._request(method, path, **kwargs)
        return response.json() if response.content else None

    async def _pages(self, path: str, params: dict[str, Any] | None = None, key: str = "values") -> list[dict]:
        """Every item of a ``startAt``/``maxResults`` paginated list."""
        items: list[dict] = []
        start = 0
        while True:
            page = await self._json("GET", path, params={**(params or {}), "startAt": start, "maxResults": PAGE})
            batch = page.get(key) or []
            items.extend(batch)
            start += len(batch)
            total = page.get("total")
            if not batch or page.get("isLast") or (total is not None and start >= total):
                return items

    # -- identity -------------------------------------------------------------------------

    async def myself(self) -> dict:
        return await self._json("GET", "/rest/api/3/myself")

    # -- issues ---------------------------------------------------------------------------

    async def issue(self, issue_id: str, fields: Iterable[str]) -> dict:
        return await self._json("GET", f"/rest/api/3/issue/{issue_id}", params={"fields": ",".join(fields)})

    async def search(self, jql: str, fields: Iterable[str]) -> list[dict]:
        """Enhanced JQL search (``/search/jql``; the old ``/search`` is being removed)."""
        issues: list[dict] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"jql": jql, "fields": ",".join(fields), "maxResults": PAGE}
            if token:
                params["nextPageToken"] = token
            page = await self._json("GET", "/rest/api/3/search/jql", params=params)
            issues.extend(page.get("issues") or [])
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return issues

    async def comments(self, issue_id: str) -> list[dict]:
        return await self._pages(f"/rest/api/3/issue/{issue_id}/comment", {"orderBy": "created"}, key="comments")

    async def comment(self, issue_id: str, comment_id: str) -> dict:
        return await self._json("GET", f"/rest/api/3/issue/{issue_id}/comment/{comment_id}")

    async def add_comment(self, issue_id: str, body: dict) -> dict:
        return await self._json("POST", f"/rest/api/3/issue/{issue_id}/comment", json={"body": body})

    async def assign(self, issue_id: str, account_id: str | None) -> None:
        await self._request("PUT", f"/rest/api/3/issue/{issue_id}/assignee", json={"accountId": account_id})

    async def get_property(self, issue_id: str, key: str) -> Any | None:
        response = await self._request("GET", f"/rest/api/3/issue/{issue_id}/properties/{key}", ok=(200, 404))
        return response.json().get("value") if response.status_code == 200 else None

    async def set_property(self, issue_id: str, key: str, value: Any) -> None:
        await self._request("PUT", f"/rest/api/3/issue/{issue_id}/properties/{key}", json=value)

    async def download(self, attachment_id: str) -> bytes:
        response = await self._request("GET", f"/rest/api/3/attachment/content/{attachment_id}", ok=(200,))
        return response.content

    async def upload(self, issue_id: str, name: str, content_type: str, data: bytes) -> list[dict]:
        return await self._json(
            "POST",
            f"/rest/api/3/issue/{issue_id}/attachments",
            files={"file": (name, data, content_type)},
            headers={"X-Atlassian-Token": "no-check"},
        )
