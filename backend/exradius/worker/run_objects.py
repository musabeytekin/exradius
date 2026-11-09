"""From a run's config to its ``Run`` object and per-run Secret (docs/kubernetes.md §5, §10).

The Run spec is secret-free: every value that holds a secret is in the per-run Secret and
the spec names it by key. The operator turns the spec into the pod; it never sees
``exradius.yaml``. Pure functions; the tests check them without a cluster.

Secret keys: ``token`` (the run token), ``env.<NAME>`` (project variables that hold a
secret), ``service.<service>.<NAME>`` (a service's variables that hold one),
``provider.<NAME>`` (the provider's variables), ``repo.<n>`` (a repository's credential,
for its clone), ``push.<n>`` (the token part, for the push credential helper).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from typing import Any

from exradius.resolve import StoreValues, exported_secrets, repo_specs, substitute
from exradius.schemas.project_config import SECRET_REF, ProjectConfig, ServiceDef, memory_bytes
from exradius.worker import git_identity
from exradius.worker.caches import cache_specs
from exradius.worker.kube import API_VERSION
from exradius.worker.registry import ready_from_healthcheck

LABEL_RUN = "exradius.dev/run"
LABEL_PROJECT = "exradius.dev/project"
LABEL_PURPOSE = "exradius.dev/purpose"
ANNOTATION_TASK = "exradius.dev/task-id"
PREFIXES = {"task": "run-", "verify": "verify-", "probe": "probe-"}


def run_name(purpose: str, object_id: uuid.UUID | str) -> str:
    """``run-<run id>``, ``verify-<verification id>``, ``probe-<test id>``."""
    return f"{PREFIXES[purpose]}{object_id}"


def parse_name(name: str) -> tuple[str, uuid.UUID] | None:
    for purpose, prefix in PREFIXES.items():
        if name.startswith(prefix):
            try:
                return purpose, uuid.UUID(name.removeprefix(prefix))
            except ValueError:
                return None
    return None


def quantity(size: str) -> str:
    """A config memory size (``6g``, ``512m``) as a Kubernetes quantity (``6Gi``, ``512Mi``)."""
    value = memory_bytes(size)
    for unit, factor in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if value % factor == 0:
            return f"{value // factor}{unit}"
    return str(value)


@dataclass
class RunObjects:
    name: str
    run: dict[str, Any]
    """The Run body to create."""
    secret: dict[str, str] = field(default_factory=dict)
    """The per-run Secret's data, in clear; ``secret_body`` encodes it."""

    def secret_body(self, owner: dict[str, Any], namespace: str) -> dict[str, Any]:
        """The Secret, owned by the created Run so it goes with it."""
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": self.name,
                "namespace": namespace,
                "labels": dict(self.run["metadata"].get("labels") or {}),
                "ownerReferences": [{
                    "apiVersion": API_VERSION, "kind": "Run",
                    "name": owner["metadata"]["name"], "uid": owner["metadata"]["uid"],
                }],
            },
            "type": "Opaque",
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in self.secret.items()},
        }


class _Env:
    """Builds env entries; values holding a secret go to the Secret under ``<prefix>.<NAME>``."""

    def __init__(self, secret: dict[str, str]):
        self.secret = secret

    def add(self, name: str, value: str, *, prefix: str, secret: bool) -> dict[str, str]:
        if not secret:
            return {"name": name, "value": value}
        key = f"{prefix}.{name}"
        self.secret[key] = value
        return {"name": name, "secretKey": key}


def project_env(config: ProjectConfig, secrets: StoreValues, data: dict[str, str], *,
                drop: frozenset[str] = frozenset()) -> list[dict[str, str]]:
    """The project's ``env`` in file order after the secrets its services export (as
    ``resolve.workstation_environment``), less ``drop`` and command-line git config."""
    env = _Env(data)
    values: dict[str, tuple[str, bool]] = {name: (secrets.lookup(name), True) for name in exported_secrets(config)}
    for name, raw in config.env.items():
        values[name] = (substitute(raw, secrets), bool(SECRET_REF.search(raw)))
    kept = git_identity.without_command_line_config({k: v for k, (v, _) in values.items() if k not in drop})
    return [env.add(name, value, prefix="env", secret=values[name][1]) for name, value in kept.items()]


def _metadata(name: str, namespace: str, purpose: str, project: str | None, task_id: str | None) -> dict[str, Any]:
    labels = {LABEL_RUN: name, LABEL_PURPOSE: purpose}
    if project:
        labels[LABEL_PROJECT] = project
    metadata: dict[str, Any] = {"name": name, "namespace": namespace, "labels": labels}
    if task_id:
        metadata["annotations"] = {ANNOTATION_TASK: task_id}
    return metadata


def _workstation(config: ProjectConfig, image: str, env: list[dict[str, str]]) -> dict[str, Any]:
    workstation: dict[str, Any] = {"image": image, "setup": list(config.workstation.setup), "env": env}
    if config.resources.memory:
        workstation["memory"] = quantity(config.resources.memory)
    if config.workstation.cache:
        workstation["caches"] = cache_specs(config.workstation.cache)
    return workstation


