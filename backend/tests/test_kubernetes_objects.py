"""The worker's side of the Kubernetes contract: Run objects and per-run Secrets
(worker/run_objects.py), reading Run status, the API client (worker/kube.py), settings, and
the diagnosis rules for Kubernetes reasons."""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest

from exradius.config import Settings
from exradius.resolve import StoreValues
from exradius.schemas.project_config import parse_config
from exradius.worker import run_objects
from exradius.worker.diagnose import diagnose
from exradius.worker.kube import Gone, KubeCluster, KubeError
from exradius.worker.run_objects import RunView, parse_name, run_name
from exradius.worker.runs import runner_error, step_error

CONFIG = """\
version: 1
project: {slug: shop}
secret_store: shop
repositories:
  - {name: web, url: 'https://git.example/acme/web.git', credential: '${secret:GIT}'}
workstation: {image: 'node:22', setup: [npm ci]}
resources: {memory: 512m}
env:
  PLAIN: "1"
  API_URL: https://user:${secret:API_PASSWORD}@api.example
agents:
  dev: {connections: [docs]}
connections:
  docs: {url: 'https://mcp.example/mcp', headers: {Authorization: 'Bearer ${secret:DOCS}'}}
"""
SECRETS = StoreValues(store="shop", values={"GIT": "user:pw", "API_PASSWORD": "hunter2", "DOCS": "d"})


def config():
    return parse_config(CONFIG).config


# Names -----------------------------------------------------------------------------------------


def test_names_round_trip():
    object_id = uuid.uuid4()
    for purpose, prefix in (("task", "run-"), ("verify", "verify-"), ("probe", "probe-")):
        name = run_name(purpose, object_id)
        assert name == f"{prefix}{object_id}" and parse_name(name) == (purpose, object_id)
    assert parse_name("run-not-a-uuid") is None and parse_name("something-else") is None


# Verify and probe Runs ---------------------------------------------------------------------------


def test_verify_run():
    vid = uuid.uuid4()
    objects = run_objects.verify_run(verification_id=vid, namespace="exradius-runs", project="shop", config=config(),
                                     secrets=SECRETS, runtime="claude_code", runtime_image="bundle:1",
                                     workstation_image="node:22", token="tok")
    run = objects.run
    assert objects.name == f"verify-{vid}" and run["kind"] == "Run" and run["apiVersion"] == "exradius.dev/v1alpha1"
    assert run["metadata"]["labels"] == {"exradius.dev/run": objects.name, "exradius.dev/purpose": "verify",
                                         "exradius.dev/project": "shop"}
    assert "annotations" not in run["metadata"]
    spec = run["spec"]
    assert (spec["purpose"], spec["id"], spec["project"], spec["secretName"]) == ("verify", str(vid), "shop", objects.name)
    assert "taskId" not in spec
    assert spec["runtime"] == {"name": "claude_code", "image": "bundle:1", "env": []}
    assert spec["workstation"] == {"image": "node:22", "setup": ["npm ci"], "memory": "512Mi", "env": [
        {"name": "PLAIN", "value": "1"}, {"name": "API_URL", "secretKey": "env.API_URL"}]}
    assert spec["repositories"] == [{"folder": "web", "url": "https://git.example/acme/web.git", "push": False,
                                     "credentialKey": "repo.0"}]
    assert spec["git"]["env"] == spec["git"]["setupEnv"]  # no agent identity in a verify pod
    assert objects.secret == {"token": "tok", "env.API_URL": "https://user:hunter2@api.example", "repo.0": "user:pw"}
    assert "hunter2" not in json.dumps(run) and "user:pw" not in json.dumps(run)


def test_probe_run():
    tid = uuid.uuid4()
    objects = run_objects.probe_run(test_id=tid, namespace="exradius-runs", runtime="claude_code",
                                    runtime_image="bundle:1", image="default:1",
                                    provider_env={"CLAUDE_CODE_OAUTH_TOKEN": "oat"}, model=None, token="tok")
    spec = objects.run["spec"]
    assert objects.name == f"probe-{tid}" and "project" not in spec and "model" not in spec
    assert objects.run["metadata"]["labels"] == {"exradius.dev/run": objects.name, "exradius.dev/purpose": "probe"}
    assert spec["runtime"]["env"] == [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "secretKey": "provider.CLAUDE_CODE_OAUTH_TOKEN"}]
    assert spec["workstation"] == {"image": "default:1", "setup": [], "env": []}
    assert objects.secret == {"token": "tok", "provider.CLAUDE_CODE_OAUTH_TOKEN": "oat"}


