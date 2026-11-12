"""The HashiCorp Vault backend's client (docs/config-repo.md §7.4).

A fake Vault (httpx.MockTransport) checks what ``VaultClient`` sends and answers as Vault
does; a fake clock replaces the module's ``time.monotonic`` and ``asyncio.sleep``, so token
leases, the retry window and the remembered failures run without waiting.

The live tests run the same client against a dev-mode Vault and skip unless its address and
root token are set:

    docker run -d --rm --name exradius-test-vault -p 127.0.0.1:18200:8200 \\
      --add-host=host.docker.internal:host-gateway \\
      -e VAULT_DEV_ROOT_TOKEN_ID=exradius-test-root \\
      hashicorp/vault:2.2.2@sha256:dfa48c2fa2b0099ccb47681ec5c4804bf5aec9d6879ecfee07ad4b92a5427ad6
    EXRADIUS_TEST_VAULT_ADDR=http://127.0.0.1:18200 \\
    EXRADIUS_TEST_VAULT_TOKEN=exradius-test-root uv run pytest tests/test_vault_backend.py
    docker stop exradius-test-vault

The root token only sets Vault up (mounts, a policy, the secrets); the client never sees it.
A dev Vault outside a cluster has no Kubernetes API to review a ServiceAccount token, so the
sign-in test enables the Kubernetes auth method against a stand-in TokenReview endpoint served
by the test itself (``EXRADIUS_TEST_VAULT_CALLBACK_HOST``, default ``host.docker.internal``,
is how the Vault container reaches it): Vault then really answers the client's
``auth/<mount>/login`` with a client token, and the reads use it. Everything else about the
sign-in (the role, the audience, the token Vault issues) is Vault's own.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from exradius.models import SecretStore
from exradius.resolve import StoreError
from exradius.secret_backends import SecretBackends, StoreUnreachable
from exradius.secret_backends import vault as vault_module
from exradius.secret_backends.kinds import VaultSettings
from exradius.secret_backends.vault import VaultClient

ADDRESS = "http://vault.test:8200"
TEAM_A = VaultSettings(mount="secret", path="teams/a")


# -- fakes --------------------------------------------------------------------------------


class Clock:
    """``time.monotonic`` and ``asyncio.sleep`` for the vault module: sleeping moves time."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeVault:
    """Vault's KV version 2, the Kubernetes auth method and the mount lookup, as far as the
    client uses them. ``fail`` holds what the next requests answer instead (a response, or an
    exception the transport raises), consumed in order."""

    def __init__(self):
        self.lease = 3600
        self.secrets: dict[tuple[str, str], dict] = {("secret", "teams/a"): {"API_TOKEN": "t0k", "DB_PASSWORD": "pw"}}
        self.mounts: dict[str, dict] = {"secret": {"type": "kv", "options": {"version": "2"}}}
        self.denied: set[str] = set()
        """Paths the role's policy does not allow: 403 whatever the token."""
        self.valid: set[str] = set()
        self.issued = 0
        self.logins: list[httpx.Request] = []
        self.reads: list[httpx.Request] = []
        self.fail: list[httpx.Response | Exception] = []
        self.fail_logins: list[httpx.Response | Exception] = []

    def revoke(self) -> None:
        self.valid.clear()

    def login_bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.logins]

    def read_paths(self) -> list[str]:
        return [r.url.path for r in self.reads]

    def tokens_used(self) -> list[str | None]:
        return [r.headers.get("X-Vault-Token") for r in self.reads]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.startswith("/v1/auth/") and path.endswith("/login"):
            self.logins.append(request)
            if self.fail_logins:
                return _answer(self.fail_logins.pop(0))
            self.issued += 1
            token = f"hvs.{self.issued}"
            self.valid.add(token)
            return httpx.Response(200, json={"auth": {"client_token": token, "lease_duration": self.lease,
                                                      "renewable": True, "policies": ["exradius"]}})
        assert request.method == "GET", request
        self.reads.append(request)
        if self.fail:
            return _answer(self.fail.pop(0))
        if request.headers.get("X-Vault-Token") not in self.valid or path in self.denied:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        prefix = "/v1/sys/internal/ui/mounts/"
        if path.startswith(prefix):
            mount = path.removeprefix(prefix).split("/", 1)[0]
            if mount not in self.mounts:
                return httpx.Response(403, json={"errors": ["preflight capability check returned 403"]})
            return httpx.Response(200, json={"data": {"path": f"{mount}/", **self.mounts[mount]}})
        mount, endpoint, secret_path = path.removeprefix("/v1/").split("/", 2)
        engine = self.mounts.get(mount)
        is_kv2 = engine is not None and engine["type"] == "kv" and (engine["options"] or {}).get("version") == "2"
        secret = self.secrets.get((mount, secret_path)) if is_kv2 else None
        if secret is None:
            # An empty KV version 2 path, and any path of a KV version 1 mount, look alike.
            return httpx.Response(404, json={"errors": []})
        if endpoint == "subkeys":
            assert request.url.params.get("depth") == "1"
            return httpx.Response(200, json={"data": {"subkeys": {k: None for k in secret}, "metadata": {}}})
        if endpoint == "data":
            return httpx.Response(200, json={"data": {"data": secret, "metadata": {"version": 1}}})
        return httpx.Response(404, json={"errors": ["1 error occurred:\n\t* unsupported path\n\n"]})


