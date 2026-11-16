"""Where Desk fetches a result's file from (design §8, §17)."""

from __future__ import annotations

from urllib.parse import urlsplit

ARTIFACTS = "/api/artifacts/"


def download_url(url: str, api_url: str) -> str:
    """The signed path of ``task.result``'s URL on ``EXRADIUS_API_URL``, which Desk reaches,
    instead of Exradius's public address, which it may not. Other URLs are fetched as given."""
    parts = urlsplit(url)
    at = parts.path.find(ARTIFACTS)
    if not api_url or at < 0:
        return url
    return f"{api_url.rstrip('/')}{parts.path[at:]}" + (f"?{parts.query}" if parts.query else "")
