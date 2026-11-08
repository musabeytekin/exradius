from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from exradius.config import Settings
from exradius.crypto import SecretBox, generate_secret_key
from exradius.db import make_engine, make_session_factory
from exradius.config_repo import read_snapshot
from exradius.models import Base, Project, Provider
from exradius.project_state import remember
from exradius.storage import MemoryObjectStore
from exradius.worker.intake import accept_task


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "exradius.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def settings(db_path: Path, tmp_path: Path) -> Settings:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        secret_key=generate_secret_key(),
        admin_username="admin",
        admin_password="correct horse",
        cache_dir=cache_dir,
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


BASE_CONFIG = """\
version: 1
project:
  slug: billing-platform
  name: Billing
notes: Run the tests with make test.
connections:
  docs:
    url: https://mcp.example/mcp
agents:
  bug-fixer:
    name: Bug fixer
    role: You fix bugs.
    identity: {name: Bug Fixer Bot, email: bug-fixer@agents.example}
    connections: [docs]
  scout:
    role: You investigate.
"""
"""The config of the ``configured`` project; tests that need more write their own."""


def make_config_repo(root: Path, files: dict[str, str], name: str = "config") -> Path:
    """A bare repository (branch main) holding ``files``; returns its path."""
    work = root / f"{name}-work"
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=work)
    remote = root / f"{name}.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    git("remote", "add", "origin", str(remote), cwd=work)
    commit_config(work.parent / f"{name}.git", files)
    return remote


def commit_config(remote: Path, files: dict[str, str | None], message: str = "config") -> str:
    """Commit ``files`` (``None`` deletes one) to main of the bare repository; the commit id."""
    work = remote.parent / f"{remote.stem}-work"
    for rel, content in files.items():
        target = work / rel
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", "-A", ".", cwd=work)
    git("commit", "-q", "--allow-empty", "-m", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return git("rev-parse", "HEAD", cwd=work).strip()


async def add_project(sessions, cache_dir: Path, remote: Path, **fields) -> Project:
    """A project row for ``remote`` with its config read (as ``POST /api/projects`` does)."""
    project = Project(config_url=str(remote), **fields)
    project.id = project.id or uuid.uuid4()
    snapshot = read_snapshot(cache_dir, project.id, project.config_url,
                             branch=project.config_branch, path=project.config_path)
    project.slug = fields.get("slug") or (snapshot.config.project.slug if snapshot.config else "broken")
    remember(project, snapshot)
    async with sessions() as session:
        session.add(project)
        await session.commit()
    return project


@pytest.fixture
def config_remote(tmp_path: Path) -> Path:
    return make_config_repo(tmp_path / "repos", {"exradius.yaml": BASE_CONFIG})


@pytest.fixture
async def configured(sessions, box, settings, config_remote):
    """The project 'billing-platform' from ``BASE_CONFIG`` (agents ``bug-fixer`` and
    ``scout``), and a default provider 'anthropic' (API key ``sk-test``)."""
    async with sessions() as session:
        provider = Provider(slug="anthropic", name="Anthropic", kind="anthropic", auth_method="api_key",
                            credential_encrypted=box.encrypt("sk-test"), settings={}, is_default=True)
        session.add(provider)
        await session.commit()
    project = await add_project(sessions, settings.cache_dir, config_remote)
    return {"project": project, "provider": provider, "remote": config_remote}


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


@pytest.fixture
def accept(sessions, store, box, settings):
    """``accept_task`` as the worker calls it."""

    async def _accept(body: bytes):
        return await accept_task(sessions, store, body, box=box, cache_dir=settings.cache_dir)

    return _accept
