"""``exradius.yaml``: the project's config file (docs/config-repo.md §2).

``parse_config`` turns the file's text into a ``ProjectConfig`` and a list of errors and
warnings, each prefixed with the YAML path it is about (``services.postgres.env.X: ...``).
Structure is checked by the pydantic models below (strict, unknown keys forbidden); the
rules that span fields (names, references, paths, the catalog, where secrets may appear)
are checked afterwards. A file with any error has no config.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, ValidationError

from exradius import catalog

SECRET_REF = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]+)\}")
"""``${secret:NAME}``: a value from the project's secret store."""
SECRET_MARK = "${secret:"

NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN, max_length=63)]
EnvKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=255)]
FolderName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)]
IdentityName = Annotated[str, StringConstraints(pattern=r"^[^<>\x00-\x1f]+$", min_length=1, max_length=255)]
IdentityEmail = Annotated[str, StringConstraints(pattern=r"^[^@<>\s]+@[^@<>\s]+$", max_length=255)]
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]*$", max_length=255)]
Port = Annotated[int, Field(ge=1, le=65535)]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]

CONFIG_FILES = ("exradius.yaml", ".exradius/exradius.yaml")
"""Where the file is looked for, in this order (§2)."""
MAX_FILE_SIZE = 1_000_000
RESERVED_SERVICE_NAMES = ("api", "workstation")
"""Plus every name starting with ``exradius``: they name the API and Exradius' containers
on the run network. ``workstation`` would share the built workstation's image tag."""
RESERVED_CONNECTION = "exradius"
"""The built-in server every agent has."""


def _scalar(value: Any) -> Any:
    """Numbers become strings, as in compose files; booleans must be quoted, because YAML
    reads ``yes``, ``on`` and ``true`` alike."""
    if isinstance(value, bool):
        raise ValueError("write true, false, yes or no in quotes: the value must be a string")
    if isinstance(value, (int, float)):
        return str(value)
    return value


Text = Annotated[str, BeforeValidator(_scalar)]
EnvMap = dict[EnvKey, Text]

MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
MEMORY_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([bkmg])$", re.I)
MIN_MEMORY = 6 * 1024**2
"""Docker's smallest container memory limit."""


def memory_bytes(text: str) -> int:
    """A Docker size string (``512m``, ``6g``, ``1.5g``) in bytes."""
    match = MEMORY_PATTERN.match(text.strip())
    if not match:
        raise ValueError(f"not a size: {text!r}")
    return int(float(match.group(1)) * MEMORY_UNITS[match.group(2).lower()])


def format_memory(value: int) -> str:
    """Bytes as the shortest Docker size string that is exact to one decimal (``6g``)."""
    for unit in ("g", "m", "k"):
        amount = value / MEMORY_UNITS[unit]
        if amount >= 1:
            return f"{amount:.1f}".removesuffix(".0") + unit
    return f"{value}b"