def _answer(item: httpx.Response | Exception) -> httpx.Response:
    if isinstance(item, Exception):
        raise item
    return item


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(vault_module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(vault_module, "asyncio", types.SimpleNamespace(
        Lock=asyncio.Lock, to_thread=asyncio.to_thread, sleep=clock.sleep))
    return clock


@pytest.fixture
def fake() -> FakeVault:
    return FakeVault()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("jwt-1\n")
    return path


@pytest.fixture
async def client(clock, fake, token_file):
    client = VaultClient(ADDRESS, role="exradius", token_file=token_file, transport=httpx.MockTransport(fake.handler))
    yield client
    await client.aclose()


# -- signing in ---------------------------------------------------------------------------


async def test_signs_in_with_the_service_account_token_and_reads_with_the_client_token(clock, fake, token_file):
    client = VaultClient(ADDRESS + "/", role="exradius-prod", auth_mount="/k8s-east/", namespace="team-a",
                         token_file=token_file, transport=httpx.MockTransport(fake.handler))
    try:
        assert await client.names("team-a", TEAM_A, retry=True) == {"API_TOKEN", "DB_PASSWORD"}
        assert await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True) == {"API_TOKEN": "t0k"}
    finally:
        await client.aclose()

    assert len(fake.logins) == 1
    login = fake.logins[0]
    assert str(login.url) == "http://vault.test:8200/v1/auth/k8s-east/login"
    assert fake.login_bodies() == [{"role": "exradius-prod", "jwt": "jwt-1"}]
    assert login.headers["X-Vault-Namespace"] == "team-a"
    assert "X-Vault-Token" not in login.headers
    assert fake.read_paths() == ["/v1/secret/subkeys/teams/a", "/v1/secret/data/teams/a"]
    assert fake.reads[0].url.params["depth"] == "1"
    assert fake.tokens_used() == ["hvs.1", "hvs.1"]
    assert all(r.headers["X-Vault-Namespace"] == "team-a" for r in fake.reads)


async def test_no_namespace_header_without_a_namespace(client, fake):
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert "X-Vault-Namespace" not in fake.logins[0].headers
    assert "X-Vault-Namespace" not in fake.reads[0].headers
    assert fake.logins[0].url.path == "/v1/auth/kubernetes/login"


async def test_the_vault_token_is_kept_until_a_minute_before_its_lease_ends(client, clock, fake):
    fake.lease = 3600
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    clock.advance(3539)
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert len(fake.logins) == 1

    clock.advance(1)  # 3540 s: a minute before the lease ends
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert len(fake.logins) == 2
    assert fake.tokens_used() == ["hvs.1", "hvs.1", "hvs.2"]


async def test_a_short_lease_is_renewed_halfway(client, clock, fake):
    fake.lease = 40  # less than two minutes: the margin is half the lease
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    clock.advance(19)
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    clock.advance(1)
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert fake.tokens_used() == ["hvs.1", "hvs.1", "hvs.2"]


async def test_concurrent_reads_share_one_sign_in(client, fake):
    results = await asyncio.gather(*(client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False) for _ in range(5)))
    assert results == [{"API_TOKEN": "t0k"}] * 5
    assert len(fake.logins) == 1


async def test_the_token_file_is_read_again_at_every_sign_in(client, clock, fake, token_file):
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    token_file.write_text("jwt-2\n")  # the kubelet rotated the projected token
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert len(fake.logins) == 1  # the Vault token is still good: no sign-in, no read of the file

    clock.advance(3600)
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    token_file.write_text("jwt-3")
    fake.revoke()  # a 403 signs in again, with the file as it is now
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert [b["jwt"] for b in fake.login_bodies()] == ["jwt-1", "jwt-2", "jwt-3"]


