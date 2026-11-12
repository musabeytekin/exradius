"""``exradius migrate`` (the init container of the api and worker pods) and the schema the
migrations build, compared with the models."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from exradius import cli, models
from exradius.config import get_settings
from exradius.config_repo import read_snapshot
from exradius.crypto import SecretBox, generate_secret_key
from exradius.db import make_engine, make_session_factory
from exradius.models import (
    Base,
    Project,
    SecretStore,
    SecretStoreChange,
    SecretStoreEntry,
    SecretStoreProject,
    utcnow,
)
from exradius.project_state import assess, cached, remember, store_values
from exradius.secret_backends import SecretBackends
from tests.conftest import make_config_repo


def test_migrate_builds_the_models_schema(tmp_path, monkeypatch):
    path = tmp_path / "migrated.sqlite"
    monkeypatch.setenv("EXRADIUS_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    try:
        cli.main(["migrate"])
        cli.main(["migrate"])  # idempotent: both pods run it
    finally:
        get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            migrated = {c["name"] for c in inspector.get_columns(table.name)}
            assert migrated == {c.name for c in table.columns}, table.name
        runs = {c["name"] for c in inspector.get_columns("runs")}
        assert {"run_object", "delivery_warnings"} <= runs
        assert not {"container_id", "directory", "worker_id"} & runs
        workers = {c["name"] for c in inspector.get_columns("workers")}
        assert "operator" in workers and not {"docker_ok", "docker_detail", "images"} & workers
    finally:
        engine.dispose()


# 0009 and 0010: secret stores bound to projects, and their settings (docs/config-repo.md §7) ------


def _alembic(path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(models.__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


STORE_CONFIG = """\
version: 1
project: {slug: %s}
secret_store: %s
env:
  DB: ${secret:DB_PASSWORD}
agents:
  dev: {}
"""


def _project(engine, tmp_path, slug: str, config: str | None) -> uuid.UUID:
    """A project row as an install before 0009 had it: its snapshot cached from a real read."""
    project = Project(id=uuid.uuid4(), slug=slug, config_url="", created_at=utcnow())
    if config is not None:
        remote = make_config_repo(tmp_path / slug, {"exradius.yaml": config})
        project.config_url = str(remote)
        remember(project, read_snapshot(tmp_path / "cache", project.id, str(remote), branch=None, path=None))
    project_id = project.id
    with Session(engine) as session:
        session.add(project)
        session.commit()
    return project_id


def test_0009_allows_each_project_for_the_store_its_config_names(tmp_path):
    path = tmp_path / "upgraded.sqlite"
    config = _alembic(path)
    command.upgrade(config, "0008")
    engine = create_engine(f"sqlite:///{path}")
    box = SecretBox(generate_secret_key())
    try:
        stores = sa.table("secret_stores", sa.column("id", sa.Uuid()), sa.column("name", sa.String()),
                          sa.column("kind", sa.String()), sa.column("created_at", sa.DateTime(timezone=True)))
        billing, spare = uuid.uuid4(), uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(stores.insert(), [
                {"id": billing, "name": "billing", "kind": "internal", "created_at": utcnow()},
                {"id": spare, "name": "spare", "kind": "internal", "created_at": utcnow()},
            ])
        with Session(engine) as session:
            session.add(SecretStoreEntry(store_id=billing, key="DB_PASSWORD", value_encrypted=box.encrypt("hunter2")))
            session.commit()
        first = _project(engine, tmp_path, "first", STORE_CONFIG % ("first", "billing"))
        second = _project(engine, tmp_path, "second", STORE_CONFIG % ("second", "billing"))
        _project(engine, tmp_path, "ghost", STORE_CONFIG % ("ghost", "nowhere"))  # names no store that exists
        _project(engine, tmp_path, "plain", "version: 1\nproject: {slug: plain}\nagents:\n  dev: {}\n")
        _project(engine, tmp_path, "unread", None)  # never read: no snapshot

        command.upgrade(config, "head")

        with Session(engine) as session:
            bound = {(row.store_id, row.project_id) for row in session.scalars(select(SecretStoreProject))}
            assert bound == {(billing, first), (billing, second)}
            logged = session.scalars(select(SecretStoreChange).order_by(SecretStoreChange.project)).all()
            assert [(c.actor, c.store_id, c.store, c.action, c.project, c.key, c.detail) for c in logged] == [
                ("migration", billing, "billing", "project_allowed", "first", None, None),
                ("migration", billing, "billing", "project_allowed", "second", None, None),
            ]
            # 0010: the stores that existed have no settings.
            assert {s.name: (s.kind, s.settings) for s in session.scalars(select(SecretStore))} == {
                "billing": ("internal", {}), "spare": ("internal", {})}
    finally:
        engine.dispose()

    # Both projects stay runnable, and their values resolve.
    async def still_runs() -> None:
        async_engine = make_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with make_session_factory(async_engine)() as session:
                backends = SecretBackends(box)
                for project_id in (first, second):
                    project = await session.get(Project, project_id)
                    snapshot = cached(project)
                    health = await assess(session, project, snapshot, backends)
                    assert (health.runnable, health.status, health.store_allowed) == (True, "ok", True)
                    values = await store_values(session, backends, project_id, snapshot.config)
                    assert values.values == {"DB_PASSWORD": "hunter2"}
        finally:
            await async_engine.dispose()

    asyncio.run(still_runs())


def test_0009_and_0010_downgrade(tmp_path):
    path = tmp_path / "downgraded.sqlite"
    config = _alembic(path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.downgrade(config, "0009")
        inspector = inspect(engine)
        assert "settings" not in {c["name"] for c in inspector.get_columns("secret_stores")}
        assert {"secret_store_projects", "secret_store_changes"} <= set(inspector.get_table_names())
        command.downgrade(config, "0008")
        inspector = inspect(engine)
        assert not {"secret_store_projects", "secret_store_changes"} & set(inspector.get_table_names())
        command.upgrade(config, "head")
        assert "settings" in {c["name"] for c in inspect(engine).get_columns("secret_stores")}
    finally:
        engine.dispose()
