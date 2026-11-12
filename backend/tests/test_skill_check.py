"""The setup skill's local check (skill/skills/exradius-project/scripts/exradius_check.py)
against the backend it imitates: the exradius.yaml schema, the catalog, the agent phase's
host list (exradius.network) and the egress proxy's decisions (egress/, docs/kubernetes.md
§11). The script is standard library only and not a package, so it is loaded by path.

The schema test walks ``ProjectConfig``: a key added to the backend fails it until the
kitchen sink below uses the key, and the sink then fails the script's validator until the
script knows the key too.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import types
import typing
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from exradius import catalog, network
from exradius.schemas.project_config import ProjectConfig, parse_config

SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "skills" / "exradius-project" / "scripts" / "exradius_check.py"


@pytest.fixture(scope="module")
def check() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("exradius_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["exradius_check"] = module
    spec.loader.exec_module(module)
    return module


def script_validate(check, text: str):
    """``(config, errors, required keys)`` from the script's own YAML reader and validator."""
    raw = check.load_yaml(text)
    config, errors, _warnings, required = check.validate(raw)
    return config, errors, required


# -- the schema ------------------------------------------------------------------------------

SINK_A = """\
version: 1
project:
  slug: sink
  name: Kitchen sink
  description: Every key of every model.
secret_store: main
resources:
  memory: 6g
repositories:
  - name: app
    url: https://github.com/acme/app.git
    branch: main
    credential: ${secret:APP_TOKEN}
    push: true
workstation:
  dockerfile: docker/Dockerfile
  context: .
  setup:
    - npm ci
  cache:
    - ~/.npm
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_PASSWORD: ${secret:DB_PASSWORD}
    ports: [5432]
    ready:
      command: [pg_isready]
    memory: 512m
  search:
    dockerfile: search/Dockerfile
    context: search
    command: [--single-node]
    ports: [9200]
    ready:
      tcp: 9200
env:
  NODE_ENV: test
  API_KEY: ${secret:API_KEY}
notes: |
  npm test runs the tests.
network:
  allow:
    - registry.npmjs.org
    - "*.pythonhosted.org"
connections:
  github:
    catalog: github
    config:
      token: ${secret:GITHUB_TOKEN}
  docs:
    command: npx
    args: [-y, docs-mcp]
    env:
      DOCS_TOKEN: ${secret:DOCS_TOKEN}
  tracker:
    url: https://mcp.example.net/mcp
    headers:
      Authorization: Bearer ${secret:TRACKER}
    type: http
agents:
  dev:
    name: Developer
    role: Writes code.
    model: claude-opus-5-5
    provider: anthropic
    runtime: claude_code
    identity:
      name: Dev Bot
      email: dev@example.com
    connections: [github, docs, tracker]
"""

SINK_B = """\
version: 1
project:
  slug: sink-b
workstation:
  image: ubuntu:24.04
agents:
  dev: {}
"""

SINKS = (SINK_A, SINK_B)


