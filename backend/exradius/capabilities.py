"""What this version of Exradius can run (docs/kubernetes-plan.md, phase 1).

Phase 1 runs image-based projects without services. A config that needs image builds or
services is valid but not runnable yet: the reason is the project's health reason, so tasks
are rejected with it and Verify stops at "Read config". ``workstation.cache`` is accepted and
ignored with a warning until the project volume exists.
"""

from __future__ import annotations

from exradius.schemas.project_config import ProjectConfig


def unsupported(config: ProjectConfig | None) -> list[str]:
    """Why ``config`` cannot run on this version, in file order; empty when it can."""
    if config is None:
        return []
    reasons = []
    if config.workstation.dockerfile:
        reasons.append("workstation.dockerfile needs image builds, not available yet")
    for name in config.services:
        reasons.append(f"services.{name}: services are not available yet")
    return reasons


def warnings(config: ProjectConfig | None) -> list[str]:
    if config is None or not config.workstation.cache:
        return []
    return ["workstation.cache is ignored: caches come with the project volume, not available yet"]
