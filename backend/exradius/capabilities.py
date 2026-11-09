"""What this version of Exradius can run (docs/kubernetes-plan.md).

Since phase 3 every valid config runs: workstation and service Dockerfiles are built
(Build objects, docs/kubernetes.md §12), services are native sidecars (§7.4) and
``workstation.cache`` lives on the project volume (§7.6). The hook stays for the phases
that add keys before they can run them: a reason here is the project's health reason, so
tasks are rejected with it and Verify stops at "Read config"; a warning is shown with the
config.
"""

from __future__ import annotations

from exradius.schemas.project_config import ProjectConfig


def unsupported(config: ProjectConfig | None) -> list[str]:
    """Why ``config`` cannot run on this version, in file order; empty when it can."""
    return []


def warnings(config: ProjectConfig | None) -> list[str]:
    """What this version accepts in ``config`` but does not do."""
    return []