def service_ready(service: ServiceDef, healthcheck: list[str] | None) -> dict[str, Any] | None:
    """A service's startup check: its ``ready``, else its image's ``HEALTHCHECK``, else none
    (started when its process starts)."""
    if service.ready is not None:
        return {"exec": list(service.ready.command)} if service.ready.command else {"tcp": service.ready.tcp}
    return ready_from_healthcheck(healthcheck)


def _services(config: ProjectConfig, secrets: StoreValues, data: dict[str, str], images: dict[str, str],
              healthchecks: dict[str, list[str] | None]) -> list[dict[str, Any]]:
    """The services in file order: env values holding a secret go to the Secret as
    ``service.<name>.<KEY>``; built images by their tag."""
    services = []
    for name, service in config.services.items():
        env = _Env(data)
        entry: dict[str, Any] = {
            "name": name,
            "image": images.get(name) or service.image or "",
            "env": [env.add(key, substitute(raw, secrets), prefix=f"service.{name}", secret=bool(SECRET_REF.search(raw)))
                    for key, raw in service.env.items()],
            "ports": list(service.ports),
        }
        if service.command:
            entry["command"] = list(service.command)
        if (ready := service_ready(service, healthchecks.get(name))) is not None:
            entry["ready"] = ready
        if service.memory:
            entry["memory"] = quantity(service.memory)
        services.append(entry)
    return services


def _repositories(config: ProjectConfig, secrets: StoreValues, data: dict[str, str]) -> list[dict[str, Any]]:
    repos = []
    for index, repo in enumerate(repo_specs(config, secrets)):
        entry: dict[str, Any] = {"folder": repo.name, "url": repo.url, "push": repo.push_allowed}
        if repo.branch:
            entry["branch"] = repo.branch
        if repo.auth:
            key = f"repo.{index}"
            data[key] = repo.auth
            entry["credentialKey"] = key
        repos.append(entry)
    return repos


def _runtime(name: str, image: str, provider_env: dict[str, str], data: dict[str, str]) -> dict[str, Any]:
    env = _Env(data)
    return {"name": name, "image": image,
            "env": [env.add(k, v, prefix="provider", secret=True) for k, v in provider_env.items()]}


def task_run(
    *,
    run_id: uuid.UUID,
    namespace: str,
    task_id: str,
    project: str,
    config: ProjectConfig,
    secrets: StoreValues,
    runtime: str,
    runtime_image: str,
    workstation_image: str,
    provider_env: dict[str, str],
    credential_variables: frozenset[str],
    identity: git_identity.Identity,
    token: str,
    service_images: dict[str, str] | None = None,
    healthchecks: dict[str, list[str] | None] | None = None,
) -> RunObjects:
    """The Run of a task: clones, services, setup, the files sidecar and the agent's runner.
    ``service_images`` names built service images; ``healthchecks`` the ``HEALTHCHECK`` of
    each service image whose ``ready`` is not set."""
    name = run_name("task", run_id)
    data: dict[str, str] = {"token": token}
    git_env, push_tokens = git_identity.workstation_git(identity, repo_specs(config, secrets))
    data.update(push_tokens)
    spec = {
        "purpose": "task",
        "id": str(run_id),
        "project": project,
        "taskId": task_id,
        "secretName": name,
        "runtime": _runtime(runtime, runtime_image, provider_env, data),
        # Only the provider's variables reach the workstation: a project's own are dropped.
        "workstation": _workstation(config, workstation_image,
                                    project_env(config, secrets, data, drop=credential_variables)),
        "repositories": _repositories(config, secrets, data),
        "services": _services(config, secrets, data, service_images or {}, healthchecks or {}),
        "git": {"setupEnv": git_identity.setup_git(), "env": git_env},
        "cancel": False,
        "podGeneration": 0,
    }
    run = {"apiVersion": API_VERSION, "kind": "Run",
           "metadata": _metadata(name, namespace, "task", project, task_id), "spec": spec}
    return RunObjects(name, run, data)


def verify_run(
    *,
    verification_id: uuid.UUID,
    namespace: str,
    project: str,
    config: ProjectConfig,
    secrets: StoreValues,
    runtime: str,
    runtime_image: str,
    workstation_image: str,
    token: str,
    service_images: dict[str, str] | None = None,
    healthchecks: dict[str, list[str] | None] | None = None,
) -> RunObjects:
    """The Run of a verification: the same pod as a task's, whose runner lists the tools of
    every connection an agent uses (docs/kubernetes.md §13)."""
    name = run_name("verify", verification_id)
    data: dict[str, str] = {"token": token}
    spec = {
        "purpose": "verify",
        "id": str(verification_id),
        "project": project,
        "secretName": name,
        "runtime": _runtime(runtime, runtime_image, {}, data),
        "workstation": _workstation(config, workstation_image, project_env(config, secrets, data)),
        "repositories": _repositories(config, secrets, data),
        "services": _services(config, secrets, data, service_images or {}, healthchecks or {}),
        "git": {"setupEnv": git_identity.setup_git(), "env": git_identity.setup_git()},
        "cancel": False,
        "podGeneration": 0,
    }
    run = {"apiVersion": API_VERSION, "kind": "Run",
           "metadata": _metadata(name, namespace, "verify", project, None), "spec": spec}
    return RunObjects(name, run, data)


