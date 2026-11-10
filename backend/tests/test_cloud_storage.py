"""The GCS and Azure Blob stores and the pod identity tokens (docs/kubernetes.md §14).

Fakes (httpx.MockTransport) check what the stores and token providers send. The emulator
tests run the same contract against fake-gcs-server and Azurite, and skip unless their
endpoints are set:

    docker run -d --rm --name exradius-test-gcs -p 127.0.0.1:4443:4443 \\
      fsouza/fake-gcs-server:1.56.1 -scheme http -port 4443 -public-host 127.0.0.1:4443
    docker run -d --rm --name exradius-test-azurite -p 127.0.0.1:10000:10000 \\
      mcr.microsoft.com/azure-storage/azurite:3.37.0 azurite-blob --blobHost 0.0.0.0
    EXRADIUS_TEST_GCS_ENDPOINT=http://127.0.0.1:4443 \\
    EXRADIUS_TEST_AZURITE_ENDPOINT=http://127.0.0.1:10000/devstoreaccount1 uv run pytest tests/test_cloud_storage.py
"""

import json
import os
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

from exradius.pod_identity import (
    MANAGEMENT_SCOPE,
    STORAGE_SCOPE,
    AzureWorkloadIdentityToken,
    GoogleMetadataToken,
    PodIdentityError,
)
from exradius.storage import (
    AzureBlobObjectStore,
    GCSObjectStore,
    InvalidObjectKey,
    ObjectNotFound,
    ObjectStoreError,
)

from tests.test_storage import AZURITE_KEY, BAD_KEYS


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("exradius.storage.asyncio.sleep", instant)


class StaticTokens:
    def __init__(self, token="tok"):
        self.value = token
        self.calls = 0

    async def token(self) -> str:
        self.calls += 1
        return self.value


# -- pod identity -------------------------------------------------------------------------


async def test_google_metadata_token_is_cached_and_asks_the_metadata_server():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": f"t{len(seen)}", "expires_in": 3599, "token_type": "Bearer"})

    tokens = GoogleMetadataToken(transport=httpx.MockTransport(handler))
    assert await tokens.token() == "t1"
    assert await tokens.token() == "t1"  # cached
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
    assert request.headers["Metadata-Flavor"] == "Google"


async def test_google_metadata_token_refreshes_near_expiry_and_reports_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 30})

    tokens = GoogleMetadataToken(host="metadata.test", transport=httpx.MockTransport(handler))
    assert await tokens.token() == "t1"
    assert await tokens.token() == "t2"  # 30 s is inside the refresh margin

    failing = GoogleMetadataToken(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="no sa")))
    with pytest.raises(PodIdentityError, match="404"):
        await failing.token()


def azure_env(tmp_path, token="federated-1"):
    path = tmp_path / "azure-identity-token"
    path.write_text(token + "\n")
    return {
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_FEDERATED_TOKEN_FILE": str(path),
        "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
    }


async def test_azure_workload_identity_exchanges_the_federated_token(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": f"aad-{len(seen)}", "expires_in": 30, "token_type": "Bearer"})

    env = azure_env(tmp_path)
    tokens = AzureWorkloadIdentityToken(STORAGE_SCOPE, env=env, transport=httpx.MockTransport(handler))
    assert await tokens.token() == "aad-1"
    request = seen[0]
    assert str(request.url) == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "client_id": "client-id",
        "scope": "https://storage.azure.com/.default",
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": "federated-1",
    }
    # The kubelet rotates the projected token: every refresh reads the file again.
    (tmp_path / "azure-identity-token").write_text("federated-2")
    assert await tokens.token() == "aad-2"
    assert parse_qs(seen[1].content.decode())["client_assertion"] == ["federated-2"]


async def test_azure_workload_identity_needs_the_webhook_and_reports_refusals(tmp_path):
    with pytest.raises(PodIdentityError, match="AZURE_FEDERATED_TOKEN_FILE"):
        AzureWorkloadIdentityToken(STORAGE_SCOPE, env={"AZURE_CLIENT_ID": "c", "AZURE_TENANT_ID": "t"})
    refused = AzureWorkloadIdentityToken(
        MANAGEMENT_SCOPE, env=azure_env(tmp_path),
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_client"})),
    )
    with pytest.raises(PodIdentityError, match="invalid_client"):
        await refused.token()


