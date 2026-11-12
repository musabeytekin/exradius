"""The ``vault`` backend: a HashiCorp Vault KV version 2 secret per store (docs/config-repo.md §7).

One Vault per install (values ``vault.*``, ``EXRADIUS_VAULT_*``); each store names a KV
version 2 mount and a path in it, and the fields of the secret at that path are the store's
keys. Field values must be strings.

- **Sign-in.** The api and the worker sign in as their Kubernetes ServiceAccount through
  Vault's Kubernetes auth method (``POST /v1/auth/<mount>/login`` with the role and the pod's
  token). Exradius stores no Vault credential. The ServiceAccount token file is read again at
  every sign-in, since the kubelet rotates it, and the Vault token is kept until shortly
  before its lease ends. A ``403`` on a request signs in once more before it counts.
- **Key names** come from ``GET /v1/<mount>/subkeys/<path>?depth=1`` (Vault 1.10 or later),
  which lists a secret's fields without their values, and are kept for ``NAMES_TTL`` seconds
  so the panel and the directory do not ask Vault on every view.
- **Values** come from ``GET /v1/<mount>/data/<path>`` when a run, a verification or the
  egress proxy needs them, and are never cached.
- **Unreachable is not missing.** A request that fails for the network, a timeout, a 5xx (a
  sealed or standby Vault) or a 429 is tried again for up to ``RETRY_SECONDS`` when the caller
  asked for it (intake, ``preparing``, Verify, the run spec, the egress policy); a refused
  sign-in or a refused read is not. Then ``StoreUnreachable`` says why. A path that holds no
  secret (``404``) is a store with no keys.
- **KV version 1 is told apart.** On a KV version 1 mount (or any other engine) the
  ``subkeys`` and ``data`` paths answer ``404`` with no error, exactly like an empty KV
  version 2 path, so a store would look empty and its projects would read "missing secret".
  When a read comes back as an empty path, and when a store's settings are saved, the mount
  is looked up with ``GET /v1/sys/internal/ui/mounts/<mount>/<path>`` (``data.type`` and
  ``data.options.version``; the store's read policy is enough), and anything but KV version 2
  is a clear reason. The answer is kept for ``MOUNT_TTL`` seconds per mount; a read that
  finds the secret never asks.

Plain httpx, as ``worker/kube.py`` and ``pod_identity.py``: no vendor SDK.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx

from exradius.resolve import StoreError
from exradius.secret_backends import StoreUnreachable
from exradius.secret_backends.kinds import VaultSettings

log = logging.getLogger(__name__)

NAMES_TTL = 30.0
"""Seconds key names are kept: the panel and the directory read them, never values."""
FAILURE_TTL = 5.0
"""Seconds a failure to list names is remembered for callers that ask once (the panel and
the directory), so an outage does not make every view wait for Vault again."""
RETRY_SECONDS = 10.0
"""How long a caller that retries keeps trying a Vault that does not answer."""
REQUEST_TIMEOUT = 5.0
"""Seconds one request may take."""
TOKEN_MARGIN = 60.0
"""Seconds before the Vault token's lease ends when it is renewed by signing in again."""
SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
MOUNT_TTL = 60.0
"""Seconds what a mount is (KV version 2 or not) is kept; asked only when a path reads empty."""


class _Refused(Exception):
    """Vault answered, and said no: not worth asking again."""


