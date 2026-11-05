"""``exradius-api``: serve the API with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from exradius.config import get_settings


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(
        "exradius.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=True,
    )


if __name__ == "__main__":
    run()
