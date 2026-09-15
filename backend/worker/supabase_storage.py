"""Trusted synchronous Supabase Storage adapter for the polling worker."""

from __future__ import annotations

from hashlib import sha256
from urllib.parse import quote

import httpx

from .analysis_job import AnalysisJobUnavailable


class SupabaseWorkerStorage:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not supabase_url.strip() or not service_role_key.strip():
            raise ValueError("Supabase URL and service-role key are required")
        if timeout_seconds <= 0:
            raise ValueError("storage timeout must be positive")
        self._base_url = supabase_url.rstrip("/")
        self._key = service_role_key
        self._timeout = timeout_seconds
        self._transport = transport

    def __repr__(self) -> str:
        return f"SupabaseWorkerStorage(base_url={self._base_url!r})"

    def _url(self, bucket: str, object_key: str) -> str:
        return (
            f"{self._base_url}/storage/v1/object/"
            f"{quote(bucket, safe='')}/{quote(object_key, safe='/')}"
        )

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def get(self, *, bucket: str, object_key: str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("storage byte limit must be positive")
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    "GET", self._url(bucket, object_key), headers=self._headers()
                ) as response:
                    if response.status_code != 200:
                        raise AnalysisJobUnavailable("private object download failed")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise AnalysisJobUnavailable("private object exceeds its byte limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except AnalysisJobUnavailable:
            raise
        except httpx.HTTPError:
            raise AnalysisJobUnavailable("private object storage is unavailable") from None

    def put_if_absent(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> bool:
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self._url(bucket, object_key),
                    headers={
                        **self._headers(content_type),
                        "x-upsert": "false",
                    },
                    content=content,
                )
        except httpx.HTTPError:
            raise AnalysisJobUnavailable("private object storage is unavailable") from None
        if response.status_code in {200, 201}:
            return True
        if response.status_code == 409:
            # Keys contain the SHA-256 of ``content``.  A conflict is therefore
            # an idempotent retry only after the existing bytes are checked;
            # a manually poisoned object must never be registered as lineage.
            existing = self.get(
                bucket=bucket,
                object_key=object_key,
                max_bytes=len(content) + 1,
            )
            if sha256(existing).digest() != sha256(content).digest():
                raise AnalysisJobUnavailable(
                    "content-addressed storage object has different content"
                )
            return False
        raise AnalysisJobUnavailable("private object upload failed")

    def delete(self, *, bucket: str, object_key: str) -> None:
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = client.delete(
                    self._url(bucket, object_key), headers=self._headers()
                )
        except httpx.HTTPError:
            raise AnalysisJobUnavailable("private object storage is unavailable") from None
        if response.status_code not in {200, 204, 404}:
            raise AnalysisJobUnavailable("private object cleanup failed")
