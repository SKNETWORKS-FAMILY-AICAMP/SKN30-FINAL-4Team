import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from app.ports.public_data_client import PublicAnnouncement, PublicDataClient


SOURCE_CODE = "BIZINFO_OPEN_API"
KST = ZoneInfo("Asia/Seoul")
_DATE_RANGE = re.compile(r"(?P<start>\d{4}[.\-/]?\d{2}[.\-/]?\d{2})\s*~\s*(?P<end>\d{4}[.\-/]?\d{2}[.\-/]?\d{2})")
_DETAIL_REFERENCE = re.compile(
    r"(?:※\s*)?(?:자세한|상세한)?\s*(?:내용은\s*)?(?:첨부\s*)?공고문\s*(?:을|를)?\s*참조(?:하세요|하시기 바랍니다|바랍니다)?[.!]?",
    re.IGNORECASE,
)


class SyncAlreadyRunningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnnouncementAttachment:
    role: Literal["PRIMARY", "AUXILIARY"]
    ordinal_no: int
    source_url: str
    original_filename: str


@dataclass(frozen=True, slots=True)
class NormalizedAnnouncement:
    pblanc_id: str
    title: str
    url: str
    jurisdiction_name: str | None
    executing_name: str | None
    summary_html: str
    summary_text: str
    purpose: str
    target: str
    content: str
    detail_ref_fields: list[str]
    category_name: str | None
    source_created_at: datetime
    source_updated_at: datetime | None
    target_name: str | None
    view_count: int | None
    hashtags: list[str]
    request_method_papers: str | None
    reference_contact: str | None
    receipt_homepage_url: str | None
    period_raw_text: str
    period_type: str
    period_start_date: date | None
    period_end_date: date | None
    period_display_text: str
    search_status: str
    attachments: list[AnnouncementAttachment]
    raw_payload: dict[str, Any]
    content_sha256_hex: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    sync_run_id: int
    rows_fetched: int
    rows_inserted: int
    rows_versioned: int
    rows_unchanged: int
    reused_success: bool = False