async def test_an_unreadable_token_file_is_a_refused_sign_in(clock, fake, tmp_path):
    client = VaultClient(ADDRESS, role="exradius", token_file=tmp_path / "absent",
                         transport=httpx.MockTransport(fake.handler))
    try:
        with pytest.raises(StoreUnreachable) as caught:
            await client.names("team-a", TEAM_A, retry=True)
    finally:
        await client.aclose()
    assert caught.value.why.startswith(f"cannot read the pod's ServiceAccount token ({tmp_path / 'absent'}): ")
    assert fake.logins == [] and fake.reads == []
    assert clock.sleeps == []


async def test_a_refused_sign_in_is_not_tried_again(client, clock, fake):
    fake.fail_logins = [httpx.Response(403, json={"errors": ["invalid audience (aud) claim: audience claim does not match any expected audience"]})] * 5
    with pytest.raises(StoreUnreachable) as caught:
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert str(caught.value) == (
        "secret store team-a is unreachable: Vault refused the sign-in as role 'exradius' at auth/kubernetes "
        "(Vault answered 403 to sign-in: invalid audience (aud) claim: audience claim does not match any "
        "expected audience)")
    assert len(fake.logins) == 1
    assert fake.reads == []
    assert clock.sleeps == []


async def test_a_sign_in_answer_without_a_client_token_is_refused(client, fake):
    fake.fail_logins = [httpx.Response(200, json={"auth": None, "warnings": ["no auth"]})]
    with pytest.raises(StoreUnreachable, match="sign-in answer has no client_token"):
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert len(fake.logins) == 1


async def test_a_sealed_vault_at_sign_in_is_tried_again(client, clock, fake):
    fake.fail_logins = [httpx.Response(503, json={"errors": ["Vault is sealed"]})]
    assert await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True) == {"API_TOKEN": "t0k"}
    assert len(fake.logins) == 2
    assert clock.sleeps == [1.0]


# -- a 403 on a read ----------------------------------------------------------------------


async def test_a_403_on_a_read_signs_in_once_more_and_asks_again(client, fake):
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    fake.revoke()  # the token was revoked before its lease ended
    assert await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False) == {"API_TOKEN": "t0k"}
    assert len(fake.logins) == 2
    assert fake.tokens_used() == ["hvs.1", "hvs.1", "hvs.2"]


async def test_a_second_403_is_a_refused_read_not_a_loop(client, clock, fake):
    fake.denied = {"/v1/secret/data/teams/a"}
    with pytest.raises(StoreUnreachable) as caught:
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert str(caught.value) == (
        "secret store team-a is unreachable: Vault denied reading secret/data/teams/a (403); the role's policy "
        "must allow read on secret/data/teams/a and secret/subkeys/teams/a")
    assert len(fake.logins) == 2  # the first sign-in, and exactly one more after the 403
    assert fake.read_paths() == ["/v1/secret/data/teams/a"] * 2
    assert fake.tokens_used() == ["hvs.1", "hvs.2"]
    assert clock.sleeps == []  # a refused read is not retried, even by a caller that retries


async def test_a_403_on_listing_names_is_a_refused_read_too(client, clock, fake):
    fake.denied = {"/v1/secret/subkeys/teams/a"}
    with pytest.raises(StoreUnreachable, match=r"Vault denied reading secret/subkeys/teams/a \(403\)"):
        await client.names("team-a", TEAM_A, retry=True)
    assert len(fake.logins) == 2 and len(fake.reads) == 2
    assert clock.sleeps == []


async def test_a_403_whose_new_sign_in_is_refused_says_so(client, fake):
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    fake.revoke()
    fake.fail_logins = [httpx.Response(400, json={"errors": ["invalid role name \"exradius\""]})]
    with pytest.raises(StoreUnreachable, match=r"Vault refused the sign-in as role 'exradius' at auth/kubernetes"):
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert len(fake.logins) == 2


# -- an unreachable Vault -----------------------------------------------------------------


async def test_an_unreachable_vault_is_tried_again_with_a_growing_pause_for_about_ten_seconds(client, clock, fake):
    fake.fail_logins = [httpx.ConnectError("[Errno 111] Connection refused")] * 20
    started = clock.now
    with pytest.raises(StoreUnreachable) as caught:
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert len(fake.logins) == 4
    assert clock.now - started <= 10
    assert str(caught.value) == (
        "secret store team-a is unreachable: cannot connect to Vault at http://vault.test:8200: "
        "[Errno 111] Connection refused (4 attempts in 7 s)")
    assert caught.value.store == "team-a"


