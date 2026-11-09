"""Reading a project's config repository (docs/config-repo.md §3).

``read_snapshot`` refreshes the config repository's bare mirror with the credential of its
git host (§6), resolves the branch head, finds ``exradius.yaml`` (at the root, then in
``.exradius/``), parses and validates it and returns a ``ProjectConfigSnapshot``. It is
synchronous (git subprocesses); callers use ``asyncio.to_thread``.

The mirror is ``<cache_dir>/config-repos/<project id>.git`` in the reading pod's own cache
(an ``emptyDir``: re-fetched after a restart). Builds read their context from it too
(``tree_hash``, ``archive``).
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import subprocess
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from exradius.models import utcnow
from exradius.schemas.project_config import CONFIG_FILES, ParseResult, ProjectConfig, load_yaml, parse_config, yaml_error
from exradius import capabilities
from exradius.git import GitError, git, sync_mirror

READ_TIMEOUT = 60
"""Seconds for each network git command (fetch, ls-remote)."""


class ConfigReadError(Exception):
    """The repository, the branch or the file could not be read at all."""


@dataclass
class ProjectConfigSnapshot:
    commit: str | None
    read_at: datetime
    config: ProjectConfig | None
    """``None`` when there are errors."""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_keys: list[str] = field(default_factory=list)
    store: str | None = None
    branch: str | None = None
    """The branch read (the configured one, or the remote's default)."""
    path: str | None = None
    """The file read, from the repository root."""
    raw: str | None = None
    """The file's text, for the panel."""
    bridges: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Bridge files (config-repo.md §2.1), by bridge name: ``{"config": {...}}`` or
    ``{"error": "..."}``. Read even when the config itself has errors."""

    @property
    def config_dir(self) -> str:
        return posixpath.dirname(self.path or "")

    def to_json(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "read_at": self.read_at.isoformat(),
            "config": self.config.model_dump(mode="json") if self.config else None,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "required_keys": list(self.required_keys),
            "store": self.store,
            "branch": self.branch,
            "path": self.path,
            "raw": self.raw,
            "bridges": dict(self.bridges),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProjectConfigSnapshot:
        return cls(
            commit=data.get("commit"),
            read_at=datetime.fromisoformat(data["read_at"]),
            config=ProjectConfig.model_validate(data["config"], strict=False) if data.get("config") else None,
            errors=list(data.get("errors") or []),
            warnings=list(data.get("warnings") or []),
            required_keys=list(data.get("required_keys") or []),
            store=data.get("store"),
            branch=data.get("branch"),
            path=data.get("path"),
            raw=data.get("raw"),
            bridges=dict(data.get("bridges") or {}),
        )

    @classmethod
    def unreadable(cls, message: str) -> ProjectConfigSnapshot:
        return cls(commit=None, read_at=utcnow(), config=None, errors=[message])


def mirror_path(cache_dir: Path, project_id: uuid.UUID) -> Path:
    return cache_dir / "config-repos" / f"{project_id}.git"


def git_host(url: str) -> str | None:
    """The host a credential is looked up by: ``github.com`` for https and ssh URLs and
    for scp-like ``git@github.com:org/repo``; ``None`` for local paths and ``file://``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme and parts.netloc:
        return (parts.hostname or "").lower() or None
    if not parts.scheme and "@" in url.split(":", 1)[0] and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0].lower() or None
    return None


def host_auth(kind: str, value: str) -> str:
    """A host credential as the ``user:password`` git sends with HTTP Basic."""
    return value if kind == "basic" else f"x-access-token:{value}"


def _git(mirror: Path, *args: str) -> str:
    return git(*args, cwd=mirror)


def _rev(commit: str, path: str) -> str:
    return f"{commit}:{path}" if path else f"{commit}^{{tree}}"


def make_lookup(mirror: Path, commit: str):
    """What a repository path is at ``commit``: blob, tree, link (a symlink on the way) or
    None."""

    def lookup(path: str) -> str | None:
        if not path:
            return "tree"
        parent = ""
        mode = None
        for name in path.split("/"):
            output = subprocess.run(
                ["git", "ls-tree", "-z", _rev(commit, parent), "--", name],
                cwd=mirror, capture_output=True, text=True, check=False,
            ).stdout
            entry = next((e for e in output.split("\0") if e.partition("\t")[2] == name), None)
            if entry is None:
                return None
            mode = entry.split(" ", 1)[0]
            if mode == "120000":
                return "link"
            parent = f"{parent}/{name}" if parent else name
        return "tree" if mode == "040000" else ("blob" if mode and mode.startswith("100") else None)

    return lookup


