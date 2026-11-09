"""docs/run-environment.md on Kubernetes: declared memory, caches and commands in the config,
and how this phase treats them (docs/kubernetes.md §6, §7.6; docs/kubernetes-plan.md phase 1)."""

from __future__ import annotations

import uuid

import pytest

from exradius import capabilities
from exradius.config_repo import read_snapshot
from exradius.models import Run
from exradius.schemas.project_config import format_memory, memory_bytes, parse_config
from exradius.worker import caches
from exradius.worker.run_objects import quantity
from tests.conftest import BASE_CONFIG, make_config_repo
from tests.test_api import admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401

GiB = 1024**3
MINIMAL = "version: 1\nproject: {slug: p}\nagents:\n  a: {}\n"

DECLARED = BASE_CONFIG + """\
resources:
  memory: 6g
workstation:
  setup: [pnpm install]
  cache:
    - ~/.local/share/pnpm/store
    - /var/cache/apt-lists
services:
  minio:
    image: minio/minio
    command: [server, /data]
    memory: 512m
"""


def errors(text: str) -> list[str]:
    result = parse_config(text)
    assert result.config is None
    return result.errors


# The config ---------------------------------------------------------------------------------


def test_resources_cache_and_command_parse():
    config = parse_config(DECLARED).config
    assert config.resources.memory == "6g" and config.workstation_memory == 6 * GiB
    assert config.services["minio"].command == ["server", "/data"] and config.services["minio"].memory == "512m"
    assert config.memory_reservation() == 6 * GiB + 512 * 1024**2
    assert config.workstation.cache == ["~/.local/share/pnpm/store", "/var/cache/apt-lists"]
    plain = parse_config(MINIMAL).config
    assert plain.resources.memory is None and plain.memory_reservation() == 0 and plain.workstation.cache == []


def test_memory_sizes():
    assert memory_bytes("512m") == 512 * 1024**2 and memory_bytes("1.5G") == int(1.5 * GiB)
    assert format_memory(6 * GiB) == "6g" and format_memory(1536 * 1024**2) == "1.5g"
    assert format_memory(512 * 1024**2) == "512m"
    for value, message in (("6", "must be a size with a unit"), ("6gb", "must be a size with a unit"),
                           ("1k", "must be at least 6m"), ("lots", "must be a size with a unit")):
        assert errors(MINIMAL + f"resources: {{memory: {value}}}\n") == [f"resources.memory: {message}, such as 512m or 6g"
                                                                         if "unit" in message else f"resources.memory: {message}"]
    assert errors(MINIMAL + "resources: {cpu: 2}\n") == ["resources.cpu: unknown key"]
    assert errors(MINIMAL + "services:\n  db: {image: postgres, memory: 512}\n") == [
        "services.db.memory: must be a size with a unit, such as 512m or 6g"]


@pytest.mark.parametrize("entry,message", [
    ("cache", "must start with ~/ (the home directory) or / (an absolute path)"),
    ("~", "must start with ~/ (the home directory) or / (an absolute path)"),
    ("~/", "must name a directory below ~ or /, not the home or the root itself"),
    ("/work/node_modules", "must be outside /work"),
    ("/work", "must be outside /work"),
    ("/exradius/state/x", "/exradius is reserved"),
    ("/etc/passwd", "/etc/passwd is reserved"),
    ("~/../etc", "must not use '..'"),
    ("/tmp", "must name a directory below /tmp, not /tmp itself"),
])
def test_cache_paths_are_checked(entry, message):
    assert errors(MINIMAL + f"workstation:\n  cache: ['{entry}']\n") == [f"workstation.cache[0]: {message}"]