def _models(annotation) -> list[type[BaseModel]]:
    """The pydantic models inside an annotation (Optional, list, dict, Annotated)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _models(arg)]


def _reachable(model: type[BaseModel], seen: dict[str, type[BaseModel]]) -> dict[str, type[BaseModel]]:
    if model.__name__ in seen:
        return seen
    seen[model.__name__] = model
    for field in model.model_fields.values():
        for inner in _models(field.annotation):
            _reachable(inner, seen)
    return seen


def _covered(model: type[BaseModel], value, found: set[tuple[str, str]]) -> None:
    """Record ``(model, field)`` for every key of ``value`` read as ``model``."""
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key not in model.model_fields:
            continue
        found.add((model.__name__, key))
        annotation = model.model_fields[key].annotation
        inner = _models(annotation)
        if not inner:
            continue
        sub = inner[0]
        if isinstance(item, list):
            for element in item:
                _covered(sub, element, found)
        elif isinstance(item, dict) and _is_mapping_of_models(annotation):
            for element in item.values():
                _covered(sub, element, found)
        else:
            _covered(sub, item, found)


def _is_mapping_of_models(annotation) -> bool:
    origin = typing.get_origin(annotation)
    if origin is dict:
        return True
    return any(_is_mapping_of_models(arg) for arg in typing.get_args(annotation))


def test_the_kitchen_sink_uses_every_key_of_every_model():
    found: set[tuple[str, str]] = set()
    for sink in SINKS:
        _covered(ProjectConfig, yaml.safe_load(sink), found)
    missing = [f"{name}.{field}" for name, model in _reachable(ProjectConfig, {}).items()
               for field in model.model_fields if (name, field) not in found]
    assert not missing, f"add these keys to the kitchen sink (and to the script's validator): {missing}"


@pytest.mark.parametrize("sink", SINKS, ids=["a", "b"])
def test_the_kitchen_sink_passes_both_validators(check, sink):
    backend = parse_config(sink)
    assert backend.errors == []
    config, errors, required = script_validate(check, sink)
    assert errors == []
    assert config is not None
    assert required == backend.required_keys


BASE = {
    "version": 1,
    "project": {"slug": "demo"},
    "secret_store": "main",
    "services": {"redis": {"image": "redis:7", "ports": [6379]}},
    "env": {"TOKEN": "${secret:TOKEN}"},
    "agents": {"dev": {}},
}


def _text(**changes) -> str:
    raw = json.loads(json.dumps(BASE))
    for key, value in changes.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return yaml.safe_dump(raw, default_flow_style=False, sort_keys=False)


ALLOW_CASES = [
    "pypi.org", "*.pythonhosted.org", "10.0.0.1", "PyPI.org.", " pypi.org ", "https://pypi.org/simple",
    "pypi.org/simple", "pypi.org:443", "*", "*.", "*.*.example.com", "foo*.example.com", "localhost",
    "*.localhost", "127.0.0.1", "*.10.0.0.1", "bad_host.com", "a..b", "-a.com", "a" * 64 + ".com",
    ".".join(["abcdefghij"] * 26), " ",
]

INVALID = {
    "unknown top-level key": _text(bogus=1),
    "network not a mapping": _text(network=["pypi.org"]),
    "network unknown key": _text(network={"deny": []}),
    "network.allow not a list": _text(network={"allow": "pypi.org"}),
    "network.allow non-string": _text(network={"allow": [1]}),
    "network.allow empty string": _text(network={"allow": [""]}),
    "duplicate allow entry": _text(network={"allow": ["pypi.org", "PYPI.org."]}),
    "two services on one port": _text(services={"a": {"image": "x", "ports": [80]}, "b": {"image": "y", "ports": [80]}}),
    "reserved service name": _text(services={"api": {"image": "x"}}),
    "reserved exradius service name": _text(services={"exradius-db": {"image": "x"}}),
    "secret in notes": _text(notes="${secret:TOKEN}"),
    "secret in setup": _text(workstation={"setup": ["echo ${secret:TOKEN}"]}),
    "memory without unit": _text(resources={"memory": 512}),
    "memory too small": _text(resources={"memory": "1m"}),
    "memory not a size": _text(resources={"memory": "lots"}),
    "service memory bad": _text(services={"redis": {"image": "redis:7", "memory": "2x"}}),
    "unknown catalog entry": _text(connections={"x": {"catalog": "nope"}}, agents={"dev": {"connections": ["x"]}}),
    "missing catalog key": _text(connections={"x": {"catalog": "slack", "config": {"bot_token": "t"}}}),
    "agent lists unknown connection": _text(agents={"dev": {"connections": ["ghost"]}}),
}
for _index, _entry in enumerate(ALLOW_CASES):
    INVALID[f"allow {_entry!r}"] = _text(network={"allow": [_entry]})


@pytest.mark.parametrize("name", list(INVALID))
def test_both_validators_agree(check, name):
    text = INVALID[name]
    backend = parse_config(text)
    _config, errors, _required = script_validate(check, text)
    assert bool(backend.errors) == bool(errors), (backend.errors, errors)
    if name.startswith("allow ") or name == "duplicate allow entry":
        ours = [e for e in errors if e.startswith("network.")]
        theirs = [e for e in backend.errors if e.startswith("network.")]
        assert ours == theirs


def test_the_named_invalid_cases_are_invalid(check):
    for name in ("unknown top-level key", "duplicate allow entry", "two services on one port", "reserved service name",
                 "secret in notes", "memory without unit", "memory too small"):
        assert parse_config(INVALID[name]).errors, name


def test_memory_messages_match(check):
    for name in ("memory without unit", "memory too small"):
        backend = parse_config(INVALID[name]).errors
        _config, errors, _required = script_validate(check, INVALID[name])
        assert errors == backend


def test_host_problem_is_a_port(check):
    from exradius.schemas.project_config import host_problem

    for entry in ALLOW_CASES:
        assert check.host_problem(entry) == host_problem(entry), entry


# -- the catalog -------------------------------------------------------------------------------


def test_the_catalog_matches_the_backends(check):
    assert list(check.CATALOG) == list(catalog.ENTRIES)
    assert check.LAUNCHERS == catalog.LAUNCHERS
    for key, item in catalog.ENTRIES.items():
        ours = check.CATALOG[key]
        assert ours["transport"] == item.transport, key
        assert ours["command"] == item.command, key
        assert ours["url"] == item.url, key
        assert ours["requires"] == item.requires, key
        assert tuple(ours["hosts"]) == tuple(item.hosts), key
        theirs = [
            {"key": k.key, "label": k.label, "target": k.target, "name": k.name, "required": k.required,
             "format": k.format, "default": k.default, "host": k.host}
            for k in item.config
        ]
        assert list(ours["config"]) == theirs, key


# -- the agent phase's hosts -------------------------------------------------------------------

HOSTS_CONFIGS = {
    "sink": SINK_A,
    "none": SINK_B,
    "connections": """\
