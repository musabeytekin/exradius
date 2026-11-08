"""App authentication, installation tokens and pagination."""

from __future__ import annotations

import time

import jwt
import pytest

from exradius_github.github import GitHubApp, GitHubError
from tests.fakes import API, APP_ID, FakeGitHub


async def test_the_jwt_and_installation_tokens(keys):
    private, public = keys
    github = FakeGitHub(public)
    github.add_repo("acme/plane", 77)
    app = GitHubApp(API, APP_ID, private, transport=github.transport())

    claims = jwt.decode(app.jwt(), public, algorithms=["RS256"])
    assert claims["iss"] == APP_ID and claims["exp"] - claims["iat"] == 600

    assert (await app.app())["slug"] == "exradius-agent"
    assert await app.installation_for("acme/plane") == 77
    assert await app.installation_for("acme/missing") is None

    first = await app.token(77)
    assert await app.token(77) == first and github.token_requests == 1
    app._tokens[77] = (first, time.time() + 100)  # within five minutes of expiring
    assert await app.token(77) != first and github.token_requests == 2
    await app.aclose()


async def test_a_wrong_key_is_refused(keys):
    _, public = keys
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    app = GitHubApp(API, APP_ID, other, transport=FakeGitHub(public).transport())
    with pytest.raises(GitHubError) as caught:
        await app.app()
    assert caught.value.status == 401
    await app.aclose()


async def test_lists_follow_link_headers(keys):
    private, public = keys
    github = FakeGitHub(public)  # pages two at a time
    github.add_repo("acme/plane", 77)
    for n in range(5):
        github.add_label("acme/plane", f"l{n}")
    app = GitHubApp(API, APP_ID, private, transport=github.transport())
    labels = await app.repo("acme/plane", 77).labels()
    assert [l["name"] for l in labels] == ["l0", "l1", "l2", "l3", "l4"]
    await app.aclose()


async def test_a_token_only_reaches_its_installations_repos(keys):
    private, public = keys
    github = FakeGitHub(public)
    github.add_repo("acme/plane", 77)
    github.add_repo("other/repo", 88)
    app = GitHubApp(API, APP_ID, private, transport=github.transport())
    with pytest.raises(GitHubError) as caught:
        await app.repo("other/repo", 77).labels()
    assert caught.value.status == 404
    await app.aclose()
