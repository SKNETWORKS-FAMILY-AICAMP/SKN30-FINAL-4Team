"""Server-only adapter for private Supabase Storage objects."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx

from app.ports.analysis_runs import (
    ObjectStorageUnavailable,
    ObjectStorageWriteUncertain,
)


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
            await self._accept_exact_existing_after_ambiguous_write(
                bucket=bucket,
                object_key=object_key,
                content=content,
                content_type=content_type,
                cause=exc,
            )
            return
        if response.status_code in {200, 201}:
            return
        if response.status_code >= 500 or response.status_code == 408 or _is_duplicate(response):
            await self._accept_exact_existing_after_ambiguous_write(
                bucket=bucket,
                object_key=object_key,
                content=content,
                content_type=content_type,
            )
            return
        raise ObjectStorageUnavailable("Private object storage rejected the upload")

    async def _accept_exact_existing_after_ambiguous_write(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
        cause: Exception | None = None,
    ) -> None:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    self._url(bucket, object_key),
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ObjectStorageWriteUncertain(
                "Private object upload could not be verified"
            ) from exc
        if response.status_code != 200:
            raise ObjectStorageWriteUncertain(
                "Private object upload could not be verified"
            ) from cause
        if not _same_object(response, content=content, content_type=content_type):
            raise ObjectStorageUnavailable(
                "Private object storage contains different object bytes"
            )

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


def _is_duplicate(response: httpx.Response) -> bool:
    if response.status_code == 409:
        return True
    if response.status_code != 400:
        return False
    try:
        payload: Any = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    status_code = payload.get("statusCode")
    error = str(payload.get("error") or "").lower()
    message = str(payload.get("message") or "").lower()
    return str(status_code) == "409" or "duplicate" in error or "already exists" in message


def _same_object(
    response: httpx.Response,
    *,
    content: bytes,
    content_type: str,
) -> bool:
    existing = response.content
    if len(existing) != len(content):
        return False
    if sha256(existing).hexdigest() != sha256(content).hexdigest():
        return False
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) != len(content):
                return False
        except ValueError:
            return False
    existing_type = response.headers.get("content-type")
    if existing_type is not None:
        normalized = existing_type.split(";", 1)[0].strip().lower()
        if normalized != content_type.strip().lower():
            return False
    return True
