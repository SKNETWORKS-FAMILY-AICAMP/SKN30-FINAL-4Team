import asyncio

from app.core.config import Settings
from app.db.session import create_database_engine
from app.infrastructure.bizinfo_public_data_client import BizinfoPublicDataClient
from app.infrastructure.openai_embedding_client import OpenAIEmbeddingClient
from app.services.retrieval.announcement_sync import sync_announcements
from app.services.retrieval.corpus_embedding import embed_current_announcements


async def main() -> None:
    settings = Settings()
    if settings.bizinfo_api_key is None:
        raise RuntimeError("BIZINFO_API_KEY is required")
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required")
    engine = create_database_engine(
        str(settings.database_url),
        settings.database_connect_timeout_seconds,
    )
    try:
        sync_result = await sync_announcements(
            engine,
            BizinfoPublicDataClient(
                api_key=settings.bizinfo_api_key.get_secret_value(),
                base_url=str(settings.bizinfo_api_url),
                timeout_seconds=settings.bizinfo_timeout_seconds,
            ),
        )
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
        print(
            f"sync_run={sync_result.sync_run_id} "
            f"fetched={sync_result.rows_fetched} "
            f"embedded={embedding_result.embedded_count}"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
