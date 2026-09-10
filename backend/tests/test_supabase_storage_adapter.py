"""Private Storage transport tests; no Supabase instance is contacted."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.infrastructure.supabase_storage import SupabasePrivateObjectStorage
from app.ports.analysis_runs import ObjectStorageUnavailable


def test_upload_and_compensating_delete_use_server_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201 if request.method == "POST" else 200)

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    async def run() -> None:
        await storage.put(
            bucket="request-temp",
            object_key="run/source/abc%20def.hwpx",
            content=b"PK\x03\x04",
            content_type="application/vnd.hancom.hwpx",
        )
        await storage.delete(
            bucket="request-temp",
            object_key="run/source/abc%20def.hwpx",
        )

    asyncio.run(run())

    assert [request.method for request in requests] == ["POST", "DELETE"]
    assert all(request.headers["apikey"] == "server-secret" for request in requests)
    assert all(
        request.headers["authorization"] == "Bearer server-secret"
        for request in requests
    )
    assert requests[0].headers["x-upsert"] == "false"
    # A literal '%' in an immutable object key is encoded once by our adapter;
    # httpx displays the decoded path while preserving the raw path on the URL.
    assert b"abc%2520def.hwpx" in requests[0].url.raw_path


def test_storage_errors_are_sanitized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "token=provider-secret"})

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ObjectStorageUnavailable) as error:
        asyncio.run(
            storage.put(
                bucket="request-temp",
                object_key="run/source/abc.hwp",
                content=b"payload",
                content_type="application/x-hwp",
            )
        )

    assert "provider-secret" not in str(error.value)
    assert "server-secret" not in repr(error.value)
