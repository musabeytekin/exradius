"""The installed exradius version, for image tags, bundles and the status page."""

from __future__ import annotations

from importlib.metadata import version


def package_version() -> str:
    try:
        return version("exradius")
    except Exception:  # noqa: BLE001 - not installed as a distribution
        return "unknown"
