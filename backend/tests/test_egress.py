"""The egress proxy's side of the api (docs/kubernetes.md §9–§11): its TokenReview-checked
routes on the internal port, the policies they give for task, verify and probe Runs, the
records they store, and the task page's summary of them. The proxy itself is tested in
egress/ (Go); the schema's ``network.allow`` and the network line elsewhere too."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from exradius.api.egress import EgressAuth
from exradius.crypto import hash_token
from exradius.models import EgressRecord, ProjectVerification, Provider, ProviderTest, Run, Task, utcnow
from exradius.runner import egress as runner_egress
from exradius.schemas.project_config import parse_config
from tests.test_api import API_CONFIG, admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401

PROXY = "system:serviceaccount:exradius:exradius-egress"


class FakeReviews:
    """The Kubernetes TokenReview, for the tokens it knows."""

    def __init__(self):
        self.users = {"egress-token": PROXY, "worker-token": "system:serviceaccount:exradius:exradius-worker"}
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, token: str, audiences: list[str]) -> dict:
        self.calls.append((token, audiences))
        if token not in self.users:
            return {"authenticated": False, "error": "invalid bearer token"}
        return {"authenticated": True, "user": {"username": self.users[token]}, "audiences": audiences}


@pytest.fixture
def reviews(client):
    fake = FakeReviews()
    state = client.app.state.exradius
    state.egress_auth = EgressAuth(fake, state.settings.egress_service_account, state.settings.egress_audience)
    return fake


EGRESS = {"Authorization": "Bearer egress-token"}

CONFIG = API_CONFIG.replace("connections:\n  docs:", """\
network:
  allow: [registry.npmjs.org, '*.pythonhosted.org']