async def test_a_sealed_vault_on_a_read_is_tried_again_and_its_error_is_the_reason(client, clock, fake):
    fake.fail = [httpx.Response(503, json={"errors": ["Vault is sealed"]})] * 20
    with pytest.raises(StoreUnreachable) as caught:
        await client.names("team-a", TEAM_A, retry=True)
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert len(fake.reads) == 4
    assert caught.value.why == ("Vault answered 503 to /v1/secret/subkeys/teams/a: Vault is sealed "
                                "(4 attempts in 7 s)")


async def test_a_vault_that_comes_back_within_the_window_answers(client, clock, fake):
    fake.fail = [httpx.ConnectError("refused"), httpx.Response(429, json={"errors": ["rate limited"]}),
                 httpx.ReadTimeout("slow")]
    assert await client.values("team-a", TEAM_A, ["DB_PASSWORD"], retry=True) == {"DB_PASSWORD": "pw"}
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert len(fake.reads) == 4


async def test_a_timeout_says_vault_did_not_answer_in_time(client, clock, fake):
    fake.fail = [httpx.ReadTimeout("timed out")]
    with pytest.raises(StoreUnreachable) as caught:
        await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=False)
    assert caught.value.why == "Vault at http://vault.test:8200 did not answer within 5 s"
    assert clock.sleeps == []


async def test_without_retry_an_unreachable_vault_fails_at_once(client, clock, fake):
    fake.fail_logins = [httpx.ConnectError("refused")]
    with pytest.raises(StoreUnreachable) as caught:
        await client.names("team-a", TEAM_A, retry=False)
    assert caught.value.why == "cannot connect to Vault at http://vault.test:8200: refused"
    assert clock.sleeps == []
    assert len(fake.logins) == 1


async def test_the_retry_window_is_the_clients_own(clock, fake, token_file):
    client = VaultClient(ADDRESS, role="exradius", token_file=token_file, retry_seconds=3.0,
                         transport=httpx.MockTransport(fake.handler))
    fake.fail_logins = [httpx.ConnectError("refused")] * 20
    try:
        with pytest.raises(StoreUnreachable, match=r"\(3 attempts in 3 s\)$"):
            await client.names("team-a", TEAM_A, retry=True)
    finally:
        await client.aclose()
    assert clock.sleeps == [1.0, 2.0]


async def test_intake_reads_the_reason_through_the_stores_backend(clock, fake, token_file):
    """``SecretBackends.of`` (what intake, ``preparing`` and the panel ask) retries by default
    and its error is ``secret store S is unreachable: <why>``."""
    client = VaultClient(ADDRESS, role="exradius", token_file=token_file, transport=httpx.MockTransport(fake.handler))
    backends = SecretBackends(vault=client)
    store = SecretStore(name="payments", kind="vault", settings={"mount": "secret", "path": "teams/a"})
    fake.fail_logins = [httpx.ConnectError("refused")] * 20
    try:
        with pytest.raises(StoreUnreachable) as caught:
            await backends.of(None, store).keys()
        assert str(caught.value) == ("secret store payments is unreachable: cannot connect to Vault at "
                                     "http://vault.test:8200: refused (4 attempts in 7 s)")
        assert isinstance(caught.value, StoreError)

        clock.sleeps.clear()
        with pytest.raises(StoreUnreachable):
            await backends.of(None, store, retry=False).values(["API_TOKEN"])
        assert clock.sleeps == []
    finally:
        await backends.aclose()


def test_a_vault_store_without_a_configured_vault_or_with_bad_settings_is_unreachable():
    store = SecretStore(name="payments", kind="vault", settings={"mount": "secret", "path": "teams/a"})
    with pytest.raises(StoreUnreachable, match=r"^secret store payments is unreachable: Vault is not configured"):
        SecretBackends().of(None, store)

    client = VaultClient(ADDRESS, role="exradius", transport=httpx.MockTransport(lambda r: pytest.fail("no request")))
    bad = SecretStore(name="payments", kind="vault", settings={"mount": "secret", "path": "../etc", "tpyo": 1})
    with pytest.raises(StoreUnreachable, match=r"its settings are not valid \(settings\.path: .*settings\.tpyo: unknown key\)"):
        SecretBackends(vault=client).of(None, bad)


# -- remembered failures ------------------------------------------------------------------


async def test_a_failed_listing_is_remembered_for_five_seconds_by_views_that_ask_once(client, clock, fake):
    fake.fail_logins = [httpx.ConnectError("refused")]
    with pytest.raises(StoreUnreachable) as first:
        await client.names("team-a", TEAM_A, retry=False)
    requests = len(fake.logins) + len(fake.reads)

    clock.advance(4.9)
    with pytest.raises(StoreUnreachable) as again:
        await client.names("team-b", TEAM_A, retry=False)  # another store at the same path
    assert again.value.why == first.value.why
    assert str(again.value).startswith("secret store team-b is unreachable: ")
    assert len(fake.logins) + len(fake.reads) == requests  # Vault was not asked again

    clock.advance(0.2)  # the failure is forgotten: Vault is asked, and answers now
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD"}
    assert len(fake.logins) + len(fake.reads) > requests


