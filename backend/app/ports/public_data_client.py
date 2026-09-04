from dataclasses import dataclass
from typing import Any, Protocol


class PublicDataUnavailableError(RuntimeError):
    pass


class PublicDataTimeoutError(RuntimeError):
    pass


class PublicDataInvalidResponseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicAnnouncement:
    pblanc_id: str
    title: str
    url: str
    jurisdiction_name: str | None
    executing_name: str | None
    summary_html: str
    category_name: str | None
    source_created_at: str
    source_updated_at: str | None
    application_period: str
    target_name: str | None
    view_count: str | None
    hashtags: str
    request_method_papers: str | None
    reference_contact: str | None
    receipt_homepage_url: str | None
    attachment_urls: str
    attachment_names: str
    print_attachment_url: str | None
    print_attachment_name: str | None
    raw_payload: dict[str, Any]


class PublicDataClient(Protocol):
    async def list_current_announcements(self) -> list[PublicAnnouncement]: ...
