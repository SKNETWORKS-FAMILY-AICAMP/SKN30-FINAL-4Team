"""Slice 4b: 공고 첨부 → Common IR → ExistingProfile v0.2 → 검색 → SIM 후보.

Slice 4a 는 이미 만들어진 프로파일 두 개를 받아 비교했다. 여기서는 그 앞을
잇는다. **얇은 수직 한 줄** 이고, 각 단계는 이미 있는 것을 쓴다.

- 내려받기: ``httpx`` 한 번. 파일 이름은 URL 이 아니라 ``Content-Disposition``
  에서 온다 (bizinfo 는 URL 에 이름을 넣지 않는다). 크기 상한과 타임아웃을
  명시하고, 저장은 ``LocalObjectStorage`` 를 그대로 쓴다.
- 파싱: HWP/HWPX 는 ``profiles.parse_to_common_ir`` (vendored rhwp 러너),
  PDF 는 vendored ``common_ir_pipeline.adapters.pdf_native`` 다. PDF 는
  네이티브 텍스트만 증거로 쓴다. 이미지 전용 PDF 는 **제외** 하고 OCR 로
  대신 채우지 않는다 (어댑터의 ``pdf_semantic_eligibility`` 계약).
- 구조화: 팀원 패키지의 Common IR → section scope → block router → source
  selection → exact-span 프로파일 조립을 포트로 연결하고, 그 결과를
  vendored ``validate_profile_v02`` 로 한 번 더 검사한 뒤 저장한다.
  기존 계약 테스트를 위해 이미 만든 프로파일을 받는 ``structure`` 주입도
  유지한다.
- 실패 격리 (초안 §9.4): 한 공고의 파싱·구조화 실패는 그 공고의 행에 상태와
  reason code 로 남고, 다른 공고의 저장된 프로파일을 지우거나 덮지 않는다.

수단 배치 (CLAUDE.md "Rule·LLM 선택 원칙"). 이 모듈에는 의미 판단이 없다.
내려받기·형식 분기·검증·저장·정확 코사인 검색은 입력 문법과 판정 조건이 닫혀
있어 전부 Rule 이다. 의미 판단은 이미 있는 ``sim_inputs`` 와 ``sim`` 이 한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Literal
from uuid import UUID

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.local_object_storage import LocalObjectStorage
from app.ports.embedding_client import EmbeddingClient
from app.ports.llm_client import LLMClient
from app.services.retrieval.corpus_embedding import vector_literal

from . import vendor  # noqa: F401  vendored 패키지를 sys.path 에 올린다
from .contracts.profile_snapshot import (
    COMMON_IR_INVALID,
    LLM_UNAVAILABLE,
    PARSE_FAILED,
    StageDiagnostic,
)
from .announcement_profiles import (
    PROMPT_BUNDLE_VERSION,
    structure_announcement_profile,
    verified_common_ir_source_sha256,
)
from .contracts.sim_result import SimCandidateResult, SimCommonProfile
from .embedding_call import embed as embed_texts
from .kb_store import ProfileStorageError, store_existing_profile
from .profiles import StageError, _subprocess_env, parse_to_common_ir
from .sim import compare_candidate
from .sim_inputs import build_common_profile

from common_ir_pipeline.schema import validation_errors  # noqa: E402
from semantic_structuring.common_ir_v1 import prepare_common_ir_v1  # noqa: E402
from semantic_structuring.profile_v02 import validate_profile_v02  # noqa: E402


__all__ = [
    "CANDIDATE_PROFILE_FAILED",
    "CANDIDATE_PROFILE_MISSING",
    "FETCH_FAILED",
    "KB_STORE_FAILED",
    "PROFILE_INVALID",
    "UNSUPPORTED_FORMAT",
    "CandidateComparison",
    "FetchResult",
    "IngestResult",
    "KbCandidate",
    "PROFILE_SCHEMA_VERSION",
    "SUPPORTED_EXTENSIONS",
    "compare_kb_candidates",
    "ensure_fake_embedding_profile",
    "fake_embedding",
    "fetch_attachment",
    "ingest_announcement",
    "ingest_attachment",
    "parse_attachment",
    "pending_attachments",
    "profile_search_text",
    "search_candidates",
    "store_profile_embedding",
]


# reason code. PARSE_FAILED·COMMON_IR_INVALID 는 Slice 1 계약을 그대로 쓰고,
# 이 슬라이스에서 처음 생기는 세 개만 여기서 정의한다. 값 자체가 계약이라
# StrEnum 이 아니라 문자열 상수다 (profile_snapshot.py 와 같은 규약).
FETCH_FAILED = "FETCH_FAILED"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
PROFILE_INVALID = "PROFILE_INVALID"
CANDIDATE_PROFILE_MISSING = "CANDIDATE_PROFILE_MISSING"
CANDIDATE_PROFILE_FAILED = "CANDIDATE_PROFILE_FAILED"
KB_STORE_FAILED = "KB_STORE_FAILED"

# 이 슬라이스가 다루는 형식. zip·png·jpg 는 애초에 뽑지 않는다.
SUPPORTED_EXTENSIONS = ("hwp", "hwpx", "pdf")

PROFILE_SCHEMA_VERSION = "existing_program_profile/v0.2"
PRODUCER_VERSION = "kb-ingest-v0.1"
NOT_CALLED = "not-called"

DEFAULT_FETCH_LIMIT = 5
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0

_FETCH_STAGE = "fetch_attachment"
_PARSE_STAGE = "parse_attachment"
_INGEST_STAGE = "ingest_announcement"


# ------------------------------------------------------------------ 1. 내려받기


@dataclass(frozen=True, slots=True)
class FetchResult:
    """첨부 한 건의 내려받기 결과. 예외를 던지지 않는다."""

    attachment_id: int
    announcement_version_id: int
    status: Literal["DOWNLOADED", "SKIPPED", "FAILED"]
    file_asset_id: int | None = None
    storage_key: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    reason_code: str | None = None
    message: str | None = None


def pending_attachments(
    engine: Engine, *, limit: int = DEFAULT_FETCH_LIMIT
) -> list[dict[str, Any]]:
    """아직 안 받은 첨부를 **구성상 유한하게** 뽑는다.

    3,655건 전체를 도는 함수는 만들지 않는다. 본문(``PRIMARY``)이고 이
    슬라이스가 파싱할 수 있는 형식이며, 호출자가 준 ``limit`` 만큼이다.
    이미 받은 것(``file_asset_id`` 가 있는 것)은 다시 받지 않는다.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    rows = _read(
        engine,
        """
        SELECT aa.id, aa.announcement_version_id, aa.source_url,
               aa.original_filename, lower(aa.extension) AS extension,
               a.pblanc_id
          FROM sims.announcement_attachment aa
          JOIN sims.announcement_version av ON av.id = aa.announcement_version_id
          JOIN sims.announcement a ON a.id = av.announcement_id
         WHERE aa.attachment_role = 'PRIMARY'
           AND lower(aa.extension) = ANY(:extensions)
           AND aa.file_asset_id IS NULL
         ORDER BY aa.id
         LIMIT :limit
        """,
        {"extensions": list(SUPPORTED_EXTENSIONS), "limit": limit},
    )
    return rows


