"""Runtime bundles (docs/runtime-bundles.md): extraction into per-build volumes, the mounts
and entrypoint of run containers, and what the heartbeat reports. Docker is faked."""

from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from exradius.config import Settings
from exradius.worker import prepare
from exradius.worker.bundles import BUNDLE_PATH, ENTRYPOINT, BundleUnavailable, RuntimeMount, volume_name
from exradius.worker.containers import ContainerManager

BUNDLE = "exradius-runtime-claude-code:0.1.0"
IMAGE_ID = "sha256:1a2b3c4d5e6f7a8b9c0d"


def tar_of(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FakeDocker:
    """Images, volumes and containers, with Docker's fill-an-empty-volume-on-create rule."""

    def __init__(self):
        self.image_store: dict[str, SimpleNamespace] = {}
        self.volume_store: dict[str, dict] = {}  # name -> {"labels", "files", "in_use"}
        self.created: list[dict] = []
        self.ran: list[dict] = []
        self.ping_error: Exception | None = None
        self.add_image(BUNDLE, IMAGE_ID, {"exradius.runtime": "claude_code", "exradius.version": "0.1.0",
                                          "exradius.role": "runtime-bundle"},
                       files={"VERSION": b"exradius 0.1.0\nruntime claude_code\nclaude-code 2.1.318\n"})
        outer = self

        class Images:
            def get(self, ref):
                if ref not in outer.image_store:
                    raise ImageNotFound(f"no such image: {ref}")
                return outer.image_store[ref]

            def list(self):
                return list({id(i): i for i in outer.image_store.values()}.values())

        class Volumes:
            def get(self, name):
                if name not in outer.volume_store:
                    raise NotFound(f"no such volume: {name}")
                return SimpleNamespace(name=name, attrs={"Labels": outer.volume_store[name]["labels"]})

            def create(self, name, labels=None):
                outer.volume_store.setdefault(name, {"labels": labels or {}, "files": {}, "in_use": False})
                return self.get(name)

            def list(self, filters=None):
                key = (filters or {}).get("label")
                found = []
                for name, volume in outer.volume_store.items():
                    if key and key not in volume["labels"]:
                        continue

                    def remove(name=name):
                        if outer.volume_store[name]["in_use"]:
                            raise APIError("volume is in use")
                        del outer.volume_store[name]

                    found.append(SimpleNamespace(name=name, remove=remove))
                return found

        class Containers:
            def create(self, image, command=None, mounts=(), labels=None):
                image_obj = next(i for i in outer.image_store.values() if i.id == image)
                outer.created.append({"image": image, "command": command, "mounts": mounts, "labels": labels})
                for mount in mounts:
                    volume = outer.volume_store[mount["Source"]]
                    if not volume["files"] and mount["Target"] == BUNDLE_PATH:
                        volume["files"] = dict(image_obj.files)  # Docker copies the image content in
                files = {m["Target"]: outer.volume_store[m["Source"]]["files"] for m in mounts}

                def get_archive(path):
                    directory, _, name = path.rpartition("/")
                    content = files.get(directory, {}).get(name)
                    if content is None:
                        raise NotFound(path)
                    return [tar_of(name, content)], {}

                return SimpleNamespace(id=f"x{len(outer.created)}", get_archive=get_archive, remove=lambda force=False: None)

            def run(self, image, **kwargs):
                outer.ran.append({"image": image, **kwargs})
                return SimpleNamespace(id=f"c{len(outer.ran)}")

        self.images, self.volumes, self.containers = Images(), Volumes(), Containers()

    def add_image(self, ref, image_id, labels=None, files=None, tags=None, created="2026-11-05T10:00:00Z", size=1):
        image = SimpleNamespace(id=image_id, labels=labels or {}, files=files or {}, tags=[ref] if tags is None else tags,
                                attrs={"Created": created, "Size": size})
        self.image_store[ref] = image
        return image

    def ping(self):
        if self.ping_error:
            raise self.ping_error


@pytest.fixture
def fake():
    return FakeDocker()


@pytest.fixture
def manager(fake, tmp_path):
    settings = Settings(data_dir=tmp_path, runtime_image_claude_code=BUNDLE,
                        project_default_image="exradius-project-default:0.1.0")
    return ContainerManager(settings, fake)


def test_volume_name():
    assert volume_name("claude_code", "0.1.0", "sha256:1a2b3c4d5e6f7a8b9c0d") == "exradius-runtime-claude-code-0.1.0-1a2b3c4d5e6f"
    assert volume_name("claude_code", "1.0+local/x", "abcdef1234567890") == "exradius-runtime-claude-code-1.0_local_x-abcdef123456"


async def test_extracts_once_per_bundle_build(manager, fake):
    mount = await manager.ensure_runtime("claude_code")
    name = "exradius-runtime-claude-code-0.1.0-1a2b3c4d5e6f"
    assert mount == RuntimeMount("claude_code", "0.1.0", name)
    assert fake.volume_store[name]["labels"] == {"exradius.runtime": "claude_code", "exradius.version": "0.1.0",
                                                 "exradius.runtime_image_id": IMAGE_ID}
    [extract] = fake.created
    assert extract["image"] == IMAGE_ID and extract["mounts"][0]["Target"] == BUNDLE_PATH
    assert fake.volume_store[name]["files"]["VERSION"].startswith(b"exradius 0.1.0")

    assert await manager.ensure_runtime("claude_code") == mount
    assert len(fake.created) == 1  # cached for this image id

    # A rebuilt bundle image (same tag, new id) gets its own volume; the old one is untouched.
    fake.add_image(BUNDLE, "sha256:ffffeeeeddddcccc", fake.image_store[BUNDLE].labels, files=fake.image_store[BUNDLE].files)
    newer = await manager.ensure_runtime("claude_code")
    assert newer.volume == "exradius-runtime-claude-code-0.1.0-ffffeeeedddd"
    assert set(fake.volume_store) == {name, newer.volume}


async def test_missing_or_wrong_bundle_image(manager, fake):
    del fake.image_store[BUNDLE]
    with pytest.raises(BundleUnavailable, match="bundle image exradius-runtime-claude-code:0.1.0 not found"):
        await manager.ensure_runtime("claude_code")
    status = manager.bundles.status["claude_code"]
    assert status.ready is False and "not found" in status.error

    fake.add_image(BUNDLE, "sha256:0000", {"exradius.runtime": "other"})
    with pytest.raises(BundleUnavailable, match="is not a claude_code runtime bundle"):
        await manager.ensure_runtime("claude_code")

    fake.add_image(BUNDLE, "sha256:1111", {"exradius.runtime": "claude_code", "exradius.version": "0.2.0"},
                   files={"VERSION": b"exradius 0.1.0\n"})
    with pytest.raises(BundleUnavailable, match="holds a different bundle"):
        await manager.ensure_runtime("claude_code")


async def test_removes_unused_old_volumes(manager, fake):
    current = await manager.ensure_runtime("claude_code")
    for name, in_use in (("exradius-runtime-claude-code-0.0.9-aaaa", False), ("exradius-runtime-claude-code-0.0.8-bbbb", True)):
        fake.volume_store[name] = {"labels": {"exradius.runtime": "claude_code"}, "files": {}, "in_use": in_use}
    fake.volume_store["unrelated"] = {"labels": {}, "files": {}, "in_use": False}
    assert await manager.bundles.remove_unused() == ["exradius-runtime-claude-code-0.0.9-aaaa"]
    assert set(fake.volume_store) == {current.volume, "exradius-runtime-claude-code-0.0.8-bbbb", "unrelated"}


async def test_run_probe_and_setup_containers(manager, fake, tmp_path):
    runtime = await manager.ensure_runtime("claude_code")
    paths = prepare.create_run_directory(tmp_path, "r1")

    await manager.start_runner(image="python:3.14", run_id="r1", paths=paths, env={"A": "1"}, runtime=runtime)
    run = fake.ran[-1]
    assert run["image"] == "python:3.14"
    assert run["entrypoint"] == [ENTRYPOINT] == ["/opt/exradius/runtime/bin/exradius-runner"]
    assert run["command"] == ["r1"]
    bundle = [m for m in run["mounts"] if m["Target"] == "/opt/exradius/runtime"]
    assert len(bundle) == 1 and bundle[0]["Source"] == runtime.volume
    assert bundle[0]["Type"] == "volume" and bundle[0]["ReadOnly"] is True
    assert {m["Target"] for m in run["mounts"]} == {"/work", "/work/.exradius/attachments", "/exradius/state",
                                                    "/opt/exradius/runtime", "/etc/passwd", "/etc/group"}
    assert run["user"] == "1000:1000" and run["read_only"] is True and run["cap_drop"] == ["ALL"]
    assert run["environment"]["A"] == "1" and run["environment"]["HOME"] == "/home/runner"
    assert run["environment"]["GIT_CONFIG_KEY_0"] == "safe.directory" and run["environment"]["GIT_CONFIG_VALUE_0"] == "*"

    fake_wait = []

    async def wait(container_id):
        fake_wait.append(container_id)
        from exradius.worker.containers import ContainerExit

        return ContainerExit(0, "ok")

    manager.wait = wait
    await manager.run_setup_command(image="python:3.14", run_id="r1", paths=paths, env={}, command="make")
    setup = fake.ran[-1]
    assert setup["entrypoint"] == ["/bin/sh", "-c"] and setup["command"] == ["make"]
    assert "/opt/exradius/runtime" not in {m["Target"] for m in setup["mounts"]}  # no bundle for setup

    await manager.run_probe(image="exradius-project-default:0.1.0", label="provider-test-1",
                            env={"CLAUDE_CODE_OAUTH_TOKEN": "t"}, timeout=10, runtime=runtime)
    probe = fake.ran[-1]
    assert probe["image"] == "exradius-project-default:0.1.0"
    assert probe["entrypoint"] == [ENTRYPOINT] and probe["command"] == ["--probe", "claude_code"]
    assert [m["Target"] for m in probe["mounts"]] == ["/opt/exradius/runtime"]


async def test_docker_report_lists_every_image_and_the_runtimes(manager, fake):
    fake.add_image("python:3.14", "sha256:p", tags=["python:3.14", "python:latest"], size=42)
    fake.add_image("exradius-backend:0.1.0", "sha256:b", {"exradius.role": "backend"})
    fake.add_image("exradius-runner:latest", "sha256:r", {"exradius.runner": "true"})
    fake.add_image("<none>", "sha256:dangling", tags=[])
    report = await manager.docker_report()
    assert report["docker_ok"] is True
    # No label filter any more; only Exradius' own backend and bundle images are left out.
    assert [i["tag"] for i in report["images"]] == ["exradius-runner:latest", "python:3.14", "python:latest"]
    assert report["images"][1] == {"tag": "python:3.14", "created_at": "2026-11-05T10:00:00Z", "size": 42}
    assert report["runtimes"] == [{"name": "claude_code", "version": "0.1.0", "ready": True,
                                   "image": BUNDLE, "error": None}]
    assert "runner_image_ok" not in report


async def test_docker_report_when_the_daemon_or_bundle_is_missing(manager, fake):
    del fake.image_store[BUNDLE]
    report = await manager.docker_report()
    [runtime] = report["runtimes"]
    assert runtime["ready"] is False and "not found" in runtime["error"]

    fake.ping_error = ConnectionError("no socket")
    down = await manager.docker_report()
    assert down["docker_ok"] is False and "no socket" in down["docker_detail"] and down["images"] == []
    assert down["runtimes"][0]["ready"] is False and down["runtimes"][0]["error"] == "Docker is not reachable"


async def test_failed_extraction_fails_the_run_not_the_worker(manager, fake):
    del fake.image_store[BUNDLE]
    await manager.bundles.ensure_all()  # logs, does not raise
    with pytest.raises(BundleUnavailable, match="runtime claude_code is not available"):
        await manager.ensure_runtime("claude_code")
