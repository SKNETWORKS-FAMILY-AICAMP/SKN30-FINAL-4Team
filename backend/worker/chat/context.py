"""Project the persisted analysis result into a safe chat context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .contracts import ChatIntent


_PRIVATE_KEYS = (
    "internal",
    "confidence",
    "probability",
    "percentile",
    "raw_score",
    "semantic_similarity",
)
_MAX_EVIDENCE = 160
_MAX_EXCERPT = 400
_WS = re.compile(r"\s+")


def _is_private(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in _PRIVATE_KEYS)


def _public(value: Any) -> Any:
    """Remove values that are not part of the user-facing result contract."""

    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if not _is_private(key)
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


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
                key: _public(model[key]) for key in allowed if key in model
            }
    return result


def _evidence_public(evidences: object) -> list[dict[str, Any]]:
    if not isinstance(evidences, Sequence) or isinstance(evidences, (str, bytes)):
        return []
    output: list[dict[str, Any]] = []
    for raw in list(evidences)[:_MAX_EVIDENCE]:
        if not isinstance(raw, Mapping):
            continue
        evidence_id = raw.get("evidence_id") or raw.get("evidence_ref")
        if evidence_id in (None, ""):
            continue
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

    clean = _public(dict(report))
    known = {
        key: clean[key]
        for key in ("case", "cpl", "fit", "sim", "report")
        if key in clean
    }
    ml = _ml_public(report.get("ml"))
    evidence = _evidence_public(report.get("evidences", report.get("evidence")))
    if intent in (ChatIntent.MODEL_1, ChatIntent.MODEL_2, ChatIntent.MODEL_3):
        result = {"ml": {intent_model_key(intent): ml.get(intent_model_key(intent), {})}}
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
        if role in ("USER", "ASSISTANT", "user", "assistant") and isinstance(content, str):
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