def test_secret_body_is_owned_by_the_run_and_base64():
    objects = run_objects.probe_run(test_id=uuid.uuid4(), namespace="ns", runtime="claude_code", runtime_image="b",
                                    image="i", provider_env={"ANTHROPIC_API_KEY": "sk-ü"}, model="m", token="tok")
    created = {"metadata": {"name": objects.name, "uid": "uid-1"}}
    body = objects.secret_body(created, "ns")
    assert body["metadata"]["ownerReferences"] == [
        {"apiVersion": "exradius.dev/v1alpha1", "kind": "Run", "name": objects.name, "uid": "uid-1"}]
    assert body["metadata"]["namespace"] == "ns" and body["type"] == "Opaque"
    assert body["metadata"]["labels"]["exradius.dev/purpose"] == "probe"
    assert {k: base64.b64decode(v).decode() for k, v in body["data"].items()} == {
        "token": "tok", "provider.ANTHROPIC_API_KEY": "sk-ü"}


def test_project_env_drops_provider_variables_and_git_config():
    cfg = parse_config(CONFIG.replace('  PLAIN: "1"\n', '  PLAIN: "1"\n  ANTHROPIC_API_KEY: sk-mine\n  GIT_CONFIG_COUNT: "9"\n')).config
    data: dict[str, str] = {}
    env = run_objects.project_env(cfg, SECRETS, data, drop=frozenset({"ANTHROPIC_API_KEY"}))
    assert [e["name"] for e in env] == ["PLAIN", "API_URL"]
    assert data == {"env.API_URL": "https://user:hunter2@api.example"}


# Reading status ------------------------------------------------------------------------------------


def view(spec_generation=0, **status) -> RunView:
    return RunView({"spec": {"podGeneration": spec_generation}, "status": status})


def test_run_view():
    assert not RunView(None).exists and RunView(None).phase == "Pending"
    assert not view().current and not view().final  # the operator has not written status yet
    running = view(phase="Running", podGeneration=0)
    assert running.current and not running.final
    assert view(phase="Succeeded", podGeneration=0).final and view(phase="Failed", podGeneration=0).final
    old = view(spec_generation=1, phase="Failed", podGeneration=0)  # a bump the operator has not acted on
    assert not old.current and not old.final
    pending = view(phase="Pending", conditions=[{"type": "Scheduled", "status": "False", "reason": "Unschedulable",
                                                 "message": "0/1 nodes are available"}])
    assert pending.unschedulable == "0/1 nodes are available"
    assert view(phase="Pending").unschedulable is None and view(phase="Running", conditions=pending.status["conditions"]).unschedulable is None
    failed = view(phase="Failed", steps=[{"name": "a", "state": "passed"}, {"name": "b", "state": "failed"}])
    assert failed.failed_step()["name"] == "b"


def test_errors_from_status():
    cfg = config()
    image = view(phase="Failed", reason="ImagePullFailed", message='image "node:22" could not be pulled')
    assert step_error(image, cfg) == 'image "node:22" could not be pulled'
    prepare = view(phase="Failed", steps=[{"name": "Prepare workspace", "state": "failed", "reason": "Error",
                                           "log": "403 Forbidden"}])
    assert step_error(prepare, cfg) == "Prepare workspace failed (Error):\n403 Forbidden"
    oom = view(phase="Failed", workstation={"state": "terminated", "exitCode": 137, "reason": "OOMKilled", "log": "x"})
    assert runner_error(oom, cfg) == "the run exceeded its declared memory (512m) and was killed (exit 137):\nx"
    lost = view(phase="Failed", reason="PodLost", message="the run's pod was deleted")
    assert runner_error(lost, cfg) == "the run's pod was lost: the run's pod was deleted"
    evicted = view(phase="Failed", reason="Evicted", message="The node was low on resource: ephemeral-storage.")
    assert runner_error(evicted, cfg) == "The node was low on resource: ephemeral-storage."


# Settings ------------------------------------------------------------------------------------------


def test_cloudnativepg_uri_means_asyncpg():
    assert Settings(database_url="postgresql://app:pw@db-rw:5432/exradius").database_url == \
        "postgresql+asyncpg://app:pw@db-rw:5432/exradius"
    assert Settings(database_url="postgres://a@b/c").database_url == "postgresql+asyncpg://a@b/c"
    assert Settings(database_url="sqlite+aiosqlite:///x").database_url == "sqlite+aiosqlite:///x"


