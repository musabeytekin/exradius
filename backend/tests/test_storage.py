import os

import pytest

from exradius.config import Settings
from exradius.storage import (
    FilesystemObjectStore,
    InvalidObjectKey,
    MemoryObjectStore,
    ObjectNotFound,
    S3ObjectStore,
    make_store,
    validate_key,
)


@pytest.fixture
async def fs_store(tmp_path):
    store = FilesystemObjectStore(tmp_path / "objects", "exradius")
    await store.ensure_bucket()
    return store


async def test_put_get_round_trip(fs_store, tmp_path):
    await fs_store.put("runs/r1/transcript.jsonl", b'{"a": 1}\n', "application/x-ndjson")
    assert await fs_store.get("runs/r1/transcript.jsonl") == b'{"a": 1}\n'
    # Objects live under <root>/<bucket>/<key>.
    assert (tmp_path / "objects" / "exradius" / "runs" / "r1" / "transcript.jsonl").read_bytes() == b'{"a": 1}\n'


async def test_put_replaces_and_leaves_no_temp_files(fs_store):
    await fs_store.put("tasks/t/attachments/a.txt", b"one")
    await fs_store.put("tasks/t/attachments/a.txt", b"two")
    assert await fs_store.get("tasks/t/attachments/a.txt") == b"two"
    assert os.listdir(fs_store.path("tasks/t/attachments/a.txt").parent) == ["a.txt"]


async def test_binary_and_empty_objects(fs_store):
    await fs_store.put("bin", bytes(range(256)))
    await fs_store.put("empty", b"")
    assert await fs_store.get("bin") == bytes(range(256))
    assert await fs_store.get("empty") == b""


async def test_missing_object(fs_store):
    with pytest.raises(ObjectNotFound):
        await fs_store.get("runs/nope/transcript.jsonl")
    await fs_store.put("runs/r1/x", b"x")
    with pytest.raises(ObjectNotFound):
        await fs_store.get("runs/r1")  # a directory is not an object


BAD_KEYS = [
    "",
    "/etc/passwd",
    "../outside",
    "runs/../../outside",
    "runs/./x",
    "runs//x",
    "runs/x/",
    "runs\\..\\x",
    "runs/x\x00y",
    "..",
]


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_keys_cannot_escape(fs_store, tmp_path, key):
    with pytest.raises(InvalidObjectKey):
        await fs_store.put(key, b"x")
    with pytest.raises(InvalidObjectKey):
        await fs_store.get(key)
    assert not (tmp_path / "outside").exists()


async def test_symlinks_cannot_escape(fs_store, tmp_path):
    (tmp_path / "secret").write_text("s3cret")
    fs_store.root.joinpath("link").symlink_to(tmp_path)
    with pytest.raises(InvalidObjectKey):
        await fs_store.get("link/secret")


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_memory_store_validates_keys_too(key):
    with pytest.raises(InvalidObjectKey):
        await MemoryObjectStore().put(key, b"x")


def test_validate_key_splits_segments():
    assert validate_key("runs/r1/artifacts/1234-report.md") == ["runs", "r1", "artifacts", "1234-report.md"]
    assert validate_key("a..b/c") == ["a..b", "c"]  # dots inside a name are fine


def test_installs_use_s3():
    """An install's store is always an S3 API (SeaweedFS locally); the filesystem store is for tests."""
    s3 = make_store(
        Settings(s3_endpoint_url="http://s3.example:9000", s3_access_key="k", s3_secret_key="s", s3_bucket="b")
    )
    assert isinstance(s3, S3ObjectStore) and s3.bucket == "b"
    assert isinstance(make_store(Settings()), S3ObjectStore)
    with pytest.raises(ValueError):
        Settings(object_store="filesystem")


def test_empty_s3_settings_mean_unset():
    settings = Settings(s3_endpoint_url="", s3_access_key="", s3_secret_key="")
    assert settings.s3_endpoint_url is None and settings.s3_access_key is None
    assert settings.object_store == "s3"