class VaultClient:
    """The install's Vault: signs in once for every store and keeps short caches."""

    def __init__(self, address: str, *, role: str, auth_mount: str = "kubernetes", namespace: str | None = None,
                 ca_file: Path | None = None, token_file: Path = SERVICE_ACCOUNT_TOKEN,
                 transport: httpx.AsyncBaseTransport | None = None, retry_seconds: float = RETRY_SECONDS):
        self.address = address.rstrip("/")
        self.role = role
        self.auth_mount = auth_mount.strip("/")
        self.namespace = namespace
        self.token_file = token_file
        self.retry_seconds = retry_seconds
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
        self._client = httpx.AsyncClient(base_url=self.address, verify=verify, timeout=REQUEST_TIMEOUT,
                                         transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()
        self._names: dict[tuple[str, str], tuple[float, set[str]]] = {}
        self._failures: dict[tuple[str, str], tuple[float, str]] = {}
        self._mounts: dict[str, tuple[float, str | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- signing in ---------------------------------------------------------------------------

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"X-Vault-Namespace": self.namespace} if self.namespace else {}
        if token:
            headers["X-Vault-Token"] = token
        return headers

    async def _login(self) -> None:
        try:
            jwt = (await asyncio.to_thread(self.token_file.read_text)).strip()
        except OSError as exc:
            raise _Refused(f"cannot read the pod's ServiceAccount token ({self.token_file}): {exc.strerror or exc}") from exc
        response = await self._client.post(
            f"/v1/auth/{self.auth_mount}/login", json={"role": self.role, "jwt": jwt}, headers=self._headers(),
        )
        if response.status_code >= 500 or response.status_code == 429:
            raise httpx.HTTPStatusError(_problem(response, "sign-in"), request=response.request, response=response)
        if response.status_code != 200:
            raise _Refused(f"Vault refused the sign-in as role {self.role!r} at auth/{self.auth_mount} "
                           f"({_problem(response, 'sign-in')})")
        auth = (response.json() or {}).get("auth") or {}
        token = auth.get("client_token")
        if not token:
            raise _Refused("Vault's sign-in answer has no client_token")
        lease = float(auth.get("lease_duration") or 0) or 3600.0
        self._token = token
        self._token_expires = time.monotonic() + lease - min(TOKEN_MARGIN, lease / 2)

    async def _token_now(self, *, replacing: str | None = None) -> str:
        """The Vault token, signing in when there is none, its lease is nearly over, or
        Vault just refused ``replacing`` (unless another request signed in meanwhile)."""
        async with self._login_lock:
            expired = self._token is None or time.monotonic() >= self._token_expires
            if expired or (replacing is not None and self._token == replacing):
                self._token = None
                await self._login()
            assert self._token is not None
            return self._token

    # -- requests -----------------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """One GET with the token; a 403 signs in again and asks once more."""
        token = await self._token_now()
        response = await self._client.get(path, params=params, headers=self._headers(token))
        if response.status_code == 403:
            token = await self._token_now(replacing=token)
            response = await self._client.get(path, params=params, headers=self._headers(token))
        if response.status_code >= 500 or response.status_code == 429:
            raise httpx.HTTPStatusError(_problem(response, path), request=response.request, response=response)
        return response

    async def _attempts(self, store: str, path: str, params: dict[str, Any] | None, retry: bool) -> httpx.Response:
        """The response to ``GET path``, trying again for ``retry_seconds`` while Vault
        cannot answer; ``StoreUnreachable`` once that is over, or at once when Vault refuses."""
        started = time.monotonic()
        deadline = started + (self.retry_seconds if retry else 0.0)
        delay = 1.0
        attempts = 0
        while True:
            attempts += 1
            try:
                return await self._get(path, params)
            except _Refused as exc:
                raise StoreUnreachable(store, str(exc)) from None
            except httpx.HTTPStatusError as exc:
                why = str(exc)
            except httpx.TimeoutException:
                why = f"Vault at {self.address} did not answer within {REQUEST_TIMEOUT:.0f} s"
            except httpx.TransportError as exc:
                why = f"cannot connect to Vault at {self.address}: {exc or type(exc).__name__}"
            if time.monotonic() + delay > deadline:
                if attempts > 1:
                    why += f" ({attempts} attempts in {time.monotonic() - started:.0f} s)"
                raise StoreUnreachable(store, why)
            log.info("secret store %s: %s; trying again in %.0f s", store, why, delay)
            await asyncio.sleep(delay)
            delay *= 2

    # -- the store's questions --------------------------------------------------------------------

    async def names(self, store: str, settings: VaultSettings, *, retry: bool) -> set[str]:
        """The fields of the secret, cached for ``NAMES_TTL``; a path that holds no secret
        has none."""
        key = (settings.mount, settings.path)
        now = time.monotonic()
        cached = self._names.get(key)
        if cached and cached[0] > now:
            return set(cached[1])
        failure = self._failures.get(key)
        if not retry and failure and failure[0] > now:
            raise StoreUnreachable(store, failure[1])
        try:
            response = await self._attempts(store, f"/v1/{settings.mount}/subkeys/{settings.path}", {"depth": 1}, retry)
            if response.status_code == 404 and not _errors(response):
                await self._kv2_or_raise(store, settings)
                names: set[str] = set()
            elif response.status_code == 200:
                subkeys = ((response.json() or {}).get("data") or {}).get("subkeys")
                if not isinstance(subkeys, dict):
                    raise StoreUnreachable(store, f"Vault's answer for {settings.where()} has no subkeys")
                names = set(subkeys)
            else:
                raise StoreUnreachable(store, _read_problem(response, settings, "subkeys"))
        except StoreUnreachable as exc:
            self._failures[key] = (time.monotonic() + FAILURE_TTL, exc.why)
            raise
        self._failures.pop(key, None)
        self._names[key] = (time.monotonic() + NAMES_TTL, names)
        return set(names)

    async def values(self, store: str, settings: VaultSettings, names: Collection[str], *, retry: bool) -> dict[str, str]:
        """The values of those ``names`` the secret has, read now. A field that is not a
        string is an error, never converted."""
        if not names:
            return {}
        response = await self._attempts(store, f"/v1/{settings.mount}/data/{settings.path}", None, retry)
        if response.status_code == 404 and not _errors(response):
            await self._kv2_or_raise(store, settings)
            return {}
        if response.status_code != 200:
            raise StoreUnreachable(store, _read_problem(response, settings, "data"))
        data = ((response.json() or {}).get("data") or {}).get("data") or {}
        out: dict[str, str] = {}
        for name in names:
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, str):
                raise StoreError(f"secret store {store}: field {name} of {settings.where()} in Vault is "
                                 f"{_json_type(value)}, not a string")
            out[name] = value
        return out


    async def mount_problem(self, store: str, settings: VaultSettings, *, fresh: bool = False) -> str | None:
        """Why the store's mount is not a KV version 2 engine, or ``None`` when it is or when
        Vault cannot say (unreachable, or no access to the mount: the reads then explain it).
        An answer is kept for ``MOUNT_TTL`` seconds per mount; ``fresh`` asks again."""
        now = time.monotonic()
        cached = self._mounts.get(settings.mount)
        if cached and cached[0] > now and not fresh:
            return cached[1]
        try:
            response = await self._attempts(
                store, f"/v1/sys/internal/ui/mounts/{settings.mount}/{settings.path}", None, retry=False)
        except StoreUnreachable as exc:
            log.info("secret store %s: cannot tell what %s is: %s", store, settings.mount, exc.why)
            return None
        if response.status_code != 200:
            log.info("secret store %s: cannot tell what %s is (%s)", store, settings.mount,
                     _problem(response, "the mount lookup"))
            self._mounts[settings.mount] = (time.monotonic() + MOUNT_TTL, None)
            return None
        data = (response.json() or {}).get("data") or {}
        engine = data.get("type")
        version = str((data.get("options") or {}).get("version") or "1")
        problem = None
        if engine != "kv":
            problem = f"{settings.mount} is a {engine or 'non-KV'} mount; stores of kind vault read KV version 2"
        elif version != "2":
            problem = (f"{settings.mount} is a KV version {version} mount; stores of kind vault read KV version 2 "
                       f"(vault kv enable-versioning {settings.mount} upgrades it)")
        self._mounts[settings.mount] = (time.monotonic() + MOUNT_TTL, problem)
        return problem

    async def _kv2_or_raise(self, store: str, settings: VaultSettings) -> None:
        """A path read empty: unless the mount is not KV version 2, that is an empty store."""
        problem = await self.mount_problem(store, settings)
        if problem is not None:
            raise StoreUnreachable(store, problem)


class VaultBackend:
    def __init__(self, client: VaultClient, store: str, settings: VaultSettings, *, retry: bool):
        self.client = client
        self.store = store
        self.settings = settings
        self.retry = retry

    async def keys(self) -> set[str]:
        return await self.client.names(self.store, self.settings, retry=self.retry)

    async def values(self, names: Collection[str]) -> dict[str, str]:
        return await self.client.values(self.store, self.settings, names, retry=self.retry)


def _errors(response: httpx.Response) -> list[str]:
    try:
        errors = (response.json() or {}).get("errors")
    except ValueError:
        return [response.text[:200]] if response.text.strip() else []
    return [str(e).strip() for e in errors] if isinstance(errors, list) else []


def _problem(response: httpx.Response, what: str) -> str:
    detail = "; ".join(e.replace("\n", " ").replace("\t", "") for e in _errors(response)) or response.reason_phrase
    return f"Vault answered {response.status_code} to {what}: {detail}"


def _read_problem(response: httpx.Response, settings: VaultSettings, endpoint: str) -> str:
    if response.status_code == 403:
        return (f"Vault denied reading {settings.mount}/{endpoint}/{settings.path} (403); the role's policy must "
                f"allow read on {settings.mount}/data/{settings.path} and {settings.mount}/subkeys/{settings.path}")
    if response.status_code == 404:
        return (f"{_problem(response, f'{settings.mount}/{endpoint}/{settings.path}')} (is {settings.mount} a KV "
                f"version 2 mount, and Vault 1.10 or later?)")
    return _problem(response, f"{settings.mount}/{endpoint}/{settings.path}")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    return type(value).__name__