def _filename_from_response(response: httpx.Response, fallback: str) -> str:
    """``Content-Disposition`` 의 이름을 쓴다. URL 에는 이름이 없다.

    파싱은 stdlib 이메일 헤더 파서에 맡긴다 (따옴표·RFC2231 처리를 다시 쓰지
    않는다). 경로 성분은 떼어내 저장 키가 root 를 벗어나지 못하게 한다.
    """

    header = response.headers.get("content-disposition")
    name = ""
    if header:
        message = EmailMessage()
        message["Content-Disposition"] = header
        name = message.get_filename() or ""
    name = Path(name.replace("\\", "/")).name.strip()
    return name or fallback


def _download(
    client: httpx.Client, url: str, *, max_bytes: int
) -> tuple[bytes, str, str | None]:
    """본문·파일이름·MIME 을 돌려준다. 상한을 넘으면 다 받지 않고 끊는다."""

    with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(
                    f"attachment exceeds the {max_bytes} byte cap"
                )
            chunks.append(chunk)
        filename = _filename_from_response(response, Path(url).name or "attachment")
        mime = response.headers.get("content-type")
    return b"".join(chunks), filename, mime


def fetch_attachment(
    engine: Engine,
    storage: LocalObjectStorage,
    attachment: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FetchResult:
    """첨부 하나를 내려받아 오브젝트 스토리지와 ``file_asset`` 에 넣는다.

    이미 ``file_asset_id`` 가 있으면 아무것도 하지 않는다. 다시 돌려도 같은
    파일이 두 벌 생기지 않는다.
    """

    attachment_id = int(attachment["id"])
    version_id = int(attachment["announcement_version_id"])
    existing = _read(
        engine,
        "SELECT file_asset_id FROM sims.announcement_attachment WHERE id = :id",
        {"id": attachment_id},
    )
    if existing and existing[0]["file_asset_id"] is not None:
        return FetchResult(
            attachment_id=attachment_id,
            announcement_version_id=version_id,
            status="SKIPPED",
            file_asset_id=int(existing[0]["file_asset_id"]),
            message="already fetched",
        )

    owned = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout_seconds, follow_redirects=True
        )
    try:
        content, filename, mime = _download(
            client, attachment["source_url"], max_bytes=max_bytes
        )
    except Exception as error:  # noqa: BLE001 - 전송 실패는 전부 한 자리에서 격리한다
        message = f"{type(error).__name__}: {error}"[:2000]
        _write(
            engine,
            """
            UPDATE sims.announcement_attachment
               SET fetch_status = 'FAILED', last_fetch_error = :message,
                   fetched_at = now()
             WHERE id = :id
            """,
            {"id": attachment_id, "message": message},
        )
        return FetchResult(
            attachment_id=attachment_id,
            announcement_version_id=version_id,
            status="FAILED",
            reason_code=FETCH_FAILED,
            message=message,
        )
    finally:
        if owned:
            client.close()

    storage_key = f"kb/announcement/{version_id}/{attachment_id}/{filename}"
    asyncio.run(storage.put(storage_key, io.BytesIO(content)))
    extension = (attachment.get("extension") or Path(filename).suffix.lstrip(".")).lower()
    asset_id = int(
        _write(
            engine,
            """
            INSERT INTO sims.file_asset (
                asset_scope, storage_key, original_filename,
                detected_mime_type, extension, size_bytes, sha256_hex, source_url
            ) VALUES (
                'SHARED', :storage_key, :filename, :mime, :extension,
                :size_bytes, :sha256_hex, :source_url
            )
            RETURNING id
            """,
            {
                "storage_key": storage_key,
                "filename": filename,
                "mime": mime,
                "extension": extension or None,
                "size_bytes": len(content),
                "sha256_hex": hashlib.sha256(content).hexdigest(),
                "source_url": attachment["source_url"],
            },
        )
    )
    _write(
        engine,
        """
        UPDATE sims.announcement_attachment
           SET fetch_status = 'DOWNLOADED', file_asset_id = :asset_id,
               fetched_at = now(), last_fetch_error = NULL
         WHERE id = :id
        """,
        {"id": attachment_id, "asset_id": asset_id},
    )
    return FetchResult(
        attachment_id=attachment_id,
        announcement_version_id=version_id,
        status="DOWNLOADED",
        file_asset_id=asset_id,
        storage_key=storage_key,
        filename=filename,
        size_bytes=len(content),
    )


# -------------------------------------------------------------------- 2. 파싱


def parse_attachment(
    *, input_path: Path | str, notice_id: str, extension: str, run_dir: Path | str
) -> dict[str, Any]:
    """원본 파일 하나를 Common IR v1 문서로 만든다. 실패는 ``StageError`` 다.

    HWP/HWPX 와 PDF 는 vendored 진입점이 다르다. PDF 는 원본에서 바로 IR 이
    나오지 않고 네이티브 텍스트 캡처(``pdf_inspector``)를 먼저 거친다.
    """

    extension = extension.lower()
    if extension in ("hwp", "hwpx"):
        return parse_to_common_ir(
            input_path=input_path,
            notice_id=notice_id,
            source_kind=extension,
            run_dir=run_dir,
        ).document
    if extension == "pdf":
        return _parse_pdf(
            input_path=Path(input_path), notice_id=notice_id, run_dir=Path(run_dir)
        )
    raise StageError(
        StageDiagnostic(
            stage=_PARSE_STAGE,
            unit=str(input_path),
            reason_code=UNSUPPORTED_FORMAT,
            message=f"이 슬라이스가 파싱하지 않는 확장자: {extension}",
        )
    )


def _run_module(module: str, arguments: list[str], *, unit: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", module, *arguments],
        text=True,
        capture_output=True,
        check=False,
        env=_subprocess_env(),
    )
    if completed.returncode != 0:
        raise StageError(
            StageDiagnostic(
                stage=_PARSE_STAGE,
                unit=unit,
                reason_code=PARSE_FAILED,
                message=(completed.stderr or completed.stdout or "").strip()[:2000],
            )
        )


