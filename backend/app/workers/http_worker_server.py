"""HTTP Worker Server for Supabase Architecture v2.2.

Listens on 0.0.0.0:8080 and handles POST /jobs from Supabase Edge Functions.
Does NOT connect directly to PostgreSQL.
Uses signed URLs and worker callback tokens only.
"""
import asyncio
import io
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure backend root is in sys.path
backend_dir = Path(__file__).resolve().parent.parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.parsers.hwp_parser import RhwpDocumentParser
from app.ports.document_parser import FileSource
from app.schemas.cpl import CplResult
from app.services.cpl.logic_validator import evaluate_cpl_rules

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("http_worker_server")

# Load settings
try:
    settings = Settings()
except Exception:
    settings = None

# Dispatch token from env or default
DISPATCH_TOKEN = os.getenv("ANALYSIS_WORKER_DISPATCH_TOKEN", "pre-review-worker-dispatch-token-2026")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")

# In-memory idempotency cache: idempotency_key -> worker_job_id
_IDEMPOTENCY_CACHE: Dict[str, str] = {}

app = FastAPI(title="Supabase Analysis Worker", version="1.0.0")


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "supabase-analysis-worker",
        "bind": "0.0.0.0:8080",
    }


def _build_request_profile(
    analysis_run_id: str,
    original_filename: str,
    cpl_result: CplResult,
) -> Dict[str, Any]:
    """Convert CplResult into pre_review_request_profile/v0.1 format."""
    profile_uuid = str(uuid.uuid4())
    ir_uuid = str(uuid.uuid4())

    # Extract title/program_name from items if available
    program_name = Path(original_filename).stem
    requesting_org = "신청기관"

    # Extract request_type from CPL items if present
    selected_code = "NEW_DETAIL_BIZ"
    value_raw = "세부사업 신설"
    for item in cpl_result.items:
        if item.field_code.value in ("REQUEST_TYPE", "BIZ_TYPE") and item.occurrences:
            value_raw = item.occurrences[0].raw_text
            break

    # Build comparison profile facts from occurrences
    comparison_facts: Dict[str, list] = {}
    for item in cpl_result.items:
        field_key = item.field_code.value.lower()
        facts_list = []
        for idx, occ in enumerate(item.occurrences):
            facts_list.append({
                "fact_id": f"fact-{uuid.uuid4()}",
                "field_name": field_key,
                "value_raw": occ.raw_text,
                "status": "identified",
                "ordinal": idx,
                "value_source": {
                    "source_block_id": occ.source_locator.get("paragraph_index", f"blk-{idx}"),
                    "start_char": 0,
                    "end_char": len(occ.raw_text),
                    "text_basis": occ.raw_text,
                },
                "evidence": [],
            })
        if facts_list:
            comparison_facts[field_key] = facts_list

    return {
        "schema_version": "pre_review_request_profile/v0.1",
        "profile_id": profile_uuid,
        "identity": {
            "program_name": program_name,
            "requesting_organization": requesting_org,
            "source_document_id": analysis_run_id,
        },
        "processing_metadata": {
            "common_ir_document_id": ir_uuid,
            "candidate_pack": {
                "id": str(uuid.uuid4()),
                "generator": "cpl_engine",
                "generator_version": "1.0",
            },
        },
        "request_type": {
            "selected_code": selected_code,
            "value_raw": value_raw,
            "value_source": {
                "source_block_id": "blk-req-type",
                "start_char": 0,
                "end_char": len(value_raw),
                "text_basis": value_raw,
            },
            "selection_source": {
                "glyph_raw": "■",
                "source_block_id": "blk-req-type",
                "start_char": 0,
                "end_char": 1,
                "text_basis": "■",
            },
            "evidence": [],
        },
        "program_hierarchy": {"nodes": []},
        "support_components": [],
        "comparison_profile": comparison_facts,
        "request_context": {},
        "derived_projections": [],
    }


