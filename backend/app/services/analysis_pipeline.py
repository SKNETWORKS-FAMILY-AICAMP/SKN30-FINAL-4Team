import asyncio
import json
import logging

from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.ports.document_parser import DocumentParser
from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.ports.object_storage import ObjectStorage
from app.schemas.cpl import CplFieldCode, CplResult, CplSemanticResponse, CplStatus
from app.schemas.parsed_document import ParsedDocument
from app.services.cpl.checker import request_reason_from_result, run_cpl
from app.services.cpl.logic_validator import (
    CPL_SEMANTIC_FIELDS,
    evaluate_cpl_rules,
    ground_llm_response,
    merge_llm_result,
)
from app.services.document_parsing import run_case_parsing


logger = logging.getLogger(__name__)
SAFE_FAILURE_CODE = "CPL_ANALYSIS_FAILED"
SAFE_FAILURE_MESSAGE = "The document checklist could not be completed"


async def run_analysis_pipeline(
    engine: Engine,
    storage: ObjectStorage,
    parser: DocumentParser,
    llm_client: LLMClient | None,
    settings: Settings,
    case_id: int,
) -> None:
    """Run Slice 5 parsing, then persist the Slice 6 CPL snapshot."""
    await run_case_parsing(engine, storage, parser, case_id)
    if _case_status(engine, case_id) != "CHECKING":
        return

    try:
        document = _load_parsed_document(engine, case_id)
        rule_result = evaluate_cpl_rules(
            document,
            ruleset_version=settings.cpl_ruleset_version,
        )
        semantic_fields = {
            item.field_code
            for item in rule_result.items
            if item.field_code in CPL_SEMANTIC_FIELDS
            and item.status not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
        }
        result = await _complete_semantic_review(
            document,
            rule_result,
            semantic_fields,
            llm_client,
            settings,
        )
        run_cpl(
            engine,
            case_id,
            result,
            request_reason=request_reason_from_result(result),
            extractor_name="cpl-rule-llm",
            extractor_version=(
                f"{settings.cpl_ruleset_version}+{settings.cpl_prompt_version}"
            ),
        )
    except asyncio.CancelledError:
        _record_cpl_failure(engine, case_id)
        raise
    except Exception as error:
        logger.warning(
            "CPL analysis failed for case %s: %s",
            case_id,
            type(error).__name__,
        )
        _record_cpl_failure(engine, case_id)


async def _complete_semantic_review(
    document: ParsedDocument,
    rule_result: CplResult,
    semantic_fields: set[CplFieldCode],
    llm_client: LLMClient | None,
    settings: Settings,
) -> CplResult:
    if not semantic_fields:
        return _with_runtime_metadata(rule_result, settings)
    if llm_client is None:
        return _with_runtime_metadata(
            merge_llm_result(rule_result, llm_error="LLM_UNAVAILABLE"),
            settings,
        )

    try:
        response = await llm_client.generate_structured(
            task_name="cpl_semantic_evidence",
            messages=_semantic_messages(document, semantic_fields),
            response_schema=CplSemanticResponse,
            model_profile=settings.cpl_model_profile,
        )
        if not isinstance(response, CplSemanticResponse):
            raise LLMInvalidResponseError("Unexpected structured response type")
        candidates = ground_llm_response(document, response, semantic_fields)
        result = merge_llm_result(
            rule_result,
            candidates,
            valid_block_ids={block.block_id for block in document.blocks},
        )
    except LLMTimeoutError:
        result = merge_llm_result(rule_result, llm_error="LLM_TIMEOUT")
    except LLMUnavailableError:
        result = merge_llm_result(rule_result, llm_error="LLM_UNAVAILABLE")
    except (LLMInvalidResponseError, ValidationError, ValueError):
        result = merge_llm_result(rule_result, llm_error="LLM_INVALID_RESPONSE")
    return _with_runtime_metadata(result, settings)


def _semantic_messages(
    document: ParsedDocument,
    semantic_fields: set[CplFieldCode],
) -> list[Message]:
    blocks = [
        {
            "block_id": block.block_id,
            "text": block.text,
            "table_cell_texts": [
                cell["text"]
                for cell in block.source_locator.get("cells", [])
                if isinstance(cell, dict) and isinstance(cell.get("text"), str)
            ],
        }
        for block in document.blocks
    ]
    requested = [
        field.value for field in CplFieldCode if field in semantic_fields
    ]
    return [
        Message(
            role="developer",
            content=(
                "Extract only explicit CPL evidence from the supplied parser blocks. "
                "Return exactly one item for every requested field. Quote raw_text "
                "verbatim and use only a supplied block_id. Do not invent evidence, "
                "source locations, policy judgments, or NOT_APPLICABLE decisions. "
                "Use NEEDS_CONFIRMATION for ambiguous or conflicting content."
            ),
        ),
        Message(
            role="user",
            content=json.dumps(
                {"requested_fields": requested, "blocks": blocks},
                ensure_ascii=False,
            ),
        ),
    ]


def _with_runtime_metadata(result: CplResult, settings: Settings) -> CplResult:
    snapshot = result.model_dump(mode="python")
    snapshot["model_profile"] = settings.cpl_model_profile
    snapshot["prompt_version"] = settings.cpl_prompt_version
    return CplResult.model_validate(snapshot)


def _case_status(engine: Engine, case_id: int) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT status FROM sims.inspection_case WHERE id = :case_id"),
            {"case_id": case_id},
        )


def _load_parsed_document(engine: Engine, case_id: int) -> ParsedDocument:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT r.parser_name, r.parser_version, r.extracted_text,
                       r.structured_content
                FROM sims.document_parse_run r
                JOIN sims.uploaded_document d ON d.file_asset_id = r.file_asset_id
                WHERE d.inspection_case_id = :case_id
                  AND r.status IN ('SUCCESS', 'PARTIAL_SUCCESS')
                ORDER BY r.attempt_no DESC
                LIMIT 1
                """
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
    if row is None or not isinstance(row["structured_content"], dict):
        raise RuntimeError("Successful parser snapshot was not found")
    structured = row["structured_content"]
    return ParsedDocument.model_validate(
        {
            "parser_name": row["parser_name"],
            "parser_version": row["parser_version"],
            "text": row["extracted_text"],
            "blocks": structured.get("blocks"),
            "warnings": structured.get("warnings", []),
            "partial": structured.get("partial", False),
        }
    )


def _record_cpl_failure(engine: Engine, case_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'FAILED',
                    failure_code = :failure_code,
                    failure_message = :failure_message
                WHERE id = :case_id AND status = 'CHECKING'
                """
            ),
            {
                "case_id": case_id,
                "failure_code": SAFE_FAILURE_CODE,
                "failure_message": SAFE_FAILURE_MESSAGE,
            },
        )
