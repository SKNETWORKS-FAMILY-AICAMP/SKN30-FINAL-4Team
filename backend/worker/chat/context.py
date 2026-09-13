"""Project the persisted analysis result into a safe chat context."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ChatIntent

_MAX_EVIDENCE = 160
_MAX_EXCERPT = 400
_WS = re.compile(r"\s+")


def _pick(value: object, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in allowed if key in value}


def _public_cpl_or_fit(value: object, *, axis_type: str) -> list[dict[str, Any]]:
    """Copy the v0.2 public detail schema, never arbitrary ``detail`` JSON.

    Legacy result rows stored raw ``result_data`` under ``detail``.  A denylist
    cannot safely distinguish its fact ids/diagnostics from newly introduced
    raw fields, so chat accepts only the typed v0.2 keys below.
    """

    if not isinstance(value, Mapping):
        return []
    rows = value.get("items")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = _pick(row, {"code", "status", "summary"})
        detail = row.get("detail")
        if not isinstance(detail, Mapping):
            # The planned v2 RPC may use the storage name directly.
            detail = row.get("public_detail")
        if axis_type == "CPL":
            safe_detail = _pick(
                detail,
                {"reason_code", "reason", "values", "source_fields", "evidence_ids"},
            )
            values = safe_detail.get("values")
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                safe_detail["values"] = [
                    _pick(entry, {"label", "value", "evidence_ids"})
                    for entry in values
                    if isinstance(entry, Mapping)
                ]
        else:
            safe_detail = _pick(
                detail,
                {
                    "comparison_performed",
                    "reason_code",
                    "reason",
                    "left",
                    "right",
                    "evidence_ids",
                },
            )
            for side in ("left", "right"):
                safe_detail[side] = _pick(
                    safe_detail.get(side), {"value_summary", "evidence_ids"}
                )
        item["detail"] = safe_detail
        output.append(item)
    return output


def _public_sim(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = _pick(value, {"status", "reason_code", "summary"})
    candidates = value.get("candidates")
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        result["candidates"] = [
            _pick(
                candidate,
                {
                    "sim_candidate_id",
                    "rank",
                    "title",
                    "comparison_status",
                    "comparison_summary",
                },
            )
            for candidate in candidates
            if isinstance(candidate, Mapping)
        ]
    return result


def _public_case(value: object) -> dict[str, Any]:
    return _pick(
        value,
        {"analysis_case_id", "program_name", "original_filename", "completed_at"},
    )


def _ml_public(ml: object) -> dict[str, Any]:
    if not isinstance(ml, Mapping):
        return {}
    allowed = {
        "status",
        "support_type",
        "predicted_amount_won",
        "anomaly_level",
        "cause_axes",
        "message",
        "reason_code",
    }
    result: dict[str, Any] = {}
    for name in ("model_1", "model_2", "model_3"):
        model = ml.get(name)
        if isinstance(model, Mapping):
            result[name] = {
                key: _ml_value(model[key]) for key in allowed if key in model
            }
    return result


def _ml_value(value: object) -> Any:
    """ML's public contract contains scalars plus a string-list cause axis."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, str)]
    return None


def _evidence_public(evidences: object) -> list[dict[str, Any]]:
    if not isinstance(evidences, Sequence) or isinstance(evidences, (str, bytes)):
        return []
    output: list[dict[str, Any]] = []
    for raw in list(evidences)[:_MAX_EVIDENCE]:
        if not isinstance(raw, Mapping):
            continue
        evidence_id = raw.get("evidence_id")
        if evidence_id in (None, ""):
            continue
        # New v2 public projection owns an explicit excerpt.  ``raw_value``
        # remains solely for pre-v2 compatibility and is not allowed to carry
        # fact ids/diagnostics into a result-shaped context.
        excerpt = raw.get("excerpt") or raw.get("raw_value") or ""
        if not isinstance(excerpt, str):
            excerpt = str(excerpt)
        excerpt = _WS.sub(" ", excerpt).strip()[:_MAX_EXCERPT]
        output.append(
            {
                "evidence_id": str(evidence_id),
                "section": raw.get("axis_type") or raw.get("section"),
                "field_name": raw.get("field_name"),
                "excerpt": excerpt,
            }
        )
    return output


def build_chat_context(
    report: Mapping[str, Any],
    question: str,
    *,
    intent: ChatIntent,
    conversation: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a JSON-native context from the current result contract.

    The function accepts a mapping rather than a DB model, keeping the worker
    core independent from PostgreSQL and FastAPI.  The expected current public
    result keys are ``case``, ``cpl``, ``fit``, ``sim``, ``ml`` and
    ``evidences``; unknown public result fields are retained under ``result``
    after private diagnostics are removed so contract additions remain usable.
    """

    if not isinstance(report, Mapping) or not report:
        raise ValueError("analysis result must be a non-empty object")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must not be blank")

    known = {
        "case": _public_case(report.get("case")),
        "cpl": {"items": _public_cpl_or_fit(report.get("cpl"), axis_type="CPL")},
        "fit": {"items": _public_cpl_or_fit(report.get("fit"), axis_type="FIT")},
        "sim": _public_sim(report.get("sim")),
    }
    report_status = _pick(
        report.get("report"),
        {"status", "can_download", "can_regenerate", "retry_count"},
    )
    if report_status:
        known["report"] = report_status
    ml = _ml_public(report.get("ml"))
    evidence = _evidence_public(report.get("evidences", report.get("evidence")))
    if intent in (ChatIntent.MODEL_1, ChatIntent.MODEL_2, ChatIntent.MODEL_3):
        result = {
            "ml": {intent_model_key(intent): ml.get(intent_model_key(intent), {})}
        }
    elif intent is ChatIntent.DOCUMENT:
        result = {
            "case": known.get("case", {}),
            "cpl": known.get("cpl", {}),
            "evidence": evidence,
        }
    else:
        result = {**known, "ml": ml}

    history: list[dict[str, str]] = []
    for item in conversation or ():
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("USER", "ASSISTANT", "user", "assistant") and isinstance(
            content, str
        ):
            history.append({"role": str(role).lower(), "content": content[-4000:]})

    return {
        "intent": intent.value,
        "question": question.strip(),
        "result": result,
        "evidence": evidence,
        "evidence_truncated": len(evidence) >= _MAX_EVIDENCE,
        "conversation": history[-20:],
    }


def intent_model_key(intent: ChatIntent) -> str:
    return {
        ChatIntent.MODEL_1: "model_1",
        ChatIntent.MODEL_2: "model_2",
        ChatIntent.MODEL_3: "model_3",
    }.get(intent, "")


def context_evidence_ids(context: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("evidence_id"))
        for item in context.get("evidence", [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    }


__all__ = ["build_chat_context", "context_evidence_ids", "intent_model_key"]