def _parse_pdf(*, input_path: Path, notice_id: str, run_dir: Path) -> dict[str, Any]:
    """네이티브 텍스트 캡처 → ``adapters.pdf_native`` → Common IR v1.

    이미지 전용 PDF 는 어댑터가 ``excluded_image_only`` 로 표시한다. 그 문서를
    OCR 로 대신 채우지 않는다: 증거가 없는 것이지 형식이 다른 것이 아니다.
    """

    run_dir.mkdir(parents=True, exist_ok=True)
    native_path = run_dir / f"{notice_id}.pdf_native.json"
    output_path = run_dir / f"{notice_id}.pdf.json"
    _run_module(
        "common_ir_pipeline.workers.pdf_inspector_capture",
        ["--notice-id", notice_id, "--pdf", str(input_path), "--output", str(native_path)],
        unit=str(input_path),
    )
    _run_module(
        "common_ir_pipeline.adapters.pdf_native",
        [
            "--notice-id", notice_id,
            "--native", str(native_path),
            "--source-path", str(input_path),
            "--output", str(output_path),
        ],
        unit=str(input_path),
    )
    document = json.loads(output_path.read_text(encoding="utf-8"))
    eligibility = document.get("document", {}).get("pdf_semantic_eligibility")
    if eligibility != "eligible_native_text":
        raise StageError(
            StageDiagnostic(
                stage=_PARSE_STAGE,
                unit=str(input_path),
                reason_code=UNSUPPORTED_FORMAT,
                message=(
                    f"네이티브 텍스트가 없는 PDF ({eligibility}). OCR 로 대신 "
                    "채우지 않는다."
                ),
            )
        )
    return document


# ------------------------------------------------------------------ 3. 구조화


@dataclass(frozen=True, slots=True)
class IngestResult:
    """공고 버전 하나의 적재 결과. 예외를 던지지 않는다."""

    announcement_version_id: int
    source_profile_id: str
    status: Literal["OK", "FAILED"]
    reason_code: str | None = None
    profile_row_id: int | None = None
    kb_profile_version_pk: UUID | None = None
    diagnostics: list[StageDiagnostic] = field(default_factory=list)


def block_texts(document: dict[str, Any]) -> dict[str, str]:
    """v0.2 검증이 보는 블록 원문.

    프로파일의 ``value_source`` 오프셋은 Common IR 원본 블록이 아니라
    CandidatePack 블록 기준이다. 그 텍스트를 만드는 것은 vendored
    ``prepare_common_ir_v1`` 하나이고, LLM 을 타지 않는다.
    """

    prepared, _projection = prepare_common_ir_v1(document)
    texts: dict[str, str] = {}
    for name in (
        "fact_candidate_blocks",
        "search_only_blocks",
        "table_candidate_blocks",
        "table_cell_candidate_blocks",
        "excluded_blocks",
    ):
        for block in getattr(prepared, name, None) or []:
            texts[block.block_id] = block.text
    return texts


def _generation_metadata(
    *,
    structure: Callable[[dict[str, Any]], dict[str, Any]] | None,
    llm_client: LLMClient | None,
    model_profile: str | None,
) -> tuple[str, str]:
    """Return the exact model/prompt lineage for this invocation."""

    production = (
        structure is None
        and llm_client is not None
        and isinstance(model_profile, str)
        and bool(model_profile.strip())
    )
    if not production:
        return NOT_CALLED, NOT_CALLED
    return model_profile or NOT_CALLED, PROMPT_BUNDLE_VERSION


def _annotate_profile_generation(
    profile: dict[str, Any],
    *,
    producer_version: str,
    model_profile: str,
    prompt_bundle_version: str,
    source_sha256_hex: str,
) -> dict[str, Any]:
    """Keep storage lineage in the profile as well as in its DB key."""

    annotated = dict(profile)
    processing = dict(annotated.get("processing_metadata") or {})
    generation = dict(processing.get("generation_lineage") or {})
    generation.update(
        {
            "producer_version": producer_version,
            "model_profile": model_profile,
            "prompt_bundle_version": prompt_bundle_version,
            "source_sha256_hex": source_sha256_hex,
        }
    )
    processing["generation_lineage"] = generation
    annotated["processing_metadata"] = processing
    return annotated


def _reusable_ok_profile(
    engine: Engine,
    *,
    announcement_version_id: int,
    source_profile_ids: list[str],
    source_sha256_hex: str | None,
    producer_version: str,
    model_profile: str,
    prompt_bundle_version: str,
    common_ir: dict[str, Any] | None = None,
    storage: LocalObjectStorage | None = None,
) -> IngestResult | None:
    """Find an exact-generation OK row before any structure/LLM work."""

    rows = _read(
        engine,
        """
        SELECT id, source_profile_id, profile_json
          FROM sims.announcement_profile
         WHERE announcement_version_id = :announcement_version_id
           AND source_profile_id = ANY(:source_profile_ids)
           AND profile_schema_version = :schema_version
           AND source_sha256_hex IS NOT DISTINCT FROM :source_sha256_hex
           AND producer_version IS NOT DISTINCT FROM :producer_version
           AND model_profile IS NOT DISTINCT FROM :model_profile
           AND prompt_bundle_version IS NOT DISTINCT FROM :prompt_bundle_version
           AND status = 'OK'
         ORDER BY id
         LIMIT 1
        """,
        {
            "announcement_version_id": announcement_version_id,
            "source_profile_ids": source_profile_ids,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "source_sha256_hex": source_sha256_hex,
            "producer_version": producer_version,
            "model_profile": model_profile,
            "prompt_bundle_version": prompt_bundle_version,
        },
    )
    if not rows:
        return None
    if storage is not None and isinstance(rows[0]["profile_json"], dict):
        return _store_profile(
            engine,
            announcement_version_id=announcement_version_id,
            source_profile_id=rows[0]["source_profile_id"],
            status="OK",
            reason_code=None,
            profile=rows[0]["profile_json"],
            producer_version=producer_version,
            source_sha256_hex=source_sha256_hex,
            model_profile=model_profile,
            prompt_bundle_version=prompt_bundle_version,
            diagnostics=[],
            common_ir=common_ir,
            storage=storage,
        )
    return IngestResult(
        announcement_version_id=announcement_version_id,
        source_profile_id=rows[0]["source_profile_id"],
        status="OK",
        profile_row_id=int(rows[0]["id"]),
    )


