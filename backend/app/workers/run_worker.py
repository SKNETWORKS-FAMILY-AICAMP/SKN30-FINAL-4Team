import asyncio
import logging
import signal
import sys
from pathlib import Path

# Ensure backend root is in sys.path
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.config import Settings
from app.db.session import create_database_engine
from app.infrastructure.local_object_storage import LocalObjectStorage
from app.infrastructure.openai_embedding_client import OpenAIEmbeddingClient
from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.infrastructure.reportlab_pdf_renderer import ReportLabPdfRenderer
from app.parsers.hwp_parser import RhwpDocumentParser
from app.workers.analysis_worker import AnalysisWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_worker")


async def main() -> None:
    settings = Settings()
    logger.info("Initializing Analysis Worker dependencies...")

    # Database Engine
    engine = create_database_engine(
        str(settings.database_url),
        settings.database_connect_timeout_seconds,
    )

    # Object Storage
    storage = LocalObjectStorage(settings.local_storage_root)

    # Document Parser
    parser = RhwpDocumentParser()

    # LLM & Embedding Clients
    llm_client = None
    if settings.openai_api_key is not None:
        llm_client = OpenAILLMClient(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=str(settings.openai_base_url),
            model_profiles={
                settings.cpl_model_profile: settings.cpl_model_profile,
                settings.fit_model_profile: settings.fit_model_profile,
                settings.sim_model_profile: settings.sim_model_profile,
                settings.chat_model_profile: settings.chat_model_profile,
            },
            timeout_seconds=settings.cpl_llm_timeout_seconds,
        )

    embedding_client = None
    if settings.openai_api_key is not None:
        embedding_client = OpenAIEmbeddingClient(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=str(settings.openai_base_url),
            model_name=settings.embedding_model_name,
            timeout_seconds=settings.embedding_timeout_seconds,
        )

    # PDF Renderer
    pdf_renderer = ReportLabPdfRenderer()

    # Instantiate Worker
    worker = AnalysisWorker(
        engine=engine,
        storage=storage,
        parser=parser,
        llm_client=llm_client,
        embedding_client=embedding_client,
        pdf_renderer=pdf_renderer,
        settings=settings,
        poll_interval=2.0,
        heartbeat_interval=10.0,
    )

    loop = asyncio.get_running_loop()

    # Graceful Shutdown signal handling
    def _shutdown_signal_handler():
        logger.info("Received termination signal, shutting down worker...")
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_signal_handler)
        except NotImplementedError:
            # add_signal_handler is not fully implemented on Windows event loop for some signals
            signal.signal(sig, lambda s, f: _shutdown_signal_handler())

    logger.info("Starting AnalysisWorker background process...")
    try:
        await worker.start()
    except asyncio.CancelledError:
        logger.info("Worker task cancelled.")
    finally:
        logger.info("Analysis worker shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated by user.")
