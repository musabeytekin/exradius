"""Which Exradius project takes an item: the bridge files in the directory, validated with
the bridge's model (design §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from exradius_bridge_kit.tracker import Tracker


@dataclass(frozen=True)
class Problem:
    project: str | None
    """The project slug it is about; ``None`` for the bridge as a whole."""
    message: str


@dataclass(frozen=True)
class Resolution:
    project: str | None = None
    """The one project claiming the scope."""
    claimants: tuple[str, ...] = ()
    """All projects claiming it: none, one, or a conflict."""


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f".{part}" if out else str(part)
    return out or "(file)"


def validation_problems(where: str, exc: ValidationError) -> list[str]:
    return [f"{where}: {_loc(e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)]


@dataclass
class Projects:
    """The map from scope to project, rebuilt from every directory fetch."""

    tracker: Tracker
    names: dict[str, str] = field(default_factory=dict)
    """Project slug to name."""
    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    """Project slug to agent slug to name."""
    claims: dict[str, list[str]] = field(default_factory=dict)
    """Scope to the projects claiming it, by slug."""
    problems: list[Problem] = field(default_factory=list)

    def update(self, directory: dict[str, Any]) -> None:
        where = f"bridges/{self.tracker.bridge}.yaml"
        names: dict[str, str] = {}
        agents: dict[str, dict[str, str]] = {}
        claims: dict[str, list[str]] = {}
        problems: list[Problem] = []
        for entry in directory.get("projects") or []:
            slug = entry.get("slug")
            if not isinstance(slug, str):
                continue
            names[slug] = entry.get("name") or slug
            agents[slug] = {a["slug"]: a.get("name") or a["slug"] for a in entry.get("agents") or [] if "slug" in a}
            if entry.get("bridge_config_error"):
                problems.append(Problem(slug, str(entry["bridge_config_error"])))
                continue
            try:
                config = self.tracker.config_model.model_validate(entry.get("bridge_config") or {})
            except ValidationError as exc:
                problems += [Problem(slug, m) for m in validation_problems(where, exc)]
                continue
            for scope in self.tracker.claims(config):
                claims.setdefault(scope, [])
                if slug not in claims[scope]:
                    claims[scope].append(slug)
        for scope, slugs in sorted(claims.items()):
            if len(slugs) > 1:
                for slug in slugs:
                    others = " and ".join(s for s in slugs if s != slug)
                    problems.append(Problem(slug, f"{where}: {scope} is also claimed by {others}"))
        self.names, self.agents, self.claims, self.problems = names, agents, claims, problems

    def resolve(self, scope: str) -> Resolution:
        slugs = tuple(self.claims.get(scope, ()))
        return Resolution(project=slugs[0] if len(slugs) == 1 else None, claimants=slugs)

    def agent_name(self, project: str | None, agent: str | None) -> str | None:
        if agent is None:
            return None
        return self.agents.get(project or "", {}).get(agent) or agent
