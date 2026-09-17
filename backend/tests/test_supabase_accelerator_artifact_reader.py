"""Focused security and contract tests for trusted accelerator artifact reads."""

from __future__ import annotations

import json

import httpx
import pytest

from worker.accelerator_coordinator import ArtifactNotFound, ArtifactReadLimitExceeded
from worker.adapters.supabase_accelerator_artifact import (
    SupabaseAcceleratorArtifactReadError,
    SupabaseAcceleratorArtifactReader,
)


SERVICE_KEY = "service-role-key-local-only"
BUCKET = "request-temp"
KEY = "accelerator/surya-layout/" + ("a" * 64) + "/result.json"


class _Chunks(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _reader(handler: object) -> SupabaseAcceleratorArtifactReader:
    return SupabaseAcceleratorArtifactReader(
        supabase_url="http://127.0.0.1:8000",
        service_role_key=SERVICE_KEY,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_reads_exact_storage_object_and_reports_unmodified_content_type() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.url == f"http://127.0.0.1:8000/storage/v1/object/{BUCKET}/{KEY}"
        assert request.headers["apikey"] == SERVICE_KEY
        assert request.headers["authorization"] == f"Bearer {SERVICE_KEY}"
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json; charset=utf-8",
                "content-length": "11",
            },
            stream=_Chunks(b'{"ok":', b"true}"),
        )

    result = _reader(handler).read(BUCKET, KEY, max_bytes=11)

    assert len(seen) == 1
    assert result.content == b'{"ok":true}'
    assert result.content_type == "application/json; charset=utf-8"


@pytest.mark.parametrize(
    ("bucket", "object_key"),
    [
        ("request-temp/other", KEY),
        (BUCKET, "/absolute/result.json"),
        (BUCKET, "accelerator/../result.json"),
        (BUCKET, "accelerator/result%2f.json"),
        (BUCKET, "accelerator\\result.json"),
        (BUCKET, "accelerator/result name.json"),
    ],
)
def test_rejects_noncanonical_storage_locations_before_any_request(
    bucket: str, object_key: str
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ValueError, match="Storage (bucket|object key) is invalid"):
        _reader(handler).read(bucket, object_key, max_bytes=100)
    assert called is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8000/",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/not-a-gateway",
        "http://127.0.0.1:8000?redirect=elsewhere",
        "http://127.0.0.1:8000#fragment",
    ],
)
def test_requires_a_canonical_supabase_origin(origin: str) -> None:
    with pytest.raises(ValueError, match="Supabase Storage API origin is invalid"):
        SupabaseAcceleratorArtifactReader(
            supabase_url=origin,
            service_role_key=SERVICE_KEY,
        )


def test_service_role_origin_rejects_cleartext_non_loopback() -> None:
    with pytest.raises(ValueError, match="origin is invalid"):
        SupabaseAcceleratorArtifactReader(
            supabase_url="http://supabase.internal:8000",
            service_role_key=SERVICE_KEY,
        )


def test_declared_or_streamed_oversize_fails_without_truncating() -> None:
    def declared(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "101"},
            stream=_Chunks(b"ignored"),
        )

    with pytest.raises(ArtifactReadLimitExceeded, match="exceeds byte limit"):
        _reader(declared).read(BUCKET, KEY, max_bytes=100)

    def streamed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_Chunks(b"a" * 50, b"b" * 51),
        )

    with pytest.raises(ArtifactReadLimitExceeded, match="exceeds byte limit"):
        _reader(streamed).read(BUCKET, KEY, max_bytes=100)


def test_exact_selfhosted_missing_object_response_is_typed_not_found() -> None:
    response = httpx.Response(
        400,
        headers={"content-type": "application/json; charset=utf-8"},
        stream=_Chunks(
            json.dumps(
                {
                    "statusCode": "404",
                    "error": "not_found",
                    "message": "Object not found",
                }
            ).encode()
        ),
    )

    with pytest.raises(ArtifactNotFound):
        _reader(lambda _: response).read(BUCKET, KEY, max_bytes=100)


@pytest.mark.parametrize(
    "payload",
    [
        {"statusCode": "400", "error": "invalid_request", "message": "bad"},
        {"statusCode": "404", "error": "not_found", "message": "missing", "extra": 1},
    ],
)
def test_other_storage_400_responses_remain_redacted_failures(
    payload: dict[str, object],
) -> None:
    response = httpx.Response(
        400,
        headers={"content-type": "application/json; charset=utf-8"},
        stream=_Chunks(json.dumps(payload).encode()),
    )

    with pytest.raises(SupabaseAcceleratorArtifactReadError):
        _reader(lambda _: response).read(BUCKET, KEY, max_bytes=100)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://attacker.invalid/"}),
        httpx.Response(500, content=b"service diagnostic that must not escape"),
        httpx.Response(200, headers={"content-type": "application/json\r\nX-Leak: x"}),
        httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "not-a-number"},
        ),
    ],
)
def test_provider_failures_are_redacted(response: httpx.Response) -> None:
    reader = _reader(lambda _: response)

    with pytest.raises(SupabaseAcceleratorArtifactReadError) as raised:
        reader.read(BUCKET, KEY, max_bytes=100)

    assert str(raised.value) == "accelerator artifact retrieval failed"
    assert SERVICE_KEY not in str(raised.value)
    assert KEY not in str(raised.value)
    assert "diagnostic" not in str(raised.value)


def test_reader_repr_never_includes_service_role_secret() -> None:
    reader = _reader(lambda _: httpx.Response(500))

    assert SERVICE_KEY not in repr(reader)
    assert "127.0.0.1:8000" in repr(reader)
