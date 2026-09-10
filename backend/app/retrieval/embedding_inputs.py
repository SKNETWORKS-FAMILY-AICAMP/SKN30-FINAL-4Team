"""Build reproducible embedding inputs without generative summarisation.

Only approved facts are copied from a structured profile.  The source JSON is
the lossless record; this module normalises whitespace solely for retrieval.
Inputs longer than the OpenAI embedding limit are split without truncation and
their vectors can be token-weighted into the one-vector-per-scope DB contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence


EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MAX_INPUT_TOKENS = 8192
DEFAULT_BATCH_SIZE = 100

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

_OUTER_LABELS = {
    "purpose": "사업목적",
    "target": "지원대상",
    "support": "지원내용",
}
_INTEGER_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class EmbeddingInputChunk:
    scope: str
    index: int
    count: int
    text: str
    token_count: int


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    scope: str
    text: str
    input_sha256: str
    token_count: int
    chunks: tuple[EmbeddingInputChunk, ...]


@dataclass(frozen=True, slots=True)
class _Fact:
    field_name: str
    value: str
    role: str
    order: tuple[Any, ...]


def _clean(value: object) -> str:
    return _WHITESPACE_RE.sub(
        " ", unicodedata.normalize("NFC", str(value or ""))
    ).strip()


def _source_order(fact: Mapping[str, Any], encounter: int) -> tuple[Any, ...]:
    source = fact.get("value_source")
    source = source if isinstance(source, Mapping) else {}
    block_id = _clean(source.get("source_block_id"))
    numbers = tuple(int(item) for item in _INTEGER_RE.findall(block_id))
    start = source.get("start_char")
    if isinstance(start, bool) or not isinstance(start, int):
        start = 0
    # Numeric block coordinates put b9 before b10; encounter order is the safe
    # fallback for profiles without source coordinates.
    return (0 if numbers else 1, numbers, block_id, start, encounter)


def _iter_profile_facts(profile: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    comparison = profile.get("comparison_profile")
    if isinstance(comparison, Mapping):
        for rows in comparison.values():
            if isinstance(rows, list):
                yield from (row for row in rows if isinstance(row, Mapping))

    components = profile.get("support_components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            rows = component.get("facts")
            if isinstance(rows, list):
                yield from (row for row in rows if isinstance(row, Mapping))


def _facts_by_field(profile: Mapping[str, Any]) -> dict[str, list[_Fact]]:
    grouped: dict[str, list[_Fact]] = {}
    seen: set[tuple[str, str, str]] = set()
    for encounter, fact in enumerate(_iter_profile_facts(profile)):
        if _clean(fact.get("status")).lower() not in ALLOWED_STATUSES:
            continue
        field_name = _clean(fact.get("field_name"))
        value = _clean(fact.get("value_raw"))
        role = _clean(fact.get("semantic_role") or fact.get("subject_role"))
        if not field_name or not value:
            continue
        key = (field_name, role, value)
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(field_name, []).append(
            _Fact(
                field_name=field_name,
                value=value,
                role=role,
                order=_source_order(fact, encounter),
            )
        )
    for facts in grouped.values():
        facts.sort(key=lambda item: item.order)
    return grouped


def _render_value(fact: _Fact) -> str:
    return f"{fact.role}: {fact.value}" if fact.role else fact.value


def _render_axis(
    grouped: Mapping[str, Sequence[_Fact]], fields: Sequence[str], *, headers: bool
) -> str:
    sections: list[str] = []
    for field_name in fields:
        values = [_render_value(fact) for fact in grouped.get(field_name, ())]
        if not values:
            continue
        body = "\n".join(values)
        sections.append(f"[{field_name}]\n{body}" if headers else body)
    return "\n\n".join(sections)


@lru_cache(maxsize=4)
def _encoding(model: str):
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise RuntimeError("install tiktoken to assemble embedding inputs") from exc
    return tiktoken.encoding_for_model(model)


def _token_count(text: str, model: str) -> int:
    return len(_encoding(model).encode(text))


def _largest_prefix(text: str, limit: int, model: str) -> int:
    """Return a non-empty Unicode-safe prefix that fits the token limit."""

    low, high = 1, len(text)
    best = 0
    while low <= high:
        middle = (low + high) // 2
        if _token_count(text[:middle], model) <= limit:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == 0:
        raise ValueError("one Unicode character exceeds the embedding token limit")
    return best


def _split_without_truncation(text: str, *, limit: int, model: str) -> list[str]:
    """Greedily split at line/Fact boundaries, then Unicode boundaries."""

    if not text:
        return []
    if _token_count(text, model) <= limit:
        return [text]

    # Keep separators in the stream so joining the chunks exactly reconstructs
    # the retrieval input and no selected value disappears.
    pieces = [piece for piece in re.split(r"(?<=\n)", text) if piece]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + piece
        if candidate and _token_count(candidate, model) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        remainder = piece
        while remainder and _token_count(remainder, model) > limit:
            cut = _largest_prefix(remainder, limit, model)
            chunks.append(remainder[:cut])
            remainder = remainder[cut:]
        current = remainder
    if current:
        chunks.append(current)
    if "".join(chunks) != text:
        raise AssertionError("embedding chunking lost source text")
    return chunks


def _make_input(
    scope: str, text: str, *, model: str, max_input_tokens: int
) -> EmbeddingInput:
    if not text:
        raise ValueError(f"embedding scope is empty: {scope}")
    chunk_texts = _split_without_truncation(
        text, limit=max_input_tokens, model=model
    )
    counts = [_token_count(chunk, model) for chunk in chunk_texts]
    count = len(chunk_texts)
    chunks = tuple(
        EmbeddingInputChunk(scope, index, count, chunk, token_count)
        for index, (chunk, token_count) in enumerate(zip(chunk_texts, counts))
    )
    return EmbeddingInput(
        scope=scope,
        text=text,
        input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        token_count=sum(counts),
        chunks=chunks,
    )


def assemble_embedding_inputs(
    profile: Mapping[str, Any],
    *,
    model: str = EMBEDDING_MODEL,
    max_input_tokens: int = MAX_INPUT_TOKENS,
) -> dict[str, EmbeddingInput]:
    """Return A (`combined`) and B (`purpose/target/support`) inputs.

    `content` in the external retrieval contract is persisted as the existing
    DB scope name `support`.
    """

    if max_input_tokens < 1 or max_input_tokens > MAX_INPUT_TOKENS:
        raise ValueError(f"max_input_tokens must be between 1 and {MAX_INPUT_TOKENS}")
    grouped = _facts_by_field(profile)
    texts = {
        "purpose": _render_axis(grouped, PURPOSE_FIELDS, headers=False),
        "target": _render_axis(grouped, TARGET_FIELDS, headers=True),
        "support": _render_axis(grouped, SUPPORT_FIELDS, headers=True),
    }
    missing = [scope for scope, text in texts.items() if not text]
    if missing:
        raise ValueError(f"embedding scopes are empty: {', '.join(missing)}")
    combined = "\n\n".join(
        f"[{_OUTER_LABELS[scope]}]\n{text}" for scope, text in texts.items()
    )
    texts["combined"] = combined
    return {
        scope: _make_input(
            scope, text, model=model, max_input_tokens=max_input_tokens
        )
        for scope, text in texts.items()
    }


def mean_pool_embeddings(
    vectors: Sequence[Sequence[float]], token_weights: Sequence[int]
) -> list[float]:
    """Token-weight and L2-normalise chunk vectors for cosine retrieval."""

    if not vectors or len(vectors) != len(token_weights):
        raise ValueError("vectors and token_weights must be non-empty and aligned")
    dimension = len(vectors[0])
    if dimension == 0 or any(len(vector) != dimension for vector in vectors):
        raise ValueError("embedding vectors must have one non-zero dimension")
    if any(weight <= 0 for weight in token_weights):
        raise ValueError("token weights must be positive")
    total_weight = float(sum(token_weights))
    pooled = [
        sum(float(vector[index]) * weight for vector, weight in zip(vectors, token_weights))
        / total_weight
        for index in range(dimension)
    ]
    norm = math.sqrt(sum(value * value for value in pooled))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("pooled embedding is not a finite non-zero vector")
    return [value / norm for value in pooled]