async def test_a_caller_that_retries_does_not_wait_on_a_remembered_failure(client, clock, fake):
    fake.fail_logins = [httpx.ConnectError("refused")]
    with pytest.raises(StoreUnreachable):
        await client.names("team-a", TEAM_A, retry=False)
    assert await client.names("team-a", TEAM_A, retry=True) == {"API_TOKEN", "DB_PASSWORD"}
    # ... and its success clears the failure for the views.
    clock.advance(31)
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD"}


async def test_a_refused_read_is_remembered_too(client, clock, fake):
    fake.denied = {"/v1/secret/subkeys/teams/a"}
    with pytest.raises(StoreUnreachable, match="denied"):
        await client.names("team-a", TEAM_A, retry=False)
    fake.denied = set()
    reads = len(fake.reads)
    with pytest.raises(StoreUnreachable, match="denied"):
        await client.names("team-a", TEAM_A, retry=False)
    assert len(fake.reads) == reads
    clock.advance(5.1)
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD"}


async def test_key_names_are_kept_thirty_seconds_and_values_never(client, clock, fake):
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD"}
    fake.secrets[("secret", "teams/a")]["NEW_KEY"] = "n"
    clock.advance(29)
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD"}
    assert await client.values("team-a", TEAM_A, ["NEW_KEY"], retry=False) == {"NEW_KEY": "n"}
    clock.advance(2)
    assert await client.names("team-a", TEAM_A, retry=False) == {"API_TOKEN", "DB_PASSWORD", "NEW_KEY"}
    assert fake.read_paths().count("/v1/secret/subkeys/teams/a") == 2


# -- an empty path, and errors ------------------------------------------------------------


async def test_a_path_with_no_secret_is_a_store_with_no_keys(client, clock, fake):
    empty = VaultSettings(mount="secret", path="teams/new")
    assert await client.names("team-new", empty, retry=True) == set()
    assert await client.values("team-new", empty, ["API_TOKEN"], retry=True) == {}
    assert clock.sleeps == []
    # The mount was looked up once (the store could have been a KV version 1 mount) and kept.
    assert fake.read_paths() == ["/v1/secret/subkeys/teams/new", "/v1/sys/internal/ui/mounts/secret/teams/new",
                                 "/v1/secret/data/teams/new"]


async def test_a_read_that_finds_the_secret_never_looks_the_mount_up(client, fake):
    await client.names("team-a", TEAM_A, retry=True)
    await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True)
    assert not any("/sys/" in p for p in fake.read_paths())


async def test_asking_for_no_values_asks_vault_nothing(client, fake):
    assert await client.values("team-a", TEAM_A, [], retry=True) == {}
    assert fake.logins == [] and fake.reads == []


async def test_a_name_the_secret_lacks_is_left_out(client):
    assert await client.values("team-a", TEAM_A, ["API_TOKEN", "NOT_THERE"], retry=True) == {"API_TOKEN": "t0k"}


async def test_a_404_that_names_an_error_is_an_error_not_an_empty_store(client, clock, fake):
    fake.fail = [httpx.Response(404, json={"errors": ["1 error occurred:\n\t* unsupported path\n\n"]})]
    with pytest.raises(StoreUnreachable) as caught:
        await client.names("team-a", TEAM_A, retry=True)
    assert caught.value.why == ("Vault answered 404 to secret/subkeys/teams/a: 1 error occurred: * unsupported path "
                                "(is secret a KV version 2 mount, and Vault 1.10 or later?)")
    assert clock.sleeps == []
    assert not any("/sys/" in p for p in fake.read_paths())


async def test_a_subkeys_answer_without_subkeys_is_an_error(client, fake):
    fake.fail = [httpx.Response(200, json={"data": {"metadata": {}}})]
    with pytest.raises(StoreUnreachable, match=r"Vault's answer for secret/teams/a has no subkeys$"):
        await client.names("team-a", TEAM_A, retry=True)


# -- KV version 1 and other engines -------------------------------------------------------