def ingest_announcement(
    engine: Engine,
    *,
    announcement_version_id: int,
    source_profile_id: str,
    document: dict[str, Any],
    storage: LocalObjectStorage | None = None,
    structure: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    llm_client: LLMClient | None = None,
    model_profile: str | None = None,
    producer_version: str = PRODUCER_VERSION,
) -> IngestResult:
    """Common IR 하나 → ExistingProfile v0.2 → 저장.

    ``structure`` 를 주입하면 기존 오프라인 계약 테스트처럼 이미 만든
    프로파일을 검증할 수 있다. 운영 경로에서는 ``llm_client`` 와
    ``model_profile`` 을 받아 팀원 패키지의 Common IR → section scope →
    block router → source selection → exact-span assembly 사슬을 실행한다.
    동일한 Common IR source hash·프로파일·producer·model·prompt 계보의
    기존 OK 행은 이 사슬보다 먼저 재사용한다.
    """

    diagnostics: list[StageDiagnostic] = []
    model_lineage, prompt_lineage = _generation_metadata(
        structure=structure, llm_client=llm_client, model_profile=model_profile
    )
    source_sha256_hex: str | None = None

    def failed(reason_code: str, message: str) -> IngestResult:
        diagnostics.append(
            StageDiagnostic(
                stage=_INGEST_STAGE,
                unit=source_profile_id,
                reason_code=reason_code,
                message=message[:2000],
            )
        )
        return _store_profile(
            engine,
            announcement_version_id=announcement_version_id,
            source_profile_id=source_profile_id,
            status="FAILED",
            reason_code=reason_code,
            profile=None,
            producer_version=producer_version,
            source_sha256_hex=source_sha256_hex,
            model_profile=model_lineage,
            prompt_bundle_version=prompt_lineage,
            diagnostics=diagnostics,
            common_ir=document,
            storage=storage,
        )

    errors = validation_errors(document)
    if errors:
        return failed(COMMON_IR_INVALID, "; ".join(errors))
    try:
        source_sha256_hex = verified_common_ir_source_sha256(document)
    except ValueError as error:
        return failed(COMMON_IR_INVALID, str(error))
    reusable = _reusable_ok_profile(
        engine,
        announcement_version_id=announcement_version_id,
        source_profile_ids=list(
            dict.fromkeys(
                [
                    source_profile_id,
                    str(document.get("document", {}).get("document_id") or ""),
                ]
            )
        ),
        source_sha256_hex=source_sha256_hex,
        producer_version=producer_version,
        model_profile=model_lineage,
        prompt_bundle_version=prompt_lineage,
        common_ir=document,
        storage=storage,
    )
    if reusable is not None:
        return reusable
    try:
        texts = block_texts(document)
    except Exception as error:  # noqa: BLE001 - 어느 예외든 이 공고만 실패시킨다
        return failed(COMMON_IR_INVALID, f"{type(error).__name__}: {error}")

    try:
        if structure is not None:
            profile = structure(document)
        elif llm_client is not None and model_profile:
            profile = structure_announcement_profile(
                document, llm_client, model_profile=model_profile
            )
        else:
            raise StageError(
                StageDiagnostic(
                    stage=_INGEST_STAGE,
                    unit=source_profile_id,
                    reason_code=LLM_UNAVAILABLE,
                    message="production profile structure requires llm_client and model_profile",
                )
            )
    except StageError as error:
        return failed(
            error.diagnostic.reason_code or PROFILE_INVALID, error.diagnostic.message
        )
    except Exception as error:  # noqa: BLE001
        return failed(PROFILE_INVALID, f"{type(error).__name__}: {error}")

    profile = _annotate_profile_generation(
        profile,
        producer_version=producer_version,
        model_profile=model_lineage,
        prompt_bundle_version=prompt_lineage,
        source_sha256_hex=source_sha256_hex,
    )
    issues = validate_profile_v02(profile, texts)
    if issues:
        return failed(PROFILE_INVALID, "; ".join(issues))
    stored_id = profile.get("source_profile_id") or source_profile_id
    return _store_profile(
        engine,
        announcement_version_id=announcement_version_id,
        source_profile_id=stored_id,
        status="OK",
        reason_code=None,
        profile=profile,
        producer_version=producer_version,
        source_sha256_hex=source_sha256_hex,
        model_profile=model_lineage,
        prompt_bundle_version=prompt_lineage,
        diagnostics=diagnostics,
        common_ir=document,
        storage=storage,
    )


def ingest_attachment(
    engine: Engine,
    storage: LocalObjectStorage,
    attachment: dict[str, Any],
    *,
    work_dir: Path | str,
    structure: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    llm_client: LLMClient | None = None,
    model_profile: str | None = None,
    client: httpx.Client | None = None,
    producer_version: str = PRODUCER_VERSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> IngestResult:
    """첨부 한 건의 세로 한 줄: 내려받기 → 파싱 → 구조화 → 저장.

    어느 단계에서 멈추든 그 공고의 행 하나에 상태와 reason code 가 남고,
    다른 공고는 건드리지 않는다 (초안 §9.4).
    """

    extension = (attachment.get("extension") or "").lower()
    notice_id = str(attachment.get("pblanc_id") or attachment["announcement_version_id"])
    # P2 가 고정한 출처 식별자. 같은 공고의 hwp 본과 pdf 본을 구분한다.
    source_profile_id = f"{extension}:{notice_id}"
    version_id = int(attachment["announcement_version_id"])

    def failed(reason_code: str, message: str) -> IngestResult:
        model_lineage, prompt_lineage = _generation_metadata(
            structure=structure, llm_client=llm_client, model_profile=model_profile
        )
        return _store_profile(
            engine,
            announcement_version_id=version_id,
            source_profile_id=source_profile_id,
            status="FAILED",
            reason_code=reason_code,
            profile=None,
            producer_version=producer_version,
            source_sha256_hex=None,
            model_profile=model_lineage,
            prompt_bundle_version=prompt_lineage,
            diagnostics=[
                StageDiagnostic(
                    stage=_FETCH_STAGE if reason_code == FETCH_FAILED else _PARSE_STAGE,
                    unit=source_profile_id,
                    reason_code=reason_code,
                    message=message[:2000],
                )
            ],
        )

    fetched = fetch_attachment(
        engine, storage, attachment, client=client, max_bytes=max_bytes
    )
    if fetched.status == "FAILED":
        return failed(FETCH_FAILED, fetched.message or "download failed")
    storage_key = fetched.storage_key or _storage_key(engine, fetched.file_asset_id)
    if storage_key is None:
        return failed(FETCH_FAILED, "file asset has no storage key")

    work_dir = Path(work_dir)
    try:
        document = parse_attachment(
            input_path=storage._path_for(storage_key),  # noqa: SLF001 - 같은 저장소 루트
            notice_id=notice_id,
            extension=extension,
            run_dir=work_dir / f"av{version_id}",
        )
    except StageError as error:
        return failed(
            error.diagnostic.reason_code or PARSE_FAILED, error.diagnostic.message
        )

    return ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id=source_profile_id,
        document=document,
        storage=storage,
        structure=structure,
        llm_client=llm_client,
        model_profile=model_profile,
        producer_version=producer_version,
    )


def _storage_key(engine: Engine, file_asset_id: int | None) -> str | None:
    if file_asset_id is None:
        return None
    rows = _read(
        engine,
        "SELECT storage_key FROM sims.file_asset WHERE id = :id",
        {"id": file_asset_id},
    )
    return rows[0]["storage_key"] if rows else None


