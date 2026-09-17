"""Security-focused tests for the worker's service-role Storage adapter."""

from __future__ import annotations

import httpx

from worker.supabase_storage import SupabaseWorkerStorage


def test_every_service_role_client_disables_ambient_environment(monkeypatch) -> None:
    seen: list[bool] = []
    real_client = httpx.Client

    def recording_client(*args: object, **kwargs: object) -> httpx.Client:
        seen.append(kwargs["trust_env"])
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", recording_client)
    storage = SupabaseWorkerStorage(
        supabase_url="http://supabase.internal:8000",
        service_role_key="server-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    storage.get(bucket="request-temp", object_key="input.bin", max_bytes=10)
    assert storage.put_if_absent(
        bucket="request-temp",
        object_key="output.bin",
        content=b"payload",
        content_type="application/octet-stream",
    )
    storage.delete(bucket="request-temp", object_key="output.bin")

    assert seen == [False, False, False]