def test_broker_credentials_from_their_own_variables():
    plain = Settings(broker_url="amqp://guest:guest@rabbit:5672/")
    assert plain.effective_broker_url == "amqp://guest:guest@rabbit:5672/"
    split = Settings(broker_url="amqp://exradius-rabbitmq.exradius:5672/", broker_username="default_user_x",
                     broker_password="p/a:s@s+=")
    assert split.effective_broker_url == "amqp://default_user_x:p%2Fa%3As%40s%2B%3D@exradius-rabbitmq.exradius:5672/"
    only_password = Settings(broker_url="amqp://user@rabbit/vhost", broker_password="x y")
    assert only_password.effective_broker_url == "amqp://user:x%20y@rabbit/vhost"
    assert Settings(broker_username="", broker_password="").effective_broker_url == Settings().broker_url


def test_kubernetes_settings_defaults():
    settings = Settings()
    assert (settings.namespace, settings.runs_namespace, settings.operator_lease) == (
        "exradius", "exradius-runs", "exradius-operator")
    assert settings.api_port == 8000 and settings.internal_port == 8001
    assert settings.kube_api_url is None and Settings(kube_api_url="").kube_api_url is None


# The API client --------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    requests: list[httpx.Request] = []
    responses: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        key = (request.method, request.url.path)
        result = responses.get(key)
        if callable(result):
            return result(request)
        return result or httpx.Response(404, json={"message": "not found"})

    cluster = KubeCluster("exradius-runs", api_url="https://k8s.example", token_file=token,
                          ca_file=tmp_path / "missing.crt")
    cluster._client = httpx.AsyncClient(base_url=cluster.api_url, transport=httpx.MockTransport(handler))
    return cluster, requests, responses


RUNS = "/apis/exradius.dev/v1alpha1/namespaces/exradius-runs/runs"


async def test_client_creates_gets_patches_and_deletes(api):
    cluster, requests, responses = api
    obj = {"metadata": {"name": "run-1", "uid": "u"}, "spec": {}}
    responses[("POST", RUNS)] = httpx.Response(201, json=obj)
    assert await cluster.create_run({"metadata": {"name": "run-1"}}) == obj
    assert requests[-1].headers["authorization"] == "Bearer sa-token"

    responses[("POST", RUNS)] = httpx.Response(409, json={"message": "already exists"})
    responses[("GET", f"{RUNS}/run-1")] = httpx.Response(200, json=obj)
    assert await cluster.create_run({"metadata": {"name": "run-1"}}) == obj  # created by an earlier attempt

    responses[("PATCH", f"{RUNS}/run-1")] = lambda r: httpx.Response(200, json={"spec": json.loads(r.content)["spec"]})
    assert await cluster.patch_run("run-1", {"cancel": True}) == {"spec": {"cancel": True}}
    assert requests[-1].headers["content-type"] == "application/merge-patch+json"
    assert await cluster.patch_run("gone", {"cancel": True}) is None

    responses[("DELETE", f"{RUNS}/run-1")] = httpx.Response(200, json={})
    assert await cluster.delete_run("run-1") is True
    assert json.loads(requests[-1].content) == {"propagationPolicy": "Background"}
    assert await cluster.delete_run("gone") is False
    assert await cluster.get_run("gone") is None

    responses[("GET", RUNS)] = httpx.Response(200, json={"items": [obj], "metadata": {"resourceVersion": "42"}})
    assert await cluster.list_runs() == ([obj], "42")

    responses[("GET", f"{RUNS}/broken")] = httpx.Response(403, json={"message": "forbidden"})
    with pytest.raises(KubeError, match="403: forbidden"):
        await cluster.get_run("broken")


async def test_client_secrets_and_lease(api):
    cluster, requests, responses = api
    secrets = "/api/v1/namespaces/exradius-runs/secrets"
    body = {"metadata": {"name": "run-1"}, "data": {}}
    responses[("POST", secrets)] = httpx.Response(409, json={"message": "exists"})
    responses[("PATCH", f"{secrets}/run-1")] = httpx.Response(200, json=body)
    await cluster.create_secret(body)  # a leftover of an earlier attempt is replaced
    assert [r.method for r in requests[-2:]] == ["POST", "PATCH"]
    responses[("DELETE", f"{secrets}/run-1")] = httpx.Response(200, json={})
    await cluster.delete_secret("run-1")
    await cluster.delete_secret("gone")  # 404 is fine

    lease = {"spec": {"holderIdentity": "op"}}
    responses[("GET", "/apis/coordination.k8s.io/v1/namespaces/exradius/leases/exradius-operator")] = \
        httpx.Response(200, json=lease)
    assert await cluster.get_lease("exradius", "exradius-operator") == lease
    assert await cluster.get_lease("exradius", "other") is None