async def sync_announcements(
    engine: Engine,
    client: PublicDataClient,
    *,
    sync_date_kst: date | None = None,
) -> SyncResult:
    target_date = sync_date_kst or datetime.now(KST).date()
    with engine.connect() as connection:
        locked = connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:lock_name))"),
            {"lock_name": f"sims:{SOURCE_CODE}:daily-sync"},
        )
        connection.commit()
        if not locked:
            raise SyncAlreadyRunningError("Announcement synchronization is running")

        sync_run_id: int | None = None
        try:
            claimed = _claim_sync_run(connection, target_date)
            if isinstance(claimed, SyncResult):
                return claimed
            sync_run_id = claimed
            fetch_started_at = time.perf_counter()
            source_items = await client.list_current_announcements()
            source_fetch_latency_ms = round(
                (time.perf_counter() - fetch_started_at) * 1000
            )
            normalized = [normalize_announcement(item, target_date) for item in source_items]
            inserted = versioned = unchanged = 0
            latest_created_at: datetime | None = None
            with connection.begin():
                for item in normalized:
                    outcome = _upsert_announcement(connection, sync_run_id, item)
                    inserted += outcome == "inserted"
                    versioned += outcome == "versioned"
                    unchanged += outcome == "unchanged"
                    if latest_created_at is None or item.source_created_at > latest_created_at:
                        latest_created_at = item.source_created_at
                connection.execute(
                    text(
                        """
                        UPDATE sims.announcement_version av
                        SET search_status = 'CLOSED',
                            status_checked_at = statement_timestamp(),
                            status_source = 'PERIOD_RULE'
                        FROM sims.announcement a
                        WHERE a.id = av.announcement_id
                          AND a.source_code = :source_code
                          AND av.is_current
                          AND av.period_type = 'FIXED'
                          AND av.period_end_date < :sync_date
                          AND av.search_status <> 'CLOSED'
                        """
                    ),
                    {"source_code": SOURCE_CODE, "sync_date": target_date},
                )
                connection.execute(
                    text(
                        """
                        UPDATE sims.api_sync_run
                        SET status = 'SUCCEEDED', completed_at = statement_timestamp(),
                            rows_fetched = :fetched, rows_inserted = :inserted,
                            rows_versioned = :versioned, rows_unchanged = :unchanged,
                            latest_source_created_at = :latest_created_at,
                            error_code = NULL, error_message = NULL,
                            statistics = CAST(:statistics AS jsonb)
                        WHERE id = :sync_run_id AND status = 'RUNNING'
                        """
                    ),
                    {
                        "sync_run_id": sync_run_id,
                        "fetched": len(normalized),
                        "inserted": inserted,
                        "versioned": versioned,
                        "unchanged": unchanged,
                        "latest_created_at": latest_created_at,
                        "statistics": json.dumps(
                            {
                                "source": SOURCE_CODE,
                                "sync_date_kst": str(target_date),
                                "source_fetch_latency_ms": source_fetch_latency_ms,
                            }
                        ),
                    },
                )
            return SyncResult(
                sync_run_id=sync_run_id,
                rows_fetched=len(normalized),
                rows_inserted=inserted,
                rows_versioned=versioned,
                rows_unchanged=unchanged,
            )
        except Exception as error:
            if connection.in_transaction():
                connection.rollback()
            if sync_run_id is not None:
                with connection.begin():
                    connection.execute(
                        text(
                            """
                            UPDATE sims.api_sync_run
                            SET status = 'FAILED', completed_at = statement_timestamp(),
                                error_code = 'SOURCE_SYNC_FAILED',
                                error_message = :error_message
                            WHERE id = :sync_run_id AND status = 'RUNNING'
                            """
                        ),
                        {
                            "sync_run_id": sync_run_id,
                            "error_message": type(error).__name__,
                        },
                    )
            raise
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:lock_name))"),
                {"lock_name": f"sims:{SOURCE_CODE}:daily-sync"},
            )
            connection.commit()