def _store_profile(
    engine: Engine,
    *,
    announcement_version_id: int,
    source_profile_id: str,
    status: str,
    reason_code: str | None,
    profile: dict[str, Any] | None,
    producer_version: str,
    source_sha256_hex: str | None,
    model_profile: str,
    prompt_bundle_version: str,
    diagnostics: list[StageDiagnostic],
    common_ir: dict[str, Any] | None = None,
    storage: LocalObjectStorage | None = None,
) -> IngestResult:
    """프로파일 행 하나를 쓴다. **실패가 성공을 덮지 않는다.**

    같은 생성 키에 이미 OK 프로파일이 있는데 재실행이 실패했다면 그 행은
    그대로 둔다. 생성 키가 달라지면 새 계보 행을 만들고, 실패는 그 계보의
    행만 FAILED 로 남긴다.
    """

    payload = {
        "announcement_version_id": announcement_version_id,
        "source_profile_id": source_profile_id,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "producer_version": producer_version,
        "source_sha256_hex": source_sha256_hex,
        "model_profile": model_profile,
        "prompt_bundle_version": prompt_bundle_version,
        "status": status,
        "profile_json": json.dumps(profile or {}, ensure_ascii=False),
        "diagnostics": json.dumps(
            [
                {
                    "stage": row.stage,
                    "unit": row.unit,
                    "reason_code": row.reason_code,
                    "message": row.message,
                }
                for row in diagnostics
            ],
            ensure_ascii=False,
        ),
    }
    row_id = _write(
        engine,
        """
        INSERT INTO sims.announcement_profile (
            announcement_version_id, source_profile_id, profile_schema_version,
            producer_version, source_sha256_hex, model_profile,
            prompt_bundle_version, status, profile_json, diagnostics
        ) VALUES (
            :announcement_version_id, :source_profile_id, :schema_version,
            :producer_version, :source_sha256_hex, :model_profile,
            :prompt_bundle_version, :status,
            CAST(:profile_json AS jsonb), CAST(:diagnostics AS jsonb)
        )
        ON CONFLICT (
            announcement_version_id, source_profile_id, profile_schema_version,
            source_sha256_hex, producer_version, model_profile, prompt_bundle_version
        )
        DO UPDATE SET producer_version = EXCLUDED.producer_version,
                      source_sha256_hex = EXCLUDED.source_sha256_hex,
                      model_profile = EXCLUDED.model_profile,
                      prompt_bundle_version = EXCLUDED.prompt_bundle_version,
                      status = EXCLUDED.status,
                      profile_json = EXCLUDED.profile_json,
                      diagnostics = EXCLUDED.diagnostics,
                      created_at = now()
         WHERE announcement_profile.status = 'FAILED'
        RETURNING id
        """,
        payload,
    )
    if row_id is None:
        existing = _read(
            engine,
            """
            SELECT id FROM sims.announcement_profile
             WHERE announcement_version_id = :announcement_version_id
               AND source_profile_id = :source_profile_id
               AND profile_schema_version = :schema_version
               AND source_sha256_hex IS NOT DISTINCT FROM :source_sha256_hex
               AND producer_version IS NOT DISTINCT FROM :producer_version
               AND model_profile IS NOT DISTINCT FROM :model_profile
               AND prompt_bundle_version IS NOT DISTINCT FROM :prompt_bundle_version
            """,
            payload,
        )
        row_id = existing[0]["id"] if existing else None
    kb_profile_version_pk: UUID | None = None
    result_status = "OK" if status == "OK" else "FAILED"
    result_reason = reason_code

    def mark_legacy_profile_failed() -> None:
        """Do not leave a non-canonical OK row searchable after KB failure."""

        if row_id is None:
            return
        _write(
            engine,
            """
            UPDATE sims.announcement_profile
               SET status = 'FAILED',
                   diagnostics = CAST(:diagnostics AS jsonb),
                   created_at = now()
             WHERE id = :id
            """,
            {
                "id": row_id,
                "diagnostics": json.dumps(
                    [
                        {
                            "stage": row.stage,
                            "unit": row.unit,
                            "reason_code": row.reason_code,
                            "message": row.message,
                        }
                        for row in diagnostics
                    ],
                    ensure_ascii=False,
                ),
            },
        )

    if status == "OK" and profile is not None and common_ir is not None and storage is not None:
        try:
            with engine.begin() as connection:
                stored = store_existing_profile(
                    connection,
                    profile=profile,
                    common_ir=common_ir,
                    source_sha256=source_sha256_hex,
                    processing_run_pk=None,
                    storage=storage,
                    diagnostics=diagnostics,
                )
            # 부분 적재는 실패가 아니다 — 남은 fact 는 그대로 쓸 수 있다.
            # 다만 무엇을 버렸는지가 결과 진단에 남아야 완전한 적재와
            # 구분된다. ``diagnostics`` 는 같은 목록이라 이미 실려 있다.
            kb_profile_version_pk = stored.profile_version_pk
        except ProfileStorageError as error:
            diagnostics.extend(
                note for note in error.diagnostics if note not in diagnostics
            )
            result_status = "FAILED"
            result_reason = KB_STORE_FAILED
            mark_legacy_profile_failed()
        except Exception as error:  # noqa: BLE001 - KB failure is per announcement
            diagnostics.append(
                StageDiagnostic(
                    stage="store_existing_profile",
                    unit=source_profile_id,
                    reason_code=KB_STORE_FAILED,
                    message=f"{type(error).__name__}: {error}"[:2000],
                )
            )
            result_status = "FAILED"
            result_reason = KB_STORE_FAILED
            mark_legacy_profile_failed()
    return IngestResult(
        announcement_version_id=announcement_version_id,
        source_profile_id=source_profile_id,
        status=result_status,
        reason_code=result_reason,
        profile_row_id=int(row_id) if row_id is not None else None,
        kb_profile_version_pk=kb_profile_version_pk,
        diagnostics=diagnostics,
    )


# ------------------------------------------------------- 4. 임베딩 포트와 검색


def fake_embedding(input_text: str, dimension: int) -> list[float]:
    """해시에서 만든 결정적 벡터. 같은 입력이면 언제나 같은 값이다.

    ponytail: 모델 품질은 이 슬라이스의 주제가 아니다. 검색 경로(차원 계약,
    정확 코사인 정렬, 프로파일 연결)만 재현 가능하게 고정한다.
    """

    if dimension < 1:
        raise ValueError("dimension must be positive")
    raw = bytearray()
    counter = 0
    while len(raw) < dimension * 2:
        raw += hashlib.sha256(f"{counter}:{input_text}".encode("utf-8")).digest()
        counter += 1
    values = [
        int.from_bytes(raw[index * 2 : index * 2 + 2], "big") / 32767.5 - 1.0
        for index in range(dimension)
    ]
    norm = sum(value * value for value in values) ** 0.5
    return [value / norm for value in values] if norm else [1.0] + [0.0] * (dimension - 1)


