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


# -- the check's report (no Docker) -------------------------------------------------------------


def test_failure_lines_are_found_above_the_tail_once_with_log_numbers(check):
    rows = ["ok"] * 10 + ["--- FAIL: TestA", "\x1b[31mFAILED\x1b[0m test_b", "--- FAIL: TestA"] + ["x"] * 5 + ["Error: last"]
    numbers = [100 + i for i in range(len(rows))]
    found = check.failure_lines("\n".join(rows), numbers, tail_lines=3)
    assert found == [(110, "--- FAIL: TestA"), (111, "FAILED test_b")]
    # Lines already in the tail are not repeated.
    assert check.failure_lines("\n".join(rows), None, tail_lines=100) == []


def test_failure_lines_keep_the_first_and_the_last(check):
    rows = ["error: %d" % i for i in range(40)] + ["tail"]
    found = check.failure_lines("\n".join(rows), None, tail_lines=1, limit=15)
    assert len(found) == 16
    assert found[0] == (1, "error: 0") and found[5] == (None, "... (25 more)") and found[-1] == (40, "error: 39")


@pytest.mark.parametrize("marker", ["--- FAIL", "FAILED", "Failed!", "Caused by:", "[ERROR]", "Error:", "error:",
                                    "panic:", "Traceback", "npm ERR!", "ELIFECYCLE", "Killed", "signal 9"])
def test_each_failure_marker(check, marker):
    assert check.failure_lines("a %s b\nend" % marker, None, tail_lines=1) == [(1, "a %s b" % marker)]


def test_ansi_and_progress_bars_are_stripped(check):
    assert check.strip_ansi("\x1b[1;32mPASS\x1b[0m 3 tests\n10%\r50%\r100%") == "PASS 3 tests\n100%"
    assert check.last_lines("a\n\n\x1b[2Kb\n   \nc\n", 2) == ["b", "c"]
    assert check.last_lines("", 10) == []


@pytest.mark.parametrize(("command", "title"), [
    ("pytest -q", "pytest -q"),
    ("cd app &&\n  npm test", "cd app && ..."),
    ("x" * 150, "x" * 97 + "..."),
    ("\n\n  make check\n", "make check"),
])
def test_short_title(check, command, title):
    assert check.short_title(command) == title


@pytest.mark.parametrize(("size", "text"), [
    (512, "1 KiB"), (5 * 1024 ** 2, "5 MiB"), (1024 ** 3, "1.0 GiB"), (int(1.3 * 1024 ** 3), "1.3 GiB"),
])
def test_memory_text(check, size, text):
    assert check.memory_text(size) == text


GIB = 1024 ** 3


@pytest.mark.parametrize(("code", "declared", "host", "kills", "summary", "note"), [
    (0, "4g", 8 * GIB, 0, "", ""),
    (137, "4g", 8 * GIB, 1,
     "exited with 137: exceeded its declared memory (4g); raise resources.memory or lower the step's parallelism", ""),
    (137, "8g", int(7.7 * GIB), 1,
     "exited with 137: killed: Docker has 7.7 GiB, less than the declared 8g; give Docker more memory or check on a "
     "larger machine (a run's limit is the declared 8g)", ""),
    (137, None, 8 * GIB, 1,
     "exited with 137 (killed: usually out of memory; Docker has 8.0 GiB and resources.memory is not declared)", ""),
    (101, "4g", 8 * GIB, 1, "", "a process in this step was killed for memory (limit: resources.memory 4g)"),
    (0, "4g", 8 * GIB, 2, "", "a process in this step was killed for memory (limit: resources.memory 4g)"),
    (1, None, 8 * GIB, 1, "",
     "a process in this step was killed for memory (limit: Docker's 8.0 GiB; resources.memory is not declared)"),
    (1, "8g", int(7.7 * GIB), 1, "",
     "a process in this step was killed for memory (limit: Docker's 7.7 GiB, less than the declared 8g)"),
])
def test_memory_verdict(check, code, declared, host, kills, summary, note):
    assert check.memory_verdict(code, declared, host, kills) == (summary, note)