version: 1
project: {slug: hosts}
secret_store: main
network:
  allow: [registry.npmjs.org, PyPI.org, "*.example.org"]
connections:
  gitlab:
    catalog: gitlab
    config: {token: "${secret:GL}"}
  gitlab-own:
    catalog: gitlab
    config: {token: "${secret:GL}", api_url: "https://git.acme.io/api/v4"}
  gitlab-secret-url:
    catalog: gitlab
    config: {token: t, api_url: "${secret:GL_URL}"}
  jira:
    catalog: jira
    config: {url: "https://acme.atlassian.net", username: u, api_token: "${secret:J}"}
  sentry:
    catalog: sentry
    config: {token: "${secret:S}"}
  linear-plain:
    catalog: linear
    config: {api_key: written-in-the-file}
  linear:
    catalog: linear
    config: {api_key: "${secret:L}"}
  plain-http:
    url: https://mcp.plain.dev/mcp
    headers: {X-Team: core}
  secret-http:
    url: https://mcp.secret.dev/mcp
    headers: {Authorization: "Bearer ${secret:H}"}
  unused:
    catalog: playwright
  local:
    command: python3
    args: [-m, server]
agents:
  dev:
    connections: [plain-http, gitlab, jira, sentry, linear-plain, linear]
  ops:
    connections: [gitlab-own, gitlab-secret-url, secret-http, local, jira]
