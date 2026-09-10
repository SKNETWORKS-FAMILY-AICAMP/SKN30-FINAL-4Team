"""Deterministic Request Profile inputs for three-axis vector retrieval.

Only server-materialised ``value_raw`` values with approved states are copied.
No generative summary is introduced.  Oversized inputs are split without
truncation and later token-weighted back into one vector per retrieval axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


ALLOWED_STATUSES = frozenset({"identified", "partial", "partially_identified"})
PURPOSE_FIELDS = ("purpose_goal",)
TARGET_FIELDS = (
    "applicant_eligibility",
    "support_target",
    "eligibility_conditions",
    "beneficiary",
    "applicable_entity",
    "exclusions",
    "duplicate_support_conditions",
    "participation_requirements",
)
SUPPORT_FIELDS = (
    "support_activities",
    "support_methods",
    "support_items",
    "support_content",
)
_LABELS = {"purpose": "사업목적", "target": "지원대상", "support": "지원내용"}
_INTEGER = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class EmbeddingChunk:
    text: str
    token_count: int


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    scope: str
    text: str
    input_sha256: str
    chunks: tuple[EmbeddingChunk, ...]


@dataclass(frozen=True, slots=True)
class _Fact:
    field_name: str
    value: str
    role: str
    order: tuple[Any, ...]


def _clean(value: object) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", str(value or ""))).strip()


def _iter_facts(profile: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    comparison = profile.get("comparison_profile")
    if isinstance(comparison, Mapping):
        for rows in comparison.values():
            if isinstance(rows, list):
                yield from (row for row in rows if isinstance(row, Mapping))
    components = profile.get("support_components")
    if isinstance(components, list):
        for component in components:
            if isinstance(component, Mapping) and isinstance(component.get("facts"), list):
                yield from (
                    row for row in component["facts"] if isinstance(row, Mapping)
                )


def _source_order(fact: Mapping[str, Any], encounter: int) -> tuple[Any, ...]:
    source = fact.get("value_source")
    source = source if isinstance(source, Mapping) else {}
    block_id = _clean(source.get("source_block_id"))
    numbers = tuple(int(item) for item in _INTEGER.findall(block_id))
    start = source.get("start_char")
    if isinstance(start, bool) or not isinstance(start, int):
        start = 0
    return (0 if numbers else 1, numbers, block_id, start, encounter)


def _group(profile: Mapping[str, Any]) -> dict[str, list[_Fact]]:
    grouped: dict[str, list[_Fact]] = {}
    seen: set[tuple[str, str, str]] = set()
    for encounter, row in enumerate(_iter_facts(profile)):
        if _clean(row.get("status")).lower() not in ALLOWED_STATUSES:
            continue
        field = _clean(row.get("field_name"))
        value = _clean(row.get("value_raw"))
        role = _clean(row.get("semantic_role") or row.get("subject_role"))
        key = (field, role, value)
        if not field or not value or key in seen:
            continue
        seen.add(key)
        grouped.setdefault(field, []).append(
            _Fact(field, value, role, _source_order(row, encounter))
        )
    for facts in grouped.values():
        facts.sort(key=lambda item: item.order)
    return grouped


def _render(grouped: Mapping[str, Sequence[_Fact]], fields: Sequence[str], *, headers: bool) -> str:
    sections: list[str] = []
    for field in fields:
        values = [f"{fact.role}: {fact.value}" if fact.role else fact.value for fact in grouped.get(field, ())]
        if values:
            body = "\n".join(values)
            sections.append(f"[{field}]\n{body}" if headers else body)
    return "\n\n".join(sections)


@lru_cache(maxsize=4)
def _encoding(model: str):
    try:
        import tiktoken
    except ImportError as error:  # pragma: no cover - deployment guard
        raise RuntimeError("tiktoken is required for retrieval input assembly") from error
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # New embedding aliases can precede tiktoken's model-name registry.
        # OpenAI's current text-embedding-3 family uses cl100k_base; falling
        # back keeps token-bound chunking deterministic for such aliases.
        return tiktoken.get_encoding("cl100k_base")


def _tokens(text: str, model: str) -> int:
    return len(_encoding(model).encode(text))


def _largest_prefix(text: str, limit: int, model: str) -> int:
    low, high, best = 1, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        if _tokens(text[:middle], model) <= limit:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    if not best:
        raise ValueError("one Unicode character exceeds the embedding token limit")
    return best


def _chunks(text: str, *, model: str, limit: int) -> tuple[EmbeddingChunk, ...]:
    if _tokens(text, model) <= limit:
        return (EmbeddingChunk(text, _tokens(text, model)),)
    output: list[str] = []
    current = ""
    for piece in (item for item in re.split(r"(?<=\n)", text) if item):
        if _tokens(current + piece, model) <= limit:
            current += piece
            continue
        if current:
            output.append(current)
        current = ""
        while piece and _tokens(piece, model) > limit:
            cut = _largest_prefix(piece, limit, model)
            output.append(piece[:cut])
            piece = piece[cut:]
        current = piece
    if current:
        output.append(current)
    if "".join(output) != text:
        raise AssertionError("embedding chunking lost source text")
    return tuple(EmbeddingChunk(item, _tokens(item, model)) for item in output)


def assemble_inputs(
    profile: Mapping[str, Any], *, model: str, max_input_tokens: int
) -> dict[str, EmbeddingInput]:
    if not 1 <= max_input_tokens <= 8192:
        raise ValueError("max_input_tokens must be between 1 and 8192")
    grouped = _group(profile)
    texts = {
        "purpose": _render(grouped, PURPOSE_FIELDS, headers=False),
        "target": _render(grouped, TARGET_FIELDS, headers=True),
        "support": _render(grouped, SUPPORT_FIELDS, headers=True),
    }
    missing = [scope for scope, value in texts.items() if not value]
    if missing:
        raise ValueError(f"embedding scopes are empty: {', '.join(missing)}")
    return {
        scope: EmbeddingInput(
            scope=scope,
            text=text,
            input_sha256=hashlib.sha256(text.encode()).hexdigest(),
            chunks=_chunks(text, model=model, limit=max_input_tokens),
        )
        for scope, text in texts.items()
    }


def pool(vectors: Sequence[Sequence[float]], weights: Sequence[int]) -> list[float]:
    if not vectors or len(vectors) != len(weights):
        raise ValueError("vectors and token weights must be non-empty and aligned")
    dimension = len(vectors[0])
    if not dimension or any(len(vector) != dimension for vector in vectors):
        raise ValueError("embedding vectors have inconsistent dimensions")
    if any(weight <= 0 for weight in weights):
        raise ValueError("embedding token weights must be positive")
    total = float(sum(weights))
    result = [
        sum(float(row[index]) * weight for row, weight in zip(vectors, weights)) / total
        for index in range(dimension)
    ]
    norm = math.sqrt(sum(value * value for value in result))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("pooled embedding must be finite and non-zero")
    return [value / norm for value in result]
