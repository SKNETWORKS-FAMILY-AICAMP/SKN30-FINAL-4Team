"""기업마당 인증키 없이 E2E 를 돌리기 위해 공고 6건을 넣는다.

실제 동기화 경로(sync_announcements → embed_current_announcements)를 그대로 쓰고
네트워크 호출만 스텁으로 바꾼다. 임베딩은 진짜 OpenAI 로 만든다.

    python scripts/seed_announcements.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import Settings  # noqa: E402
from app.db.session import create_database_engine  # noqa: E402
from app.infrastructure.openai_embedding_client import OpenAIEmbeddingClient  # noqa: E402
from app.ports.public_data_client import PublicAnnouncement  # noqa: E402
from app.services.retrieval.announcement_sync import sync_announcements  # noqa: E402
from app.services.retrieval.corpus_embedding import (  # noqa: E402
    embed_current_announcements,
)

PERIOD = "2026-08-01 ~ 2026-12-31"

# (id, 제목, 소관, 수행기관, 요약, 분류, 대상)
ITEMS = [
    (
        "MOCK-ICT-0001",
        "2026년 ICT 미래시장 선점 R&D 지원사업 공고",
        "과학기술정보통신부",
        "정보통신기획평가원",
        "<p>ICT혁신기업이 신시장 창출 동력을 확보하여 고성장 기업으로 도약할 수 있도록 "
        "시장예측 기반 단계별 기술개발 및 사업화를 지원합니다. 지원대상은 ICT분야 "
        "중소·벤처기업(법인)이며, 신시장 선점을 위하여 기업간 M&A 또는 전략적 제휴를 "
        "계획하고 있는 기업입니다. 지원규모는 과제당 총 9억원에서 15억원, 연간 6억원이며 "
        "지원기간은 최대 3년(2년+1년)입니다. 시장수요최적화기술개발과 "
        "고성장기업도약기술개발을 단계별로 추진합니다.</p>",
        "기술",
        "중소기업",
    ),
    (
        "MOCK-COMM-0002",
        "2026년 중소기업 기술사업화 지원사업 공고",
        "중소벤처기업부",
        "중소기업기술정보진흥원",
        "<p>기술력은 있으나 자금력이 취약한 중소기업의 잠재적 보유기술을 상품화하여 "
        "시장 진입과 매출 증대를 지원합니다. 시제품 제작, 디자인 개발, 인체적용시험, "
        "성과 분석 및 평가비 등을 지원하며 총 15개사를 선정합니다. "
        "사업기간은 2026년 1월부터 12월까지 단년도입니다.</p>",
        "기술",
        "중소기업",
    ),
    (
        "MOCK-PROTO-0003",
        "2026년 시제품 제작 및 디자인 개발 바우처 지원사업",
        "중소벤처기업부",
        "창업진흥원",
        "<p>창업 7년 이내 중소기업을 대상으로 시제품 제작과 디자인 개발 비용을 "
        "바우처 형태로 지원합니다. 기업당 최대 5천만원, 총 40개사를 지원합니다.</p>",
        "창업",
        "중소기업",
    ),
    (
        "MOCK-SMART-0004",
        "2026년 스마트공장 구축 및 고도화 지원사업 공고",
        "중소벤처기업부",
        "스마트제조혁신추진단",
        "<p>제조 중소기업의 생산성 향상을 위해 스마트공장 솔루션 도입과 "
        "설비 연동을 지원합니다. 기초 단계 최대 7천만원, 고도화 단계 최대 2억원을 "
        "지원하며 매출액 3천억원 미만 중소기업이 대상입니다.</p>",
        "기술",
        "중소기업",
    ),
    (
        "MOCK-EXPORT-0005",
        "2026년 수출바우처 지원사업 통합공고",
        "중소벤처기업부",
        "중소기업진흥공단",
        "<p>수출 중소기업의 해외 마케팅을 지원합니다. 전년도 수출액 규모에 따라 "
        "3천만원에서 1억원까지 바우처를 지급하며 통번역, 해외규격인증, 전시회 참가 등에 "
        "사용할 수 있습니다.</p>",
        "수출",
        "중소기업",
    ),
    (
        "MOCK-HR-0006",
        "2026년 중소기업 인력양성 및 채용연계 지원사업",
        "고용노동부",
        "한국산업인력공단",
        "<p>중소기업의 인력난 해소를 위해 직무교육과 채용연계를 지원합니다. "
        "참여기업에 1인당 월 100만원의 인건비를 최대 6개월간 지원합니다.</p>",
        "인력",
        "중소기업",
    ),
]


def _announcement(item: tuple[str, ...]) -> PublicAnnouncement:
    pblanc_id, title, jurisdiction, executing, summary, category, target = item
    return PublicAnnouncement(
        pblanc_id=pblanc_id,
        title=title,
        url=f"https://www.bizinfo.go.kr/mock/{pblanc_id}",
        jurisdiction_name=jurisdiction,
        executing_name=executing,
        summary_html=summary,
        category_name=category,
        source_created_at="2026-08-01 09:00:00",
        source_updated_at="2026-08-01 09:00:00",
        application_period=PERIOD,
        target_name=target,
        view_count="0",
        hashtags="",
        request_method_papers="온라인 신청",
        reference_contact="1357",
        receipt_homepage_url="https://www.bizinfo.go.kr/",
        attachment_urls="",
        attachment_names="",
        print_attachment_url=None,
        print_attachment_name=None,
        raw_payload={"pblancId": pblanc_id, "mock": True},
    )


class StubClient:
    """네트워크 대신 고정 목록을 돌려준다."""

    async def list_current_announcements(self) -> list[PublicAnnouncement]:
        return [_announcement(item) for item in ITEMS]


async def main() -> None:
    settings = Settings()
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required")
    engine = create_database_engine(
        str(settings.database_url),
        settings.database_connect_timeout_seconds,
    )
    try:
        sync_result = await sync_announcements(engine, StubClient())
        print(f"동기화  run={sync_result.sync_run_id}  건수={sync_result.rows_fetched}")

        embedding_result = await embed_current_announcements(
            engine,
            OpenAIEmbeddingClient(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=str(settings.openai_base_url),
                model_name=settings.embedding_model_name,
                timeout_seconds=settings.embedding_timeout_seconds,
            ),
            requested_model_name=settings.embedding_model_name,
            profile_name=settings.embedding_profile_name,
            profile_version=settings.embedding_profile_version,
            preprocessing_version=settings.embedding_preprocessing_version,
            batch_size=settings.embedding_batch_size,
        )
        print(f"임베딩  건수={embedding_result.embedded_count}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
