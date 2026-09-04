import asyncio
import json
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.ports.document_parser import DocumentParser
from app.ports.embedding_client import (
    EmbeddingClient,
    EmbeddingInvalidResponseError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from app.ports.llm_client import (
    LLMClient,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.ports.object_storage import ObjectStorage
from app.ports.pdf_renderer import PdfRenderer
from app.schemas.cpl import (
    CplAxisCode,
    CplFieldCode,
    CplResult,
    CplSemanticResponse,
    CplStatus,
)
from app.schemas.fit import FitInputFeedback, FitResult
from app.schemas.parsed_document import ParsedDocument
from app.services.cpl.checker import request_reason_from_result, run_cpl
from app.services.cpl.logic_validator import (
    CPL_SEMANTIC_FIELDS,
    CPL_FIELD_AXES,
    evaluate_cpl_rules,
    ground_llm_response,
    merge_llm_result,
    semantic_fragments,
)
from app.services.document_parsing import run_case_parsing
from app.services.fit.fit_engine import analyze_fit, load_fit_prompt, load_fit_scoring
from app.services.retrieval.retrieval import (
    RetrievalResult,
    RetrievalNotReadyError,
    compose_inspection_embedding_text,
    retrieve_top_five,
)
from app.services.reporting import finalize_report
from app.schemas.sim import SimComparisonResult
from app.services.agents.orchestrator import reconcile_cpl_for_fit
from app.services.sim.sim_engine import (
    analyze_sim_candidates,
    load_sim_prompt,
    load_sim_scoring,
)


logger = logging.getLogger(__name__)
SAFE_FAILURE_CODE = "CPL_ANALYSIS_FAILED"
SAFE_FAILURE_MESSAGE = "The document checklist could not be completed"
RETRIEVAL_FAILURE_MESSAGE = "Similar-program retrieval could not be completed"


async def run_analysis_pipeline(
    engine: Engine,
    storage: ObjectStorage,
    parser: DocumentParser,
    llm_client: LLMClient | None,
    settings: Settings,
    case_id: int,
    pdf_renderer: PdfRenderer,
    embedding_client: EmbeddingClient | None = None,
) -> FitResult | None:
    """Run parsing, CPL, FIT, retrieval, then candidate-by-candidate SIM."""
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
        if llm_client is not None:
            async def recheck_cpl(
                current: CplResult,
                feedback: tuple[FitInputFeedback, ...],
            ) -> CplResult:
                fields = {item.field_code for item in feedback}
                return await _complete_semantic_review(
                    document,
                    current,
                    fields,
                    llm_client,
                    settings,
                    fit_feedback=feedback,
                )

            reconciliation = await reconcile_cpl_for_fit(
                result,
                recheck=recheck_cpl,
            )
            result = reconciliation.result
        cpl_run = run_cpl(
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
        return

    fit_result = await _run_fit(result, llm_client, settings, case_id)
    retrieval_result = await _run_retrieval(
        engine,
        embedding_client,
        case_id,
        result,
    )
    if retrieval_result is not None:
        sim_results = await _run_sim(
            engine,
            retrieval_result,
            result,
            llm_client,
            settings,
            case_id,
        )
        await finalize_report(
            engine,
            storage,
            pdf_renderer,
            settings,
            case_id=case_id,
            missing_check_run_id=cpl_run.missing_check_run_id,
            retrieval_run_id=retrieval_result.retrieval_run_id,
            cpl_result=result,
            fit_result=fit_result,
            sim_results=sim_results,
            expected_candidate_count=len(retrieval_result.candidates),
        )
    return fit_result


async def _run_fit(
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    settings: Settings,
    case_id: int,
) -> FitResult | None:
    try:
        return await analyze_fit(
            cpl_result,
            llm_client,
            scoring=load_fit_scoring(settings.fit_scoring_path),
            prompt=load_fit_prompt(settings.fit_prompt_path),
            ruleset_version=settings.fit_ruleset_version,
            prompt_version=settings.fit_prompt_version,
            model_profile=settings.fit_model_profile,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "FIT analysis failed for case %s: %s",
            case_id,
            type(error).__name__,
        )
        return None


async def _run_retrieval(
    engine: Engine,
    client: EmbeddingClient | None,
    case_id: int,
    cpl_result: CplResult,
) -> RetrievalResult | None:
    if client is None:
        logger.warning("Retrieval skipped for case %s: embedding client disabled", case_id)
        _record_retrieval_failure(engine, case_id, "RETRIEVAL_UNAVAILABLE")
        return
    axes = {
        field_code: _cpl_axis_text(cpl_result, field_code)
        for field_code in (
            CplFieldCode.PURPOSE_GOAL,
            CplFieldCode.TARGET_AND_CONDITIONS,
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
        )
    }
    try:
        input_text = compose_inspection_embedding_text(
            purpose=axes[CplFieldCode.PURPOSE_GOAL],
            target=axes[CplFieldCode.TARGET_AND_CONDITIONS],
            content=axes[CplFieldCode.SUPPORT_CONTENT_AND_SCALE],
        )
        return await retrieve_top_five(
            engine,
            client,
            case_id=case_id,
            input_text=input_text,
        )
    except asyncio.CancelledError:
        _record_retrieval_failure(engine, case_id, "RETRIEVAL_CANCELLED")
        raise
    except EmbeddingTimeoutError:
        failure_code = "RETRIEVAL_TIMEOUT"
    except EmbeddingUnavailableError:
        failure_code = "RETRIEVAL_UNAVAILABLE"
    except EmbeddingInvalidResponseError:
        failure_code = "RETRIEVAL_INVALID_RESPONSE"
    except RetrievalNotReadyError:
        failure_code = "RETRIEVAL_NOT_READY"
    except ValueError:
        failure_code = "RETRIEVAL_INPUT_INVALID"
    except Exception as error:
        failure_code = "RETRIEVAL_FAILED"
        logger.warning(
            "Retrieval incomplete for case %s: %s",
            case_id,
            type(error).__name__,
        )
    logger.warning("Retrieval incomplete for case %s: %s", case_id, failure_code)
    _record_retrieval_failure(engine, case_id, failure_code)
    return None


async def _run_sim(
    engine: Engine,
    retrieval_result: RetrievalResult,
    cpl_result: CplResult,
    llm_client: LLMClient | None,
    settings: Settings,
    case_id: int,
) -> list[SimComparisonResult]:
    try:
        return await analyze_sim_candidates(
            engine,
            retrieval_result,
            cpl_result,
            llm_client,
            scoring=load_sim_scoring(settings.sim_scoring_path),
            prompt=load_sim_prompt(settings.sim_prompt_path),
            ruleset_version=settings.sim_ruleset_version,
            prompt_version=settings.sim_prompt_version,
            model_profile=settings.sim_model_profile,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "SIM analysis incomplete for case %s: %s",
            case_id,
            type(error).__name__,
        )
        return []


def _cpl_axis_text(result: CplResult, field_code: CplFieldCode) -> str:
    item = next(item for item in result.items if item.field_code == field_code)
    values = dict.fromkeys(
        occurrence.raw_text.strip()
        for occurrence in item.occurrences
        if occurrence.raw_text.strip()
    )
    return "\n".join(values)


async def _complete_semantic_review(
    document: ParsedDocument,
    rule_result: CplResult,
    semantic_fields: set[CplFieldCode],
    llm_client: LLMClient | None,
    settings: Settings,
    *,
    fit_feedback: tuple[FitInputFeedback, ...] = (),
) -> CplResult:
    if not semantic_fields:
        return _with_runtime_metadata(rule_result, settings)
    if llm_client is None:
        return _with_runtime_metadata(
            merge_llm_result(
                rule_result,
                llm_error="LLM_UNAVAILABLE",
                requested_fields=semantic_fields,
            ),
            settings,
        )

    try:
        response = await llm_client.generate_structured(
            task_name="cpl_semantic_evidence",
            messages=_semantic_messages(
                document,
                semantic_fields,
                load_cpl_prompt(settings.cpl_prompt_path),
                fit_feedback=fit_feedback,
            ),
            response_schema=CplSemanticResponse,
            model_profile=settings.cpl_model_profile,
        )
        if not isinstance(response, CplSemanticResponse):
            raise LLMInvalidResponseError("Unexpected structured response type")
        grounding_warnings: list[str] = []
        candidates = ground_llm_response(
            document,
            response,
            semantic_fields,
            warning_sink=grounding_warnings,
        )
        result = merge_llm_result(
            rule_result,
            candidates,
            additional_warnings=grounding_warnings,
        )
    except LLMTimeoutError as error:
        result = merge_llm_result(
            rule_result, llm_error="LLM_TIMEOUT", requested_fields=semantic_fields
        )
        _log_semantic_failure("LLM_TIMEOUT", error)
    except LLMUnavailableError as error:
        result = merge_llm_result(
                rule_result,
                llm_error="LLM_UNAVAILABLE",
                requested_fields=semantic_fields,
            )
        _log_semantic_failure("LLM_UNAVAILABLE", error)
    except (LLMInvalidResponseError, ValidationError, ValueError) as error:
        # 이 절은 응답 형식 오류와 ground_llm_response() 의 접지 실패를 함께
        # 잡는다. 코드만으로는 둘을 구분할 수 없어 예외 유형과 메시지를 남긴다.
        result = merge_llm_result(
            rule_result,
            llm_error="LLM_INVALID_RESPONSE",
            requested_fields=semantic_fields,
        )
        _log_semantic_failure("LLM_INVALID_RESPONSE", error)
    return _with_runtime_metadata(result, settings)


def _log_semantic_failure(failure_code: str, error: Exception) -> None:
    """요청서 원문과 LLM 응답 본문은 남기지 않는다(구현기준서 15)."""
    detail: str | None = None
    if isinstance(error, ValidationError):
        detail = ",".join(
            f"{'.'.join(map(str, item['loc']))}:{item['type']}"
            for item in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
    elif type(error) is ValueError:
        detail = str(error)
    logger.warning(
        "CPL semantic extraction failed failure=%s error=%s detail=%s",
        failure_code,
        type(error).__name__,
        detail,
    )


def _semantic_messages(
    document: ParsedDocument,
    semantic_fields: set[CplFieldCode],
    prompt: str,
    *,
    fit_feedback: tuple[FitInputFeedback, ...] = (),
) -> list[Message]:
    fragments = [
        {
            "evidence_ref": fragment.evidence_ref,
            "field_codes": sorted(field.value for field in fragment.field_codes),
            "source_role": fragment.source_role,
            "text": fragment.text,
            "scopes": [
                {
                    "start": scope.start,
                    "end": scope.end,
                    "field_codes": sorted(
                        field.value for field in scope.field_codes
                    ),
                    "source_role": scope.source_role,
                }
                for scope in fragment.scopes
            ],
        }
        for fragment in semantic_fragments(document)
        if not fragment.field_codes
        or bool(fragment.field_codes & semantic_fields)
    ]
    requested = [
        field.value for field in CplFieldCode if field in semantic_fields
    ]
    axis_constraints = _feedback_axis_constraints(fit_feedback)
    allowed_axes = {
        field.value: sorted(
            axis.value
            for axis in axis_constraints.get(field, CPL_FIELD_AXES[field])
        )
        for field in CplFieldCode
        if field in semantic_fields
    }
    payload: dict[str, object] = {
        "requested_fields": requested,
        "allowed_axes": allowed_axes,
        "fragments": fragments,
    }
    if fit_feedback:
        payload["fit_input_feedback"] = [
            item.model_dump(mode="json") for item in fit_feedback
        ]
    return [
        Message(
            role="developer",
            content=prompt,
        ),
        Message(
            role="user",
            content=json.dumps(
                payload,
                ensure_ascii=False,
            ),
        ),
    ]


def _feedback_axis_constraints(
    feedback: tuple[FitInputFeedback, ...],
) -> dict[CplFieldCode, frozenset[CplAxisCode]]:
    constraints: dict[CplFieldCode, set[CplAxisCode]] = {}
    for item in feedback:
        constraints.setdefault(item.field_code, set()).update(
            item.required_axis_codes
        )
    return {
        field_code: frozenset(axes)
        for field_code, axes in constraints.items()
        if axes
    }


def load_cpl_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("CPL prompt must not be blank")
    return prompt


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


def _record_retrieval_failure(
    engine: Engine,
    case_id: int,
    failure_code: str,
) -> None:
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
                "failure_code": failure_code,
                "failure_message": RETRIEVAL_FAILURE_MESSAGE,
            },
        )
