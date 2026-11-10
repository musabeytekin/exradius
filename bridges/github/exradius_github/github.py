"""GitHub as a GitHub App: a JWT for app calls, installation tokens for repository calls,
and only the REST calls the bridge makes (design §2, §6)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

import httpx
import jwt

PAGE = 100
TOKEN_MARGIN = 300  # renew an installation token five minutes before it expires


class GitHubError(Exception):
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
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return str(body)[:300]


def _expiry(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHubApp:
    def __init__(
        self,
        api_url: str,
        app_id: str,
        private_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.app_id = app_id
        self._key = private_key
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "exradius-github",
            },
            timeout=30.0,
            transport=transport,
            follow_redirects=True,
        )
        self._tokens: dict[int, tuple[str, float]] = {}
        self._installations: dict[str, int | None] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    async def request(self, method: str, path: str, *, token: str, ok: Iterable[int] = (200, 201, 204), **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(method, path, 0, str(exc) or type(exc).__name__) from exc
        if response.status_code not in tuple(ok):
            raise GitHubError(method, path, response.status_code, _detail(response))
        return response

    async def pages(self, path: str, *, token: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Every item of a list, following ``Link: rel="next"``."""
        items: list[dict] = []
        url: str | None = path
        query: dict[str, Any] | None = {**(params or {}), "per_page": PAGE}
        while url:
            response = await self.request("GET", url, token=token, params=query)
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            query = None  # the next link carries the query
        return items

    # -- the App -----------------------------------------------------------------------

    async def app(self) -> dict:
        return (await self.request("GET", "/app", token=self.jwt())).json()

    def remember_installation(self, repo: str, installation: int) -> None:
        self._installations[repo.lower()] = installation

    def forget_installations(self) -> None:
        self._installations.clear()

    async def installation_for(self, repo: str) -> int | None:
        """The App's installation on a repository, or None if it is not installed there."""
        key = repo.lower()
        if key not in self._installations:
            response = await self.request("GET", f"/repos/{repo}/installation", token=self.jwt(), ok=(200, 404))
            self._installations[key] = int(response.json()["id"]) if response.status_code == 200 else None
        return self._installations[key]

    async def token(self, installation: int) -> str:
        cached = self._tokens.get(installation)
        if cached and cached[1] - TOKEN_MARGIN > self._clock():
            return cached[0]
        response = await self.request("POST", f"/app/installations/{installation}/access_tokens", token=self.jwt())
        body = response.json()
        self._tokens[installation] = (body["token"], _expiry(body["expires_at"]))
        return body["token"]

    def repo(self, full_name: str, installation: int) -> Repo:
        return Repo(self, full_name, installation)


class Repo:
    """Calls on one repository with its installation's token."""

    def __init__(self, app: GitHubApp, full_name: str, installation: int):
        self.app = app
        self.name = full_name
        self.installation = installation

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = await self.app.token(self.installation)
        return await self.app.request(method, f"/repos/{self.name}{path}", token=token, **kwargs)

    async def _pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        token = await self.app.token(self.installation)
        return await self.app.pages(f"/repos/{self.name}{path}", token=token, params=params)

    # labels
    async def labels(self) -> list[dict]:
        return await self._pages("/labels")

    async def create_label(self, name: str, color: str, description: str) -> None:
        await self._request("POST", "/labels", json={"name": name, "color": color, "description": description})

    async def update_label(self, current: str, name: str, color: str, description: str) -> None:
        body = {"new_name": name, "color": color, "description": description}
        await self._request("PATCH", f"/labels/{quote(current, safe='')}", json=body)

    async def delete_label(self, name: str) -> None:
        await self._request("DELETE", f"/labels/{quote(name, safe='')}", ok=(204, 404))

    # issues
    async def issue(self, number: int) -> dict:
        return (await self._request("GET", f"/issues/{number}")).json()

    async def issues_with_label(self, name: str) -> list[dict]:
        issues = await self._pages("/issues", {"labels": name, "state": "open"})
        return [i for i in issues if "pull_request" not in i]

    async def remove_label(self, number: int, name: str) -> None:
        await self._request("DELETE", f"/issues/{number}/labels/{quote(name, safe='')}", ok=(200, 404))

    async def label_events(self, number: int) -> list[dict]:
        return [e for e in await self._pages(f"/issues/{number}/events") if e.get("event") == "labeled"]

    # comments
    async def comments(self, number: int) -> list[dict]:
        return await self._pages(f"/issues/{number}/comments")

    async def comment(self, number: int, body: str) -> dict:
        return (await self._request("POST", f"/issues/{number}/comments", json={"body": body})).json()

    async def edit_comment(self, comment_id: int, body: str) -> dict:
        return (await self._request("PATCH", f"/issues/comments/{comment_id}", json={"body": body})).json()