async def _process_analysis_job(job: Dict[str, Any], worker_job_id: str) -> None:
    """Background worker job execution pipeline."""
    analysis_run_id = job.get("analysis_run_id")
    source = job.get("source", {})
    callback = job.get("callback", {})
    
    logger.info(f"[{worker_job_id}] Starting analysis for run={analysis_run_id}")
    
    download_url = source.get("download_url")
    bucket = source.get("bucket", "request-temp")
    object_key = source.get("object_key", "")
    original_filename = source.get("original_filename", "request.hwpx")
    content_type = source.get("content_type") or "application/hwp+zip"
    
    callback_url = callback.get("ingest_request_profile_url")
    callback_token = callback.get("callback_token")

    # 1. Download file content
    file_bytes: Optional[bytes] = None
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        if download_url:
            logger.info(f"[{worker_job_id}] Downloading from signed URL: {download_url[:60]}...")
            res = await http_client.get(download_url)
            if res.status_code == 200:
                file_bytes = res.content
            else:
                logger.error(f"[{worker_job_id}] Failed to download from signed URL: {res.status_code}")
        
        # Fallback: Storage API direct if download_url not supplied or failed
        if file_bytes is None and SUPABASE_URL and object_key:
            storage_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{object_key}"
            logger.info(f"[{worker_job_id}] Attempting Storage URL fallback: {storage_url}")
            headers = {}
            if SUPABASE_ANON_KEY:
                headers["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"
            res = await http_client.get(storage_url, headers=headers)
            if res.status_code == 200:
                file_bytes = res.content
            else:
                logger.error(f"[{worker_job_id}] Storage fallback failed: {res.status_code}")

    if file_bytes is None:
        logger.error(f"[{worker_job_id}] Aborting: Could not obtain file bytes for run={analysis_run_id}")
        return

    logger.info(f"[{worker_job_id}] Successfully downloaded file ({len(file_bytes)} bytes)")

    # 2. Parse HWP / HWPX
    ext = Path(original_filename).suffix.lstrip(".").lower() or "hwpx"
    parser = RhwpDocumentParser()
    file_source = FileSource(
        content=io.BytesIO(file_bytes),
        filename=original_filename,
        mime_type=content_type,
        extension=ext,
    )
    
    try:
        parsed_doc = await parser.parse(file_source)
        logger.info(f"[{worker_job_id}] Parsed document: {len(parsed_doc.blocks)} blocks, text len={len(parsed_doc.text)}")
    except Exception as e:
        logger.error(f"[{worker_job_id}] Document parsing failed: {e}", exc_info=True)
        return

    # 3. CPL Rule Evaluation
    try:
        cpl_result = evaluate_cpl_rules(parsed_doc)
        logger.info(f"[{worker_job_id}] CPL evaluation complete: {len(cpl_result.items)} items evaluated")
    except Exception as e:
        logger.error(f"[{worker_job_id}] CPL evaluation failed: {e}", exc_info=True)
        return

    # 4. Construct Request Profile v0.1 JSON
    profile_payload = _build_request_profile(analysis_run_id, original_filename, cpl_result)

    # 5. Send Callback to Supabase Edge Function
    if callback_url:
        logger.info(f"[{worker_job_id}] Sending callback to {callback_url}")
        headers = {
            "Content-Type": "application/json",
            "x-worker-callback-token": callback_token or "",
        }
        if SUPABASE_ANON_KEY:
            headers["Authorization"] = f"Bearer {SUPABASE_ANON_KEY}"

        payload = {
            "analysis_run_id": analysis_run_id,
            "request_profile": profile_payload,
        }

        async with httpx.AsyncClient(timeout=30.0) as http_client:
            try:
                cb_res = await http_client.post(callback_url, headers=headers, json=payload)
                logger.info(f"[{worker_job_id}] Callback responded: HTTP {cb_res.status_code} - {cb_res.text[:200]}")
            except Exception as e:
                logger.error(f"[{worker_job_id}] Callback invocation failed: {e}")
    else:
        logger.warning(f"[{worker_job_id}] No callback_url provided in job")

    logger.info(f"[{worker_job_id}] Job pipeline finished successfully!")


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def receive_job(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """POST /jobs endpoint strictly adhering to analysis_worker_job/v1 contract."""
    # 1. Authorization check: Bearer token
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != DISPATCH_TOKEN:
        logger.warning(f"Rejecting job request: Invalid dispatch token '{token[:10]}...'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dispatch token",
        )

    # 2. Idempotency Check
    if idempotency_key and idempotency_key in _IDEMPOTENCY_CACHE:
        existing_job_id = _IDEMPOTENCY_CACHE[idempotency_key]
        logger.info(f"Idempotent request received: key={idempotency_key}, returning existing job_id={existing_job_id}")
        return {"worker_job_id": existing_job_id}

    # 3. Parse JSON Body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    schema_version = body.get("schema_version")
    if schema_version != "analysis_worker_job/v1":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported schema_version: {schema_version}",
        )

    analysis_run_id = body.get("analysis_run_id")
    if not analysis_run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="analysis_run_id is required",
        )

    # 4. Generate worker_job_id and register idempotency
    worker_job_id = f"worker-job-{uuid.uuid4()}"
    effective_idempotency_key = idempotency_key or analysis_run_id
    _IDEMPOTENCY_CACHE[effective_idempotency_key] = worker_job_id

    # 5. Dispatch background execution (must respond within 10 seconds with 202)
    background_tasks.add_task(_process_analysis_job, body, worker_job_id)

    logger.info(f"Accepted job for analysis_run_id={analysis_run_id} -> assigned worker_job_id={worker_job_id}")
    return {"worker_job_id": worker_job_id}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"Starting HTTP Worker Server on 0.0.0.0:{port}")
    logger.info(f"DISPATCH_TOKEN is configured: '{DISPATCH_TOKEN[:6]}***'")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