def probe_run(
    *,
    test_id: uuid.UUID,
    namespace: str,
    runtime: str,
    runtime_image: str,
    image: str,
    provider_env: dict[str, str],
    model: str | None,
    token: str,
) -> RunObjects:
    """The Run of an ``oauth_token`` provider test: the default project image and the
    bundle, no workspace, a one-line session (docs/kubernetes.md §13)."""
    name = run_name("probe", test_id)
    data: dict[str, str] = {"token": token}
    spec: dict[str, Any] = {
        "purpose": "probe",
        "id": str(test_id),
        "secretName": name,
        "runtime": _runtime(runtime, runtime_image, provider_env, data),
        "workstation": {"image": image, "setup": [], "env": []},
        "repositories": [],
        "git": {"setupEnv": [], "env": []},
        "cancel": False,
        "podGeneration": 0,
    }
    if model:
        spec["model"] = model
    run = {"apiVersion": API_VERSION, "kind": "Run",
           "metadata": _metadata(name, namespace, "probe", None, None), "spec": spec}
    return RunObjects(name, run, data)


# -- reading a Run's status -------------------------------------------------------------------

FINAL_PHASES = ("Succeeded", "Failed")


@dataclass
class RunView:
    """What the worker reads from a Run object (the operator writes ``status``)."""

    obj: dict[str, Any] | None

    @property
    def exists(self) -> bool:
        return self.obj is not None

    @property
    def spec(self) -> dict[str, Any]:
        return (self.obj or {}).get("spec") or {}

    @property
    def status(self) -> dict[str, Any]:
        return (self.obj or {}).get("status") or {}

    @property
    def phase(self) -> str:
        return self.status.get("phase") or "Pending"

    @property
    def reason(self) -> str:
        return self.status.get("reason") or ""

    @property
    def message(self) -> str:
        return self.status.get("message") or ""

    @property
    def spec_generation(self) -> int:
        return int(self.spec.get("podGeneration") or 0)

    @property
    def current(self) -> bool:
        """The status describes the pod of the spec's generation."""
        return int(self.status.get("podGeneration") or 0) == self.spec_generation and bool(self.status.get("phase"))

    @property
    def final(self) -> bool:
        """The pod of the spec's generation ended (or was lost)."""
        return self.current and self.phase in FINAL_PHASES

    @property
    def unschedulable(self) -> str | None:
        """The scheduler's message while the pod waits for cluster capacity."""
        if self.phase != "Pending":
            return None
        for condition in self.status.get("conditions") or []:
            if condition.get("type") == "Scheduled" and condition.get("status") == "False" \
                    and condition.get("reason") == "Unschedulable":
                return condition.get("message") or "no node has room for the run's pod"
        return None

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self.status.get("steps") or [])

    @property
    def workstation(self) -> dict[str, Any]:
        return self.status.get("workstation") or {}

    def failed_step(self) -> dict[str, Any] | None:
        return next((s for s in self.steps if s.get("state") == "failed"), None)

    def service(self, name: str) -> dict[str, Any]:
        """A service of the spec, by name."""
        return next((s for s in self.spec.get("services") or [] if s.get("name") == name), {})


def tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


SERVICE_STEP = "Start service "


def service_check(spec: dict[str, Any]) -> str:
    """What a service's startup probe checks, in words."""
    ready = spec.get("ready") or {}
    if ready.get("exec"):
        return "command " + " ".join(ready["exec"])
    if ready.get("tcp"):
        return f"port {ready['tcp']}"
    return "its process to start (no readiness check)"


def service_failure(step: dict[str, Any], spec: dict[str, Any], config: ProjectConfig | None,
                    limit: int = 2000) -> str:
    """A service that did not start, as the run's error and the Verify step's log: the
    first line as v0.1.0 wrote it (``service db (postgres:16) did not become ready within
    120s waiting for ...``), then the service's log tail."""
    name = step.get("name", "").removeprefix(SERVICE_STEP)
    message = step.get("message") or f"failed ({step.get('reason') or 'unknown'})"
    if step.get("reason") == "OOMKilled":
        declared = config.services[name].memory if config is not None and name in config.services else None
        cause = f"exceeded its declared memory ({declared})" if declared else "ran out of memory (OOMKilled)"
        message = f"{cause}: {message}"
    image = spec.get("image") or (config.services[name].image if config is not None and name in config.services
                                  else None) or "?"
    return f"service {name} ({image}) {message}\n{tail(step.get('log') or '', limit)}".rstrip()
