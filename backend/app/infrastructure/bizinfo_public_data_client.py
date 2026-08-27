import asyncio
import logging
import time
from dataclasses import dataclass
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

PAGE_SIZE = 500
MAX_PAGES = 100
PAGE_INTERVAL_SECONDS = 0.2
RETRY_INTERVAL_SECONDS = 0.25

# 공공데이터포털 공통 결과 코드.
RESULT_OK = "00"
RESULT_TIMEOUT = "05"
RESULT_RATE_LIMITED = "23"
RESULT_QUOTA_EXCEEDED = "22"
RESULT_KEY_ERRORS = frozenset({"20", "30"})
RETRYABLE_RESULTS = frozenset({RESULT_TIMEOUT, RESULT_RATE_LIMITED})


@dataclass(frozen=True, slots=True)
class _Page:
    items: list[Any]
    total_count: int | None
    page_size: int | None


def _result_code(response: httpx.Response) -> str | None:
    """결과 코드만 꺼낸다. 본문이 JSON 이 아니면 None 을 돌려준다."""
    try:
        header = response.json()["response"]["header"]
    except (KeyError, TypeError, ValueError):
        return None
    code = header.get("resultCode")
    return str(code) if code is not None else None


def _parse_page(response: httpx.Response) -> _Page:
    try:
        body: Any = response.json()
        envelope = body["response"]
        header = envelope["header"]
        payload = envelope["body"]
    except (KeyError, TypeError, ValueError):
        raise PublicDataInvalidResponseError(
            "Bizinfo returned an invalid response"
        ) from None

    code = str(header.get("resultCode"))
    if code != RESULT_OK:
        message = header.get("resultMsg")
        # serviceKey 는 절대 남기지 않는다.
        logger.warning("Bizinfo result code=%s message=%s", code, message)
        if code == RESULT_QUOTA_EXCEEDED:
            raise PublicDataUnavailableError("Bizinfo daily quota exceeded")
        if code in RESULT_KEY_ERRORS:
            raise PublicDataUnavailableError("Bizinfo rejected the service key")
        if code == RESULT_TIMEOUT:
            raise PublicDataTimeoutError("Bizinfo service timed out")
        if code == RESULT_RATE_LIMITED:
            raise PublicDataUnavailableError("Bizinfo rate limit exceeded")
        raise PublicDataInvalidResponseError("Bizinfo returned an error result")

    # 공고가 0건이면 items 가 없거나 빈 문자열로 온다. 예외로 다루지 않는다.
    container = payload.get("items") if isinstance(payload, dict) else None
    items: Any = []
    if isinstance(container, dict):
        items = container.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []

    return _Page(
        items=items,
        total_count=_as_int(payload.get("totalCount")),
        page_size=_as_int(payload.get("numOfRows")),
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        collected: dict[str, PublicAnnouncement] = {}
        total_count: int | None = None
        page_no = 1
        pages_read = 0

        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            while page_no <= MAX_PAGES:
                response = await self._request_page(client, page_no)
                pages_read += 1
                page = _parse_page(response)

                if not page.items:
                    break
                if total_count is None:
                    total_count = page.total_count

                before = len(collected)
                for item in page.items:
                    announcement = _parse_item(item)
                    collected.setdefault(announcement.pblanc_id, announcement)
                # 같은 페이지가 반복되면 서버가 pageNo 를 무시하는 것이므로 멈춘다.
                if len(collected) == before:
                    break
                # 응답이 알려준 실제 페이지 크기로 종료를 판단한다.
                # 요청한 값과 다를 수 있다.
                if len(page.items) < (page.page_size or PAGE_SIZE):
                    break
                if total_count is not None and len(collected) >= total_count:
                    break

                page_no += 1
                await asyncio.sleep(PAGE_INTERVAL_SECONDS)

        logger.info(
            "Bizinfo request completed items=%s pages=%s total_count=%s duration_ms=%s",
            len(collected),
            pages_read,
            total_count,
            round((time.perf_counter() - started_at) * 1000),
        )
        return list(collected.values())

    async def _request_page(
        self,
        client: httpx.AsyncClient,
        page_no: int,
    ) -> httpx.Response:
        try:
            for attempt in range(2):
                response = await client.get(
                    self._base_url,
                    params={
                        # 공공데이터포털의 디코딩 키를 넣는다. 인코딩 키를 넣으면
                        # httpx 가 % 를 다시 인코딩해 인증에 실패한다.
                        "serviceKey": self._api_key,
                        "dataType": "json",
                        "pageNo": str(page_no),
                        "numOfRows": str(PAGE_SIZE),
                    },
                    headers={"Accept": "application/json"},
                )
                retryable = (
                    response.status_code == 429
                    or response.status_code >= 500
                    or _result_code(response) in RETRYABLE_RESULTS
                )
                if not retryable:
                    break
                if attempt == 0:
                    await asyncio.sleep(RETRY_INTERVAL_SECONDS)
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
        return response


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
