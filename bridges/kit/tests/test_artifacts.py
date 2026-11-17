import uuid

import httpx
import pytest

from exradius_bridge_kit.exradius import ExradiusError, IntegrationApi


async def test_artifact_download_uses_configured_api_and_token_not_event_host():
    artifact_id = str(uuid.uuid4())
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b"report")

    api = IntegrationApi("https://exradius.internal", "private-token", transport=httpx.MockTransport(handle))
    try:
        content = await api.artifact(f"https://untrusted.example/api/artifacts/{artifact_id}/content?sig=old")
        assert content == b"report"
        assert str(requests[0].url) == f"https://exradius.internal/api/integration/v1/artifacts/{artifact_id}/content"
        assert requests[0].headers["authorization"] == "Bearer private-token"
        with pytest.raises(ExradiusError):
            await api.artifact("https://untrusted.example/secrets")
        assert len(requests) == 1
    finally:
        await api.aclose()


async def test_artifact_download_does_not_follow_redirects():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.example"})

    api = IntegrationApi("https://exradius.internal", "private-token", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ExradiusError):
            await api.artifact(str(uuid.uuid4()))
        assert len(requests) == 1
    finally:
        await api.aclose()
