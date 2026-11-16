"""``bridges/github.yaml`` in a project's config repository (design §3)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


def _repository(value: str) -> str:
    if not REPOSITORY.fullmatch(value) or value.endswith((".git", "/.", "/..")):
        raise ValueError(f"{value!r} is not a GitHub repository (like acme/plane)")
    return value.lower()


def _unique(values: list[str]) -> list[str]:
    repeated = sorted({v for v in values if values.count(v) > 1})
    if repeated:
        raise ValueError("listed more than once: " + ", ".join(repeated))
    return values


class GitHubConfig(BaseModel):
    """``repositories: [acme/plane]``: the repositories whose issues go to this project."""

    model_config = ConfigDict(extra="forbid")

    repositories: Annotated[
        list[Annotated[str, AfterValidator(_repository)]], Field(min_length=1), AfterValidator(_unique)
    ]
