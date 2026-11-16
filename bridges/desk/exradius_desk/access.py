"""``bridges/desk.yaml`` and who sees which project (design §2)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any

from exradius_bridge_kit import Problem, validation_problems
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError

WHERE = "bridges/desk.yaml"


def _unique(values: list[str]) -> list[str]:
    repeated = sorted({v for v in values if values.count(v) > 1})
    if repeated:
        raise ValueError("listed more than once: " + ", ".join(repeated))
    return values


Group = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class DeskConfig(BaseModel):
    """``groups: [payments-team]``: the identity provider's groups whose members use the project."""

    model_config = ConfigDict(extra="forbid")

    groups: Annotated[list[Group], Field(min_length=1), AfterValidator(_unique)]


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    runnable: bool
    groups: frozenset[str]
    agents: dict[str, str]
    """Agent slug to name, for naming the agent routing picked."""


@dataclass
class Directory:
    """The projects with a valid ``bridges/desk.yaml``, rebuilt on every directory fetch."""

    projects: dict[str, Project] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def update(self, directory: dict[str, Any]) -> None:
        projects: dict[str, Project] = {}
        problems: list[Problem] = []
        for entry in directory.get("projects") or []:
            slug = entry.get("slug")
            if not isinstance(slug, str):
                continue
            if entry.get("bridge_config_error"):
                problems.append(Problem(slug, str(entry["bridge_config_error"])))
                continue
            try:
                config = DeskConfig.model_validate(entry.get("bridge_config") or {})
            except ValidationError as exc:
                problems += [Problem(slug, m) for m in validation_problems(WHERE, exc)]
                continue
            projects[slug] = Project(
                slug=slug,
                name=entry.get("name") or slug,
                runnable=bool(entry.get("runnable", True)),
                groups=frozenset(config.groups),
                agents={a["slug"]: a.get("name") or a["slug"] for a in entry.get("agents") or [] if "slug" in a},
            )
        self.projects, self.problems = projects, problems

    def visible(self, groups: Iterable[str]) -> list[Project]:
        mine = set(groups)
        return sorted((p for p in self.projects.values() if p.groups & mine), key=lambda p: p.slug)

    def allows(self, groups: Iterable[str], slug: str) -> bool:
        project = self.projects.get(slug)
        return project is not None and bool(project.groups & set(groups))

    def agent_name(self, slug: str, agent: str | None) -> str | None:
        if agent is None:
            return None
        project = self.projects.get(slug)
        return (project.agents.get(agent) if project else None) or agent
