"""``exradius-api``: serve the API with uvicorn on the api and internal ports."""

from __future__ import annotations

import logging
import socket

import uvicorn

from exradius.config import get_settings


def _listen(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    # One process, one app, two listening sockets: uvicorn serves both, and ``PortSplit``
    # keeps each port to its own routes.
    sockets = [_listen(settings.api_host, settings.api_port), _listen(settings.api_host, settings.internal_port)]
    config = uvicorn.Config(
        "exradius.api.app:create_app",
        factory=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
    )
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    run()
