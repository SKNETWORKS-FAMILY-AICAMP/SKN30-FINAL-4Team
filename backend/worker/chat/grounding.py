"""Cheap post-response checks for a result-grounded chat answer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import re
from typing import Any

from .context import context_evidence_ids
from .contracts import ChatAnswer


_NUMBER = re.compile(r"\d[\d,]*")
_INTERNAL = re.compile(
    r"백분위|퍼센타일|percentile|신뢰도|확신도|confidence|확률|probabilit"
)
_CAUSE = re.compile(r"(?:주요|가장 큰|핵심)?\s*원인(?:입니다|이다|은|는)?")


def _numbers(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _numbers(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _numbers(nested)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        yield str(value).replace(",", "")
    elif isinstance(value, str):
        yield from (match.group(0).replace(",", "") for match in _NUMBER.finditer(value))


def check_grounding(answer: str, context: Mapping[str, Any]) -> list[str]:
    """Return warnings without blocking a response that is still useful."""

    serialized = json.dumps(context, ensure_ascii=False, default=str)
    available_numbers = set(_numbers(serialized))
    unsupported = sorted(
        {
            match.group(0)
            for match in _NUMBER.finditer(answer or "")
            if len(match.group(0).replace(",", "")) >= 3
            and match.group(0).replace(",", "") not in available_numbers
        }
    )
    warnings: list[str] = []
    if unsupported:
        warnings.append("unsupported_numeric_claim: " + ", ".join(unsupported))
    internal = sorted(set(match.group(0) for match in _INTERNAL.finditer(answer or "")))
    if internal:
        warnings.append("internal_value_mentions: " + ", ".join(internal))

    model3 = ((context.get("result") or {}).get("ml") or {}).get("model_3")
    if isinstance(model3, Mapping) and not model3.get("cause_axes") and _CAUSE.search(answer or ""):
        warnings.append("unsupported_causal_claim: model_3 cause_axes is empty")
    return warnings


def validate_references(answer: ChatAnswer, context: Mapping[str, Any]) -> list[str]:
    """Check only evidence IDs; result references may legitimately have null IDs."""

    allowed = context_evidence_ids(context)
    invalid = sorted(
        {
            reference.evidence_id
            for reference in answer.references
            if reference.evidence_id is not None and reference.evidence_id not in allowed
        }
    )
    if not invalid:
        return []
    return ["invalid_reference: " + ", ".join(invalid)]


__all__ = ["check_grounding", "validate_references"]