async def test_a_kv_version_1_mount_is_told_apart_from_an_empty_store(client, clock, fake):
    fake.mounts["kv1"] = {"type": "kv", "options": {"version": "1"}}
    on_kv1 = VaultSettings(mount="kv1", path="teams/a")
    expected = ("secret store team-a is unreachable: kv1 is a KV version 1 mount; stores of kind vault read KV "
                "version 2 (vault kv enable-versioning kv1 upgrades it)")
    with pytest.raises(StoreUnreachable) as names:
        await client.names("team-a", on_kv1, retry=True)
    assert str(names.value) == expected
    with pytest.raises(StoreUnreachable) as values:
        await client.values("team-a", on_kv1, ["API_TOKEN"], retry=True)
    assert str(values.value) == expected
    # The mount's kind is kept a minute: one lookup for both reads.
    assert fake.read_paths().count("/v1/sys/internal/ui/mounts/kv1/teams/a") == 1
    assert clock.sleeps == []


async def test_a_kv_mount_without_a_version_is_version_1(client, fake):
    fake.mounts["old"] = {"type": "kv", "options": None}
    with pytest.raises(StoreUnreachable, match="old is a KV version 1 mount"):
        await client.names("team-a", VaultSettings(mount="old", path="x"), retry=False)


async def test_another_engine_is_told_apart_too(client, fake):
    fake.mounts["transit"] = {"type": "transit", "options": None}
    with pytest.raises(StoreUnreachable) as caught:
        await client.values("team-a", VaultSettings(mount="transit", path="keys/a"), ["K"], retry=True)
    assert caught.value.why == "transit is a transit mount; stores of kind vault read KV version 2"


async def test_a_mount_the_role_cannot_see_reads_as_an_empty_store(client, fake):
    # Neither the secret nor the mount is visible: Vault cannot say it is not KV version 2.
    hidden = VaultSettings(mount="hidden", path="a")
    assert await client.names("team-a", hidden, retry=True) == set()


async def test_saving_a_store_asks_about_the_mount_afresh(client, fake):
    backends = SecretBackends(vault=client)
    fake.mounts["kv1"] = {"type": "kv", "options": {"version": "1"}}
    settings = {"mount": "kv1", "path": "teams/a"}
    problem = await backends.settings_problem("team-a", "vault", settings)
    assert problem == ("settings.mount: kv1 is a KV version 1 mount; stores of kind vault read KV version 2 "
                       "(vault kv enable-versioning kv1 upgrades it)")
    fake.mounts["kv1"] = {"type": "kv", "options": {"version": "2"}}  # vault kv enable-versioning kv1
    assert await backends.settings_problem("team-a", "vault", settings) is None
    assert fake.read_paths().count("/v1/sys/internal/ui/mounts/kv1/teams/a") == 2

    fake.fail = [httpx.ConnectError("refused")]  # an unreachable Vault does not stop a save
    assert await backends.settings_problem("team-a", "vault", settings) is None
    assert await backends.settings_problem("team-a", "internal", {}) is None


# -- fields that are not text -------------------------------------------------------------


@pytest.mark.parametrize(("value", "described"), [
    (42, "a number"),
    (1.5, "a number"),
    (True, "a boolean"),
    ({"user": "u"}, "an object"),
    (["a", "b"], "a list"),
    (None, "null"),
])
async def test_a_field_that_is_not_text_is_an_error_never_converted(client, fake, value, described):
    fake.secrets[("secret", "teams/a")]["PORT"] = value
    assert "PORT" in await client.names("team-a", TEAM_A, retry=True)
    with pytest.raises(StoreError) as caught:
        await client.values("team-a", TEAM_A, ["API_TOKEN", "PORT"], retry=True)
    assert not isinstance(caught.value, StoreUnreachable)
    assert str(caught.value) == f"secret store team-a: field PORT of secret/teams/a in Vault is {described}, not a string"


async def test_a_field_that_is_not_text_does_not_matter_unless_it_is_asked_for(client, fake):
    fake.secrets[("secret", "teams/a")]["PORT"] = 5432
    assert await client.values("team-a", TEAM_A, ["API_TOKEN"], retry=True) == {"API_TOKEN": "t0k"}


async def test_an_empty_string_is_a_value(client, fake):
    fake.secrets[("secret", "teams/a")]["EMPTY"] = ""
    assert await client.values("team-a", TEAM_A, ["EMPTY"], retry=True) == {"EMPTY": ""}


# -- live: a dev-mode Vault ---------------------------------------------------------------

LIVE_ADDR = os.environ.get("EXRADIUS_TEST_VAULT_ADDR")
LIVE_TOKEN = os.environ.get("EXRADIUS_TEST_VAULT_TOKEN")
CALLBACK_HOST = os.environ.get("EXRADIUS_TEST_VAULT_CALLBACK_HOST", "host.docker.internal")
live = pytest.mark.skipif(not (LIVE_ADDR and LIVE_TOKEN),
                          reason="EXRADIUS_TEST_VAULT_ADDR and EXRADIUS_TEST_VAULT_TOKEN are not set")