def ensure_fake_embedding_profile(
    engine: Engine,
    *,
    dimension: int,
    profile_name: str = "kb-fake-v1",
    version_no: int = 1,
) -> int:
    """가짜 임베딩용 모델·프로파일 행을 만들고 프로파일 id 를 돌려준다.

    ``is_active`` 는 false 로 둔다. 활성 SUMMARY 프로파일은 정확히 하나여야
    하는 기존 검색 계약이 있고, 이 슬라이스의 검색은 프로파일 id 를 인자로
    받으므로 그 계약을 건드릴 이유가 없다.
    """

    model_id = _write(
        engine,
        """
        INSERT INTO sims.embedding_model (
            provider, model_name, model_version, dimension, distance_metric
        ) VALUES ('fake', 'kb-fake-hash', '', :dimension, 'COSINE')
        ON CONFLICT (provider, model_name, model_version, dimension) DO NOTHING
        RETURNING id
        """,
        {"dimension": dimension},
    )
    if model_id is None:
        model_id = _read(
            engine,
            """
            SELECT id FROM sims.embedding_model
             WHERE provider = 'fake' AND model_name = 'kb-fake-hash'
               AND model_version = '' AND dimension = :dimension
            """,
            {"dimension": dimension},
        )[0]["id"]
    profile_id = _write(
        engine,
        """
        INSERT INTO sims.embedding_profile (
            embedding_model_id, profile_name, version_no, profile_kind,
            input_template, configuration, preprocessing_version, is_active
        ) VALUES (
            :model_id, :profile_name, :version_no, 'SUMMARY',
            'ExistingProfile v0.2 비교 컨테이너 원문',
            CAST(:configuration AS jsonb), 'kb-profile-v1', false
        )
        ON CONFLICT (profile_name, version_no) DO NOTHING
        RETURNING id
        """,
        {
            "model_id": model_id,
            "profile_name": profile_name,
            "version_no": version_no,
            "configuration": json.dumps({"embedding": "fake-sha256"}),
        },
    )
    if profile_id is None:
        profile_id = _read(
            engine,
            """
            SELECT id FROM sims.embedding_profile
             WHERE profile_name = :profile_name AND version_no = :version_no
            """,
            {"profile_name": profile_name, "version_no": version_no},
        )[0]["id"]
    return int(profile_id)


def embedding_profile_spec(engine: Engine, embedding_profile_id: int) -> tuple[int, str]:
    """Return the registered vector dimension and provider model identity."""

    rows = _read(
        engine,
        """
        SELECT m.dimension, m.model_name
          FROM sims.embedding_profile p
          JOIN sims.embedding_model m ON m.id = p.embedding_model_id
         WHERE p.id = :profile_id
        """,
        {"profile_id": embedding_profile_id},
    )
    if not rows:
        raise ValueError(f"unknown embedding profile: {embedding_profile_id}")
    model_name = rows[0]["model_name"]
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError(f"embedding profile has no model identity: {embedding_profile_id}")
    return int(rows[0]["dimension"]), model_name


def embedding_dimension(engine: Engine, embedding_profile_id: int) -> int:
    """차원은 DB 의 프로파일 행에서 오고, 코드에 상수로 박지 않는다."""

    return embedding_profile_spec(engine, embedding_profile_id)[0]


def profile_search_text(profile: dict[str, Any]) -> str:
    """프로파일에서 검색 입력을 만든다. 원문만 쓰고 요약을 지어내지 않는다.

    필드 이름 순으로 돈다. jsonb 는 객체 키 순서를 보존하지 않아서, 저장 전
    dict 와 저장 후 dict 를 그냥 순회하면 같은 프로파일에서 다른 입력 문자열이
    나오고 임베딩이 갈린다.
    """

    comparison = profile.get("comparison_profile") or {}
    parts: list[str] = []
    for _field_name, rows in sorted(comparison.items()):
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("value_raw"):
                parts.append(str(row["value_raw"]))
    return "\n".join(parts)


def store_profile_embedding(
    engine: Engine,
    *,
    announcement_version_id: int,
    embedding_profile_id: int,
    embedding_client: EmbeddingClient | None = None,
) -> bool:
    """Inject an embedding provider and persist the exact profile lineage.

    The provider is called before the upsert.  Therefore a provider error cannot
    replace an existing vector or otherwise damage a previously stored profile.
    """

    rows = _read(
        engine,
        """
        SELECT id, profile_json FROM sims.announcement_profile
         WHERE announcement_version_id = :version_id AND status = 'OK'
         ORDER BY id DESC LIMIT 1
        """,
        {"version_id": announcement_version_id},
    )
    if not rows:
        return False
    input_text = profile_search_text(rows[0]["profile_json"])
    if not input_text.strip():
        return False
    dimension, model_name = embedding_profile_spec(engine, embedding_profile_id)
    batch = embed_texts(
        embedding_client,
        [input_text],
        expected_dimension=dimension,
        expected_model_name=model_name,
    )
    _write(
        engine,
        """
        INSERT INTO sims.announcement_embedding (
            announcement_version_id, announcement_profile_id,
            embedding_profile_id,
            input_text, input_sha256_hex, embedding
        ) VALUES (
            :version_id, :announcement_profile_id, :profile_id,
            :input_text, :digest,
            CAST(:embedding AS vector)
        )
        ON CONFLICT (announcement_version_id, embedding_profile_id)
        DO UPDATE SET
            announcement_profile_id = EXCLUDED.announcement_profile_id,
            input_text = EXCLUDED.input_text,
            input_sha256_hex = EXCLUDED.input_sha256_hex,
            embedding = EXCLUDED.embedding,
            created_at = now()
        """,
        {
            "version_id": announcement_version_id,
            "announcement_profile_id": int(rows[0]["id"]),
            "profile_id": embedding_profile_id,
            "input_text": input_text,
            "digest": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
            "embedding": vector_literal(batch.vectors[0]),
        },
    )
    return True


@dataclass(frozen=True, slots=True)
class KbCandidate:
    announcement_version_id: int
    pblanc_nm: str
    source_profile_id: str
    distance: float
    # Legacy callers may construct candidates positionally without lineage;
    # the 4b search path always fills this with the exact profile row id.
    announcement_profile_id: int | None = None
    kb_profile_version_pk: UUID | None = None