connections:
  status: {url: 'https://status.example/mcp'}
  docs:""").replace("    connections: [docs, sentry]", "    connections: [docs, sentry, status]").replace(
    "    credential: ${secret:GIT_TOKEN}\n", "    credential: ${secret:GIT_TOKEN}\n    push: true\n"
    "  - {name: wiki, url: 'https://git.example/wiki.git'}\n")


def task_run(admin, sync_db, tmp_path, *, state="running") -> uuid.UUID:
    cfg = setup_configuration(admin, tmp_path, CONFIG)
    project = cfg["project"]

    provider = sync_db.query(Provider).one()
    sync_db.add(Task(id="JIRA-1", project_id=uuid.UUID(project["id"]), agent="bug-fixer", instructions="Fix it.",
                     state=state))
    run = Run(id=uuid.uuid4(), task_id="JIRA-1", state=state, token_hash=hash_token("tok"), config=project["config"],
              config_commit=project["commit"], provider_id=provider.id)
    sync_db.add(run)
    sync_db.commit()
    return run.id


@pytest.fixture
def provider(admin):
    response = admin.post("/api/providers", json={"slug": "anthropic", "name": "Anthropic", "kind": "anthropic", "auth_method": "api_key",
                                                  "credential": "sk-ant-real"})
    assert response.status_code == 201, response.text
    return response.json()


def test_only_the_proxys_token_is_accepted(client, reviews):
    path = f"/internal/egress/runs/run-{uuid.uuid4()}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    refused = client.get(path, headers={"Authorization": "Bearer worker-token"})
    assert refused.status_code == 403 and "is not the egress proxy" in refused.json()["detail"]
    assert client.get(path, headers=EGRESS).status_code == 404  # authenticated; no such run
    assert client.get(path, headers=EGRESS).status_code == 404
    # The review is asked once per token for the audience exradius-api, then remembered.
    assert [c for c in reviews.calls if c[0] == "egress-token"] == [("egress-token", ["exradius-api"])]
    # A run token is no egress token.
    assert client.post("/internal/egress/records", json={"records": []}, headers={"Authorization": "Bearer tok"}
                       ).status_code == 401


def test_a_task_runs_policy(client, admin, sync_db, tmp_path, reviews, provider):
    run_id = task_run(admin, sync_db, tmp_path)
    response = client.get(f"/internal/egress/runs/run-{run_id}", headers=EGRESS)
    assert response.status_code == 200, response.text
    policy = response.json()
    assert policy["run"] == f"run-{run_id}"
    # The model route with the provider's key as Claude Code sends it.
    assert policy["model"] == {"url": "https://api.anthropic.com", "headers": {"x-api-key": "sk-ant-real"}}
    basic = "Basic " + base64.b64encode(b"x-access-token:ghp_secret").decode()
    assert policy["repositories"] == [
        {"url": "https://git.example/api.git", "push": True, "headers": {"Authorization": basic}},
        {"url": "https://git.example/wiki.git", "push": False, "headers": {}},
    ]
    # Only the HTTP connection with a secret header has a route; the other one's host is allowed.
    assert policy["connections"] == [
        {"name": "docs", "url": "https://mcp.example/mcp", "headers": {"Authorization": "Bearer docs-key"}}]
    assert policy["allow"] == ["registry.npmjs.org", "*.pythonhosted.org", "status.example"]


def test_the_install_allow_list_comes_first(client, admin, sync_db, tmp_path, reviews, provider):
    client.app.state.exradius.settings.egress_allow = "artifacts.corp.example, pypi.org"
    run_id = task_run(admin, sync_db, tmp_path)
    policy = client.get(f"/internal/egress/runs/run-{run_id}", headers=EGRESS).json()
    assert policy["allow"][:3] == ["artifacts.corp.example", "pypi.org", "registry.npmjs.org"]
    spec = client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).json()
    # The agent is told the same list the proxy enforces.
    assert ("Outbound network access is limited to the repositories above and to: artifacts.corp.example, pypi.org, "
            "registry.npmjs.org, *.pythonhosted.org, status.example.") in spec["environment_text"]


def test_no_policy_for_runs_that_are_not_active(client, admin, sync_db, tmp_path, reviews, provider):
    run_id = task_run(admin, sync_db, tmp_path)
    run = sync_db.get(Run, run_id)
    run.ended_at = utcnow()
    sync_db.commit()
    assert client.get(f"/internal/egress/runs/run-{run_id}", headers=EGRESS).status_code == 404
    for name in ("run-nope", "build-x", f"verify-{uuid.uuid4()}", f"probe-{uuid.uuid4()}"):
        assert client.get(f"/internal/egress/runs/{name}", headers=EGRESS).status_code == 404


def test_a_verify_runs_policy_has_no_model(client, admin, sync_db, tmp_path, reviews):
    project = setup_configuration(admin, tmp_path, CONFIG)["project"]
    verification = ProjectVerification(project_id=uuid.UUID(project["id"]), state="running", steps=[])
    sync_db.add(verification)
    sync_db.commit()
    policy = client.get(f"/internal/egress/runs/verify-{verification.id}", headers=EGRESS).json()
    assert policy["model"] is None and len(policy["repositories"]) == 2
    assert [c["name"] for c in policy["connections"]] == ["docs"]
    assert "status.example" in policy["allow"]


def test_a_probes_policy_is_its_providers_model_route(client, sync_db, reviews, admin):
    response = admin.post("/api/providers", json={"slug": "sub", "name": "Sub", "kind": "anthropic", "auth_method": "oauth_token",
                                                  "credential": "oat-real"})
    test = ProviderTest(provider_id=uuid.UUID(response.json()["id"]), state="running", fingerprint="f")
    sync_db.add(test)
    sync_db.commit()
    policy = client.get(f"/internal/egress/runs/probe-{test.id}", headers=EGRESS).json()
    assert policy["model"] == {"url": "https://api.anthropic.com", "headers": {"Authorization": "Bearer oat-real"}}
    assert policy["repositories"] == [] and policy["allow"] == []


def test_records_and_the_task_network_summary(client, admin, sync_db, tmp_path, reviews, provider):
    run_id = task_run(admin, sync_db, tmp_path)
    at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    def record(**fields):
        return {"run": f"run-{run_id}", "phase": "agent", "route": "connect", "host": "registry.npmjs.org",
                "port": 443, "allowed": True, "status": 200, "bytes_sent": 10, "bytes_received": 90,
                "duration_ms": 5, "at": at.isoformat(), **fields}

    batch = [
        record(phase="setup", host="github.com"),
        record(),
        record(at=datetime(2026, 11, 10, 12, 5, tzinfo=UTC).isoformat()),
        record(host="example.com", allowed=False, status=None, bytes_sent=0, bytes_received=0,
               reason="example.com is not on this task's network allowlist (network.allow)"),
        record(route="model", host="api.anthropic.com", method="POST", path="/v1/messages"),
        record(run="build-saleor-x"),  # not a run: skipped
    ]
    response = client.post("/internal/egress/records", json={"records": batch}, headers=EGRESS)
    assert response.status_code == 204, response.text
    rows = sync_db.query(EgressRecord).order_by(EgressRecord.id).all()
    assert len(rows) == 5 and rows[0].kind == "task" and rows[0].object_id == run_id and rows[0].phase == "setup"

    summary = admin.get("/api/tasks/JIRA-1/network").json()
    assert summary["requests"] == 5
    by_host = {h["host"]: h for h in summary["reached"]}
    assert list(by_host) == ["registry.npmjs.org", "api.anthropic.com", "github.com"]  # by requests, then name
    npm = by_host["registry.npmjs.org"]
    assert (npm["requests"], npm["bytes"], npm["phases"], npm["routes"], npm["ports"]) == (2, 200, ["agent"], ["connect"], [443])
    assert npm["last_at"].startswith("2026-11-10T12:05")
    assert by_host["api.anthropic.com"]["routes"] == ["model"]
    [refused] = summary["refused"]
    assert refused["host"] == "example.com" and "network allowlist" in refused["reason"] and refused["phases"] == ["agent"]
    assert admin.get("/api/tasks/nope/network").status_code == 404


def test_the_internal_port_only(settings, store, control):
    """/internal/egress/* is served on the internal port and refused on the api port."""
    from fastapi.testclient import TestClient

    from exradius.api.app import create_app

    app = create_app(settings, store=store, broker=control)
    with TestClient(app, base_url=f"http://testserver:{settings.api_port}") as api:
        assert api.get("/internal/egress/runs/run-x").status_code == 404
        assert api.get("/internal/egress/runs/run-x").json() == {"detail": "Not Found"}


# The schema's network.allow ------------------------------------------------------------------


def config_with(allow: str):
    return parse_config(f"version: 1\nproject: {{slug: p}}\nnetwork:\n  allow: {allow}\nagents:\n  a: {{}}\n")


def test_network_allow_is_validated_strictly():
    assert config_with("[pypi.org, '*.pythonhosted.org', 10.20.0.5, Registry.NPMjs.org]").errors == []
    errors = config_with("[https://pypi.org, 'pypi.org:443', '*', 'a*.b.com', localhost, pypi.org, PYPI.org]").errors
    assert errors == [
        "network.allow[0]: write a host name, not a URL (pypi.org, not https://pypi.org/simple)",
        "network.allow[1]: write the host without a port: every port of an allowed host is allowed",
        "network.allow[2]: * stands for the subdomains of a name: *.example.com, never every host",
        "network.allow[3]: * is allowed only as a leading *. (*.example.com)",
        "network.allow[4]: the run's own services are on localhost and need no entry",
        "network.allow[6]: 'PYPI.org' is listed twice",
    ]
    assert parse_config("version: 1\nproject: {slug: p}\nnetwork: {hosts: [x]}\nagents:\n  a: {}\n").errors == [
        "network.hosts: unknown key"]


# The runner's wait for the agent phase ---------------------------------------------------------


async def test_the_runner_waits_for_every_proxy_to_see_the_agent_phase(monkeypatch):
    answers = iter(["setup", "setup", "agent"])
    asked = []

    async def addresses(host, port):
        return ["10.0.0.1", "10.0.0.2"]

    class Response:
        status_code = 200

        def __init__(self, phase):
            self.phase = phase

        def json(self):
            return {"phase": self.phase}

    async def get(self, url):
        asked.append(url)
        return Response("agent" if url.startswith("http://10.0.0.2") else next(answers))

    monkeypatch.setattr(runner_egress, "_addresses", addresses)
    monkeypatch.setattr(runner_egress.httpx.AsyncClient, "get", get)
    await runner_egress.wait_for_agent_phase({"EXRADIUS_EGRESS_URL": "http://egress.ns:3129",
                                              "EXRADIUS_EGRESS_PEERS": "egress-pods.ns"}, poll=0)
    assert asked[0] == "http://10.0.0.1:3129/phase" and asked[-1] == "http://10.0.0.2:3129/phase"
    assert len(asked) == 6  # three rounds of two pods

    async def never(self, url):
        return Response("setup")

    monkeypatch.setattr(runner_egress.httpx.AsyncClient, "get", never)
    with pytest.raises(runner_egress.EgressNotReady, match="did not see this run's agent phase"):
        await runner_egress.wait_for_agent_phase({"EXRADIUS_EGRESS_URL": "http://egress.ns:3129"}, timeout=0.05,
                                                 poll=0.01)
    await runner_egress.wait_for_agent_phase({})  # outside a cluster: nothing to wait for


def test_isolate_keeps_the_proxy_routes_from_the_agent():
    from exradius.runner.environment import isolate

    environ = {"EXRADIUS_EGRESS_URL": "http://egress:3129", "EXRADIUS_EGRESS_PEERS": "pods", "HTTPS_PROXY": "http://p:3128"}
    environment = isolate(environ)
    assert environment.egress == {"EXRADIUS_EGRESS_URL": "http://egress:3129", "EXRADIUS_EGRESS_PEERS": "pods"}
    assert environment.container == {"HTTPS_PROXY": "http://p:3128"}  # the agent's tools use the proxy itself
    assert json.dumps(environment.container).count("egress") == 0


# Verify's diagnoses ---------------------------------------------------------------------------


def test_network_diagnoses():
    from exradius.worker.diagnose import diagnose

    tools = diagnose("List tools docs", "McpError: CONNECT tunnel failed: docs.example is not on this task's network "
                                        "allowlist (network.allow)")
    assert tools == ("The List tools docs step reached `docs.example`, which the agent phase does not allow: add it "
                     "to `network.allow` in the config if the agent needs it.")
    private = diagnose("Setup: pip install -r requirements.txt",
                       "ProxyError: 403 nexus.corp resolves to a private or cluster-internal address")
    assert "values `egress.allowPrivate`" in private and "`nexus.corp`" in private
    maven = diagnose("Setup: mvn -q dependency:go-offline", "[ERROR] Could not transfer artifact x from/to central "
                     "(https://repo.maven.apache.org/maven2): Connect to repo.maven.apache.org:443 [repo.maven.apache.org/"
                     "104.18.18.12] failed: Connect timed out")
    assert maven.startswith("A connection timed out") and "settings.xml" in maven
    assert diagnose("Setup: make", "make: *** [all] Error 2") is None