class _TokenReviews(BaseHTTPRequestHandler):
    """The Kubernetes API's TokenReview, as far as Vault's Kubernetes auth method asks it: a
    token the test issued is the ServiceAccount exradius/exradius-worker."""

    tokens: dict[str, list[str]] = {}
    reviews: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).reviews.append(body)
        audiences = type(self).tokens.get(body["spec"]["token"])
        status = {"authenticated": False, "error": "unknown token"}
        if audiences is not None and set(body["spec"].get("audiences") or audiences) & set(audiences):
            status = {
                "authenticated": True,
                "user": {"username": "system:serviceaccount:exradius:exradius-worker",
                         "uid": "6f1d2a2e-0000-4000-8000-000000000001", "groups": ["system:serviceaccounts"]},
                "audiences": list(body["spec"].get("audiences") or audiences),
            }
        payload = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                              "status": status}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _service_account_jwt(audience: str, serial: int) -> str:
    """A token shaped as a projected ServiceAccount token. Vault takes its identity from the
    TokenReview; the signature is never checked against a key (none is configured)."""

    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"iss": "https://kubernetes.default.svc.cluster.local", "aud": [audience],
              "sub": "system:serviceaccount:exradius:exradius-worker", "exp": 4102444800, "iat": 1700000000 + serial,
              "kubernetes.io": {"namespace": "exradius",
                                "serviceaccount": {"name": "exradius-worker",
                                                   "uid": "6f1d2a2e-0000-4000-8000-000000000001"}}}
    return f"{part({'alg': 'RS256', 'kid': 'test'})}.{part(claims)}.{part({'sig': serial})}"


@pytest.fixture
async def live_vault():
    """A KV version 2 mount with a secret, a KV version 1 mount, a policy as the docs ask for
    (read on ``data/`` and ``subkeys/`` under each store's path, on both mounts), and teardown: all under a fresh suffix, so runs do not see each other."""
    suffix = uuid.uuid4().hex[:8]
    kv2, kv1 = f"exr-{suffix}", f"exr1-{suffix}"
    root = httpx.AsyncClient(base_url=LIVE_ADDR, headers={"X-Vault-Token": LIVE_TOKEN}, timeout=10)

    async def call(method: str, path: str, body: dict | None = None) -> httpx.Response:
        response = await root.request(method, path, json=body)
        assert response.status_code < 300, (path, response.status_code, response.text)
        return response

    await call("POST", f"/v1/sys/mounts/{kv2}", {"type": "kv", "options": {"version": "2"}})
    await call("POST", f"/v1/sys/mounts/{kv1}", {"type": "kv", "options": {"version": "1"}})
    await call("POST", f"/v1/{kv2}/data/teams/a", {"data": {"API_TOKEN": "t0k", "DB_PASSWORD": "pw", "PORT": 5432}})
    await call("POST", f"/v1/{kv1}/teams/a", {"API_TOKEN": "t0k"})
    policy = (f'path "{kv2}/data/teams/*" {{ capabilities = ["read"] }}\n'
              f'path "{kv2}/subkeys/teams/*" {{ capabilities = ["read"] }}\n'
              f'path "{kv1}/data/teams/*" {{ capabilities = ["read"] }}\n'
              f'path "{kv1}/subkeys/teams/*" {{ capabilities = ["read"] }}\n')
    await call("PUT", f"/v1/sys/policies/acl/{kv2}", {"policy": policy})
    try:
        yield types.SimpleNamespace(kv2=kv2, kv1=kv1, policy=kv2, call=call, root=root)
    finally:
        for mount in (f"sys/mounts/{kv2}", f"sys/mounts/{kv1}", f"sys/policies/acl/{kv2}", f"sys/auth/{kv2}"):
            await root.delete(f"/v1/{mount}")
        await root.aclose()


async def _client_token(live_vault) -> tuple[str, float]:
    response = await live_vault.call("POST", "/v1/auth/token/create",
                                     {"policies": [live_vault.policy], "ttl": "10m", "no_default_policy": True})
    auth = response.json()["auth"]
    return auth["client_token"], float(auth["lease_duration"])