def search_candidates(
    engine: Engine,
    *,
    embedding_profile_id: int,
    query_text: str,
    top_k: int = 5,
    embedding_client: EmbeddingClient | None = None,
) -> list[KbCandidate]:
    """정확 코사인(``<=>``) 상위 top_k.

    ``retrieval._search_candidates`` 와 같은 모양이다: 프로파일 id 를 인자로
    받고, 거리로 정렬하고, 동점은 ``av.id`` 로 끊는다. 다른 점은 대상 집합
    하나뿐이다 — **저장된 OK 프로파일이 있는 공고** 만 본다. 프로파일이 없는
    공고를 후보로 올리면 SIM 이 볼 것이 없다.
    """

    dimension, model_name = embedding_profile_spec(engine, embedding_profile_id)
    query_batch = embed_texts(
        embedding_client,
        [query_text],
        expected_dimension=dimension,
        expected_model_name=model_name,
    )
    query_vector = vector_literal(query_batch.vectors[0])
    search_sql = """
        SELECT av.id AS announcement_version_id, av.pblanc_nm,
               ap.id AS announcement_profile_id, ap.source_profile_id,
               pv.profile_version_pk AS kb_profile_version_pk,
               ae.embedding <=> CAST(:query AS vector) AS distance
          FROM sims.announcement_embedding ae
          JOIN sims.announcement_version av ON av.id = ae.announcement_version_id
          JOIN sims.announcement_profile ap
            ON ap.id = ae.announcement_profile_id
           AND ap.announcement_version_id = av.id
           AND ap.status = 'OK'
          JOIN kb.source_profile sp
            ON sp.source_profile_id = ap.source_profile_id
          JOIN kb.source_version sv
            ON sv.source_profile_pk = sp.source_profile_pk
           AND sv.is_current
          JOIN kb.profile_version pv
            ON pv.source_version_pk = sv.source_version_pk
           AND pv.is_current
         WHERE ae.embedding_profile_id = :profile_id
         ORDER BY ae.embedding <=> CAST(:query AS vector), av.id, ap.id
         LIMIT :top_k
        """
    if not _kb_schema_available(engine):
        search_sql = """
        SELECT av.id AS announcement_version_id, av.pblanc_nm,
               ap.id AS announcement_profile_id, ap.source_profile_id,
               ae.embedding <=> CAST(:query AS vector) AS distance
          FROM sims.announcement_embedding ae
          JOIN sims.announcement_version av ON av.id = ae.announcement_version_id
          JOIN sims.announcement_profile ap
            ON ap.id = ae.announcement_profile_id
           AND ap.announcement_version_id = av.id
           AND ap.status = 'OK'
         WHERE ae.embedding_profile_id = :profile_id
         ORDER BY ae.embedding <=> CAST(:query AS vector), av.id, ap.id
         LIMIT :top_k
        """
    rows = _read(
        engine,
        search_sql,
        {
            "query": query_vector,
            "profile_id": embedding_profile_id,
            "top_k": top_k,
        },
    )
    return [
        KbCandidate(
            announcement_version_id=int(row["announcement_version_id"]),
            announcement_profile_id=int(row["announcement_profile_id"]),
            pblanc_nm=row["pblanc_nm"],
            source_profile_id=row["source_profile_id"],
            distance=float(row["distance"]),
            kb_profile_version_pk=row.get("kb_profile_version_pk"),
        )
        for row in rows
    ]


# ------------------------------------------------------------- 5. SIM 후보 비교


@dataclass(frozen=True, slots=True)
class CandidateComparison:
    """후보 한 건의 비교 결과 **또는** 왜 비교하지 못했는지."""

    announcement_version_id: int
    source_profile_id: str | None
    result: SimCandidateResult | None = None
    reason_code: str | None = None
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
    announcement_profile_id: int | None = None
    # 성공 비교가 실제로 소비한 Existing 공통 프로파일. 결과 저장 시
    # ``result.evidence_snapshot``의 EXISTING 근거를 잃지 않도록 전달한다.
    candidate_common: SimCommonProfile | None = None


def _load_kb_profile(engine: Engine, profile_version_pk: UUID) -> dict[str, Any] | None:
    """Rebuild the SIM input from the durable ``kb.*`` facts."""

    rows = _read(
        engine,
        """
        SELECT pv.profile_version_pk, pv.schema_version,
               sp.source_profile_id, n.notice_id
          FROM kb.profile_version pv
          JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
          JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
          JOIN kb.notice n ON n.notice_pk = sp.notice_pk
         WHERE pv.profile_version_pk = :profile_version_pk
           AND pv.is_current
        """,
        {"profile_version_pk": profile_version_pk},
    )
    if not rows:
        return None
    fact_rows = _read(
        engine,
        """
        SELECT f.fact_id, f.field_name, f.value_raw, f.status, f.scope,
               f.source_block_id, f.start_char, f.end_char, f.text_basis,
               e.source_block_id AS evidence_source_block_id, e.section_id,
               e.common_ir_document_id, e.common_ir_block_id,
               e.common_ir_cell_id, e.common_ir_occurrence_ids, e.ordinal AS evidence_ordinal
          FROM kb.fact_occurrence f
          LEFT JOIN kb.fact_evidence e ON e.fact_pk = f.fact_pk
         WHERE f.profile_version_pk = :profile_version_pk
         ORDER BY f.ordinal, e.ordinal
        """,
        {"profile_version_pk": profile_version_pk},
    )
    profile: dict[str, Any] = {
        "notice_id": rows[0]["notice_id"],
        "source_profile_id": rows[0]["source_profile_id"],
        "schema_version": rows[0]["schema_version"],
        "comparison_profile": {},
    }
    by_fact: dict[str, dict[str, Any]] = {}
    for row in fact_rows:
        fact_id = row["fact_id"]
        fact = by_fact.get(fact_id)
        if fact is None:
            fact = {
                "fact_id": fact_id,
                "value_raw": row["value_raw"],
                "status": row["status"],
                "scope": row["scope"],
                "value_source": {
                    "source_block_id": row["source_block_id"],
                    "start_char": row["start_char"],
                    "end_char": row["end_char"],
                    "text_basis": row["text_basis"],
                },
                "evidence": [],
            }
            by_fact[fact_id] = fact
            profile["comparison_profile"].setdefault(row["field_name"], []).append(fact)
        if row["evidence_source_block_id"] is not None:
            fact["evidence"].append(
                {
                    "source_block_id": row["evidence_source_block_id"],
                    "section_id": row["section_id"],
                    "common_ir_document_id": row["common_ir_document_id"],
                    "common_ir_block_id": row["common_ir_block_id"],
                    "common_ir_cell_id": row["common_ir_cell_id"],
                    "common_ir_occurrence_ids": list(row["common_ir_occurrence_ids"] or []),
                }
            )
    return profile


def _kb_profile_pk_for_candidate(
    engine: Engine, *, version_id: int, announcement_profile_id: int | None
) -> UUID | None:
    if not _kb_schema_available(engine):
        return None
    if announcement_profile_id is not None:
        clause = "ap.id = :announcement_profile_id AND ap.announcement_version_id = :version_id"
        params = {
            "announcement_profile_id": announcement_profile_id,
            "version_id": version_id,
        }
    else:
        clause = "ap.announcement_version_id = :version_id"
        params = {"version_id": version_id}
    rows = _read(
        engine,
        f"""
        SELECT pv.profile_version_pk
          FROM sims.announcement_profile ap
          JOIN kb.source_profile sp ON sp.source_profile_id = ap.source_profile_id
          JOIN kb.source_version sv ON sv.source_profile_pk = sp.source_profile_pk
           AND sv.is_current
          JOIN kb.profile_version pv ON pv.source_version_pk = sv.source_version_pk
           AND pv.is_current
         WHERE {clause} AND ap.status = 'OK'
         ORDER BY ap.id DESC
         LIMIT 1
        """,
        params,
    )
    return rows[0]["profile_version_pk"] if rows else None