""",
}


@pytest.mark.parametrize("name", list(HOSTS_CONFIGS))
@pytest.mark.parametrize("install", [(), ("proxy.corp.example", "pypi.org")])
def test_the_agent_phase_hosts_match_the_backends(check, name, install):
    text = HOSTS_CONFIGS[name]
    backend = parse_config(text)
    assert backend.errors == [], backend.errors
    config, errors, _required = script_validate(check, text)
    assert errors == []
    assert check.allowed_hosts(config, list(install)) == network.allowed_hosts(backend.config, None, install)


# -- the egress proxy --------------------------------------------------------------------------

ALLOW = ["pypi.org", "*.pythonhosted.org"]


@pytest.mark.parametrize(
    ("phase", "host", "addresses", "private", "allowed", "reason", "via"),
    [
        ("setup", "example.com", ["93.184.215.14"], [], True, "", ""),
        ("agent", "example.com", ["93.184.215.14"], [], False,
         "example.com is not on this task's network allowlist (network.allow)", ""),
        ("agent", "pypi.org", ["151.101.0.223"], [], True, "", ""),
        ("agent", "PyPI.org.", ["151.101.0.223"], [], True, "", ""),
        ("agent", "files.pythonhosted.org", ["151.101.0.223"], [], True, "", ""),
        ("agent", "pythonhosted.org", ["151.101.0.223"], [], False,
         "pythonhosted.org is not on this task's network allowlist (network.allow)", ""),
        ("agent", "github.com", ["140.82.121.4"], [], True, "", "repository"),
        ("setup", "db.internal", ["10.0.0.5"], [], False,
         "db.internal resolves to a private or cluster-internal address", ""),
        ("setup", "mixed.example", ["10.0.0.5", "93.184.215.14"], [], True, "", ""),
        ("setup", "10.0.0.5", None, [], False, "10.0.0.5 is a private or cluster-internal address", ""),
        ("setup", "169.254.169.254", None, [], False,
         "169.254.169.254 is a private or cluster-internal address", ""),
        ("setup", "db.internal", ["fd00::1"], [], False,
         "db.internal resolves to a private or cluster-internal address", ""),
        ("setup", "db.internal", ["::ffff:10.0.0.5"], [], False,
         "db.internal resolves to a private or cluster-internal address", ""),
        ("setup", "db.internal", ["10.0.0.5"], ["10.0.0.0/8"], True, "", "allow-private"),
        ("setup", "db.internal", ["10.0.0.5"], ["db.internal"], True, "", "allow-private"),
        ("setup", "db.internal", ["10.0.0.5"], ["db.internal:5000"], False,
         "db.internal resolves to a private or cluster-internal address", ""),
        ("setup", "10.0.0.5", None, ["10.0.0.5"], True, "", "allow-private"),
        ("agent", "10.0.0.5", None, ["10.0.0.0/8"], False,
         "10.0.0.5 is not on this task's network allowlist (network.allow)", ""),
        ("setup", "nowhere.invalid", None, [], True, "cannot resolve nowhere.invalid", ""),
    ],
)
def test_the_proxys_decision(check, phase, host, addresses, private, allowed, reason, via):
    literal = addresses is None and host[0].isdigit()
    decision = check.egress_decide(phase, host, 443, None if literal else addresses, ALLOW, ["github.com"], private)
    assert (decision["allowed"], decision["reason"], decision["via"]) == (allowed, reason, via)


def test_private_allow_with_a_port(check):
    decision = check.egress_decide("setup", "db.internal", 5000, ["10.0.0.5"], (), (), ["db.internal:5000"])
    assert decision["allowed"] and decision["addresses"] == ["10.0.0.5"]


def _request(port: int, text: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(text.encode())
        data = b""
        while not data.endswith(b"\r\n\r\n") and b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        return data


@pytest.fixture
def proxy(check, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "phase").write_text("agent\n")
    (state / "policy.json").write_text(json.dumps({"allow": ["127.0.0.1"], "repositories": [],
                                                   "private": ["127.0.0.1"]}))
    server = check.EgressProxy(str(state), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, state
    server.shutdown()


def test_the_proxy_refuses_connect_in_the_agent_phase(proxy):
    server, state = proxy
    head = _request(server.port, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    reason = "example.com is not on this task's network allowlist (network.allow)"
    assert head.startswith(f"HTTP/1.1 403 {reason}\r\n".encode())
    records = [json.loads(line) for line in (state / "records.jsonl").read_text().splitlines()]
    assert records[-1]["phase"] == "agent"
    assert records[-1]["host"] == "example.com"
    assert records[-1]["allowed"] is False
    assert records[-1]["reason"] == reason


def test_the_proxy_tunnels_to_an_allowed_private_address(proxy):
    server, state = proxy
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    port = upstream.getsockname()[1]

    def echo():
        conn, _ = upstream.accept()
        with conn:
            conn.sendall(conn.recv(100).upper())

    threading.Thread(target=echo, daemon=True).start()
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as conn:
        conn.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        assert head.startswith(b"HTTP/1.1 200")
        conn.sendall(b"ping")
        assert conn.recv(100) == b"PING"
    upstream.close()
    record = json.loads((state / "records.jsonl").read_text().splitlines()[-1])
    assert record["allowed"] is True and record["via"] == "allow-private"


def test_the_proxy_refuses_a_private_address_in_setup(proxy):
    server, state = proxy
    (state / "phase").write_text("setup\n")
    (state / "policy.json").write_text(json.dumps({"allow": [], "repositories": [], "private": []}))
    head = _request(server.port, "GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403 127.0.0.1 is a private or cluster-internal address\r\n")