def test_cache_entries_are_unique_and_commands_not_empty():
    assert errors(MINIMAL + "workstation:\n  cache: [~/.m2, ~/.m2/]\n") == ["workstation.cache[1]: '~/.m2/' is listed twice"]
    assert errors(MINIMAL + "workstation:\n  cache: [~/.m2, /home/runner/.m2]\n") == [
        "workstation.cache[1]: '/home/runner/.m2' is listed twice"]
    assert errors(MINIMAL + "services:\n  r: {image: redis, command: [redis-server, --port, 6380]}\n") == [
        "services.r.command[2]: must be a string"]
    config = parse_config(MINIMAL + "services:\n  r: {image: valkey, command: [valkey-server, --save, '']}\n").config
    assert config.services["r"].command == ["valkey-server", "--save", ""]
    assert errors(MINIMAL + "services:\n  db: {image: postgres, command: []}\n") == [
        "services.db.command: List should have at least 1 item after validation, not 0"]


# Caches: path naming (the project volume mounts them in phase 3) -------------------------------------


def test_cache_paths_and_names():
    assert caches.target_path("~/.local/share/pnpm/store/", "/home/runner") == "/home/runner/.local/share/pnpm/store"
    assert caches.target_path("/var/cache/apt-lists", "/home/runner") == "/var/cache/apt-lists"
    assert caches.sanitise("~/.local/share/pnpm/store") == "local_share_pnpm_store"
    assert caches.sanitise("/var/cache/apt-lists") == "var_cache_apt-lists"
    assert caches.sanitise("~/...") == "cache"


def test_a_cache_is_accepted_and_ignored_with_a_warning(tmp_path):
    remote = make_config_repo(tmp_path / "repos", {"exradius.yaml": DECLARED.replace(
        "services:\n  minio:\n    image: minio/minio\n    command: [server, /data]\n    memory: 512m\n", "")})
    snapshot = read_snapshot(tmp_path / "cache", uuid.uuid4(), str(remote))
    assert snapshot.config is not None and snapshot.errors == []
    assert "workstation.cache is ignored: caches come with the project volume, not available yet" in snapshot.warnings
    assert capabilities.unsupported(snapshot.config) == []  # still runnable
    assert capabilities.warnings(parse_config(MINIMAL).config) == [] and capabilities.warnings(None) == []


def test_what_this_version_cannot_run():
    config = parse_config(DECLARED).config
    assert capabilities.unsupported(config) == ["services.minio: services are not available yet"]
    built = parse_config(MINIMAL + "workstation: {dockerfile: Dockerfile}\n", lookup=lambda path: "tree" if path in ("", ".") else "blob").config
    assert capabilities.unsupported(built) == []  # phase 2 builds workstations
    assert capabilities.unsupported(parse_config(MINIMAL).config) == [] and capabilities.unsupported(None) == []


def test_project_page_lists_what_cannot_run_yet(admin, tmp_path):
    project = setup_configuration(admin, tmp_path, BASE_CONFIG + "services:\n  db: {image: 'postgres:16'}\n")["project"]
    assert project["runnable"] is False and project["health"] == "config_errors"
    assert project["reason"] == "services.db: services are not available yet"
    assert "services.db: services are not available yet" in project["errors"]
    assert project["cache_bytes"] is None  # no Exradius process mounts the caches any more


def test_declared_memory_becomes_a_quantity():
    assert quantity("6g") == "6Gi" and quantity("512m") == "512Mi" and quantity("1.5g") == "1536Mi"
    assert quantity("6m") == "6Mi"


def test_task_rows_say_waiting_for_cluster_capacity(admin, sync_db, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path, state="preparing", with_token=None)
    sync_db.execute(Run.__table__.update().where(Run.id == uuid.UUID(run_id)).values(waiting_for="capacity"))
    sync_db.commit()
    [row] = admin.get("/api/tasks").json()
    assert row["waiting_for"] == "capacity" and row["state"] == "preparing"
    detail = admin.get(f"/api/tasks/{task_id}").json()
    assert detail["waiting_for"] == "capacity" and detail["run"]["waiting_for"] == "capacity"
