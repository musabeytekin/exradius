from __future__ import annotations

import copy
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from exradius_github.exradius import DirectoryClient
from exradius_github.flow import Bridge, IssueRef
from exradius_github.github import GitHubApp
from exradius_github.labels import Labels
from exradius_github.settings import Settings
from tests.fakes import API, APP_ID, BOT, EXRADIUS, FakeDirectory, FakeGitHub, FakePublisher, artifacts_transport

DIRECTORY = {
    "projects": [
        {
            "slug": "plane",
            "name": "Plane",
            "runnable": True,
            "repositories": [{"name": "plane", "url": "https://github.com/acme/plane.git"}],
            "agents": [{"slug": "developer", "name": "Developer"}, {"slug": "scout", "name": "Scout"}],
        },
        {
            "slug": "saleor",
            "name": "Saleor",
            "runnable": False,
            "repositories": [{"name": "saleor", "url": "git@github.com:acme/saleor.git"}],
            "agents": [{"slug": "developer", "name": "Developer"}],
        },
    ]
}
INSTALLATION = 77


@pytest.fixture(scope="session")
def keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def make_settings(private_key: str, **overrides) -> Settings:
    values = dict(
        github_app_id=APP_ID,
        github_private_key=private_key,
        github_webhook_secret="hook-secret",
        github_api_url=API,
        exradius_broker_url="amqp://guest:guest@localhost/",
        exradius_api_url=EXRADIUS,
        exradius_integration_token="exr_test",
        port=8080,
    )
    values.update(overrides)
    return Settings(**values)


@dataclass
class World:
    github: FakeGitHub
    directory: FakeDirectory
    publisher: FakePublisher
    bridge: Bridge
    app: GitHubApp
    artifacts: dict[str, bytes] = field(default_factory=dict)

    repo = "acme/plane"

    def ref(self, number: int = 7) -> IssueRef:
        return IssueRef(self.repo, number, INSTALLATION)

    def issue(self, number: int = 7, title: str = "Estimates return 500", **kwargs) -> IssueRef:
        self.github.add_issue(self.repo, number, title, **kwargs)
        return self.ref(number)

    async def label(self, ref: IssueRef, name: str = "agent: Developer", actor: str = "octocat") -> None:
        label = self.github.label_issue(ref.repo, ref.number, name, actor)
        await self.bridge.labeled(ref, label, actor)

    def state(self, ref: IssueRef):
        from exradius_github.state import latest

        return latest(self.github.comments(ref.repo, ref.number), BOT)[0]

    def event(self, ref: IssueRef, kind: str, payload: dict, *, task_id: str | None = None) -> dict:
        return {
            "version": 1,
            "task_id": task_id or self.state(ref).task_id,
            "run_id": "run-1",
            "correlation": {"github": {"host": "github.com", "repository": ref.repo, "issue": ref.number, "installation": ref.installation}},
            "timestamp": "2026-11-08T10:00:00Z",
            "type": kind,
            "payload": payload,
        }


@pytest.fixture
async def world(keys):
    private, public = keys
    github = FakeGitHub(public)
    github.add_repo("acme/plane", INSTALLATION)
    github.add_repo("acme/saleor", INSTALLATION)
    github.add_repo("acme/elsewhere", None)
    github.add_label("acme/plane", "bug", "Something isn't working")
    directory = FakeDirectory(copy.deepcopy(DIRECTORY))
    publisher = FakePublisher()
    artifacts: dict[str, bytes] = {}
    settings = make_settings(private)

    app = GitHubApp(API, APP_ID, private, transport=github.transport())
    directory_client = DirectoryClient(EXRADIUS, "exr_test", transport=directory.transport())
    downloads = httpx.AsyncClient(transport=artifacts_transport(artifacts))
    bridge = Bridge(
        settings=settings,
        app=app,
        labels=Labels(app, settings.github_host),
        directory_client=directory_client,
        publisher=publisher,
        bot=BOT,
        downloads=downloads,
    )
    await bridge.refresh_directory()
    yield World(github, directory, publisher, bridge, app, artifacts)
    await app.aclose()
    await directory_client.aclose()
    await downloads.aclose()
