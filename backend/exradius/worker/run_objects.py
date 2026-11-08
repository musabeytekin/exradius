"""From a run's config to its ``Run`` object and per-run Secret (docs/kubernetes.md §5, §10).

The Run spec is secret-free: every value that holds a secret is in the per-run Secret and
the spec names it by key. The operator turns the spec into the pod; it never sees
``exradius.yaml``. Pure functions; the tests check them without a cluster.

Secret keys: ``token`` (the run token), ``env.<NAME>`` (project variables that hold a
secret), ``provider.<NAME>`` (the provider's variables), ``repo.<n>`` (a repository's
credential, for its clone), ``push.<n>`` (the token part, for the push credential helper).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from typing import Any

from exradius.resolve import StoreValues, exported_secrets, repo_specs, substitute
from exradius.schemas.project_config import SECRET_REF, ProjectConfig, memory_bytes
from exradius.worker import git_identity
from exradius.worker.kube import API_VERSION

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
    return workstation


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
) -> RunObjects:
    """The Run of a task: clones, setup, the files sidecar and the agent's runner."""
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


def tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]
