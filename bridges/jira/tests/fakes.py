"""An in-memory Jira Cloud: the REST v3 calls the bridge makes, as an httpx transport."""

from __future__ import annotations

import base64
import itertools
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

SITE = "https://acme.atlassian.net"


@dataclass
class User:
    account_id: str
    name: str
    email: str
    admin: bool = False

    def ref(self) -> dict:
        return {"accountId": self.account_id, "displayName": self.name}


def _json(status: int, body: Any = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body)


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessages": [message], "errors": {}})


class FakeJira:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = defaultdict(list)
        self.properties: dict[tuple[str, str], Any] = {}
        self.attachment_data: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str, bytes]] = []
        self.unassignable: set[str] = set()
        self._ids = itertools.count(10000)
        self._clock = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)

    # -- setting up --------------------------------------------------------------------

    def add_user(self, email: str, name: str, *, admin: bool = False) -> User:
        user = User(f"acc-{len(self.users) + 1}", name, email, admin)
        self.users[email] = user
        return user

    def now(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def add_issue(self, key: str, summary: str, *, reporter: User, assignee: User | None = None, **fields: Any) -> str:
        issue_id = str(next(self._ids))
        self.issues[issue_id] = {
            "id": issue_id,
            "key": key,
            "fields": {
                "summary": summary,
                "reporter": reporter.ref(),
                "assignee": assignee.ref() if assignee else None,
                "created": self.now(),
                "project": {"key": key.rsplit("-", 1)[0]},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
                "status": {"name": "To Do"},
                "labels": [],
                "components": [],
                "issuelinks": [],
                "attachment": [],
                **fields,
            },
        }
        return issue_id

    def assign(self, issue_id: str, user: User | None) -> None:
        self.issues[issue_id]["fields"]["assignee"] = user.ref() if user else None

    def add_comment(self, issue_id: str, author: User, body: dict) -> str:
        comment = {"id": str(next(self._ids)), "author": author.ref(), "body": body, "created": self.now()}
        self.comments[issue_id].append(comment)
        return comment["id"]

    def add_attachment(self, issue_id: str, filename: str, data: bytes, mime: str = "text/plain") -> str:
        attachment_id = str(next(self._ids))
        self.attachment_data[attachment_id] = data
        self.issues[issue_id]["fields"]["attachment"].append(
            {"id": attachment_id, "filename": filename, "size": len(data), "mimeType": mime, "created": self.now()}
        )
        return attachment_id

    # -- reading back -------------------------------------------------------------------

    def comment_texts(self, issue_id: str) -> list[str]:
        return [_plain(c["body"]) for c in self.comments[issue_id]]

    def assignee(self, issue_id: str) -> str | None:
        value = self.issues[issue_id]["fields"]["assignee"]
        return value["accountId"] if value else None

    # -- the transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _user(self, request: httpx.Request) -> User | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        email = base64.b64decode(header[6:]).decode().split(":", 1)[0]
        return self.users.get(email)

    def handle(self, request: httpx.Request) -> httpx.Response:
        user = self._user(request)
        if user is None:
            return _error(401, "Client must be authenticated to access this resource.")
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        method = request.method
        body = json.loads(request.content) if request.content and "json" in request.headers.get("content-type", "") else None

        if path == "/rest/api/3/myself":
            return _json(200, {"accountId": user.account_id, "displayName": user.name, "emailAddress": user.email})
        if path == "/rest/api/3/search/jql":
            return self._search(query)
        if m := re.fullmatch(r"/rest/api/3/attachment/content/(\d+)", path):
            data = self.attachment_data.get(m[1])
            return httpx.Response(200, content=data) if data is not None else _error(404, "no attachment")
        if m := re.fullmatch(r"/rest/api/3/issue/([^/]+)(/.*)?", path):
            issue = self._issue(m[1])
            if issue is None:
                return _error(404, "Issue does not exist or you do not have permission to see it.")
            return self._issue_routes(user, method, issue, m[2] or "", query, body, request)
        return _error(404, f"no route {method} {path}")

    def _issue(self, id_or_key: str) -> dict | None:
        if id_or_key in self.issues:
            return self.issues[id_or_key]
        return next((i for i in self.issues.values() if i["key"] == id_or_key), None)

    def _issue_routes(self, user, method, issue, rest, query, body, request) -> httpx.Response:
        issue_id = issue["id"]
        if rest == "" and method == "GET":
            wanted = query.get("fields", "").split(",")
            fields = {k: v for k, v in issue["fields"].items() if k in wanted}
            return _json(200, {"id": issue_id, "key": issue["key"], "fields": fields})
        if rest == "/comment" and method == "GET":
            start, size = int(query.get("startAt", 0)), int(query.get("maxResults", 50))
            items = self.comments[issue_id]
            return _json(200, {"comments": items[start : start + size], "startAt": start, "maxResults": size, "total": len(items)})
        if rest == "/comment" and method == "POST":
            comment_id = self.add_comment(issue_id, user, body["body"])
            return _json(201, self.comments[issue_id][-1] | {"id": comment_id})
        if m := re.fullmatch(r"/comment/(\d+)", rest):
            comment = next((c for c in self.comments[issue_id] if c["id"] == m[1]), None)
            return _json(200, comment) if comment else _error(404, "no comment")
        if rest == "/assignee" and method == "PUT":
            account = body.get("accountId")
            if account in self.unassignable:
                return _error(400, "User cannot be assigned issues.")
            target = next((u for u in self.users.values() if u.account_id == account), None)
            issue["fields"]["assignee"] = target.ref() if target else None
            return _json(204)
        if m := re.fullmatch(r"/properties/([^/]+)", rest):
            key = (issue_id, m[1])
            if method == "GET":
                return _json(200, {"key": m[1], "value": self.properties[key]}) if key in self.properties else _error(404, "no property")
            if method == "PUT":
                created = key not in self.properties
                self.properties[key] = body
                return _json(201 if created else 200)
        if rest == "/attachments" and method == "POST":
            if request.headers.get("X-Atlassian-Token") != "no-check":
                return _error(403, "XSRF check failed")
            name = re.search(rb'filename="([^"]+)"', request.content)[1].decode()
            data = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            self.uploads.append((issue_id, name, data))
            return _json(200, [{"id": str(next(self._ids)), "filename": name}])
        return _error(404, f"no route {method} issue{rest}")

    def _search(self, query: dict) -> httpx.Response:
        jql = query.get("jql", "")
        m = re.search(r'assignee = "([^"]+)"', jql)
        found = [i for i in self.issues.values() if m and (i["fields"]["assignee"] or {}).get("accountId") == m[1]]
        wanted = query.get("fields", "").split(",")
        issues = [{"id": i["id"], "key": i["key"], "fields": {k: v for k, v in i["fields"].items() if k in wanted}} for i in found]
        return _json(200, {"issues": issues, "isLast": True})


def _plain(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        if node.get("type") == "mention":
            return node["attrs"].get("text", "")
        sep = "\n" if node.get("type") == "doc" else ""
        return sep.join(_plain(c) for c in node.get("content") or [])
    return ""
