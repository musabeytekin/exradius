"""What this version of Exradius can run (docs/kubernetes-plan.md, phases 1 and 2).

Phase 2 builds workstation Dockerfiles (Build objects, docs/kubernetes.md §12). Services
come in phase 3: a config with services is valid but not runnable yet, and the reason is the
project's health reason, so tasks are rejected with it and Verify stops at "Read config".
``workstation.cache`` is accepted and ignored with a warning until the project volume
exists.
"""

from __future__ import annotations

from exradius.schemas.project_config import ProjectConfig


def unsupported(config: ProjectConfig | None) -> list[str]:
    """Why ``config`` cannot run on this version, in file order; empty when it can."""
    if config is None:
        return []
    return [f"services.{name}: services are not available yet" for name in config.services]


def warnings(config: ProjectConfig | None) -> list[str]:
    if config is None or not config.workstation.cache:
        return []
    return ["workstation.cache is ignored: caches come with the project volume, not available yet"]
