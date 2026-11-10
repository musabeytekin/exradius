"""Settings and startup: every problem is listed at once."""

from __future__ import annotations

import copy

import pytest

from exradius_github.exradius import DirectoryClient
from exradius_github.github import GitHubApp
from exradius_github.main import StartupError, start
from exradius_github.settings import Settings, SettingsError
from tests.conftest import DIRECTORY, make_settings
from tests.fakes import API, APP_ID, BOT, EXRADIUS, FakeDirectory, FakeGitHub, FakePublisher


class FakeBroker(FakePublisher):
    def __init__(self, fail: bool = False):
        super().__init__()
        self.fail_connect = fail
        self.handler = None

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("connection refused")

    async def consume(self, handler) -> None:
        self.handler = handler

    async def close(self) -> None:
        pass


def test_settings(tmp_path, keys):
    key_file = tmp_path / "app.pem"
    key_file.write_text(keys[0])
    env = {
        "GITHUB_APP_ID": "1234", "GITHUB_PRIVATE_KEY_FILE": str(key_file), "GITHUB_WEBHOOK_SECRET": "s",
        "EXRADIUS_BROKER_URL": "amqp://rabbitmq/", "EXRADIUS_API_URL": "https://exradius.acme.test/", "EXRADIUS_INTEGRATION_TOKEN": "exr_x",
    }
    settings = Settings.from_env(env)
    assert settings.github_host == "github.com" and settings.exradius_api_url == "https://exradius.acme.test"
    assert Settings.from_env(env | {"GITHUB_API_URL": "https://ghe.acme.test/api/v3"}).github_host == "ghe.acme.test"

    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"GITHUB_APP_ID": "my-app", "GITHUB_PRIVATE_KEY": "not a key", "PORT": "x"})
    assert caught.value.problems == [
        "GITHUB_APP_ID must be the App's numeric id",
        "the GitHub App private key is not a PEM private key",
        "PORT must be a whole number, not 'x'",
        "GITHUB_WEBHOOK_SECRET is not set",
        "EXRADIUS_BROKER_URL is not set",
        "EXRADIUS_API_URL is not set",
        "EXRADIUS_INTEGRATION_TOKEN is not set",
    ]


async def test_every_problem_is_listed(keys):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    app = GitHubApp(API, APP_ID, wrong, transport=FakeGitHub(keys[1]).transport())
    directory = DirectoryClient(EXRADIUS, "wrong", transport=FakeDirectory(DIRECTORY).transport())
    with pytest.raises(StartupError) as caught:
        await start(make_settings(wrong), app=app, directory_client=directory, broker=FakeBroker(fail=True))
    assert [p.split(":")[0] for p in caught.value.problems] == [
        "GITHUB_APP_ID and the private key", "EXRADIUS_API_URL and EXRADIUS_INTEGRATION_TOKEN", "EXRADIUS_BROKER_URL",
    ]
    await app.aclose()
    await directory.aclose()


async def test_a_good_start_syncs_reconciles_and_consumes(keys):
    github = FakeGitHub(keys[1])
    github.add_repo("acme/plane", 77)
    app = GitHubApp(API, APP_ID, keys[0], transport=github.transport())
    directory = DirectoryClient(EXRADIUS, "exr_test", transport=FakeDirectory(copy.deepcopy(DIRECTORY)).transport())
    broker = FakeBroker()
    bridge = await start(make_settings(keys[0]), app=app, directory_client=directory, broker=broker)
    assert bridge.bot == BOT
    assert "agent: Developer" in github.label_names("acme/plane")
    assert broker.handler == bridge.on_event
    await app.aclose()
    await directory.aclose()