def test_memory_advice(check):
    assert check.memory_advice(GIB, "Setup: a", "4g") == ['highest peak 1.0 GiB in "Setup: a"; resources.memory 4g']
    tight = check.memory_advice(int(3.5 * GIB), "Setup: a", "4g")
    assert tight[1].startswith("warning: that leaves less than 1 GiB of resources.memory (4g)")
    assert tight[1].endswith("(5g)")
    assert check.memory_advice(int(1.3 * GIB), "s", None) == [
        'highest peak 1.3 GiB in "s"; resources.memory not declared',
        "declare resources.memory: the peak plus about 1 GB for the agent, at least 4g (4g)"]


def test_wrapper_report(check):
    marker = "@@exradius-check-abc@@"
    assert check.wrapper_report(["%s ws=1048576" % marker, "%s peak=2097152 oom_kill=1" % marker]) == {
        "ws": 1048576, "peak": 2097152, "oom_kill": 1}
    # cgroup v1: nothing to read, nothing reported.
    assert check.wrapper_report(["%s peak= oom_kill=" % marker]) == {}


@pytest.mark.parametrize(("host", "kind"), [
    ("172.25.0.3", "own"), ("127.0.0.1", "loopback"), ("10.1.2.3", "private"), ("[fd00::1]", "private"),
    ("feed.invalid", "fake"), ("db", "fake"), ("api.test", "fake"), ("x.example", "fake"), ("app.localhost", "fake"),
    ("pypi.org", ""), ("93.184.215.14", ""),
])
def test_host_remark(check, host, kind):
    assert check.host_remark(host, "172.25.0.3")[0] == kind


def test_refusal_text(check):
    reason = "172.25.0.3 is not on this task's network allowlist (network.allow)"
    assert check.refusal_text("172.25.0.3", reason, "172.25.0.3") == (
        "172.25.0.3: 172.25.0.3 is the run's own address: a request to it goes to the proxy, in a run too; use "
        "127.0.0.1 or localhost")
    assert "egress.allowPrivate" in check.refusal_text("10.0.0.5", "x", "172.25.0.3")
    assert check.refusal_text("feed.invalid", "r", None) == (
        "feed.invalid: r; not a real host: a test fixture or a script error")
    assert check.refusal_text("example.com", "r", None) == "example.com: r"
    assert check.proxy_error_line("repo.hex.pm", 3, "cannot resolve repo.hex.pm") == (
        "could not reach (proxy error): repo.hex.pm (3): cannot resolve repo.hex.pm")


@pytest.mark.parametrize("text", [
    "couldn't resolve github.com (#<NoIPsError>)", "TypeError: fetch failed", "getaddrinfo EAI_AGAIN x",
    "Temporary failure in name resolution", "Name or service not known", "No such host is known",
])
def test_direct_connection_hints(check, text):
    assert check.NO_NAME.search(text)


def test_host_fault(check):
    assert "EAGAIN" in check.host_fault(-1, "[Errno 35] Resource temporarily unavailable")
    assert "exit 125" in check.host_fault(125, "docker: Error response from daemon")
    assert "error getting credentials" in check.host_fault(1, "error getting credentials - err: exit status 1")
    assert check.host_fault(1, "Resource temporarily unavailable") == ""


@pytest.mark.parametrize(("size", "empty", "text"), [
    (0, True, "empty"), (4096, False, "4 KB"), (503_808, False, "504 KB"), (236_000_000, False, "236 MB"),
    (3_100_000_000, False, "3.1 GB"),
])
def test_cache_sizes(check, size, empty, text):
    assert check.size_text(size, empty) == text


