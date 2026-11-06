from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from exradius.config import Settings
from exradius.crypto import SecretBox, generate_secret_key
from exradius.db import make_engine, make_session_factory
from exradius.models import Agent, Base, Connection, Project, ProjectAgent, Provider
from exradius.storage import MemoryObjectStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "exradius.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        secret_key=generate_secret_key(),
        admin_username="admin",
        admin_password="correct horse",
        data_dir=data_dir,
        broker_url="amqp://unused/",
    )


@pytest.fixture
async def sessions(settings: Settings):
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


@pytest.fixture
def box(settings: Settings) -> SecretBox:
    return SecretBox(settings.secret_key)


@pytest.fixture
async def configured(sessions, box):
    """A project 'billing-platform' with agent 'bug-fixer' attached, plus an unattached agent,
    and a default provider 'anthropic' (API key ``sk-test``)."""
    async with sessions() as session:
        provider = Provider(slug="anthropic", name="Anthropic", kind="anthropic", auth_method="api_key",
                            credential_encrypted=box.encrypt("sk-test"), settings={}, is_default=True)
        session.add(provider)
        connection = Connection(slug="docs", name="Docs", transport="http", url="https://mcp.example/mcp")
        project = Project(slug="billing-platform", name="Billing", setup_commands=[])
        agent = Agent(slug="bug-fixer", name="Bug fixer", prompt="You fix bugs.", connections=[connection],
                      identity_name="Bug Fixer Bot", identity_email="bug-fixer@agents.example")
        loner = Agent(slug="loner", name="Not attached", prompt="", identity_name="Loner", identity_email="loner@agents.example")
        session.add_all([connection, project, agent, loner])
        await session.flush()
        session.add(ProjectAgent(project_id=project.id, agent_id=agent.id))
        await session.commit()
        return {"project": project, "agent": agent, "loner": loner, "connection": connection, "provider": provider}


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
             "HOME": str(cwd or Path.cwd()), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    ).stdout


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """A bare 'remote' repository with a main and a feature branch."""
    work = tmp_path / "seed"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "README.md").write_text("hello\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "initial", cwd=work)
    git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "feature.txt").write_text("feature\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "feature", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(remote))
    return remote
