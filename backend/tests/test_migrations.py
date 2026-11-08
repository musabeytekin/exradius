"""``exradius migrate`` (the init container of the api and worker pods) and the schema the
migrations build, compared with the models."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from exradius import cli
from exradius.config import get_settings
from exradius.models import Base


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