async def test_client_builds_and_their_secrets(api):
    cluster, requests, responses = api
    builds = "/apis/exradius.dev/v1alpha1/namespaces/exradius-builds/builds"
    obj = {"metadata": {"name": "build-p-workstation-1a2b", "namespace": "exradius-builds", "uid": "b1"}, "spec": {}}
    responses[("POST", builds)] = httpx.Response(201, json=obj)
    assert await cluster.create_build({"metadata": {"name": "build-p-workstation-1a2b"}}) == (obj, True)
    responses[("POST", builds)] = httpx.Response(409, json={"message": "already exists"})
    responses[("GET", f"{builds}/build-p-workstation-1a2b")] = httpx.Response(200, json=obj)
    assert await cluster.create_build({"metadata": {"name": "build-p-workstation-1a2b"}}) == (obj, False)  # joined
    assert await cluster.get_build("build-p-workstation-1a2b") == obj
    assert await cluster.get_build("gone") is None

    responses[("DELETE", f"{builds}/build-p-workstation-1a2b")] = httpx.Response(200, json={})
    assert await cluster.delete_build("build-p-workstation-1a2b", uid="b1") is True
    assert json.loads(requests[-1].content) == {"propagationPolicy": "Background", "preconditions": {"uid": "b1"}}
    responses[("DELETE", f"{builds}/build-p-workstation-1a2b")] = httpx.Response(409, json={"message": "precondition"})
    assert await cluster.delete_build("build-p-workstation-1a2b", uid="old") is False  # replaced meanwhile
    assert await cluster.delete_build("gone") is False

    secrets = "/api/v1/namespaces/exradius-builds/secrets"
    responses[("POST", secrets)] = httpx.Response(201, json={})
    await cluster.create_secret({"metadata": {"name": "build-p-workstation-1a2b", "namespace": "exradius-builds"}})
    assert requests[-1].url.path == secrets  # in the namespace the Secret names


async def test_client_watch(api):
    cluster, requests, responses = api
    lines = [
        json.dumps({"type": "ADDED", "object": {"metadata": {"name": "run-1"}}}),
        "",
        json.dumps({"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "7"}}}),
        json.dumps({"type": "ERROR", "object": {"code": 410, "message": "too old resource version"}}),
    ]
    responses[("GET", RUNS)] = httpx.Response(200, content="\n".join(lines).encode())
    seen = []
    with pytest.raises(Gone):
        async for kind, obj in cluster.watch_runs("5"):
            seen.append(kind)
    assert seen == ["ADDED", "BOOKMARK"]
    params = requests[-1].url.params
    assert params["watch"] == "1" and params["resourceVersion"] == "5" and params["allowWatchBookmarks"] == "true"

    responses[("GET", RUNS)] = httpx.Response(410, json={"message": "gone"})
    with pytest.raises(Gone):
        async for _ in cluster.watch_runs("1"):
            pass


def test_in_cluster_address(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(RuntimeError, match="not in a Kubernetes pod"):
        KubeCluster("ns", token_file=tmp_path / "t", ca_file=tmp_path / "ca")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    assert KubeCluster("ns", token_file=tmp_path / "t", ca_file=tmp_path / "ca").api_url == "https://10.96.0.1:443"
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    assert KubeCluster("ns", token_file=tmp_path / "t", ca_file=tmp_path / "ca").api_url == "https://[fd00::1]:443"


# Diagnosis of Kubernetes reasons (docs/kubernetes.md §13) -------------------------------------------------


@pytest.mark.parametrize("log", [
    'ImagePullFailed: image "acme/node:1" could not be pulled: failed to resolve reference "acme/node:1"',
    'Failed to pull image "acme/node:1": rpc error: code = NotFound desc = manifest unknown',
    "Back-off pulling image \"acme/node:1\": ImagePullBackOff",
])
def test_image_pull_failures(log):
    sentence = diagnose("Prepare workspace", log)
    assert sentence.startswith("The image `acme/node:1` could not be pulled: check `workstation.image`")
    assert "runs.imagePullSecrets" in sentence


def test_unschedulable():
    memory = diagnose("Prepare workspace", "waiting for cluster capacity: 0/1 nodes are available: 1 Insufficient memory.")
    assert memory.startswith("No node has enough memory for the run's pod")
    taint = diagnose("Prepare workspace", "waiting for cluster capacity: 0/3 nodes are available: 3 node(s) didn't match "
                                          "Pod's node affinity/selector.")
    assert taint.startswith("No node can take the run's pod: check the install's `runs.nodeSelector`")
    assert diagnose("Setup: make", "Insufficient memory") is None  # only while the pod waits for a node