def test_cache_summary_tells_small_from_empty(check, tmp_path):
    entries = [{"path": "~/.cache/a"}, {"path": "~/.cache/b"}]
    (tmp_path / check.cache_folder(0, "~/.cache/a")).mkdir()
    folder = tmp_path / check.cache_folder(1, "~/.cache/b")
    folder.mkdir()
    (folder / "f").write_bytes(b"x" * 300_000)
    text, warm = check.cache_summary(str(tmp_path), entries)
    assert text.startswith("~/.cache/a empty, ~/.cache/b ") and text.endswith(" KB")
    assert warm is True


def test_disk_text(check):
    sizes = {"work": 1_600_000_000, "home": 700_000_000, "tmp": 100_000_000}
    assert check.disk_text(sizes, 1_200_000_000) == "2.4 GB (/work 1.6 GB, home 700 MB, /tmp 100 MB); caches 1.2 GB"
    assert check.disk_text({"work": 4096, "home": 0, "tmp": 4096}) == "8 KB (/work 4 KB, home 0 KB, /tmp 4 KB)"


def test_log_path_and_pruning(check):
    when = 1_700_000_000
    path = check.default_log_path("demo", when, "/base")
    assert path.startswith("/base/logs/demo-") and path.endswith(".log")
    assert len(path.rsplit("/", 1)[1]) == len("demo-20231114-221320.log")
    names = ["demo-202311%02d-120000.log" % d for d in range(1, 26)] + ["demo-x-20231101-120000.log", "other.txt"]
    removed = check.logs_to_remove(names, "demo", keep=20)
    assert removed == ["demo-202311%02d-120000.log" % d for d in range(1, 6)]


def test_default_cache_dir(check, tmp_path):
    config = tmp_path / "exradius.yaml"
    config.write_text("version: 1\n")
    assert check.default_cache_dir(str(config), "demo", "/base") == ("/base/demo", False)
    (tmp_path / ".exradius-cache").mkdir()
    assert check.default_cache_dir(str(config), "demo", "/base") == (str(tmp_path / ".exradius-cache"), True)


@pytest.mark.parametrize(("first", "command"), [
    ("#!/usr/bin/env bash", "/usr/bin/env bash /x/probe.sh"),
    ("#!/bin/bash -eu\r", "/bin/bash -eu /x/probe.sh"),
    ("echo hi", "/bin/sh /x/probe.sh"),
    ("#!", "/bin/sh /x/probe.sh"),
])
def test_run_file_command(check, first, command):
    assert check.run_file_command(first + "\necho body\n", "/x/probe.sh") == command


def test_run_and_run_file_keep_their_order(check, tmp_path):
    script = tmp_path / "probe.sh"
    script.write_text("#!/bin/sh\n")
    args = check.parse_args(["--run", "a", "--run-file", str(script), "--run", "b"])
    assert args.runs == [("cmd", "a"), ("file", str(script)), ("cmd", "b")]
    assert args.run == ["a", "b"]
    with pytest.raises(SystemExit):
        check.parse_args(["--run-file", str(tmp_path / "missing.sh")])


def test_guess_slug(check, tmp_path):
    config = tmp_path / "My Config" / "exradius.yaml"
    config.parent.mkdir()
    config.write_text("version: 1\nproject:\n  slug: demo\n")
    assert check.guess_slug(str(config)) == "demo"
    config.write_text("not: [valid\n")
    assert check.guess_slug(str(config)) == "my-config"


def test_a_refused_local_port_is_not_a_tool_ignoring_the_proxy(check):
    refused = "curl: (7) Failed to connect to localhost port 8096 after 0 ms: Couldn't connect to server"
    assert check.NO_PROXY_USED.search(refused)
    assert check.LOCAL_TARGET.search(refused)
    assert check.LOCAL_TARGET.search("connect ECONNREFUSED 127.0.0.1:5432")
    assert not check.LOCAL_TARGET.search("curl: (6) Could not resolve host: jellyfin.org")
