import asyncio
import logging
import time
from typing import Any

import httpx

from app.ports.public_data_client import (
    PublicAnnouncement,
    PublicDataClient,
    PublicDataInvalidResponseError,
    PublicDataTimeoutError,
    PublicDataUnavailableError,
)


logger = logging.getLogger(__name__)


class BizinfoPublicDataClient(PublicDataClient):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    async def list_current_announcements(self) -> list[PublicAnnouncement]:
        started_at = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                for attempt in range(2):
                    response = await client.get(
                        self._base_url,
                        params={
                            "crtfcKey": self._api_key,
                            "dataType": "json",
                            "searchCnt": "0",
                        },
                        headers={"Accept": "application/json"},
                    )
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.25)
        except httpx.TimeoutException:
            raise PublicDataTimeoutError("Bizinfo request timed out") from None
        except httpx.RequestError:
            raise PublicDataUnavailableError("Bizinfo is unavailable") from None
        if response.is_error:
            logger.warning(
                "Bizinfo HTTP error status=%s retryable=%s",
                response.status_code,
                response.status_code == 429 or response.status_code >= 500,
            )
            raise PublicDataUnavailableError("Bizinfo rejected the request")

        try:
            body: Any = response.json()
            channel = body["jsonArray"]
            items = channel.get("item", [])
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                raise ValueError
            parsed = [_parse_item(item) for item in items]
            logger.info(
                "Bizinfo request completed items=%s duration_ms=%s",
                len(parsed),
                round((time.perf_counter() - started_at) * 1000),
            )
            return parsed
        except (KeyError, TypeError, ValueError):
            raise PublicDataInvalidResponseError(
                "Bizinfo returned an invalid response"
            ) from None


def _parse_item(item: Any) -> PublicAnnouncement:
    if not isinstance(item, dict):
        raise ValueError
    pblanc_id = _required(item, "pblancId", "seq")
    title = _required(item, "pblancNm", "title")
    url = _required(item, "pblancUrl", "link")
    created_at = _required(item, "creatPnttm", "pubDate")
    return PublicAnnouncement(
        pblanc_id=pblanc_id,
        title=title,
        url=url,
        jurisdiction_name=_optional(item, "jrsdInsttNm", "author"),
        executing_name=_optional(item, "excInsttNm"),
        summary_html=_optional(item, "bsnsSumryCn", "description") or "",
        category_name=_optional(item, "pldirSportRealmLclasCodeNm", "lcategory"),
        source_created_at=created_at,
        source_updated_at=_optional(item, "updtPnttm"),
        application_period=_optional(item, "reqstBeginEndDe", "reqstDt") or "",
        target_name=_optional(item, "trgetNm"),
        view_count=_optional(item, "inqireCo"),
        hashtags=_optional(item, "hashTags", "hashtags") or "",
        request_method_papers=_optional(item, "reqstMthPapersCn"),
        reference_contact=_optional(item, "refrncNm"),
        receipt_homepage_url=_optional(item, "rceptEngnHmpgUrl"),
        attachment_urls=_optional(item, "flpthNm") or "",
        attachment_names=_optional(item, "fileNm") or "",
        print_attachment_url=_optional(item, "printFlpthNm"),
        print_attachment_name=_optional(item, "printFileNm"),
        raw_payload=dict(item),
    )


def _required(item: dict[str, Any], *keys: str) -> str:
    value = _optional(item, *keys)
    if value is None:
        raise ValueError
    return value


def _optional(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None
