import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from app.schemas.cpl import CplResult, CplStatus
from app.schemas.fit import FitResult, FitStatus
from app.schemas.sim import SimComparisonResult

logger = logging.getLogger(__name__)


def persist_analysis_case_result(
    engine: Engine,
    *,
    analysis_run_pk: UUID | str,
    user_id: UUID | str,
    original_filename: str,
    program_name: str | None,
    document_text: str,
    cpl_result: CplResult,
    fit_result: FitResult | None = None,
    sim_results: list[SimComparisonResult] | None = None,
    ml_results: dict[str, Any] | None = None,
    report_type: str = "final_pdf",
) -> UUID:
    """Persist full pipeline analysis results into Supabase v2.2 result.* schema.
    
    Operates in a single database transaction with SAVEPOINT protection:
    1. result.analysis_case
    2. result.axis_result (Bulk batch INSERT for CPL 13 and FIT 7 axes)
    3. result.sim_candidate (SAVEPOINT begin_nested() protection against FK mismatch)
    4. result.report_generation (PDF report tracking)
    5. result.analysis_session (Closes previous active session, activates new one)
    """
    now = datetime.now(timezone.utc)
    retention_expires_at = now + timedelta(days=90)
    session_expires_at = now + timedelta(minutes=30)
    analysis_case_pk = uuid4()

    input_profile = {
        "program_name": program_name or "사전협의 요청서",
        "original_filename": original_filename,
        "text_preview": (document_text or "")[:500],
        "char_count": len(document_text or ""),
        "analyzed_at": now.isoformat(),
    }

    with engine.begin() as conn:
        # 1. result.analysis_case
        conn.execute(
            text(
                """
                INSERT INTO result.analysis_case (
                    analysis_case_pk,
                    source_analysis_run_id,
                    user_id,
                    case_status,
                    original_filename,
                    program_name,
                    input_profile_snapshot,
                    result_schema_version,
                    analysis_completed_at,
                    retention_expires_at,
                    created_at,
                    updated_at
                ) VALUES (
                    :case_pk,
                    :run_id,
                    :user_id,
                    'ready',
                    :original_filename,
                    :program_name,
                    CAST(:input_profile AS jsonb),
                    'v1',
                    :now,
                    :retention_expires_at,
                    :now,
                    :now
                )
                """
            ),
            {
                "case_pk": analysis_case_pk,
                "run_id": analysis_run_pk,
                "user_id": user_id,
                "original_filename": original_filename,
                "program_name": program_name,
                "input_profile": json.dumps(input_profile),
                "now": now,
                "retention_expires_at": retention_expires_at,
            },
        )

        # 2. result.axis_result - Bulk batch INSERT for CPL 13 axes
        cpl_rows = []
        for ordinal, item in enumerate(cpl_result.items, start=1):
            item_detail = {
                "field_code": item.field_code.value,
                "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                "reason_code": item.reason_code,
                "explanation": item.explanation,
                "occurrences": [
                    {
                        "raw_value": occ.raw_value,
                        "context_excerpt": occ.context_excerpt,
                        "method": occ.extraction_method,
                    }
                    for occ in item.occurrences
                ],
            }
            cpl_rows.append(
                {
                    "case_pk": analysis_case_pk,
                    "code": item.field_code.value,
                    "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                    "summary": item.explanation or f"{item.field_code.value} 점검 완료",
                    "detail": json.dumps(item_detail),
                    "ordinal": ordinal,
                    "now": now,
                }
            )

        if cpl_rows:
            conn.execute(
                text(
                    """
                    INSERT INTO result.axis_result (
                        axis_result_pk,
                        analysis_case_pk,
                        axis_type,
                        axis_code,
                        status,
                        summary_text,
                        result_data,
                        ordinal,
                        created_at
                    ) VALUES (
                        gen_random_uuid(),
                        :case_pk,
                        'CPL',
                        :code,
                        :status,
                        :summary,
                        CAST(:detail AS jsonb),
                        :ordinal,
                        :now
                    )
                    """
                ),
                cpl_rows,
            )

        # 3. result.axis_result - Bulk batch INSERT for FIT 7 axes
        if fit_result is not None:
            fit_items = getattr(fit_result, "items", [])
            fit_rows = []
            for ordinal, fit_item in enumerate(fit_items, start=1):
                f_code = getattr(fit_item, "code", f"FIT-{ordinal}")
                f_status = getattr(fit_item, "status", FitStatus.SUITABLE)
                f_summary = getattr(fit_item, "summary", "") or getattr(fit_item, "evaluation", "")
                f_detail = (
                    fit_item.model_dump(mode="json")
                    if hasattr(fit_item, "model_dump")
                    else {"code": f_code, "status": str(f_status), "summary": f_summary}
                )
                fit_rows.append(
                    {
                        "case_pk": analysis_case_pk,
                        "code": str(f_code),
                        "status": f_status.value if hasattr(f_status, "value") else str(f_status),
                        "summary": f_summary or f"{f_code} 적합성 검토 완료",
                        "detail": json.dumps(f_detail),
                        "ordinal": ordinal,
                        "now": now,
                    }
                )
            if fit_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO result.axis_result (
                            axis_result_pk,
                            analysis_case_pk,
                            axis_type,
                            axis_code,
                            status,
                            summary_text,
                            result_data,
                            ordinal,
                            created_at
                        ) VALUES (
                            gen_random_uuid(),
                            :case_pk,
                            'FIT',
                            :code,
                            :status,
                            :summary,
                            CAST(:detail AS jsonb),
                            :ordinal,
                            :now
                        )
                        """
                    ),
                    fit_rows,
                )

        # 4. result.axis_result - DIF (Model 3) if present in ml_results
        if ml_results and "dif" in ml_results:
            dif_data = ml_results["dif"]
            conn.execute(
                text(
                    """
                    INSERT INTO result.axis_result (
                        axis_result_pk,
                        analysis_case_pk,
                        axis_type,
                        axis_code,
                        status,
                        summary_text,
                        result_data,
                        ordinal,
                        created_at
                    ) VALUES (
                        gen_random_uuid(),
                        :case_pk,
                        'DIF',
                        'DIF-M3',
                        'COMPLETED',
                        '통계 기반 이례성(DIF) 분석 완료',
                        CAST(:detail AS jsonb),
                        1,
                        :now
                    )
                    """
                ),
                {
                    "case_pk": analysis_case_pk,
                    "detail": json.dumps(dif_data),
                    "now": now,
                },
            )

        # 5. result.sim_candidate (SAVEPOINT begin_nested() protection per candidate)
        if sim_results:
            for rank_no, sim_cand in enumerate(sim_results[:5], start=1):
                cand_title = getattr(sim_cand, "title", f"유사 공고 {rank_no}")
                cand_org = getattr(sim_cand, "organization", getattr(sim_cand, "issuing_organization", "공공기관"))
                cand_url = getattr(sim_cand, "source_url", getattr(sim_cand, "url", None))
                cand_status = getattr(sim_cand, "overall_verdict", getattr(sim_cand, "status", "SIMILAR"))
                cand_summary = getattr(sim_cand, "summary", getattr(sim_cand, "summary_text", ""))
                axes_dict = getattr(sim_cand, "axes", {})

                purpose_res = axes_dict.get("purpose") if isinstance(axes_dict, dict) else None
                target_res = axes_dict.get("target") if isinstance(axes_dict, dict) else None
                support_res = axes_dict.get("support") if isinstance(axes_dict, dict) else None
                delivery_res = axes_dict.get("delivery") if isinstance(axes_dict, dict) else None

                # Isolated in nested SAVEPOINT so that an FK/constraint error does NOT abort the outer transaction
                try:
                    with conn.begin_nested():
                        conn.execute(
                            text(
                                """
                                INSERT INTO result.sim_candidate (
                                    sim_candidate_pk,
                                    analysis_case_pk,
                                    rank_no,
                                    announcement_title,
                                    issuing_organization,
                                    source_url,
                                    notice_status,
                                    status,
                                    summary_text,
                                    comparable_axes,
                                    purpose_result,
                                    target_result,
                                    support_result,
                                    delivery_result,
                                    created_at
                                ) VALUES (
                                    gen_random_uuid(),
                                    :case_pk,
                                    :rank_no,
                                    :title,
                                    :org,
                                    :url,
                                    'ACTIVE',
                                    :status,
                                    :summary,
                                    ARRAY['purpose', 'target', 'support', 'delivery'],
                                    CAST(:purpose AS jsonb),
                                    CAST(:target AS jsonb),
                                    CAST(:support AS jsonb),
                                    CAST(:delivery AS jsonb),
                                    :now
                                )
                                """
                            ),
                            {
                                "case_pk": analysis_case_pk,
                                "rank_no": rank_no,
                                "title": cand_title,
                                "org": cand_org,
                                "url": cand_url,
                                "status": str(cand_status),
                                "summary": cand_summary,
                                "purpose": json.dumps(purpose_res or {}),
                                "target": json.dumps(target_res or {}),
                                "support": json.dumps(support_res or {}),
                                "delivery": json.dumps(delivery_res or {}),
                                "now": now,
                            },
                        )
                except Exception as ex:
                    logger.warning("Sim candidate %d insert failed (rolled back to savepoint): %s", rank_no, ex)

        # 6. result.report_generation (PDF generation tracker)
        conn.execute(
            text(
                """
                INSERT INTO result.report_generation (
                    report_generation_pk,
                    analysis_case_pk,
                    report_type,
                    status,
                    retry_count,
                    requested_at,
                    completed_at,
                    updated_at
                ) VALUES (
                    gen_random_uuid(),
                    :case_pk,
                    :report_type,
                    'ready',
                    0,
                    :now,
                    :now,
                    :now
                )
                """
            ),
            {
                "case_pk": analysis_case_pk,
                "report_type": report_type,
                "now": now,
            },
        )

        # 7. result.analysis_session (Manage user active conversation session)
        conn.execute(
            text(
                """
                UPDATE result.analysis_session
                SET status = 'closed',
                    close_reason = 'new_analysis',
                    closed_at = :now,
                    updated_at = :now
                WHERE user_id = :user_id AND status = 'active'
                """
            ),
            {"user_id": user_id, "now": now},
        )

        conn.execute(
            text(
                """
                INSERT INTO result.analysis_session (
                    analysis_session_pk,
                    analysis_case_pk,
                    user_id,
                    status,
                    expires_at,
                    last_activity_at,
                    created_at,
                    updated_at
                ) VALUES (
                    gen_random_uuid(),
                    :case_pk,
                    :user_id,
                    'active',
                    :expires_at,
                    :now,
                    :now,
                    :now
                )
                """
            ),
            {
                "case_pk": analysis_case_pk,
                "user_id": user_id,
                "expires_at": session_expires_at,
                "now": now,
            },
        )

    logger.info(
        "Successfully persisted analysis case %s for user %s",
        analysis_case_pk,
        user_id,
    )
    return analysis_case_pk
