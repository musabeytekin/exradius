"""A small GitHub REST client: only the calls the bridge makes, as the bot account (design §2, §4)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

PAGE = 100
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


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


def parse_time(value: str) -> datetime:
    """GitHub writes ``2026-11-16T19:30:49Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHub:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=timeout,
            transport=transport,
            # Transferred issues and renamed repositories answer with redirects.
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "exradius-github",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, ok: Iterable[int] = (200, 201, 204), **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(method, path, 0, str(exc) or type(exc).__name__) from exc
        if response.status_code not in tuple(ok):
            raise GitHubError(method, path, response.status_code, _detail(response))
        return response

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._request(method, path, **kwargs)
        return response.json() if response.content else None

    async def _pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Every item of a list, following ``Link: <...>; rel="next"``."""
        items: list[dict] = []
        url: str | None = path
        query: dict[str, Any] | None = {**(params or {}), "per_page": PAGE}
        while url:
            response = await self._request("GET", url, params=query)
            items.extend(response.json() or [])
            found = _NEXT.search(response.headers.get("link", ""))
            url, query = (found[1], None) if found else (None, None)
        return items

    # -- identity -------------------------------------------------------------------------

    async def me(self) -> dict:
        return await self._json("GET", "/user")

    # -- issues ---------------------------------------------------------------------------

    async def issue(self, repo: str, number: int) -> dict:
        return await self._json("GET", f"/repos/{repo}/issues/{number}")

    async def assigned(self) -> list[dict]:
        """Open issues and pull requests assigned to the authenticated user, in every
        repository it can see."""
        return await self._pages("/issues", {"filter": "assigned", "state": "open"})

    async def comments(self, repo: str, number: int) -> list[dict]:
        return await self._pages(f"/repos/{repo}/issues/{number}/comments")

    async def add_comment(self, repo: str, number: int, body: str) -> dict:
        return await self._json("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> dict:
        return await self._json("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body})

    async def add_assignees(self, repo: str, number: int, logins: list[str]) -> dict:
        """GitHub leaves out, silently, anyone who cannot be assigned."""
        return await self._json("POST", f"/repos/{repo}/issues/{number}/assignees", json={"assignees": logins})

    async def remove_assignees(self, repo: str, number: int, logins: list[str]) -> dict:
        return await self._json("DELETE", f"/repos/{repo}/issues/{number}/assignees", json={"assignees": logins})

    # -- git data: the artifacts branch (design §4.1) ----------------------------------------

    async def branch_head(self, repo: str, branch: str) -> str | None:
        """The commit a branch points at; ``None`` when there is no such branch."""
        response = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}", ok=(200, 404))
        return response.json()["object"]["sha"] if response.status_code == 200 else None

    async def commit_tree(self, repo: str, commit: str) -> str:
        return (await self._json("GET", f"/repos/{repo}/git/commits/{commit}"))["tree"]["sha"]

    async def create_blob(self, repo: str, content_base64: str) -> str:
        body = {"content": content_base64, "encoding": "base64"}
        return (await self._json("POST", f"/repos/{repo}/git/blobs", json=body))["sha"]

    async def create_tree(self, repo: str, files: dict[str, str], base_tree: str | None) -> str:
        """``files`` maps paths to blob shas."""
        body: dict[str, Any] = {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": sha}
                                         for path, sha in files.items()]}
        if base_tree:
            body["base_tree"] = base_tree
        return (await self._json("POST", f"/repos/{repo}/git/trees", json=body))["sha"]

    async def create_commit(self, repo: str, message: str, tree: str, parents: list[str]) -> str:
        body = {"message": message, "tree": tree, "parents": parents}
        return (await self._json("POST", f"/repos/{repo}/git/commits", json=body))["sha"]

    async def create_branch(self, repo: str, branch: str, commit: str) -> None:
        await self._request("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit})

    async def move_branch(self, repo: str, branch: str, commit: str) -> None:
        """Fast-forward only: a ``422`` means someone else moved it first."""
        await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit, "force": False})
