"""Server-only adapter for private Supabase Storage objects."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.ports.analysis_runs import ObjectStorageUnavailable


class SupabasePrivateObjectStorage:
    """Use the Supabase Storage REST API with a non-browser service key."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = supabase_url.rstrip("/")
        self._service_role_key = service_role_key
        self._timeout = timeout_seconds
        self._transport = transport

    def _ensure_configured(self) -> None:
        if not self._base_url or not self._service_role_key:
            raise ObjectStorageUnavailable("Private object storage is not configured")

    def _url(self, bucket: str, object_key: str) -> str:
        bucket_path = quote(bucket, safe="")
        key_path = quote(object_key, safe="/")
        return f"{self._base_url}/storage/v1/object/{bucket_path}/{key_path}"

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def put(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> None:
        self._ensure_configured()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._url(bucket, object_key),
                    headers={**self._headers(content_type=content_type), "x-upsert": "false"},
                    content=content,
                )
        except httpx.HTTPError as exc:
            raise ObjectStorageUnavailable("Private object storage is unavailable") from exc
        if response.status_code not in {200, 201}:
            raise ObjectStorageUnavailable("Private object storage rejected the upload")

    async def delete(self, *, bucket: str, object_key: str) -> None:
        self._ensure_configured()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.delete(
                    self._url(bucket, object_key),
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ObjectStorageUnavailable("Private object storage is unavailable") from exc
        # A repeated compensating delete is harmless when the object vanished.
        if response.status_code not in {200, 204, 404}:
            raise ObjectStorageUnavailable("Private object storage rejected cleanup")
