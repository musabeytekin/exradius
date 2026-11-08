"""A run's state, carried in the bot's own comments as a hidden marker (design §5).

GitHub issues have no hidden properties, so every comment the bot posts ends with
``<!-- exradius-github:<base64 JSON> -->``; the issue's state is the one in the bot's
latest comment that has a marker. The marker never renders, and Quote reply never copies it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

_MARKER = re.compile(r"\n*<!-- exradius-github:([A-Za-z0-9_=-]+) -->")


class IssueState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    runs: int = 0
    task_id: str | None = None
    project: str | None = None
    agent: str | None = None
    label: str | None = None
    """``Plane · Developer``: how comments name the agent of the current run."""
    agent_label: str | None = None
    """The label that started the run; removed when it ends."""
    requester: str | None = None
    """Who added the label; questions and results mention them."""
    state: Literal["active", "waiting", "ended"] | None = None
    request_id: str | None = None
    retries: int = 0

    @property
    def running(self) -> bool:
        return self.state in ("active", "waiting")


def task_id_for(repo: str, number: int, runs: int) -> str:
    """``plane-123`` for the first run of acme/plane#123, then ``plane-123-2``, ``-3``."""
    base = f"{repo.split('/')[-1]}-{number}"
    return base if runs <= 1 else f"{base}-{runs}"


def encode(state: IssueState) -> str:
    raw = json.dumps(state.model_dump(mode="json"), separators=(",", ":")).encode()
    return f"<!-- exradius-github:{base64.urlsafe_b64encode(raw).decode()} -->"


def decode(body: str | None) -> IssueState | None:
    found = _MARKER.findall(body or "")
    if not found:
        return None
    try:
        return IssueState.model_validate(json.loads(base64.urlsafe_b64decode(found[-1])))
    except (binascii.Error, ValueError, ValidationError):
        return None


def with_state(body: str, state: IssueState) -> str:
    return f"{strip(body).rstrip()}\n\n{encode(state)}"


def strip(body: str | None) -> str:
    return _MARKER.sub("", body or "")


def latest(comments: list[dict], bot: str) -> tuple[IssueState, dict | None]:
    """The state in the bot's latest comment that carries one, and that comment."""
    for comment in reversed(comments):
        if (comment.get("user") or {}).get("login") != bot:
            continue
        state = decode(comment.get("body"))
        if state is not None:
            return state, comment
    return IssueState(), None
