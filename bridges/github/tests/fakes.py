"""An in-memory GitHub: the REST calls the bridge makes, as an httpx transport."""

from __future__ import annotations

import itertools
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

API = "https://api.github.com"


def _json(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status) if body is None else httpx.Response(status, json=body)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"message": message})


class FakeGitHub:
    def __init__(self, page_size: int = 2) -> None:
        self.tokens: dict[str, str] = {}
        """Token to login."""
        self.collaborators: dict[str, set[str]] = defaultdict(set)
        """Repository to the logins that can be assigned there."""
        self.issues: dict[tuple[str, int], dict] = {}
        self.comments: dict[tuple[str, int], list[dict]] = defaultdict(list)
        self.page_size = page_size
        """Small, so pagination is exercised."""
        self._ids = itertools.count(1000)
        self._clock = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    # -- setting up --------------------------------------------------------------------

    def now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_user(self, login: str, *, token: str | None = None, repos: tuple[str, ...] = ()) -> str:
        if token:
            self.tokens[token] = login
        for repo in repos:
            self.collaborators[repo].add(login)
        return login

    def add_issue(self, repo: str, number: int, title: str, *, author: str, assignees: tuple[str, ...] = (),
                  body: str = "", pull_request: bool = False) -> tuple[str, int]:
        issue = {
            "id": next(self._ids), "number": number, "title": title, "body": body, "state": "open",
            "user": {"login": author}, "assignees": [{"login": a} for a in assignees],
            "labels": [{"name": "bug"}], "milestone": None, "created_at": self.now(),
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"{API}/repos/{repo}",
        }
        if pull_request:
            issue["pull_request"] = {"url": f"{API}/repos/{repo}/pulls/{number}"}
        self.issues[(repo, number)] = issue
        return repo, number

    def assign(self, key: tuple[str, int], *logins: str) -> None:
        self.issues[key]["assignees"] = [{"login": a} for a in logins]

    def add_comment(self, key: tuple[str, int], author: str, body: str) -> int:
        comment = {"id": next(self._ids), "user": {"login": author}, "body": body, "created_at": self.now()}
        self.comments[key].append(comment)
        return comment["id"]

    # -- reading back -------------------------------------------------------------------

    def bodies(self, key: tuple[str, int]) -> list[str]:
        return [c["body"] for c in self.comments[key]]

    def assignees(self, key: tuple[str, int]) -> list[str]:
        return [a["login"] for a in self.issues[key]["assignees"]]

    # -- the transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Authorization", "")
        login = self.tokens.get(header.removeprefix("Bearer ")) if header.startswith("Bearer ") else None
        if login is None:
            return _error(401, "Bad credentials")
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        method = request.method
        body = json.loads(request.content) if request.content else None

        if path == "/user":
            return _json(200, {"login": login, "id": 1, "type": "User"})
        if path == "/issues" and method == "GET":
            found = [dict(i, repository={"full_name": repo}) for (repo, _), i in self.issues.items()
                     if login in [a["login"] for a in i["assignees"]] and i["state"] == "open"]
            return self._page(request, found, query)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/comments/(\d+)", path):
            if method == "PATCH":
                for comments in self.comments.values():
                    for comment in comments:
                        if comment["id"] == int(m[2]):
                            if comment["user"]["login"] != login:
                                return _error(403, "not your comment")
                            comment["body"] = body["body"]
                            return _json(200, comment)
                return _error(404, "Not Found")
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)(/.*)?", path):
            key = (m[1], int(m[2]))
            if key not in self.issues:
                return _error(404, "Not Found")
            return self._issue_routes(login, method, key, m[3] or "", query, body, request)
        return _error(404, f"no route {method} {path}")

    def _page(self, request: httpx.Request, items: list, query: dict) -> httpx.Response:
        page = int(query.get("page", 1))
        size = self.page_size
        chunk = items[(page - 1) * size: page * size]
        headers = {}
        if page * size < len(items):
            nxt = request.url.copy_merge_params({"page": page + 1})
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _issue_routes(self, login, method, key, rest, query, body, request) -> httpx.Response:
        issue = self.issues[key]
        if rest == "" and method == "GET":
            return _json(200, issue)
        if rest == "/comments" and method == "GET":
            return self._page(request, self.comments[key], query)
        if rest == "/comments" and method == "POST":
            self.add_comment(key, login, body["body"])
            return _json(201, self.comments[key][-1])
        if rest == "/assignees" and method == "POST":
            current = [a["login"] for a in issue["assignees"]]
            for who in body["assignees"]:
                if who in self.collaborators[key[0]] and who not in current:
                    current.append(who)
            self.assign(key, *current)
            return _json(201, issue)
        if rest == "/assignees" and method == "DELETE":
            self.assign(key, *[a["login"] for a in issue["assignees"] if a["login"] not in body["assignees"]])
            return _json(200, issue)
        return _error(404, f"no route {method} issue{rest}")
