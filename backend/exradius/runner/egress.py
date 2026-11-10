"""The runner waits until the egress proxy sees its pod in the agent phase
(docs/kubernetes.md §11).

The proxy tells a pod's setup phase (any host) from its agent phase (the allowlist) by
watching the pod: the agent phase starts once the workstation container runs. Its watch
may be a moment behind, so before the agent's session starts, and before a verify Run lists
tools or a probe talks to the model, the runner asks every proxy pod what it sees of this
pod (``GET /phase`` on the reverse port, from the pod's own address) and goes on only once
each says ``agent``. No agent request is therefore ever decided on a stale setup phase.

The proxy pods are the addresses of a headless Service (``EXRADIUS_EGRESS_PEERS``), so a
second replica is asked too. Without ``EXRADIUS_EGRESS_URL`` (tests, development) there is
nothing to wait for.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

WAIT_SECONDS = 60.0


class EgressNotReady(RuntimeError):
    """The proxy did not see the agent phase in time; the run fails rather than start."""


async def _addresses(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out = []
    for family, _, _, _, sockaddr in infos:
        address = f"[{sockaddr[0]}]" if family == socket.AF_INET6 else sockaddr[0]
        if address not in out:
            out.append(address)
    return out


async def wait_for_agent_phase(egress: dict[str, str], timeout: float = WAIT_SECONDS, poll: float = 0.5) -> None:
    url = egress.get("EXRADIUS_EGRESS_URL")
    if not url:
        return
    parts = urlsplit(url)
    port = parts.port or 80
    peers = egress.get("EXRADIUS_EGRESS_PEERS") or parts.hostname or ""
    deadline = time.monotonic() + timeout
    seen = "no answer"
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        while True:
            try:
                addresses = await _addresses(peers, port)
                phases = []
                for address in addresses:
                    response = await client.get(f"http://{address}:{port}/phase")
                    phases.append((response.json() or {}).get("phase") if response.status_code == 200 else None)
                seen = ", ".join(f"{a}: {p or 'unknown'}" for a, p in zip(addresses, phases, strict=True))
                if addresses and all(p == "agent" for p in phases):
                    log.info("the egress proxy sees the agent phase (%s)", seen)
                    return
            except (OSError, httpx.HTTPError, ValueError) as exc:
                seen = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise EgressNotReady(f"the network proxy did not see this run's agent phase within "
                                     f"{timeout:.0f}s ({seen})")
            await asyncio.sleep(poll)