def compare_kb_candidates(
    engine: Engine,
    request_common: SimCommonProfile,
    announcement_version_ids: list[int | KbCandidate],
    llm_client: LLMClient,
    *,
    model_profile: str,
) -> list[CandidateComparison]:
    """후보 목록을 요청서 공통 프로파일과 비교한다.

    프로파일이 없거나 FAILED 인 후보는 reason code 를 달고 목록에 남는다.
    빼버리면 "정보가 없어서 못 봤다" 와 "봤는데 안 닮았다" 가 같아진다
    (초안 §7.2). 한 후보의 실패가 다른 후보의 결과를 지우지 않는다.
    """

    comparisons: list[CandidateComparison] = []
    kb_schema_available = _kb_schema_available(engine)
    for candidate_or_version in announcement_version_ids:
        candidate_profile_id = (
            candidate_or_version.announcement_profile_id
            if isinstance(candidate_or_version, KbCandidate)
            else None
        )
        version_id = (
            candidate_or_version.announcement_version_id
            if isinstance(candidate_or_version, KbCandidate)
            else int(candidate_or_version)
        )
        kb_profile_pk = (
            candidate_or_version.kb_profile_version_pk
            if isinstance(candidate_or_version, KbCandidate)
            else None
        ) or _kb_profile_pk_for_candidate(
            engine,
            version_id=version_id,
            announcement_profile_id=candidate_profile_id,
        )
        if kb_profile_pk is not None:
            kb_profile = _load_kb_profile(engine, kb_profile_pk)
            if kb_profile is not None:
                candidate_common = build_common_profile(
                    kb_profile, llm_client, model_profile=model_profile
                )
                comparisons.append(
                    CandidateComparison(
                        announcement_version_id=version_id,
                        source_profile_id=kb_profile["source_profile_id"],
                        announcement_profile_id=candidate_profile_id,
                        candidate_common=candidate_common,
                        result=compare_candidate(
                            request_common,
                            candidate_common,
                            llm_client,
                            model_profile=model_profile,
                        ),
                    )
                )
                continue
        if kb_schema_available:
            # Once the KB schema is present, a legacy JSONB profile is not a
            # second SIM source of truth.  A profile that did not materialize
            # into kb.* remains an explicitly unviewable candidate.
            comparisons.append(
                CandidateComparison(
                    announcement_version_id=version_id,
                    source_profile_id=(
                        candidate_or_version.source_profile_id
                        if isinstance(candidate_or_version, KbCandidate)
                        else None
                    ),
                    announcement_profile_id=candidate_profile_id,
                    reason_code=CANDIDATE_PROFILE_MISSING,
                    diagnostics=[
                        StageDiagnostic(
                            stage=_INGEST_STAGE,
                            unit=str(version_id),
                            reason_code=CANDIDATE_PROFILE_MISSING,
                            message="kb.*에 현재 ExistingProfile이 없어 SIM을 수행하지 않았다.",
                        )
                    ],
                )
            )
            continue
        if candidate_profile_id is None:
            profile_filter = "announcement_version_id = :version_id"
            profile_parameters = {"version_id": version_id}
        else:
            profile_filter = (
                "id = :announcement_profile_id "
                "AND announcement_version_id = :version_id"
            )
            profile_parameters = {
                "announcement_profile_id": candidate_profile_id,
                "version_id": version_id,
            }
        rows = _read(
            engine,
            f"""
            SELECT id, source_profile_id, status, profile_json, diagnostics
              FROM sims.announcement_profile
             WHERE {profile_filter}
             ORDER BY (status = 'OK') DESC, id DESC
             LIMIT 1
            """,
            profile_parameters,
        )
        if not rows:
            comparisons.append(
                CandidateComparison(
                    announcement_version_id=version_id,
                    source_profile_id=None,
                    announcement_profile_id=candidate_profile_id,
                    reason_code=CANDIDATE_PROFILE_MISSING,
                    diagnostics=[
                        StageDiagnostic(
                            stage=_INGEST_STAGE,
                            unit=str(version_id),
                            reason_code=CANDIDATE_PROFILE_MISSING,
                            message="이 공고에는 저장된 프로파일이 없다.",
                        )
                    ],
                )
            )
            continue
        row = rows[0]
        if row["status"] != "OK":
            stored = [
                StageDiagnostic(
                    stage=item.get("stage") or _INGEST_STAGE,
                    unit=item.get("unit"),
                    reason_code=item.get("reason_code"),
                    message=item.get("message") or "",
                )
                for item in row["diagnostics"] or []
            ]
            comparisons.append(
                CandidateComparison(
                    announcement_version_id=version_id,
                    source_profile_id=row["source_profile_id"],
                    announcement_profile_id=int(row["id"]),
                    reason_code=CANDIDATE_PROFILE_FAILED,
                    diagnostics=[
                        StageDiagnostic(
                            stage=_INGEST_STAGE,
                            unit=row["source_profile_id"],
                            reason_code=CANDIDATE_PROFILE_FAILED,
                            message="프로파일 생산이 실패한 후보다.",
                        ),
                        *stored,
                    ],
                )
            )
            continue
        candidate_common = build_common_profile(
            row["profile_json"], llm_client, model_profile=model_profile
        )
        comparisons.append(
            CandidateComparison(
                announcement_version_id=version_id,
                source_profile_id=row["source_profile_id"],
                announcement_profile_id=int(row["id"]),
                candidate_common=candidate_common,
                result=compare_candidate(
                    request_common,
                    candidate_common,
                    llm_client,
                    model_profile=model_profile,
                ),
            )
        )
    return comparisons


# --------------------------------------------------------------------- SQL


def _read(engine: Engine, statement: str, parameters: dict[str, Any]) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(statement), parameters).mappings().all()
    return [dict(row) for row in rows]


def _kb_schema_available(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            return connection.scalar(text("SELECT to_regclass('kb.profile_version')")) is not None
    except SQLAlchemyError:
        return False


def _write(engine: Engine, statement: str, parameters: dict[str, Any]) -> Any:
    """``RETURNING`` 이 있으면 그 값을, 없으면 None 을 돌려준다."""

    with engine.begin() as connection:
        result = connection.execute(text(statement), parameters)
        return result.scalar_one_or_none() if result.returns_rows else None
