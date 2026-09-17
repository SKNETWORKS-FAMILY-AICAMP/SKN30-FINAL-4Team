"""Private Storage transport tests; no Supabase instance is contacted."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.infrastructure.supabase_storage import SupabasePrivateObjectStorage
from app.ports.analysis_runs import (
    ObjectStorageUnavailable,
    ObjectStorageWriteUncertain,
)
from app.ports.results import ReportStorageUnavailable


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


@pytest.mark.parametrize("post_outcome", ["duplicate-409", "duplicate-400", "timeout"])
def test_ambiguous_upload_accepts_only_exact_existing_object(
    post_outcome: str,
) -> None:
    requests: list[httpx.Request] = []
    content = b"PK\x03\x04exact-content"
    content_type = "application/vnd.hancom.hwpx"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            if post_outcome == "timeout":
                raise httpx.ReadTimeout("response lost", request=request)
            if post_outcome == "duplicate-400":
                return httpx.Response(
                    400,
                    json={
                        "statusCode": "409",
                        "error": "Duplicate",
                        "message": "The resource already exists",
                    },
                )
            return httpx.Response(409, json={"message": "The resource already exists"})
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": f"{content_type}; charset=binary"},
        )

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        storage.put(
            bucket="request-temp",
            object_key="run/source/exact.hwpx",
            content=content,
            content_type=content_type,
        )
    )

    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.parametrize(
    ("existing", "headers"),
    [
        (b"PK\x03\x04different", {"Content-Type": "application/vnd.hancom.hwpx"}),
        (b"PK\x03\x04expected", {"Content-Type": "application/octet-stream"}),
    ],
)
def test_ambiguous_upload_rejects_mismatched_existing_object(
    existing: bytes,
    headers: dict[str, str],
) -> None:
    expected = b"PK\x03\x04expected"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"error": "Duplicate"})
        return httpx.Response(200, content=existing, headers=headers)

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ObjectStorageUnavailable):
        asyncio.run(
            storage.put(
                bucket="request-temp",
                object_key="run/source/exact.hwpx",
                content=expected,
                content_type="application/vnd.hancom.hwpx",
            )
        )


def test_unverifiable_ambiguous_upload_is_preserved_for_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(404)

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ObjectStorageWriteUncertain):
        asyncio.run(
            storage.put(
                bucket="request-temp",
                object_key="run/source/exact.hwpx",
                content=b"PK\x03\x04expected",
                content_type="application/vnd.hancom.hwpx",
            )
        )


def test_clear_validation_rejection_does_not_probe_object() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(422)

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ObjectStorageUnavailable):
        asyncio.run(
            storage.put(
                bucket="request-temp",
                object_key="run/source/exact.hwpx",
                content=b"PK\x03\x04expected",
                content_type="application/vnd.hancom.hwpx",
            )
        )

    assert [request.method for request in requests] == ["POST"]

def test_private_report_download_uses_service_credentials_and_is_bounded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"%PDF-1.7\nreport",
            headers={"Content-Length": "15", "Content-Type": "application/pdf"},
        )

    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    content = asyncio.run(
        storage.get(
            bucket="analysis-reports",
            object_key="private/case/report.pdf",
            max_bytes=1024,
        )
    )

    assert content == b"%PDF-1.7\nreport"
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].headers["apikey"] == "server-secret"
    assert requests[0].headers["authorization"] == "Bearer server-secret"
    assert b"analysis-reports/private/case/report.pdf" in requests[0].url.raw_path


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "provider-secret"}),
        httpx.Response(200, content=b"x", headers={"Content-Length": "1025"}),
    ],
)
def test_private_report_download_rejects_missing_or_oversized_objects(
    response: httpx.Response,
) -> None:
    storage = SupabasePrivateObjectStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(lambda _request: response),
    )

    with pytest.raises(ReportStorageUnavailable) as error:
        asyncio.run(
            storage.get(
                bucket="analysis-reports",
                object_key="private/case/report.pdf",
                max_bytes=1024,
            )
        )

    assert "provider-secret" not in str(error.value)
    assert "server-secret" not in repr(error.value)