# -- GCS against a fake --------------------------------------------------------------------


class FakeGCS:
    """The JSON API's media upload, media download and object list, for one bucket."""

    def __init__(self, bucket="exradius"):
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_next: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        path = request.url.raw_path.decode().split("?")[0]
        upload = f"/upload/storage/v1/b/{self.bucket}/o"
        objects = f"/storage/v1/b/{self.bucket}/o"
        if request.method == "POST" and path == upload:
            assert request.url.params["uploadType"] == "media"
            name = request.url.params["name"]
            self.objects[name] = (request.content, request.headers["content-type"])
            return httpx.Response(200, json={"name": name, "bucket": self.bucket})
        if request.method == "GET" and path == objects:
            return httpx.Response(200, json={"kind": "storage#objects"})
        if request.method == "GET" and path.startswith(objects + "/"):
            from urllib.parse import unquote

            name = unquote(path[len(objects) + 1:])
            if request.url.params.get("alt") != "media" or name not in self.objects:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, content=self.objects[name][0])
        return httpx.Response(404, json={"error": {"code": 404, "message": "bucket not found"}})


async def test_gcs_store_round_trip_with_a_bearer_token():
    fake = FakeGCS()
    tokens = StaticTokens("ya29.x")
    store = GCSObjectStore("exradius", tokens=tokens, transport=httpx.MockTransport(fake.handler))
    await store.ensure_bucket()
    key = "runs/r1/artifacts/1234-my report#1.md"
    await store.put(key, b"hello", "text/markdown")
    assert await store.get(key) == b"hello"
    assert fake.objects[key] == (b"hello", "text/markdown")
    assert all(r.headers["authorization"] == "Bearer ya29.x" for r in fake.requests)
    assert str(fake.requests[0].url).startswith("https://storage.googleapis.com/storage/v1/b/exradius/o?")
    # The object name is one path segment: its slashes and "#" are escaped.
    assert fake.requests[-1].url.raw_path.decode().startswith(
        "/storage/v1/b/exradius/o/runs%2Fr1%2Fartifacts%2F1234-my%20report%231.md?")
    with pytest.raises(ObjectNotFound):
        await store.get("runs/r1/missing")


async def test_gcs_store_retries_transient_errors_and_reports_the_rest():
    fake = FakeGCS()
    store = GCSObjectStore("exradius", transport=httpx.MockTransport(fake.handler))
    fake.fail_next = [503, 429]
    await store.put("a", b"1")
    assert fake.objects["a"][0] == b"1"
    fake.fail_next = [403]
    with pytest.raises(ObjectStoreError, match="403"):
        await store.get("a")
    fake.fail_next = [500, 500, 500]
    with pytest.raises(ObjectStoreError, match="500"):
        await store.put("a", b"2")
    missing = GCSObjectStore("other", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(ObjectStoreError, match="does not exist"):
        await missing.ensure_bucket()


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_cloud_stores_validate_keys(key):
    never = httpx.MockTransport(lambda r: pytest.fail("no request for an invalid key"))
    gcs = GCSObjectStore("b", transport=never)
    azure = AzureBlobObjectStore("acct", "c", account_key=AZURITE_KEY, transport=never)
    for store in (gcs, azure):
        with pytest.raises(InvalidObjectKey):
            await store.put(key, b"x")
        with pytest.raises(InvalidObjectKey):
            await store.get(key)


# -- Azure Blob against a fake (bearer) -----------------------------------------------------


async def test_azure_store_with_workload_identity_tokens():
    blobs: dict[str, bytes] = {}
    containers: set[str] = set()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer aad"
        assert request.headers["x-ms-version"] and request.headers["x-ms-date"]
        path = request.url.path
        if request.url.params.get("restype") == "container":
            if request.method == "HEAD":
                return httpx.Response(200 if path in containers else 404)
            containers.add(path)
            return httpx.Response(201)
        if request.method == "PUT":
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            blobs[path] = request.content
            return httpx.Response(201)
        if path in blobs:
            return httpx.Response(200, content=blobs[path])
        return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})

    store = AzureBlobObjectStore("acct", "exradius", tokens=StaticTokens("aad"), transport=httpx.MockTransport(handler))
    await store.ensure_bucket()
    await store.ensure_bucket()
    assert [r.method for r in seen] == ["HEAD", "PUT", "HEAD"]
    assert str(seen[1].url) == "https://acct.blob.core.windows.net/exradius?restype=container"
    await store.put("runs/r1/transcript.jsonl", b"{}\n", "application/x-ndjson")
    assert seen[-1].url.raw_path == b"/exradius/runs/r1/transcript.jsonl"
    assert await store.get("runs/r1/transcript.jsonl") == b"{}\n"
    with pytest.raises(ObjectNotFound):
        await store.get("runs/r2/transcript.jsonl")
    with pytest.raises(ValueError):
        AzureBlobObjectStore("acct", "c")