def read_snapshot(
    cache_dir: Path,
    project_id: uuid.UUID,
    url: str,
    *,
    branch: str | None = None,
    path: str | None = None,
    auth: str | None = None,
) -> ProjectConfigSnapshot:
    """Fetch and read the config. Raises ``ConfigReadError`` when there is no file to read;
    a file that does not validate gives a snapshot with errors."""
    mirror = mirror_path(cache_dir, project_id)
    try:
        sync_mirror(mirror, url, auth, timeout=READ_TIMEOUT)
        if branch is None:
            head = _git(mirror, "symbolic-ref", "HEAD").strip()
            branch = head.removeprefix("refs/heads/")
    except GitError as exc:
        raise ConfigReadError(f"could not read the config repository: {exc}") from exc
    try:
        commit = _git(mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").strip()
    except GitError as exc:
        raise ConfigReadError(f"branch {branch!r} does not exist in the config repository") from exc
    return read_at_commit(mirror, commit, branch=branch, path=path)


def read_at_commit(mirror: Path, commit: str, *, branch: str | None, path: str | None) -> ProjectConfigSnapshot:
    lookup = make_lookup(mirror, commit)
    candidates = [path.strip("/")] if path else list(CONFIG_FILES)
    found = next((c for c in candidates if lookup(c) is not None), None)
    if found is None:
        where = candidates[0] if path else "exradius.yaml or .exradius/exradius.yaml"
        raise ConfigReadError(f"no {where} on branch {branch} (commit {commit[:12]})")
    if lookup(found) != "blob":
        raise ConfigReadError(f"{found} is not a regular file (commit {commit[:12]})")
    try:
        text = _git(mirror, "cat-file", "blob", f"{commit}:{found}")
    except GitError as exc:
        raise ConfigReadError(str(exc)) from exc
    config_dir = posixpath.dirname(found)
    result: ParseResult = parse_config(text, config_dir=config_dir, lookup=lookup)
    bridges, bridge_warnings = read_bridges(mirror, commit, config_dir, lookup)
    return ProjectConfigSnapshot(
        commit=commit,
        read_at=utcnow(),
        config=result.config,
        errors=result.errors,
        warnings=[*result.warnings, *capabilities.warnings(result.config), *bridge_warnings],
        required_keys=result.required_keys,
        store=result.store,
        branch=branch,
        path=found,
        raw=text,
        bridges=bridges,
    )


# -- bridge files (config-repo.md §2.1) ------------------------------------------------

BRIDGES_DIR = "bridges"
BRIDGE_NAME = re.compile(r"[a-z][a-z0-9-]*")
MAX_BRIDGE_FILE = 64 * 1024


def parse_bridge_file(text: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """The file's content as JSON-compatible data, or the problem with it. Exradius checks
    only that it is YAML with a mapping on top, at most ``MAX_BRIDGE_FILE`` bytes."""
    if len(text) > MAX_BRIDGE_FILE:
        return None, f"larger than {MAX_BRIDGE_FILE // 1024} KiB"
    try:
        raw = load_yaml(text.decode())
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    except Exception as exc:  # noqa: BLE001 - every YAML problem is reported the same way
        return None, f"not valid YAML: {yaml_error(exc)}"
    if not isinstance(raw, dict):
        return None, "must be a mapping"
    # As JSON: dates and other YAML-only values become strings, keys become strings.
    return json.loads(json.dumps(raw, default=str)), None


def read_bridges(
    mirror: Path, commit: str, config_dir: str, lookup
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every ``bridges/<name>.yaml`` next to the config file at ``commit``, and warnings
    for entries that are not bridge files or files that are broken."""
    folder = posixpath.join(config_dir, BRIDGES_DIR) if config_dir else BRIDGES_DIR
    if lookup(folder) != "tree":
        return {}, []
    listing = subprocess.run(
        ["git", "ls-tree", "-z", _rev(commit, folder)], cwd=mirror, capture_output=True, text=True, check=False,
    ).stdout
    bridges: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for entry in sorted(e for e in listing.split("\0") if e):
        meta, _, name = entry.partition("\t")
        mode = meta.split(" ", 1)[0]
        where = f"{folder}/{name}"
        stem = name.removesuffix(".yaml")
        if not name.endswith(".yaml") or not BRIDGE_NAME.fullmatch(stem) or not mode.startswith("100"):
            warnings.append(f"{where}: ignored; a bridge file is a regular file <name>.yaml, the name in [a-z][a-z0-9-]*")
            continue
        content, problem = parse_bridge_file(_git_bytes(mirror, "cat-file", "blob", f"{commit}:{where}"))
        if problem is not None:
            message = f"{where}: {problem}"
            warnings.append(message)
            bridges[stem] = {"error": message}
        else:
            bridges[stem] = {"config": content}
    return bridges, warnings


# -- builds (§4) -----------------------------------------------------------------------


def tree_hash(mirror: Path, commit: str, path: str) -> str:
    """The git object id of ``path`` at ``commit`` (a tree for a context directory)."""
    return _git(mirror, "rev-parse", _rev(commit, path)).strip()


BUILD_DOCKERFILE = ".exradius.Dockerfile"
"""Name of a Dockerfile from outside the context, added to the build context."""


def build_dockerfile_name(context: str, dockerfile: str) -> str:
    """The Dockerfile's name inside the context tar ``build_context`` makes."""
    if not context:
        return dockerfile
    return dockerfile.removeprefix(context + "/") if dockerfile.startswith(context + "/") else BUILD_DOCKERFILE


def build_context(mirror: Path, commit: str, context: str, dockerfile: str) -> tuple[bytes, str]:
    """A tar of the context directory at ``commit`` and the Dockerfile's name in it. A
    Dockerfile outside the context is added to the tar as ``BUILD_DOCKERFILE``."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", _rev(commit, context)],
        cwd=mirror, capture_output=True, check=False,
    )
    if archive.returncode != 0:
        raise GitError(f"git archive of {context or '.'} failed: {archive.stderr.decode(errors='replace').strip()}")
    inside = build_dockerfile_name(context, dockerfile)
    if inside == BUILD_DOCKERFILE:
        content = _git_bytes(mirror, "cat-file", "blob", f"{commit}:{dockerfile}")
        out = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as source, tarfile.open(fileobj=out, mode="w") as target:
            for member in source.getmembers():
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo(BUILD_DOCKERFILE)
            info.size = len(content)
            info.mode = 0o644
            target.addfile(info, io.BytesIO(content))
        return out.getvalue(), inside
    return archive.stdout, inside


def _git_bytes(mirror: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=mirror, capture_output=True, check=False)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout
