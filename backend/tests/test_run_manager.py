"""Run lifecycle end to end, with a fake Kubernetes API standing in for the cluster
(tests/fakes.py: a stand-in operator and runner)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from exradius import downloads
from exradius.crypto import hash_token
from exradius.models import Artifact, ImageBuild, InfoRequest, Project, Run, SecretStore, SecretStoreEntry, SecretStoreProject, Task
from exradius.states import State
from exradius.worker.intake import accept_answer
from exradius.worker.runs import RunManager
from tests.conftest import BASE_CONFIG, commit_config
from tests.fakes import FakeCluster, FakeImageConfigs, runner


@pytest.fixture
def cluster(sessions, store):
    fake = FakeCluster()
    fake.sessions = sessions
    fake.store = store
    return fake


@pytest.fixture
def published():
    return []


@pytest.fixture
def manager(settings, sessions, store, cluster, published):
    async def emit(events):
        published.extend(e.envelope for e in events)

    manager = RunManager(settings, sessions, store, cluster, emit)
    manager.builder.poll = 0.01
    manager.image_configs = FakeImageConfigs()  # no registry is reached from the tests
    cluster.manager = manager
    return manager


def states(published) -> list[str]:
    return [e.payload["state"] for e in published if e.type == "status"]


def results(published):
    return [e.payload for e in published if e.type == "result"]


async def submit_task(accept, published, task_id="T-1", **extra) -> uuid.UUID:
    body = {"version": 1, "task_id": task_id, "project": "billing-platform", "agent": "bug-fixer",
            "instructions": "Fix it.", **extra}
    intake = await accept(json.dumps(body).encode())
    published.extend(e.envelope for e in intake.events)
    assert intake.run_id
    return intake.run_id


async def execute(manager, cluster, run_id) -> None:
    """``execute`` returns when the run waits or ends; then every simulated pod settles."""
    await asyncio.wait_for(manager.execute(run_id), 10)
    await asyncio.wait_for(cluster.idle(), 10)


async def set_config(configured, extra: str) -> str:
    return commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + extra})


async def add_store(sessions, box, name: str, **values: str) -> None:
    async with sessions() as session:
        secret_store = SecretStore(name=name)
        session.add(secret_store)
        await session.flush()
        for project_id in await session.scalars(select(Project.id)):
            session.add(SecretStoreProject(store_id=secret_store.id, project_id=project_id))
        for key, value in values.items():
            session.add(SecretStoreEntry(store_id=secret_store.id, key=key, value_encrypted=box.encrypt(value)))
        await session.commit()


async def answer(sessions, store, task_id, request_id, text="eu-west-1", attachments=()):
    body = json.dumps({"version": 1, "task_id": task_id, "request_id": request_id, "answer": text,
                       "attachments": list(attachments)}).encode()
    return await accept_answer(sessions, store, body)


async def test_completed_run(manager, sessions, store, cluster, published, settings, configured, box, accept):
    commit = await set_config(configured, (
        "secret_store: billing\n"
        "env:\n  LOG_LEVEL: debug\n  DB_URL: postgres://app:${secret:DB_PASSWORD}@db/app\n"
    ))
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2", OTHER="x")

    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)

    assert states(published) == ["queued", "preparing", "running", "completed"]
    [result] = results(published)
    assert result["state"] == "completed"
    assert result["summary"] == "All fixed."
    assert result["output"] == {"fixed": True}
    assert result["error"] is None
    path = f"/api/runs/{run_id}/transcript"
    assert result["transcript_url"] == f"{settings.public_api_url}{path}?sig={downloads.sign(settings, path)}"
    transcript = store.objects[f"runs/{run_id}/transcript.jsonl"][0].decode().splitlines()
    assert json.loads(transcript[0])["type"] == "AssistantMessage"

    [env] = cluster.envs
    # The model credential never reaches the pod: a placeholder, and the egress proxy's
    # model route, which holds the provider's key (docs/kubernetes.md §10).
    assert env["ANTHROPIC_API_KEY"] == "placeholder"
    assert env["ANTHROPIC_BASE_URL"] == settings.egress_url + "/anthropic"
    assert "sk-test" not in json.dumps(env)
    assert env["LOG_LEVEL"] == "debug" and env["DB_URL"] == "postgres://app:hunter2@db/app"
    assert "DB_PASSWORD" not in env and "OTHER" not in env  # used by reference only
    assert env["GIT_AUTHOR_NAME"] == "Bug Fixer Bot" and env["EXRADIUS_AGENT"] == "bug-fixer"
    assert env["EXRADIUS_ID"] == str(run_id)

    [created] = cluster.created
    name = f"run-{run_id}"
    assert created["metadata"]["name"] == name and created["metadata"]["namespace"] == settings.runs_namespace
    assert created["metadata"]["labels"] == {
        "exradius.dev/run": name, "exradius.dev/purpose": "task", "exradius.dev/project": "billing-platform",
    }
    assert created["metadata"]["annotations"] == {"exradius.dev/task-id": "T-1"}
    spec = created["spec"]
    assert spec["workstation"]["image"] == settings.project_default_image
    assert spec["runtime"] == {"name": "claude_code", "image": settings.runtime_image_claude_code, "env": [
        {"name": "ANTHROPIC_BASE_URL", "value": settings.egress_url + "/anthropic"},
        {"name": "ANTHROPIC_API_KEY", "value": "placeholder"}]}
    # The spec is secret-free: values that hold a secret are Secret keys.
    assert spec["workstation"]["env"] == [
        {"name": "LOG_LEVEL", "value": "debug"}, {"name": "DB_URL", "secretKey": "env.DB_URL"},
    ]
    assert "hunter2" not in json.dumps(spec) and "sk-test" not in json.dumps(spec)

    async with sessions() as session:
        run = await session.get(Run, run_id)
        assert run.state == State.COMPLETED and run.ended_at is not None
        assert run.token_hash is None  # revoked
        assert run.run_object == name
        assert run.provider_id is not None  # the proxy's model route uses its credential
        assert run.config_commit == commit
        assert run.config["env"]["DB_URL"] == "postgres://app:${secret:DB_PASSWORD}@db/app"  # as written
        assert run.agent_snapshot == {
            "slug": "bug-fixer", "name": "Bug fixer", "model": None, "provider": None, "runtime": "claude_code",
            "identity": {"name": "Bug Fixer Bot", "email": "bug-fixer@agents.example"}, "connections": ["docs"],
        }
        assert (await session.get(Task, "T-1")).state == State.COMPLETED
    assert cluster.deleted == [name] and name not in cluster.runs  # pod, workspace and Secret go with it
    assert cluster.secrets == {}


async def test_secret_is_owned_by_the_run_and_holds_the_token(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    seen = {}

    async def check(rid, env):
        name = f"run-{rid}"
        secret = cluster.secrets[name]
        seen["owner"] = secret["metadata"]["ownerReferences"]
        seen["uid"] = cluster.runs[name]["metadata"]["uid"]
        async with sessions() as session:
            seen["token_ok"] = (await session.get(Run, rid)).token_hash == hash_token(env["EXRADIUS_RUN_TOKEN"])
        return await runner(sessions)(rid, env)

    cluster.behaviours.append(check)
    await execute(manager, cluster, run_id)
    assert seen["owner"] == [{"apiVersion": "exradius.dev/v1alpha1", "kind": "Run", "name": f"run-{run_id}",
                              "uid": seen["uid"]}]
    assert seen["token_ok"]


async def test_config_is_read_in_preparing(manager, sessions, store, cluster, published, configured, accept):
    """A config that stops running between intake and preparing fails the run with the reason."""
    run_id = await submit_task(accept, published)
    await set_config(configured, "env:\n  X: ${secret:GONE}\nsecret_store: nowhere\n")
    await execute(manager, cluster, run_id)
    assert cluster.created == []
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "secret store nowhere does not exist"


async def test_agent_removed_before_preparing(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published, agent="scout")
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG.replace("  scout:\n    role: You investigate.\n", "")})
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["error"] == "agent 'scout' is no longer defined in project 'billing-platform'"


def builds_of(cluster) -> list[dict]:
    return [o for o in cluster.created if o["kind"] == "Build"]


def runs_of(cluster) -> list[dict]:
    return [o for o in cluster.created if o["kind"] == "Run"]


async def test_workstation_is_built_from_the_config_repository(
        manager, sessions, store, cluster, published, settings, configured, accept):
    settings.builds_registry = "kind-registry:5000"
    await set_config(configured, "workstation:\n  dockerfile: docker/Dockerfile\n  setup: [make deps]\n")
    commit_config(configured["remote"], {"docker/Dockerfile": "FROM ubuntu:24.04\n", "docker/tools.sh": "echo\n"})
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)

    assert states(published) == ["queued", "preparing", "running", "completed"]
    [build] = builds_of(cluster)
    image = build["spec"]["image"]
    assert image.startswith("kind-registry:5000/exradius/billing-platform/workstation:")
    assert len(image.rsplit(":", 1)[1]) == 12
    assert build["metadata"]["name"] == "build-billing-platform-workstation-" + image.rsplit(":", 1)[1]
    assert build["metadata"]["namespace"] == settings.builds_namespace
    assert build["spec"]["dockerfile"] == "Dockerfile"
    assert build["spec"]["cache"] == image.rsplit(":", 1)[0] + ":buildcache"
    assert build["spec"]["secretName"] == build["metadata"]["name"]
    assert set(cluster.contexts[build["metadata"]["name"]]) == {"Dockerfile", "tools.sh"}  # the context only
    [run] = runs_of(cluster)
    assert run["spec"]["workstation"]["image"] == image
    async with sessions() as session:
        [row] = (await session.scalars(select(ImageBuild))).all()
    assert (row.name, row.state, row.image, row.build_object) == ("workstation", "succeeded", image,
                                                                  build["metadata"]["name"])
    assert row.digest.startswith("sha256:") and row.token_hash is None and "RUN make tools" in row.log_tail
    assert str(row.id) == build["spec"]["id"]
    assert build["metadata"]["name"] not in cluster.secrets  # the token's Secret goes once the outcome is recorded

    # Same context: no second Build, even though the commit changed.
    commit_config(configured["remote"], {"README": "unrelated\n"})
    run_id = await submit_task(accept, published, task_id="T-2")
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    assert len(builds_of(cluster)) == 1 and len(cluster.build_pods) == 1
    assert runs_of(cluster)[1]["spec"]["workstation"]["image"] == image


async def test_a_dockerfile_outside_its_context_rebuilds_when_only_it_changes(
        manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "workstation:\n  dockerfile: workstation.Dockerfile\n  context: docker\n")
    commit_config(configured["remote"], {"workstation.Dockerfile": "FROM ubuntu:24.04\n", "docker/tools.sh": "echo\n"})
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    [first] = builds_of(cluster)
    assert first["spec"]["dockerfile"] == ".exradius.Dockerfile"
    assert set(cluster.contexts[first["metadata"]["name"]]) == {"tools.sh", ".exradius.Dockerfile"}

    commit_config(configured["remote"], {"workstation.Dockerfile": "FROM ubuntu:24.04\nRUN true\n"})
    run_id = await submit_task(accept, published, task_id="T-2")
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    second = builds_of(cluster)[1]
    assert second["spec"]["image"] != first["spec"]["image"]
    assert cluster.contexts[second["metadata"]["name"]][".exradius.Dockerfile"].endswith(b"RUN true\n")


async def test_two_runs_at_a_new_commit_share_one_build(
        manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "workstation:\n  dockerfile: Dockerfile\n")
    commit_config(configured["remote"], {"Dockerfile": "FROM ubuntu:24.04\n"})
    first = await submit_task(accept, published)
    second = await submit_task(accept, published, task_id="T-2")
    cluster.behaviours += [runner(sessions), runner(sessions)]
    cluster.build_seconds = 0.2
    await asyncio.wait_for(asyncio.gather(manager.execute(first), manager.execute(second)), 10)
    await asyncio.wait_for(cluster.idle(), 10)
    assert len(builds_of(cluster)) == 1 and len(cluster.build_pods) == 1
    assert [r["state"] for r in results(published)] == ["completed", "completed"]
    images = {r["spec"]["workstation"]["image"] for r in runs_of(cluster)}
    assert images == {builds_of(cluster)[0]["spec"]["image"]}
    async with sessions() as session:
        assert len((await session.scalars(select(ImageBuild))).all()) == 1


async def test_failed_build_fails_the_run_in_preparing(manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "workstation:\n  dockerfile: Dockerfile\n")
    commit_config(configured["remote"], {"Dockerfile": "FROM ubuntu:24.04\nRUN make tools\n"})
    cluster.build_failure = {"log": "#5 [2/2] RUN make tools\n#5 0.2 make: *** No rule to make target 'tools'.  Stop.\n"
                                    "error: failed to solve: process \"/bin/sh -c make tools\" did not complete successfully"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    assert states(published) == ["queued", "preparing", "failed"]
    assert runs_of(cluster) == []
    [result] = results(published)
    assert result["error"].startswith("building workstation from Dockerfile failed:\nthe build exited with 1\n#5 [2/2]")
    assert "No rule to make target 'tools'" in result["error"]
    async with sessions() as session:
        row = await session.scalar(select(ImageBuild))
        assert row.state == "failed" and "No rule to make target" in row.log_tail
        run = await session.get(Run, run_id)
        assert run.waiting_for is None

    # The next run builds again: the failed Build is replaced.
    run_id = await submit_task(accept, published, task_id="T-2")
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    assert results(published)[1]["state"] == "completed"
    assert len(builds_of(cluster)) == 2 and len(cluster.build_pods) == 2


async def test_a_run_waits_in_preparing_while_its_image_builds(
        manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "workstation:\n  dockerfile: Dockerfile\n")
    commit_config(configured["remote"], {"Dockerfile": "FROM ubuntu:24.04\n"})
    cluster.build_seconds = 5
    run_id = await submit_task(accept, published)
    running = asyncio.create_task(manager.execute(run_id))
    for _ in range(200):
        async with sessions() as session:
            run = await session.get(Run, run_id)
        if run.waiting_for == "build":
            break
        await asyncio.sleep(0.01)
    assert run.state == State.PREPARING and run.waiting_for == "build"

    await manager.cancel(run_id)  # stops the wait; the Build goes on
    await asyncio.wait_for(running, 5)
    assert states(published) == ["queued", "preparing", "cancelled"]
    assert runs_of(cluster) == [] and len(builds_of(cluster)) == 1
    assert builds_of(cluster)[0]["metadata"]["name"] in cluster.builds


async def test_repositories_and_setup_go_into_the_spec(
        manager, sessions, store, cluster, published, configured, box, accept):
    await set_config(configured, (
        "secret_store: billing\n"
        "resources: {memory: 6g}\n"
        "workstation:\n  image: acme/node:22\n  setup: [npm ci, make check]\n"
        "repositories:\n"
        "  - {name: app, url: 'https://git.example/acme/app.git', branch: feature, credential: '${secret:GIT}', push: true}\n"
        "  - {name: docs, url: 'https://git.example/acme/docs.git'}\n"
    ))
    await add_store(sessions, box, "billing", GIT="ghp_token")
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)

    spec = cluster.created[0]["spec"]
    assert spec["workstation"]["image"] == "acme/node:22"
    assert spec["workstation"]["setup"] == ["npm ci", "make check"]
    assert spec["workstation"]["memory"] == "6Gi"
    assert spec["repositories"] == [
        {"folder": "app", "url": "https://git.example/acme/app.git", "push": True, "branch": "feature"},
        {"folder": "docs", "url": "https://git.example/acme/docs.git", "push": False},
    ]
    assert spec["git"]["setupEnv"] == [{"name": "GIT_CONFIG_COUNT", "value": "1"},
                                       {"name": "GIT_CONFIG_KEY_0", "value": "safe.directory"},
                                       {"name": "GIT_CONFIG_VALUE_0", "value": "*"}]
    git_env = {e["name"]: e["value"] for e in spec["git"]["env"]}
    assert git_env["GIT_CONFIG_GLOBAL"] == "/exradius/state/git/config"
    assert "core.hooksPath" in git_env.values() and "/opt/exradius/runtime/git-hooks" in git_env.values()
    # Each repository goes to the egress proxy's git route, which holds its credential.
    egress = manager.settings.egress_url
    assert f"url.{egress}/git/git.example/acme/app.git.insteadOf" in git_env.values()
    assert f"url.{egress}/git/git.example/acme/docs.git.insteadOf" in git_env.values()
    [env] = cluster.envs
    assert "ghp_token" not in json.dumps(spec) and "ghp_token" not in json.dumps(env)
    assert not any(key.startswith(("repo.", "push.", "provider.")) for secret in cluster.secret_bodies
                   for key in secret)


async def test_url_attachments_are_stored_before_the_run(
        manager, sessions, store, cluster, published, configured, accept, monkeypatch):
    async def fetch(url):
        assert url == "https://files.example/log.txt"
        return b"stack trace"

    monkeypatch.setattr(manager, "_fetch", fetch)
    run_id = await submit_task(accept, published, attachments=[
        {"name": "log.txt", "content_type": "text/plain", "url": "https://files.example/log.txt"}])
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    async with sessions() as session:
        task = await session.get(Task, "T-1")
        [attachment] = task.attachments
    assert attachment.object_key and store.objects[attachment.object_key][0] == b"stack trace"


async def test_info_request_waits_in_the_pod_and_resumes_there(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Which region?"))
    await asyncio.wait_for(manager.execute(run_id), 10)  # returns: the run waits, its slot is free

    assert states(published)[-1] == "waiting_for_input"
    [info] = [e for e in published if e.type == "info_request"]
    assert info.payload["question"] == "Which region?"
    assert results(published) == []
    name = f"run-{run_id}"
    assert cluster.runs[name]["status"]["phase"] == "Running"  # the pod keeps running
    async with sessions() as session:
        assert (await session.get(Run, run_id)).token_hash is not None  # the token lives with the Run

    intake = await answer(sessions, store, "T-1", info.payload["request_id"])
    assert intake.run_id == run_id

    def check(run, env):
        assert run.resume_request_ids == [info.payload["request_id"]]

    cluster.behaviours.append(runner(sessions, check=check))
    await asyncio.wait_for(manager.resume(run_id), 10)
    await cluster.idle()

    assert states(published) == [
        "queued", "preparing", "running", "waiting_for_input", "resuming", "running", "completed",
    ]
    assert results(published)[0]["state"] == "completed"
    assert len(cluster.created) == 1 and len(cluster.envs) == 1  # one pod for both sessions
    assert cluster.deleted == [name]
    assert not [p for p in cluster.patches if "podGeneration" in p[1]]


async def test_answer_before_the_waiting_state_resumes_at_once(
        manager, sessions, store, cluster, published, configured, accept):
    resumed = []
    manager.resume_later = lambda run_id: resumed.append(run_id) or cluster.pods.append(
        asyncio.ensure_future(manager.resume(run_id)))
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?", answer_early=True))
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    assert resumed == [run_id]
    assert states(published)[-4:] == ["waiting_for_input", "resuming", "running", "completed"]


async def test_a_waiting_runs_lost_pod_is_recreated_on_the_same_workspace(
        manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await asyncio.wait_for(manager.execute(run_id), 10)
    name = f"run-{run_id}"

    # The pod is deleted by hand while the run waits: nothing happens until the answer.
    cluster._stop[name].set()
    await asyncio.wait_for(cluster.idle(), 10)
    cluster.set_status(name, phase="Failed", reason="PodLost", message="the run's pod was deleted")
    await manager.settle(run_id)
    assert states(published)[-1] == "waiting_for_input"

    async with sessions() as session:
        request = await session.scalar(select(InfoRequest))
    await answer(sessions, store, "T-1", str(request.id))

    def check(run, env):
        assert run.state == State.RUNNING and run.resume_request_ids == [str(request.id)]

    cluster.behaviours.append(runner(sessions, check=check))
    await asyncio.wait_for(manager.resume(run_id), 10)
    await cluster.idle()

    assert (name, {"podGeneration": 1}) in cluster.patches
    assert states(published)[-3:] == ["resuming", "running", "completed"]
    assert len(cluster.created) == 1  # the same Run, PVC and Secret: a new pod only


async def test_setup_failure_fails_the_run_with_the_steps_log(
        manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "workstation:\n  setup: [make deps]\n")
    cluster.prepare_failure = {"container": "setup-0", "name": "Setup: make deps", "exitCode": 2,
                               "log": "setup output\nboom"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    assert states(published) == ["queued", "preparing", "failed"]
    [result] = results(published)
    assert result["error"] == "setup command 'make deps' exited with 2:\nsetup output\nboom"


async def test_setup_killed_under_a_declared_limit(manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "resources: {memory: 2g}\nworkstation:\n  setup: [make deps]\n")
    cluster.prepare_failure = {"container": "setup-0", "name": "Setup: make deps", "exitCode": 137,
                               "reason": "OOMKilled", "log": "Killed"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["error"] == "setup command 'make deps' exceeded its declared memory (2g) and was killed (exit 137):\nKilled"


async def test_clone_failure_names_the_repository(manager, sessions, store, cluster, published, configured, accept):
    cluster.prepare_failure = {"container": "clone-0", "name": "Clone app (main)", "exitCode": 1,
                               "log": "git clone failed: Repository not found"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["error"] == "clone app (main) failed:\ngit clone failed: Repository not found"


async def test_runner_crash_without_outcome(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status=None, exit_code=1))
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["state"] == "failed"
    assert result["error"] == "runner exited with code 1 without reporting an outcome:\nrunner log line"


async def test_exit_137_under_a_declared_limit_names_it(manager, sessions, store, cluster, published, configured, accept):
    await set_config(configured, "resources: {memory: 4g}\n")
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status=None, exit_code=137))
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["error"].startswith("the run exceeded its declared memory (4g) and was killed (exit 137)")


async def test_runtime_failure_is_reported(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="failed", error="invalid api key", exit_code=1))
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "invalid api key"


async def test_delivery_warnings_reach_the_result(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, warnings=["app: 1 commit on main was not pushed"]))
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["warnings"] == ["app: 1 commit on main was not pushed"]


async def test_cancel_a_waiting_run(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await asyncio.wait_for(manager.execute(run_id), 10)

    await manager.cancel(run_id)
    await asyncio.wait_for(cluster.idle(), 10)
    assert (f"run-{run_id}", {"cancel": True}) in cluster.patches
    assert states(published)[-1] == "cancelled"
    [result] = results(published)
    assert result["state"] == "cancelled" and result["error"] is None
    assert cluster.deleted == [f"run-{run_id}"]

    await manager.cancel(run_id)  # idempotent
    assert len(results(published)) == 1


async def test_cancel_while_running_waits_for_the_pod(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)

    async def cancelled_mid_run(rid, env):
        await manager.cancel(rid)  # the operator deletes the pod; the runner reports first
        async with sessions() as session:
            run = await session.get(Run, rid)
            assert run.state == State.CANCELLED and run.ended_at is None  # not finalised yet
            run.delivery_warnings = ["app: uncommitted changes in 2 files were discarded with the task directory"]
            await session.commit()
        return 143

    cluster.behaviours.append(cancelled_mid_run)
    await execute(manager, cluster, run_id)
    assert states(published) == ["queued", "preparing", "running", "cancelled"]
    [result] = results(published)  # exactly one result
    assert result["state"] == "cancelled"
    assert result["warnings"] == ["app: uncommitted changes in 2 files were discarded with the task directory"]


async def test_cancel_in_preparing_before_the_run_exists(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    await manager.cancel(run_id)
    await execute(manager, cluster, run_id)
    assert cluster.created == []
    assert [r["state"] for r in results(published)] == ["cancelled"]


async def test_waiting_for_cluster_capacity(manager, sessions, store, cluster, published, configured, accept):
    cluster.auto = False
    run_id = await submit_task(accept, published)
    task = asyncio.create_task(manager.execute(run_id))
    while f"run-{run_id}" not in cluster.secrets:
        await asyncio.sleep(0.01)
    name = f"run-{run_id}"
    cluster.set_status(name, phase="Pending", conditions=[
        {"type": "Scheduled", "status": "False", "reason": "Unschedulable",
         "message": "0/1 nodes are available: 1 Insufficient memory."}])
    await manager.settle(run_id)
    async with sessions() as session:
        run = await session.get(Run, run_id)
        assert run.state == State.PREPARING and run.waiting_for == "capacity"
    assert states(published) == ["queued", "preparing"]  # no event for it

    cluster.set_status(name, phase="Running", conditions=[], workstation={"state": "running"})
    await manager.settle(run_id)
    async with sessions() as session:
        run = await session.get(Run, run_id)
        assert run.state == State.RUNNING and run.waiting_for is None
    cluster.set_status(name, phase="Failed", reason="Error", workstation={"state": "terminated", "exitCode": 1, "log": "x"})
    await manager.settle(run_id)
    await asyncio.wait_for(task, 5)
    assert results(published)[0]["state"] == "failed"


async def test_a_run_deleted_by_hand_fails(manager, sessions, store, cluster, published, configured, accept):
    cluster.auto = False
    run_id = await submit_task(accept, published)
    task = asyncio.create_task(manager.execute(run_id))
    while f"run-{run_id}" not in cluster.secrets:
        await asyncio.sleep(0.01)
    await cluster.delete_run(f"run-{run_id}")
    await manager.on_event("DELETED", {"metadata": {"name": f"run-{run_id}"}})
    await asyncio.wait_for(task, 5)
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "the run's Kubernetes object disappeared"


async def test_execute_ignores_runs_that_are_not_queued(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    await manager.cancel(run_id)
    await execute(manager, cluster, run_id)
    assert cluster.created == []
    assert [r["state"] for r in results(published)] == ["cancelled"]


async def test_resumable_runs(manager, sessions, store, cluster, published, configured, accept):
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await asyncio.wait_for(manager.execute(run_id), 10)
    assert await manager.resumable_runs() == []
    async with sessions() as session:
        request = await session.scalar(select(InfoRequest))
        request.answer, request.answered_at = "A", request.created_at
        await session.commit()
    assert await manager.resumable_runs() == [run_id]


async def test_result_links_artifacts_by_signed_http_url(
        manager, sessions, store, cluster, published, settings, configured, accept):
    settings.public_api_url = "https://exradius.example.com/"
    run_id = await submit_task(accept, published)
    base = runner(sessions)

    async def with_artifact(rid, env):
        async with sessions() as session:
            session.add(Artifact(run_id=rid, name="report.md", content_type="text/markdown", object_key="runs/x/report.md"))
            await session.commit()
        return await base(rid, env)

    cluster.behaviours.append(with_artifact)
    await execute(manager, cluster, run_id)
    [result] = results(published)
    async with sessions() as session:
        artifact = await session.scalar(select(Artifact))
    [ref] = result["artifacts"]
    path = f"/api/artifacts/{artifact.id}/content"
    assert ref == {
        "name": "report.md",
        "content_type": "text/markdown",
        "url": f"https://exradius.example.com{path}?sig={downloads.sign(settings, path)}",
    }
    assert result["transcript_url"].startswith(f"https://exradius.example.com/api/runs/{run_id}/transcript?sig=")


async def test_two_runs_of_a_project_run_in_parallel(manager, sessions, store, cluster, published, configured, accept):
    first = await submit_task(accept, published, task_id="T-1")
    second = await submit_task(accept, published, task_id="T-2")
    gate = asyncio.Event()
    seen = []

    async def together(rid, env):
        seen.append(rid)
        if len(seen) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 5)  # both pods are running at once
        return await runner(sessions)(rid, env)

    cluster.behaviours += [together, together]
    await asyncio.wait_for(asyncio.gather(manager.execute(first), manager.execute(second)), 10)
    await cluster.idle()
    assert sorted(seen) == sorted([first, second])
    assert [r["state"] for r in results(published)] == ["completed", "completed"]