# -- the emulators ------------------------------------------------------------------------

GCS_ENDPOINT = os.environ.get("EXRADIUS_TEST_GCS_ENDPOINT")
AZURITE_ENDPOINT = os.environ.get("EXRADIUS_TEST_AZURITE_ENDPOINT")

CONTRACT_KEYS = [
    "runs/0b9a/transcript.jsonl",
    "runs/0b9a/artifacts/1731-report final (2).md",
    "tasks/t-1/attachments/ü-日本.txt",
    "builds/42/context.tar",
]


async def check_contract(store) -> None:
    await store.ensure_bucket()
    for key in CONTRACT_KEYS:
        await store.put(key, key.encode() + b"\n", "text/plain")
    for key in CONTRACT_KEYS:
        assert await store.get(key) == key.encode() + b"\n"
    await store.put(CONTRACT_KEYS[0], b"replaced")
    assert await store.get(CONTRACT_KEYS[0]) == b"replaced"
    await store.put("bin", bytes(range(256)) * 1000)
    assert await store.get("bin") == bytes(range(256)) * 1000
    await store.put("empty", b"")
    assert await store.get("empty") == b""
    with pytest.raises(ObjectNotFound):
        await store.get("runs/0b9a/missing")
    with pytest.raises(ObjectNotFound):
        await store.get("runs/0b9a")  # a prefix is not an object


@pytest.mark.skipif(not GCS_ENDPOINT, reason="EXRADIUS_TEST_GCS_ENDPOINT (fake-gcs-server) is not set")
async def test_gcs_store_against_fake_gcs_server():
    bucket = f"exradius-{uuid.uuid4().hex[:8]}"
    store = GCSObjectStore(bucket, endpoint=GCS_ENDPOINT)
    with pytest.raises(ObjectStoreError, match="does not exist"):
        await store.ensure_bucket()
    async with httpx.AsyncClient() as client:  # the installer's part: Terraform makes the bucket
        response = await client.post(f"{GCS_ENDPOINT}/storage/v1/b", params={"project": "test"},
                                     content=json.dumps({"name": bucket}))
        assert response.status_code == 200, response.text
    await check_contract(store)


@pytest.mark.skipif(not AZURITE_ENDPOINT, reason="EXRADIUS_TEST_AZURITE_ENDPOINT (Azurite) is not set")
async def test_azure_store_against_azurite_with_a_shared_key():
    container = f"exradius-{uuid.uuid4().hex[:8]}"
    store = AzureBlobObjectStore("devstoreaccount1", container, endpoint=AZURITE_ENDPOINT, account_key=AZURITE_KEY)
    await check_contract(store)
    await store.ensure_bucket()  # exists: no error
    wrong = AzureBlobObjectStore("devstoreaccount1", container, endpoint=AZURITE_ENDPOINT,
                                 account_key="d3Jvbmcta2V5LXdyb25nLWtleS13cm9uZy1rZXk=")
    with pytest.raises(ObjectStoreError, match="403"):
        await wrong.get(CONTRACT_KEYS[0])