def _memory(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return value  # the string check reports it
    text = str(value).strip()
    if not MEMORY_PATTERN.match(text):
        raise ValueError("must be a size with a unit, such as 512m or 6g")
    if memory_bytes(text) < MIN_MEMORY:
        raise ValueError("must be at least 6m")
    return text


Memory = Annotated[str, BeforeValidator(_memory)]
"""A Docker size string with a unit: ``512m``, ``6g``."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectInfo(_Model):
    slug: Name
    name: str | None = None
    """Defaults to the slug."""
    description: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.slug


class RepositoryDef(_Model):
    name: FolderName
    """The folder name in the project directory."""
    url: NonEmpty
    branch: Text | None = None
    """``None`` is the remote's default branch."""
    credential: str | None = None
    """``${secret:NAME}``: a token or ``user:password``, for fetching and (with ``push``)
    pushing. Never the host credential of the config repository."""
    push: bool = False


class _Image(_Model):
    image: NonEmpty | None = None
    dockerfile: NonEmpty | None = None
    """Relative to the config file's directory, inside the repository. ``parse_config``
    rewrites it (and ``context``) as a path from the repository root."""
    context: str | None = None
    """Build context; defaults to the Dockerfile's directory (``""`` is the root)."""


class Workstation(_Image):
    setup: list[NonEmpty] = Field(default_factory=list)
    """Run after cloning, in the workstation, with the project directory as cwd."""
    cache: list[NonEmpty] = Field(default_factory=list)
    """Directories kept across the project's runs (``~/.cache/uv``): ``~``-relative or
    absolute, outside ``/work`` (docs/run-environment.md §5)."""


class Resources(_Model):
    memory: Memory | None = None
    """The workstation's memory (setup, run and resume containers): reserved before a run
    starts and enforced as its limit. ``None`` reserves nothing and limits nothing."""


class Ready(_Model):
    command: list[NonEmpty] | None = Field(default=None, min_length=1)
    tcp: Port | None = None


class ServiceDef(_Image):
    env: EnvMap = Field(default_factory=dict)
    ports: list[Port] = Field(default_factory=list)
    """Documentation for the agent; nothing is published."""
    ready: Ready | None = None
    """``None``: the image's HEALTHCHECK, else "running"."""
    command: list[str] | None = Field(default=None, min_length=1)
    """The container command; the image's entrypoint is kept. Strings only (numbers are
    refused, write them quoted); an empty one is an argument (valkey's ``--save ""``)."""
    memory: Memory | None = None
    """Reserved and enforced like ``resources.memory``."""


class ConnectionDef(_Model):
    catalog: str | None = None
    config: dict[str, Text] | None = None
    command: NonEmpty | None = None
    args: list[Text] | None = None
    env: EnvMap | None = None
    url: NonEmpty | None = None
    headers: dict[NonEmpty, Text] | None = None
    type: str | None = None
    """Accepted for ``mcpServers`` compatibility and ignored."""


class IdentityDef(_Model):
    name: IdentityName | None = None
    email: IdentityEmail | None = None


class AgentDef(_Model):
    name: str | None = None
    """Defaults to the slug, capitalised."""
    role: str = ""
    """Appended to the system prompt after the environment section."""
    model: ModelId | None = None
    provider: str | None = None
    """A provider's slug; ``None`` is the default provider."""
    runtime: Literal["claude_code"] = "claude_code"
    identity: IdentityDef | None = None
    connections: list[str] = Field(default_factory=list)


class ProjectConfig(_Model):
    version: Literal[1]
    project: ProjectInfo
    secret_store: Name | None = None
    repositories: list[RepositoryDef] = Field(default_factory=list)
    workstation: Workstation = Field(default_factory=Workstation)
    resources: Resources = Field(default_factory=Resources)
    services: dict[str, ServiceDef] = Field(default_factory=dict)
    env: EnvMap = Field(default_factory=dict)
    notes: str = ""
    connections: dict[str, ConnectionDef] = Field(default_factory=dict)
    agents: dict[str, AgentDef] = Field(min_length=1)

    # -- derived values --------------------------------------------------------------

    @property
    def workstation_memory(self) -> int | None:
        return memory_bytes(self.resources.memory) if self.resources.memory else None

    def memory_reservation(self) -> int:
        """Bytes a run or verification reserves: the workstation and every service that
        declares its memory (docs/run-environment.md §4)."""
        declared = [self.resources.memory, *(s.memory for s in self.services.values())]
        return sum(memory_bytes(m) for m in declared if m)

    def agent_name(self, slug: str) -> str:
        agent = self.agents[slug]
        return agent.name or slug.replace("-", " ").capitalize()

    def identity(self, slug: str) -> tuple[str, str]:
        agent = self.agents[slug]
        given = agent.identity or IdentityDef()
        return (given.name or self.agent_name(slug), given.email or f"{slug}@{self.project.slug}.exradius.local")

    def connection_definition(self, name: str) -> dict[str, Any]:
        """The connection as ``transport``, ``command``, ``args``, ``url``, ``env`` and
        ``headers``; catalog connections expanded, secret references as written."""
        conn = self.connections[name]
        if conn.catalog:
            item = catalog.entry(conn.catalog)
            assert item is not None  # checked by parse_config
            return {"name": name, **catalog.expand(item, dict(conn.config or {}))}
        return {
            "name": name,
            "transport": "http" if conn.url else "stdio",
            "command": conn.command,
            "args": list(conn.args or []),
            "url": conn.url,
            "env": dict(conn.env or {}),
            "headers": dict(conn.headers or {}),
        }


# -- reading the file ----------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping key given twice (PyYAML keeps the last)."""


def _mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen: dict[Any, Any] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            continue  # unhashable keys fail in construct_mapping with their own message
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen[key] = True
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


Lookup = Callable[[str], str | None]
"""What a path in the repository is: ``"blob"``, ``"tree"``, ``"link"`` (a symlink on the
way) or ``None`` (absent). Paths are relative to the repository root, ``""`` is the root."""


@dataclass
class ParseResult:
    config: ProjectConfig | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_keys: list[str] = field(default_factory=list)
    """Secret names referenced in the file, sorted."""
    store: str | None = None


def format_path(loc: tuple[Any, ...]) -> str:
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part == "[key]":
            text += " (key)"
        else:
            text += ("." if text else "") + str(part)
    return text or "(file)"


SECRET_PLACES: tuple[tuple[Any, ...], ...] = (
    ("repositories", int, "credential"),
    ("services", str, "env", str),
    ("env", str),
    ("connections", str, "config", str),
    ("connections", str, "env", str),
    ("connections", str, "headers", str),
)
"""Where ``${secret:NAME}`` may appear; ``str``/``int`` match any key/index."""


def _secret_allowed(path: tuple[Any, ...]) -> bool:
    for place in SECRET_PLACES:
        if len(place) == len(path) and all(
            (p is str and isinstance(x, str)) or (p is int and isinstance(x, int)) or p == x
            for p, x in zip(place, path, strict=True)
        ):
            return True
    return False


def _strings(value: Any, path: tuple[Any, ...] = ()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _scan_secrets(raw: Any, errors: list[str]) -> list[str]:
    """Secret names in the allowed places; errors for references anywhere else and for
    malformed ones."""
    names: set[str] = set()
    for path, value in _strings(raw):
        if SECRET_MARK not in value:
            continue
        where = format_path(path)
        if not _secret_allowed(path):
            errors.append(f"{where}: secrets are not allowed here")
            continue
        if value.count(SECRET_MARK) != len(SECRET_REF.findall(value)):
            errors.append(f"{where}: malformed secret reference; write ${{secret:NAME}} with a NAME of "
                          "letters, digits, '_', '.' and '-'")
            continue
        names.update(SECRET_REF.findall(value))
    return sorted(names)


_MESSAGES = {
    "extra_forbidden": "unknown key",
    "missing": "required",
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "bool_type": "must be true or false",
    "dict_type": "must be a mapping",
    "model_type": "must be a mapping",
    "model_attributes_type": "must be a mapping",
    "list_type": "must be a list",
}


def _pydantic_errors(exc: ValidationError) -> list[str]:
    errors = []
    for error in exc.errors(include_url=False):
        loc, kind = tuple(error["loc"]), error["type"]
        if kind == "literal_error" and loc == ("version",):
            message = "must be 1"
        elif kind == "string_pattern_mismatch":
            message = f"must match {error['ctx']['pattern']}"
        elif kind == "too_short" and loc == ("agents",):
            message = "at least one agent is required"
        elif kind in _MESSAGES:
            message = _MESSAGES[kind]
        else:
            message = error["msg"].removeprefix("Value error, ")
        errors.append(f"{format_path(loc)}: {message}")
    return errors


def repo_path(config_dir: str, value: str) -> str | None:
    """``value`` (relative to the config file's directory) as a path from the repository
    root; ``None`` when it is absolute or uses ``..``."""
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return None
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts:
        return None
    return posixpath.join(config_dir, *parts) if parts else config_dir


@dataclass(frozen=True)
class BuildPaths:
    """Where an image is built from, as paths from the repository root."""

    dockerfile: str
    context: str

    @property
    def dockerfile_in_context(self) -> str | None:
        """The Dockerfile's path inside the context, when it is inside it."""
        if not self.context:
            return self.dockerfile
        prefix = self.context + "/"
        return self.dockerfile.removeprefix(prefix) if self.dockerfile.startswith(prefix) else None


def build_paths(config_dir: str, item: _Image) -> BuildPaths:
    """For an item whose ``dockerfile`` passed the checks; after ``parse_config`` (which
    stores repository paths) call it with ``config_dir=""``."""
    assert item.dockerfile
    dockerfile = repo_path(config_dir, item.dockerfile)
    context = repo_path(config_dir, item.context) if item.context is not None else posixpath.dirname(dockerfile or "")
    assert dockerfile is not None and context is not None
    return BuildPaths(dockerfile, context)


def _check_image(where: str, item: _Image, config_dir: str, lookup: Lookup | None, errors: list[str]) -> None:
    if item.image and item.dockerfile:
        errors.append(f"{where}: give image or dockerfile, not both")
        return
    if item.context is not None and not item.dockerfile:
        errors.append(f"{where}.context: only with dockerfile")
    if not item.dockerfile:
        return
    dockerfile = repo_path(config_dir, item.dockerfile)
    if dockerfile is None:
        errors.append(f"{where}.dockerfile: must be a relative path inside the repository (no '..')")
    context = repo_path(config_dir, item.context) if item.context is not None else posixpath.dirname(dockerfile or "")
    if context is None:
        errors.append(f"{where}.context: must be a relative path inside the repository (no '..')")
    if lookup is None:
        return
    for key, path, kind in (("dockerfile", dockerfile, "blob"), ("context", context, "tree")):
        if path is None:
            continue
        found = lookup(path)
        if found == "link":
            errors.append(f"{where}.{key}: {path or '.'} is or passes through a symlink")
        elif found is None:
            errors.append(f"{where}.{key}: {path or '.'} does not exist in the repository")
        elif found != kind:
            errors.append(f"{where}.{key}: {path or '.'} is not a {'file' if kind == 'blob' else 'directory'}")


CACHE_RESERVED = ("/work", "/exradius", "/opt/exradius", "/proc", "/sys", "/dev", "/etc/passwd", "/etc/group")
"""Absolute paths a cache may not be or lie under: the project directory, Exradius's own
mounts, the kernel's file systems and the generated identity files."""


def cache_problem(entry: str) -> str | None:
    """Why a ``workstation.cache`` entry is not usable, or ``None``."""
    if "\x00" in entry or "\\" in entry or "\n" in entry:
        return "must be a plain path"
    if entry.startswith("~/"):
        rest = entry[2:]
    elif entry.startswith("/"):
        rest = entry[1:]
    else:
        return "must start with ~/ (the home directory) or / (an absolute path)"
    parts = [p for p in rest.split("/") if p not in ("", ".")]
    if ".." in parts:
        return "must not use '..'"
    if not parts:
        return "must name a directory below ~ or /, not the home or the root itself"
    if entry.startswith("/"):
        path = "/" + "/".join(parts)
        for reserved in CACHE_RESERVED:
            if path == reserved or path.startswith(reserved + "/"):
                return f"must be outside {reserved}" if reserved == "/work" else f"{reserved} is reserved"
        if path in ("/home", "/home/runner", "/tmp", "/etc", "/usr", "/bin", "/lib", "/var", "/opt"):
            return f"must name a directory below {path}, not {path} itself"
    return None


def _uses_launcher(command: str | None) -> str | None:
    return command if command in catalog.LAUNCHERS else None


def _check(config: ProjectConfig, config_dir: str, lookup: Lookup | None, errors: list[str], warnings: list[str]) -> None:
    folders: set[str] = set()
    for index, repo in enumerate(config.repositories):
        where = f"repositories[{index}]"
        if repo.name in folders:
            errors.append(f"{where}.name: duplicate repository name {repo.name!r}")
        folders.add(repo.name)
        if repo.push and not repo.credential:
            errors.append(f"{where}.push: needs a credential")
        if repo.credential and not SECRET_REF.search(repo.credential):
            warnings.append(f"{where}.credential: written in the file; use ${{secret:NAME}}")

    _check_image("workstation", config.workstation, config_dir, lookup, errors)
    seen_caches: set[str] = set()
    for index, entry in enumerate(config.workstation.cache):
        where = f"workstation.cache[{index}]"
        problem = cache_problem(entry)
        if problem:
            errors.append(f"{where}: {problem}")
            continue
        normal = posixpath.normpath("/home/runner" + entry[1:] if entry.startswith("~") else entry)
        if normal in seen_caches:
            errors.append(f"{where}: {entry!r} is listed twice")
        seen_caches.add(normal)
    default_workstation = not (config.workstation.image or config.workstation.dockerfile)

    for name, service in config.services.items():
        where = f"services.{name}"
        if not re.match(NAME_PATTERN, name) or len(name) > 63:
            errors.append(f"{where}: service names must match {NAME_PATTERN}")
        elif name in RESERVED_SERVICE_NAMES or name.startswith("exradius"):
            errors.append(f"{where}: the name {name!r} is reserved")
        elif name in folders:
            errors.append(f"{where}: the name {name!r} is also a repository folder")
        if not (service.image or service.dockerfile):
            errors.append(f"{where}: needs image or dockerfile")
        _check_image(where, service, config_dir, lookup, errors)
        if len(set(service.ports)) != len(service.ports):
            errors.append(f"{where}.ports: ports must be unique")
        if service.ready is not None and (service.ready.command is None) == (service.ready.tcp is None):
            errors.append(f"{where}.ready: give exactly one of command or tcp")

    for name, conn in config.connections.items():
        where = f"connections.{name}"
        if not re.match(NAME_PATTERN, name) or len(name) > 63:
            errors.append(f"{where}: connection names must match {NAME_PATTERN}")
            continue
        if name == RESERVED_CONNECTION:
            errors.append(f"{where}: the name {name!r} is reserved for the built-in connection")
            continue
        kinds = [k for k in ("catalog", "command", "url") if getattr(conn, k) is not None]
        if len(kinds) != 1:
            errors.append(f"{where}: give exactly one of catalog, command or url")
            continue
        allowed = {"catalog": {"config"}, "command": {"args", "env"}, "url": {"headers"}}[kinds[0]]
        for key in ("config", "args", "env", "headers"):
            if getattr(conn, key) is not None and key not in allowed:
                errors.append(f"{where}.{key}: not used with {kinds[0]}")
        if conn.catalog is not None:
            item = catalog.entry(conn.catalog)
            if item is None:
                errors.append(f"{where}.catalog: unknown catalog entry {conn.catalog!r} "
                              f"(known: {', '.join(catalog.ENTRIES)})")
                continue
            for key, message in catalog.config_errors(item, dict(conn.config or {})):
                errors.append(f"{where}.config.{key}: {message}")
            launcher = item.requires
        else:
            launcher = _uses_launcher(conn.command)
        if launcher and default_workstation:
            warnings.append(f"{where}: needs {launcher}, which the default workstation image does not have")

    for slug, agent in config.agents.items():
        where = f"agents.{slug}"
        if not re.match(NAME_PATTERN, slug) or len(slug) > 63:
            errors.append(f"{where}: agent names must match {NAME_PATTERN}")
        seen: set[str] = set()
        for index, name in enumerate(agent.connections):
            if name in seen:
                errors.append(f"{where}.connections[{index}]: {name!r} is listed twice")
            elif name not in config.connections:
                errors.append(f"{where}.connections[{index}]: no connection named {name!r}")
            seen.add(name)


def _yaml_error(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        mark = exc.problem_mark
        return f"line {mark.line + 1}, column {mark.column + 1}: {exc.problem}"
    return str(exc)


def parse_config(text: str, *, config_dir: str = "", lookup: Lookup | None = None) -> ParseResult:
    """Parse and validate the file. ``config_dir`` is the file's directory in the
    repository (``""`` or ``.exradius``); ``lookup`` checks Dockerfile and context paths
    (skipped when ``None``)."""
    if len(text.encode()) > MAX_FILE_SIZE:
        return ParseResult(None, [f"(file): larger than {MAX_FILE_SIZE} bytes"])
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        return ParseResult(None, [f"(file): not valid YAML: {_yaml_error(exc)}"])
    if not isinstance(raw, dict):
        return ParseResult(None, ["(file): must be a mapping with version, project and agents"])

    errors: list[str] = []
    warnings: list[str] = []
    required = _scan_secrets(raw, errors)
    store = raw.get("secret_store") if isinstance(raw.get("secret_store"), str) else None
    try:
        config: ProjectConfig | None = ProjectConfig.model_validate(raw)
    except ValidationError as exc:
        errors += _pydantic_errors(exc)
        config = None
    if config is not None:
        _check(config, config_dir, lookup, errors, warnings)
        if required and not config.secret_store:
            errors.append("secret_store: required, because the file references secrets ("
                          + ", ".join(required) + ")")
    if config is None or errors:
        return ParseResult(None, errors, warnings, required, store)
    for item in (config.workstation, *config.services.values()):
        if item.dockerfile:
            paths = build_paths(config_dir, item)
            item.dockerfile, item.context = paths.dockerfile, paths.context
    return ParseResult(config, errors, warnings, required, store)
