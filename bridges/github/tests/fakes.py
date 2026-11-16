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
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        """Tree sha to its files (path to blob sha), flattened."""
        self.commits: dict[str, dict] = {}
        self.refs: dict[tuple[str, str], str] = {}
        """(repo, branch) to commit sha."""
        self.uploaded: dict[str, bytes] = {}
        """Images people dropped into the editor, by uuid (``private-user-images`` serves them)."""
        self.race = 0
        """Moves the branch behind the bridge's back this many times (a concurrent writer)."""
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

    def add_pull_request(self, repo: str, number: int, title: str, *, author: str, branch: str,
                         body: str = "", head_repo: str | None = None) -> tuple[str, int]:
        key = self.add_issue(repo, number, title, author=author, body=body, pull_request=True)
        self.issues[key]["_pull"] = {"number": number, "state": "open", "merged": False,
                                     "head": {"ref": branch, "repo": {"full_name": head_repo or repo}},
                                     "base": {"ref": "main"}}
        self.review_comments: dict = getattr(self, "review_comments", {})
        return key

    def pull_payload(self, key: tuple[str, int]) -> dict:
        return dict(self.issues[key]["_pull"])

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

    def files(self, repo: str, branch: str) -> dict[str, bytes]:
        """The files on a branch's head."""
        tree = self.trees[self.commits[self.refs[(repo, branch)]]["tree"]]
        return {path: self.blobs[sha] for path, sha in tree.items()}

    def upload_image(self, data: bytes) -> str:
        """An image as the editor uploads it: its Markdown link, and the bytes behind it."""
        uuid = f"{next(self._ids):08x}-1111-4222-8333-{next(self._ids):012x}"
        self.uploaded[uuid] = data
        return f"https://github.com/user-attachments/assets/{uuid}"

    @staticmethod
    def _html(body: str) -> str:
        """``body_html``: each uploaded image gets a signed private-user-images address."""
        html = re.sub(r"https://github\.com/user-attachments/assets/([0-9a-f-]{36})",
                      lambda m: f"https://private-user-images.githubusercontent.com/1/9-{m[1]}.png?jwt=signed&amp;x=1",
                      body)
        # As GitHub renders it: an image becomes a link around an <img>, both signed.
        html = re.sub(r"!\[([^\]]*)\]\((\S+?)\)", r'<a href="\2"><img src="\2" alt="\1"></a>', html)
        return f"<p>{html}</p>"

    def _full(self, request: httpx.Request, item: dict) -> dict:
        item = {k: v for k, v in item.items() if not k.startswith("_")}
        if "full+json" in request.headers.get("Accept", ""):
            return dict(item, body_html=self._html(item.get("body") or ""))
        return item

    def _sha(self, kind: str) -> str:
        return f"{kind}{next(self._ids):037d}"

    # -- the transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "private-user-images.githubusercontent.com":
            # Signed: the address is the credential, and it must not carry the bot's token.
            uuid = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})\.png$", request.url.path)
            if request.url.params.get("jwt") != "signed" or "Authorization" in request.headers or not uuid:
                return _error(403, "denied")
            return httpx.Response(200, content=self.uploaded[uuid[1]], headers={"Content-Type": "image/png"})
        if host == "github.com":
            # A private repository's upload wants a browser session: GitHub shows a sign-in page.
            return httpx.Response(200, text="<html>Sign in to GitHub</html>", headers={"Content-Type": "text/html"})
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
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)(/reviews/(\d+)/comments)?", path):
            key = (m[1], int(m[2]))
            if key not in self.issues or "_pull" not in self.issues[key]:
                return _error(404, "Not Found")
            if m[3]:
                return self._page(request, getattr(self, "review_comments", {}).get(int(m[4]), []), query)
            return _json(200, self.issues[key]["_pull"])
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/git/(.+)", path):
            if login not in self.collaborators[m[1]]:
                return _error(404, "Not Found")
            return self._git(m[1], method, m[2], body)
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

    def _git(self, repo: str, method: str, rest: str, body: Any) -> httpx.Response:
        import base64

        if (m := re.fullmatch(r"ref/heads/(.+)", rest)) and method == "GET":
            sha = self.refs.get((repo, m[1]))
            return _json(200, {"object": {"sha": sha}}) if sha else _error(404, "Not Found")
        if rest == "blobs" and method == "POST":
            sha = self._sha("b")
            self.blobs[sha] = base64.b64decode(body["content"])
            return _json(201, {"sha": sha})
        if rest == "trees" and method == "POST":
            files = dict(self.trees[body["base_tree"]]) if body.get("base_tree") else {}
            files.update({e["path"]: e["sha"] for e in body["tree"]})
            sha = self._sha("t")
            self.trees[sha] = files
            return _json(201, {"sha": sha})
        if rest == "commits" and method == "POST":
            sha = self._sha("c")
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return _json(201, {"sha": sha})
        if (m := re.fullmatch(r"commits/(\w+)", rest)) and method == "GET":
            return _json(200, {"sha": m[1], "tree": {"sha": self.commits[m[1]]["tree"]}})
        if rest == "refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            if (repo, branch) in self.refs:
                return _error(422, "Reference already exists")
            self.refs[(repo, branch)] = body["sha"]
            return _json(201, {"ref": body["ref"]})
        if (m := re.fullmatch(r"refs/heads/(.+)", rest)) and method == "PATCH":
            key = (repo, m[1])
            if self.race:
                self.race -= 1
                other = self._sha("c")
                self.commits[other] = {"tree": self.commits[self.refs[key]]["tree"], "parents": [self.refs[key]],
                                       "message": "someone else"}
                self.refs[key] = other
            if self.refs[key] not in self.commits[body["sha"]]["parents"]:
                return _error(422, "Update is not a fast forward")
            self.refs[key] = body["sha"]
            return _json(200, {"object": {"sha": body["sha"]}})
        return _error(404, f"no route {method} git/{rest}")

    def _issue_routes(self, login, method, key, rest, query, body, request) -> httpx.Response:
        issue = self.issues[key]
        if rest == "" and method == "GET":
            return _json(200, self._full(request, issue))
        if rest == "/comments" and method == "GET":
            return self._page(request, [self._full(request, c) for c in self.comments[key]], query)
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
