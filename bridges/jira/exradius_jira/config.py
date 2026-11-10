"""``bridges/jira.yaml`` in a project's config repository (design §3)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

SPACE_KEY = re.compile(r"[A-Z][A-Z0-9_]*")


def _key(value: str) -> str:
    if not SPACE_KEY.fullmatch(value):
        raise ValueError(f"{value!r} is not a Jira space key (like PLANE)")
    return value


def _unique(values: list[str]) -> list[str]:
    repeated = sorted({v for v in values if values.count(v) > 1})
    if repeated:
        raise ValueError("listed more than once: " + ", ".join(repeated))
    return values


class JiraConfig(BaseModel):
    """``projects: [PLANE]``: the Jira spaces whose issues go to this Exradius project."""

    model_config = ConfigDict(extra="forbid")

    projects: Annotated[list[Annotated[str, AfterValidator(_key)]], Field(min_length=1), AfterValidator(_unique)]
