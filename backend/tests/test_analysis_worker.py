import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.workers.analysis_worker import AnalysisWorker


@pytest.fixture
def mock_worker():
    engine = MagicMock()
    storage = MagicMock()
    parser = MagicMock()
    llm_client = MagicMock()
    embedding_client = MagicMock()
    pdf_renderer = MagicMock()
    settings = Settings()

    worker = AnalysisWorker(
        engine=engine,
        storage=storage,
        parser=parser,
        llm_client=llm_client,
        embedding_client=embedding_client,
        pdf_renderer=pdf_renderer,
        settings=settings,
        poll_interval=0.01,
        heartbeat_interval=0.05,
    )
    return worker


def test_worker_initialization(mock_worker):
    assert mock_worker.poll_interval == 0.01
    assert mock_worker.heartbeat_interval == 0.05
    assert not mock_worker._is_running


def test_claim_when_table_not_found(mock_worker):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.scalar.return_value = None
    mock_worker.engine.begin.return_value.__enter__.return_value = mock_conn

    assert mock_worker.claim_next_analysis_run() is None
    assert mock_worker.claim_next_ops_run() is None


def test_claim_analysis_run_success(mock_worker):
    mock_conn = MagicMock()
    run_id = str(uuid4())
    fake_row = {
        "analysis_run_pk": run_id,
        "user_id": str(uuid4()),
        "source_bucket": "request-temp",
        "source_object_key": "user/file.hwp",
        "original_filename": "test.hwp",
        "declared_mime_type": "application/x-hwp",
        "declared_size_bytes": 1024,
        "attempt_count": 1,
    }

    mock_conn.execute.return_value.scalar.return_value = 1
    mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = fake_row
    mock_worker.engine.begin.return_value.__enter__.return_value = mock_conn

    claimed = mock_worker.claim_next_analysis_run()
    assert claimed is not None
    assert claimed["analysis_run_pk"] == run_id
    assert claimed["original_filename"] == "test.hwp"


def test_heartbeat_loop_execution(mock_worker):
    async def _test():
        stop_event = asyncio.Event()
        with patch.object(mock_worker, "_update_heartbeat") as mock_hb:
            hb_task = asyncio.create_task(
                mock_worker._heartbeat_loop("workspace.analysis_run", "analysis_run_pk", "test-pk", stop_event)
            )
            await asyncio.sleep(0.12)  # Wait for at least 2 ticks
            stop_event.set()
            await hb_task
            assert mock_hb.call_count >= 1

    asyncio.run(_test())


def test_worker_start_stop(mock_worker):
    async def _test():
        with patch.object(mock_worker, "claim_next_analysis_run", return_value=None):
            with patch.object(mock_worker, "claim_next_ops_run", return_value=None):
                worker_task = asyncio.create_task(mock_worker.start())
                await asyncio.sleep(0.03)
                assert mock_worker._is_running
                mock_worker.stop()
                await worker_task
                assert not mock_worker._is_running

    asyncio.run(_test())


def test_update_heartbeat_whitelist_enforcement(mock_worker):
    # Valid target succeeds
    mock_conn = MagicMock()
    mock_worker.engine.begin.return_value.__enter__.return_value = mock_conn
    mock_worker._update_heartbeat("workspace.analysis_run", "analysis_run_pk", "123")
    assert mock_conn.execute.call_count == 1

    # Unauthorized target raises ValueError (preventing SQL injection)
    with pytest.raises(ValueError, match="Unauthorized heartbeat target"):
        mock_worker._update_heartbeat("unauthorized_table", "pk", "123")


def test_load_file_bytes_behavior(mock_worker):
    async def _test():
        # Missing key raises ValueError
        with pytest.raises(ValueError, match="source_key is missing"):
            await mock_worker._load_file_bytes("", "test.hwp")

        # Storage open failure raises FileNotFoundError (no fake bytes)
        mock_worker.storage.open = AsyncMock(side_effect=IOError("Storage down"))
        with pytest.raises(FileNotFoundError, match="Storage file could not be read"):
            await mock_worker._load_file_bytes("some/key", "test.hwp")

        # Storage success returns valid bytes
        mock_file = MagicMock()
        mock_file.read.return_value = b"VALID_BYTES"
        mock_worker.storage.open = AsyncMock(return_value=mock_file)
        data = await mock_worker._load_file_bytes("some/key", "test.hwp")
        assert data == b"VALID_BYTES"

    asyncio.run(_test())