@live
async def test_live_reads_names_values_an_empty_path_and_tells_kv1_apart(live_vault, tmp_path):
    """The reads against a real Vault. The client's token is a policy-bound token made with
    the root token and handed to the client where its own sign-in puts it (``_token`` and
    ``_token_expires``): this test is about the reads, the next one about the sign-in."""
    client = VaultClient(LIVE_ADDR, role="none", token_file=tmp_path / "absent")
    token, lease = await _client_token(live_vault)
    client._token, client._token_expires = token, vault_module.time.monotonic() + lease
    store = VaultSettings(mount=live_vault.kv2, path="teams/a")
    try:
        assert await client.names("live", store, retry=False) == {"API_TOKEN", "DB_PASSWORD", "PORT"}
        assert await client.values("live", store, ["API_TOKEN", "DB_PASSWORD", "MISSING"], retry=False) == {
            "API_TOKEN": "t0k", "DB_PASSWORD": "pw"}
        with pytest.raises(StoreError, match=f"field PORT of {live_vault.kv2}/teams/a in Vault is a number, not a string"):
            await client.values("live", store, ["PORT"], retry=False)

        empty = VaultSettings(mount=live_vault.kv2, path="teams/none")
        assert await client.names("live", empty, retry=False) == set()
        assert await client.values("live", empty, ["API_TOKEN"], retry=False) == {}

        on_kv1 = VaultSettings(mount=live_vault.kv1, path="teams/a")
        with pytest.raises(StoreUnreachable) as caught:
            await client.names("live", on_kv1, retry=False)
        assert caught.value.why == (f"{live_vault.kv1} is a KV version 1 mount; stores of kind vault read KV version 2 "
                                    f"(vault kv enable-versioning {live_vault.kv1} upgrades it)")
        assert await client.mount_problem("live", store, fresh=True) is None

        # A path outside the policy: 403, one new sign-in (which cannot work here: no token file).
        outside = VaultSettings(mount=live_vault.kv2, path="other/a")
        with pytest.raises(StoreUnreachable, match="cannot read the pod's ServiceAccount token"):
            await client.values("live", outside, ["API_TOKEN"], retry=False)
    finally:
        await client.aclose()


@live
async def test_live_kubernetes_sign_in_with_a_rotated_token_and_a_403_after_revocation(live_vault, tmp_path):
    server = ThreadingHTTPServer(("0.0.0.0", 0), _TokenReviews)
    _TokenReviews.tokens, _TokenReviews.reviews = {}, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    auth_mount = live_vault.kv2
    try:
        await live_vault.call("POST", f"/v1/sys/auth/{auth_mount}", {"type": "kubernetes"})
        await live_vault.call("POST", f"/v1/auth/{auth_mount}/config", {
            "kubernetes_host": f"http://{CALLBACK_HOST}:{server.server_address[1]}",
            "disable_local_ca_jwt": True,
        })
        await live_vault.call("POST", f"/v1/auth/{auth_mount}/role/exradius", {
            "bound_service_account_names": ["exradius-api", "exradius-worker"],
            "bound_service_account_namespaces": ["exradius"],
            "audience": "vault", "token_policies": [live_vault.policy], "token_ttl": "1h",
        })
        first, rotated = _service_account_jwt("vault", 1), _service_account_jwt("vault", 2)
        _TokenReviews.tokens = {first: ["vault"], rotated: ["vault"]}
        token_file = tmp_path / "token"
        token_file.write_text(first)

        client = VaultClient(LIVE_ADDR, role="exradius", auth_mount=auth_mount, token_file=token_file)
        store = VaultSettings(mount=live_vault.kv2, path="teams/a")
        try:
            assert await client.values("live", store, ["API_TOKEN"], retry=False) == {"API_TOKEN": "t0k"}
            assert [r["spec"]["token"] for r in _TokenReviews.reviews] == [first]
            assert _TokenReviews.reviews[0]["spec"].get("audiences") == ["vault"]
            issued = client._token

            # The kubelet rotates the file; Vault revokes the client token: the 403 signs in
            # once more, with the rotated token, and the read goes through.
            token_file.write_text(rotated)
            await live_vault.call("POST", "/v1/auth/token/revoke", {"token": issued})
            assert await client.names("live", store, retry=False) == {"API_TOKEN", "DB_PASSWORD", "PORT"}
            assert [r["spec"]["token"] for r in _TokenReviews.reviews] == [first, rotated]
            assert client._token != issued

            # A token for another audience is refused, and not tried again.
            token_file.write_text(_service_account_jwt("kubernetes", 3))
            await live_vault.call("POST", "/v1/auth/token/revoke", {"token": client._token})
            with pytest.raises(StoreUnreachable, match=r"Vault refused the sign-in as role 'exradius'"):
                await client.values("live", store, ["API_TOKEN"], retry=True)
        finally:
            await client.aclose()
    finally:
        server.shutdown()
        server.server_close()