def normalize_announcement(
    source: PublicAnnouncement,
    as_of_date: date,
) -> NormalizedAnnouncement:
    summary_text = _html_to_text(source.summary_html)
    parts = [part.strip() for part in summary_text.split("☞", 2)]
    purpose = parts[0] if parts else ""
    target = parts[1] if len(parts) > 1 else ""
    content = parts[2] if len(parts) > 2 else ""
    detail_ref_fields = [
        field
        for field, value in (("target", target), ("content", content))
        if _DETAIL_REFERENCE.search(value)
    ]
    hashtags = [value.strip() for value in source.hashtags.split(",") if value.strip()]
    period_type, start_date, end_date = _parse_period(source.application_period)
    status = _search_status(period_type, end_date, as_of_date)
    attachments = _attachments(source)
    version_fields = {
        "pblanc_id": source.pblanc_id.strip(),
        "title": source.title.strip(),
        "url": source.url.strip(),
        "jurisdiction_name": source.jurisdiction_name,
        "executing_name": source.executing_name,
        "summary_html": source.summary_html,
        "summary_text": summary_text,
        "purpose": purpose,
        "target": target,
        "content": content,
        "detail_ref_fields": detail_ref_fields,
        "category_name": source.category_name,
        "source_created_at": source.source_created_at,
        "source_updated_at": source.source_updated_at,
        "target_name": source.target_name,
        "view_count": _nonnegative_int(source.view_count),
        "hashtags": hashtags,
        "request_method_papers": source.request_method_papers,
        "reference_contact": source.reference_contact,
        "receipt_homepage_url": source.receipt_homepage_url,
        "period_raw_text": source.application_period.strip(),
        "attachments": [asdict(item) for item in attachments],
    }
    digest = hashlib.sha256(
        json.dumps(
            version_fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return NormalizedAnnouncement(
        pblanc_id=version_fields["pblanc_id"],
        title=version_fields["title"],
        url=version_fields["url"],
        jurisdiction_name=source.jurisdiction_name,
        executing_name=source.executing_name,
        summary_html=source.summary_html,
        summary_text=summary_text,
        purpose=purpose,
        target=target,
        content=content,
        detail_ref_fields=detail_ref_fields,
        category_name=source.category_name,
        source_created_at=_parse_timestamp(source.source_created_at),
        source_updated_at=(
            _parse_timestamp(source.source_updated_at)
            if source.source_updated_at
            else None
        ),
        target_name=source.target_name,
        view_count=version_fields["view_count"],
        hashtags=hashtags,
        request_method_papers=source.request_method_papers,
        reference_contact=source.reference_contact,
        receipt_homepage_url=source.receipt_homepage_url,
        period_raw_text=source.application_period.strip(),
        period_type=period_type,
        period_start_date=start_date,
        period_end_date=end_date,
        period_display_text=source.application_period.strip() or "기간 확인 필요",
        search_status=status,
        attachments=attachments,
        raw_payload=source.raw_payload,
        content_sha256_hex=digest,
    )


def announcement_embedding_text(item: NormalizedAnnouncement) -> str:
    return compose_embedding_text(
        title=item.title,
        purpose=item.purpose,
        target=item.target,
        content=item.content,
        hashtags=item.hashtags,
    )


def compose_embedding_text(
    *,
    title: str = "",
    purpose: str = "",
    target: str = "",
    content: str = "",
    hashtags: list[str] | None = None,
) -> str:
    values = [
        ("공고명", title),
        ("사업목적", purpose),
        ("지원대상", _DETAIL_REFERENCE.sub("", target).strip()),
        ("지원내용", _DETAIL_REFERENCE.sub("", content).strip()),
        ("해시태그", ", ".join(hashtags or [])),
    ]
    return "\n".join(f"{label}: {value}" for label, value in values if value)


def _claim_sync_run(connection: Connection, target_date: date) -> int | SyncResult:
    with connection.begin():
        row = connection.execute(
            text(
                """
                SELECT id, status, rows_fetched, rows_inserted,
                       rows_versioned, rows_unchanged
                FROM sims.api_sync_run
                WHERE source_code = :source_code AND sync_date_kst = :sync_date
                FOR UPDATE
                """
            ),
            {"source_code": SOURCE_CODE, "sync_date": target_date},
        ).mappings().one_or_none()
        if row is not None and row["status"] == "SUCCEEDED":
            return SyncResult(
                sync_run_id=row["id"],
                rows_fetched=row["rows_fetched"],
                rows_inserted=row["rows_inserted"],
                rows_versioned=row["rows_versioned"],
                rows_unchanged=row["rows_unchanged"],
                reused_success=True,
            )
        if row is None:
            sync_run_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.api_sync_run (
                        source_code, sync_date_kst, status, started_at
                    ) VALUES (
                        :source_code, :sync_date, 'RUNNING', statement_timestamp()
                    ) RETURNING id
                    """
                ),
                {"source_code": SOURCE_CODE, "sync_date": target_date},
            )
        else:
            sync_run_id = row["id"]
            connection.execute(
                text(
                    """
                    UPDATE sims.api_sync_run
                    SET status = 'RUNNING', attempt_count = attempt_count + 1,
                        started_at = statement_timestamp(), completed_at = NULL,
                        error_code = NULL, error_message = NULL
                    WHERE id = :sync_run_id
                    """
                ),
                {"sync_run_id": sync_run_id},
            )
    if sync_run_id is None:
        raise RuntimeError("Failed to create synchronization run")
    return int(sync_run_id)


def _upsert_announcement(
    connection: Connection,
    sync_run_id: int,
    item: NormalizedAnnouncement,
) -> Literal["inserted", "versioned", "unchanged"]:
    current = connection.execute(
        text(
            """
            SELECT a.id, av.id AS version_id, av.version_no, av.content_sha256_hex
            FROM sims.announcement a
            LEFT JOIN sims.announcement_version av
              ON av.announcement_id = a.id AND av.is_current
            WHERE a.pblanc_id = :pblanc_id
            FOR UPDATE OF a
            """
        ),
        {"pblanc_id": item.pblanc_id},
    ).mappings().one_or_none()
    if current is None:
        announcement_id = connection.scalar(
            text(
                """
                INSERT INTO sims.announcement (
                    source_code, pblanc_id, first_seen_at, last_seen_at
                ) VALUES (
                    :source_code, :pblanc_id, statement_timestamp(), statement_timestamp()
                ) RETURNING id
                """
            ),
            {"source_code": SOURCE_CODE, "pblanc_id": item.pblanc_id},
        )
        version_no = 1
        outcome: Literal["inserted", "versioned", "unchanged"] = "inserted"
    else:
        announcement_id = current["id"]
        connection.execute(
            text(
                "UPDATE sims.announcement SET last_seen_at = statement_timestamp() WHERE id = :id"
            ),
            {"id": announcement_id},
        )
        if current["content_sha256_hex"] == item.content_sha256_hex:
            connection.execute(
                text(
                    """
                    UPDATE sims.announcement_version
                    SET search_status = :search_status,
                        status_checked_at = statement_timestamp(),
                        status_source = 'PERIOD_RULE'
                    WHERE id = :version_id
                    """
                ),
                {"version_id": current["version_id"], "search_status": item.search_status},
            )
            return "unchanged"
        connection.execute(
            text(
                """
                UPDATE sims.announcement_version
                SET is_current = false, valid_to = statement_timestamp()
                WHERE id = :version_id AND is_current
                """
            ),
            {"version_id": current["version_id"]},
        )
        version_no = int(current["version_no"]) + 1
        outcome = "versioned"

    version_id = connection.scalar(
        text(
            """
            INSERT INTO sims.announcement_version (
                announcement_id, source_sync_run_id, version_no, content_sha256_hex,
                pblanc_nm, pblanc_url, jrsd_instt_nm, exc_instt_nm,
                bsns_sumry_html, bsns_sumry_text, purpose, target, content,
                detail_ref_fields, category_name, source_created_at, source_updated_at,
                target_name, view_count, hashtags, request_method_papers,
                reference_contact, receipt_homepage_url, period_raw_text,
                period_type, period_start_date, period_end_date, period_display_text,
                search_status, status_checked_at, status_source, raw_payload
            ) VALUES (
                :announcement_id, :sync_run_id, :version_no, :content_sha256_hex,
                :title, :url, :jurisdiction_name, :executing_name,
                :summary_html, :summary_text, :purpose, :target, :content,
                :detail_ref_fields, :category_name, :source_created_at, :source_updated_at,
                :target_name, :view_count, :hashtags, :request_method_papers,
                :reference_contact, :receipt_homepage_url, :period_raw_text,
                :period_type, :period_start_date, :period_end_date, :period_display_text,
                :search_status, statement_timestamp(), 'PERIOD_RULE', CAST(:raw_payload AS jsonb)
            ) RETURNING id
            """
        ),
        {
            "announcement_id": announcement_id,
            "sync_run_id": sync_run_id,
            "version_no": version_no,
            "content_sha256_hex": item.content_sha256_hex,
            "title": item.title,
            "url": item.url,
            "jurisdiction_name": item.jurisdiction_name,
            "executing_name": item.executing_name,
            "summary_html": item.summary_html,
            "summary_text": item.summary_text,
            "purpose": item.purpose,
            "target": item.target,
            "content": item.content,
            "detail_ref_fields": item.detail_ref_fields,
            "category_name": item.category_name,
            "source_created_at": item.source_created_at,
            "source_updated_at": item.source_updated_at,
            "target_name": item.target_name,
            "view_count": item.view_count,
            "hashtags": item.hashtags,
            "request_method_papers": item.request_method_papers,
            "reference_contact": item.reference_contact,
            "receipt_homepage_url": item.receipt_homepage_url,
            "period_raw_text": item.period_raw_text,
            "period_type": item.period_type,
            "period_start_date": item.period_start_date,
            "period_end_date": item.period_end_date,
            "period_display_text": item.period_display_text,
            "search_status": item.search_status,
            "raw_payload": json.dumps(item.raw_payload, ensure_ascii=False),
        },
    )
    if version_id is None:
        raise RuntimeError("Failed to create announcement version")
    if item.attachments:
        connection.execute(
            text(
                """
                INSERT INTO sims.announcement_attachment (
                    announcement_version_id, attachment_role, ordinal_no,
                    source_url, original_filename, extension
                ) VALUES (
                    :version_id, :role, :ordinal_no,
                    :source_url, :original_filename, :extension
                )
                """
            ),
            [
                {
                    "version_id": version_id,
                    "role": attachment.role,
                    "ordinal_no": attachment.ordinal_no,
                    "source_url": attachment.source_url,
                    "original_filename": attachment.original_filename,
                    "extension": PurePosixPath(attachment.original_filename).suffix.lower().lstrip(".") or None,
                }
                for attachment in item.attachments
            ],
        )
    return outcome


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def _parse_timestamp(value: str) -> datetime:
    cleaned = value.strip()
    parsed: datetime | None = None
    for format_string in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, format_string)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            raise ValueError("Invalid Bizinfo timestamp") from None
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed


def _parse_period(value: str) -> tuple[str, date | None, date | None]:
    cleaned = " ".join(value.split())
    match = _DATE_RANGE.search(cleaned)
    if match:
        return (
            "FIXED",
            _parse_date(match.group("start")),
            _parse_date(match.group("end")),
        )
    if "예산 소진" in cleaned or "소진시" in cleaned:
        return "UNTIL_EXHAUSTED", None, None
    if "상시" in cleaned:
        return "ALWAYS", None, None
    if "모집 완료" in cleaned or "충원" in cleaned:
        return "UNTIL_FILLED", None, None
    if "세부" in cleaned and "상이" in cleaned:
        return "BY_SUBPROGRAM", None, None
    if cleaned:
        return "VARIABLE", None, None
    return "UNKNOWN", None, None


def _parse_date(value: str) -> date:
    digits = re.sub(r"\D", "", value)
    return datetime.strptime(digits, "%Y%m%d").date()


def _search_status(period_type: str, end_date: date | None, as_of_date: date) -> str:
    if period_type == "FIXED" and end_date is not None:
        return "CLOSED" if end_date < as_of_date else "OPEN"
    return "UNKNOWN"


def _attachments(source: PublicAnnouncement) -> list[AnnouncementAttachment]:
    result: list[AnnouncementAttachment] = []
    if source.print_attachment_url:
        result.append(
            AnnouncementAttachment(
                role="PRIMARY",
                ordinal_no=0,
                source_url=source.print_attachment_url,
                original_filename=source.print_attachment_name
                or _filename_from_url(source.print_attachment_url),
            )
        )
    urls = [value.strip() for value in source.attachment_urls.split("@") if value.strip()]
    names = [value.strip() for value in source.attachment_names.split("@")]
    for index, url in enumerate(urls):
        result.append(
            AnnouncementAttachment(
                role="AUXILIARY",
                ordinal_no=index,
                source_url=url,
                original_filename=(names[index] if index < len(names) and names[index] else _filename_from_url(url)),
            )
        )
    return result


def _filename_from_url(value: str) -> str:
    return PurePosixPath(urlparse(value).path).name or "attachment"


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.replace(",", ""))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
