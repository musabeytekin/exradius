"""In-memory stand-ins for GitHub (as a GitHub App sees it), the directory and the broker.

FakeGitHub checks the App's JWT against the test key, ties installation tokens to the
repositories of their installation, and pages lists two at a time to exercise ``Link``.
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import jwt

API = "https://api.github.com"
EXRADIUS = "https://exradius.acme.test"
APP_ID = "1234"
SLUG = "exradius-agent"
BOT = f"{SLUG}[bot]"


def _json(status: int, body: Any = None, headers: dict | None = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"message": message})


class FakeGitHub:
    page_size = 2

    def __init__(self, public_key: str):
        self.public_key = public_key
        self.installations: dict[int, set[str]] = {}
        self.repos: dict[str, dict] = {}
        self.tokens: dict[str, int] = {}
        self.token_requests = 0
        self._ids = itertools.count(1000)
        self._clock = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)

    # -- setting up --------------------------------------------------------------------

    def now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def add_repo(self, full_name: str, installation: int | None) -> None:
        self.repos[full_name.lower()] = {
            "full_name": full_name, "labels": [], "issues": {}, "comments": defaultdict(list), "events": defaultdict(list),
        }
        if installation is not None:
            self.installations.setdefault(installation, set()).add(full_name.lower())

    def add_label(self, repo: str, name: str, description: str = "", color: str = "ededed") -> None:
        self.repos[repo.lower()]["labels"].append({"id": next(self._ids), "name": name, "description": description, "color": color})

    def add_issue(self, repo: str, number: int, title: str, *, author: str = "ayse", body: str | None = None, pull: bool = False) -> dict:
        issue = {
            "number": number, "title": title, "body": body, "state": "open", "user": {"login": author},
            "labels": [], "created_at": self.now(), "html_url": f"https://github.com/{repo}/issues/{number}",
        }
        if pull:
            issue["pull_request"] = {"url": "..."}
        self.repos[repo.lower()]["issues"][number] = issue
        return issue

    def label_issue(self, repo: str, number: int, name: str, actor: str = "octocat") -> dict:
        """Put an existing repo label on an issue, as a person would; returns the label."""
        data = self.repos[repo.lower()]
        label = next(l for l in data["labels"] if l["name"] == name)
        data["issues"][number]["labels"].append(dict(label))
        data["events"][number].append({"event": "labeled", "label": {"name": name}, "actor": {"login": actor}})
        return dict(label)

    def add_comment(self, repo: str, number: int, login: str, body: str) -> dict:
        comment = {"id": next(self._ids), "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
                   "body": body, "created_at": self.now()}
        self.repos[repo.lower()]["comments"][number].append(comment)
        return comment

    # -- reading back -------------------------------------------------------------------

    def label_names(self, repo: str) -> dict[str, str]:
        return {l["name"]: l["description"] for l in self.repos[repo.lower()]["labels"]}

    def issue_labels(self, repo: str, number: int) -> list[str]:
        return [l["name"] for l in self.repos[repo.lower()]["issues"][number]["labels"]]

    def comments(self, repo: str, number: int) -> list[dict]:
        return self.repos[repo.lower()]["comments"][number]

    def bodies(self, repo: str, number: int) -> list[str]:
        from exradius_github.state import strip

        return [strip(c["body"]).strip() for c in self.comments(repo, number)]

    # -- the transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _page(self, request: httpx.Request, items: list) -> httpx.Response:
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        page = int(query.get("page", 1))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        headers = {}
        if start + self.page_size < len(items):
            query["page"] = str(page + 1)
            link = f"{API}{request.url.path}?" + "&".join(f"{k}={v}" for k, v in query.items())
            headers["Link"] = f'<{link}>; rel="next"'
        return _json(200, chunk, headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _error(401, "Requires authentication")
        credential = auth[7:]
        segments = [unquote(s) for s in request.url.raw_path.decode().split("?")[0].split("/") if s]
        method = request.method
        body = json.loads(request.content) if request.content else None

        if segments[:1] == ["app"] or segments[-1:] == ["installation"]:
            try:
                claims = jwt.decode(credential, self.public_key, algorithms=["RS256"])
            except jwt.PyJWTError as exc:
                return _error(401, f"A JSON web token could not be decoded: {exc}")
            if claims.get("iss") != APP_ID or claims["exp"] - claims["iat"] > 600:
                return _error(401, "bad JWT claims")
            if segments == ["app"]:
                return _json(200, {"id": int(APP_ID), "slug": SLUG, "name": "Exradius Agent"})
            if segments[:2] == ["app", "installations"] and segments[3:] == ["access_tokens"] and method == "POST":
                installation = int(segments[2])
                self.token_requests += 1
                token = f"ghs_{installation}_{self.token_requests}"
                self.tokens[token] = installation
                expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return _json(201, {"token": token, "expires_at": expires})
            if segments[0] == "repos" and len(segments) == 4:
                repo = f"{segments[1]}/{segments[2]}".lower()
                for installation, repos in self.installations.items():
                    if repo in repos:
                        return _json(200, {"id": installation})
                return _error(404, "Not Found")
            return _error(404, "no route")

        installation = self.tokens.get(credential)
        if installation is None:
            return _error(401, "Bad credentials")
        if segments[0] != "repos" or len(segments) < 4:
            return _error(404, "no route")
        repo = f"{segments[1]}/{segments[2]}".lower()
        if repo not in self.installations.get(installation, set()):
            return _error(404, "Not Found")
        data = self.repos[repo]
        rest = segments[3:]

        if rest == ["labels"]:
            if method == "GET":
                return self._page(request, data["labels"])
            if any(l["name"].casefold() == body["name"].casefold() for l in data["labels"]):
                return _error(422, "Validation Failed: already_exists")
            if len(body["name"]) > 50:
                return _error(422, "Validation Failed: name is too long")
            label = {"id": next(self._ids), **body}
            data["labels"].append(label)
            return _json(201, label)
        if rest[0] == "labels" and len(rest) == 2:
            label = next((l for l in data["labels"] if l["name"] == rest[1]), None)
            if label is None:
                return _error(404, "Not Found")
            if method == "PATCH":
                label.update({"name": body.get("new_name", label["name"]), "color": body.get("color", label["color"]),
                              "description": body.get("description", label["description"])})
                return _json(200, label)
            if method == "DELETE":
                data["labels"].remove(label)
                for issue in data["issues"].values():
                    issue["labels"] = [l for l in issue["labels"] if l["name"] != rest[1]]
                return _json(204)
        if rest == ["issues"] and method == "GET":
            query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            found = [i for i in data["issues"].values()
                     if i["state"] == query.get("state", "open") and query.get("labels") in [l["name"] for l in i["labels"]]]
            return self._page(request, found)
        if rest[:2] == ["issues", "comments"] and len(rest) == 3 and method == "PATCH":
            for comments in data["comments"].values():
                for comment in comments:
                    if comment["id"] == int(rest[2]):
                        comment["body"] = body["body"]
                        return _json(200, comment)
            return _error(404, "Not Found")
        if rest[0] == "issues" and len(rest) >= 2 and rest[1].isdigit():
            number = int(rest[1])
            issue = data["issues"].get(number)
            if issue is None:
                return _error(404, "Not Found")
            tail = rest[2:]
            if tail == [] and method == "GET":
                return _json(200, issue)
            if tail == ["comments"] and method == "GET":
                return self._page(request, data["comments"][number])
            if tail == ["comments"] and method == "POST":
                return _json(201, self.add_comment(repo, number, BOT, body["body"]))
            if tail == ["events"] and method == "GET":
                return self._page(request, data["events"][number])
            if tail[:1] == ["labels"] and len(tail) == 2 and method == "DELETE":
                if tail[1] not in [l["name"] for l in issue["labels"]]:
                    return _error(404, "Label does not exist")
                issue["labels"] = [l for l in issue["labels"] if l["name"] != tail[1]]
                return _json(200, issue["labels"])
        return _error(404, f"no route {method} {'/'.join(segments)}")


class FakeDirectory:
    def __init__(self, directory: dict, token: str = "exr_test"):
        self.directory = directory
        self.token = token

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return httpx.Response(401, json={"detail": "invalid token"})
            return httpx.Response(200, json=self.directory)

        return httpx.MockTransport(handle)


class FakePublisher:
    def __init__(self) -> None:
        self.tasks: list[dict] = []
        self.answers: list[dict] = []
        self.fail = False

    async def publish_task(self, message: dict) -> None:
        if self.fail:
            raise ConnectionError("broker is down")
        self.tasks.append(message)

    async def publish_answer(self, message: dict) -> None:
        if self.fail:
            raise ConnectionError("broker is down")
        self.answers.append(message)


def artifacts_transport(files: dict[str, bytes]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        data = files.get(str(request.url))
        return httpx.Response(200, content=data) if data is not None else httpx.Response(404)

    return httpx.MockTransport(handle)
