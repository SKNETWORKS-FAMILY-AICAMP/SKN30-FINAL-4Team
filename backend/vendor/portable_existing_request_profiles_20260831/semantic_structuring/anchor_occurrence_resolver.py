"""Deterministic resolution of repeated exact anchors in CandidatePack text.

This module is deliberately *not* a Common IR parser or an extraction step.
It only turns an exact ``(CandidatePack block, anchor_text)`` selection into
either one verified span or a small set of server-generated candidates.  The
text basis is always the canonical CandidatePack block text, which is the
same basis used by ``ValueSource`` in the v0.2 profile contract.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .models import CandidatePack, StrictModel
from .profile_v02 import ValueSource, find_all_occurrences


ANCHOR_OCCURRENCE_RESOLVER_VERSION = "candidate_pack_anchor_occurrence_resolver/v1"
DEFAULT_CONTEXT_WINDOW = 32
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"


class AnchorOccurrenceRequest(StrictModel):
    """Lineage-bound request for one exact CandidatePack anchor."""

    common_ir_document_id: str = Field(min_length=1)
    common_ir_source_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    candidate_pack_id: str = Field(min_length=1)
    candidate_pack_generator: str = Field(min_length=1)
    candidate_pack_generator_version: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)


class AnchorOccurrenceCandidate(StrictModel):
    """One server-resolved repeated anchor occurrence.

    ``start_char`` / ``end_char`` are server-verification fields.  A model
    correction prompt must receive only ``candidate_id``, ``anchor_text`` and
    the two context fields, never the offsets.
    """

    # Context is a faithful excerpt of CandidatePack text; trimming it would
    # erase the exact left/right distinction the correction model needs.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    candidate_id: str = Field(min_length=1)
    source_block_id: str = Field(min_length=1)
    anchor_text: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    context_before: str
    context_after: str
    common_ir_document_id: str = Field(min_length=1)
    common_ir_source_sha256: str = Field(min_length=1)
    common_ir_block_id: str = Field(min_length=1)
    common_ir_cell_id: str | None = None
    common_ir_occurrence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def span_is_ordered(self) -> "AnchorOccurrenceCandidate":
        if self.end_char <= self.start_char:
            raise ValueError("candidate end_char must be greater than start_char")
        return self


class AnchorOccurrenceResolution(StrictModel):
    """Unique spans resolve immediately; repeated spans require selection."""

    resolver_version: Literal[ANCHOR_OCCURRENCE_RESOLVER_VERSION] = ANCHOR_OCCURRENCE_RESOLVER_VERSION
    request: AnchorOccurrenceRequest
    status: Literal["unique", "ambiguous"]
    value_source: ValueSource | None = None
    candidates: list[AnchorOccurrenceCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolution_shape_is_safe(self) -> "AnchorOccurrenceResolution":
        if self.status == "unique":
            if self.value_source is None or self.candidates:
                raise ValueError("unique resolution requires one value_source and no candidates")
        elif self.value_source is not None or len(self.candidates) < 2:
            raise ValueError("ambiguous resolution requires two or more candidates and no value_source")
        return self


def _candidate_id(request: AnchorOccurrenceRequest, *, start_char: int, end_char: int) -> str:
    """Create a deterministic CandidatePack-local ID without global node IDs."""

    payload = {
        "resolver_version": ANCHOR_OCCURRENCE_RESOLVER_VERSION,
        "common_ir_document_id": request.common_ir_document_id,
        "common_ir_source_sha256": request.common_ir_source_sha256,
        "candidate_pack_id": request.candidate_pack_id,
        "candidate_pack_generator": request.candidate_pack_generator,
        "candidate_pack_generator_version": request.candidate_pack_generator_version,
        "source_block_id": request.source_block_id,
        "anchor_text": request.anchor_text,
        "start_char": start_char,
        "end_char": end_char,
    }
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"anchor-occurrence-v1:{digest}"


def _validate_request_against_pack(
    request: AnchorOccurrenceRequest,
    pack: CandidatePack,
    *,
    trusted_common_ir_source_sha256: str,
) -> None:
    """Validate request lineage against the loaded, trusted Common IR artifact.

    CandidatePack deliberately does not duplicate mutable artifact lineage.
    The caller must therefore supply the SHA-256 read from the Common IR
    artifact it loaded to construct the pack; a request-supplied hash alone is
    never trusted.
    """

    if re.fullmatch(SHA256_HEX_PATTERN, trusted_common_ir_source_sha256) is None:
        raise ValueError("trusted_common_ir_source_sha256 must be a lowercase 64-character SHA-256 hex digest")
    if pack.common_ir_document_id is None:
        raise ValueError("anchor occurrence resolver requires a Common IR CandidatePack")
    if request.candidate_pack_id != pack.pack_id:
        raise ValueError("request candidate_pack_id does not match supplied CandidatePack")
    if request.common_ir_document_id != pack.common_ir_document_id:
        raise ValueError("request common_ir_document_id does not match supplied CandidatePack")
    if request.candidate_pack_generator != pack.generator:
        raise ValueError("request candidate_pack_generator does not match supplied CandidatePack")
    if request.candidate_pack_generator_version != pack.generator_version:
        raise ValueError("request candidate_pack_generator_version does not match supplied CandidatePack")
    if request.common_ir_source_sha256 != trusted_common_ir_source_sha256:
        raise ValueError("request common_ir_source_sha256 does not match trusted Common IR lineage")


def resolve_anchor_occurrences(
    request: AnchorOccurrenceRequest,
    pack: CandidatePack,
    *,
    trusted_common_ir_source_sha256: str,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> AnchorOccurrenceResolution:
    """Resolve an exact anchor against canonical CandidatePack block text.

    A unique occurrence returns the existing ``ValueSource`` representation.
    Only repeated text yields candidate IDs.  The caller must validate the
    ``trusted_common_ir_source_sha256`` must be read from the loaded Common IR
    artifact's lineage, and is checked before any candidate is returned.
    CandidatePack intentionally carries no mutable copy of that artifact.
    """

    if context_window < 0:
        raise ValueError("context_window must be non-negative")
    _validate_request_against_pack(
        request,
        pack,
        trusted_common_ir_source_sha256=trusted_common_ir_source_sha256,
    )
    block_by_id = {block.block_id: block for block in pack.blocks}
    block = block_by_id.get(request.source_block_id)
    if block is None:
        raise ValueError("request source_block_id is not in supplied CandidatePack")
    if block.common_ir_block_id is None:
        raise ValueError("Common IR CandidatePack block lacks common_ir_block_id")

    starts = find_all_occurrences(block.text, request.anchor_text)
    if not starts:
        raise ValueError("anchor_text is not an exact substring of canonical CandidatePack block text")
    end_chars = [start + len(request.anchor_text) for start in starts]
    if len(starts) == 1:
        return AnchorOccurrenceResolution(
            request=request,
            status="unique",
            value_source=ValueSource(
                source_block_id=block.block_id,
                start_char=starts[0],
                end_char=end_chars[0],
            ),
        )

    candidates = [
        AnchorOccurrenceCandidate(
            candidate_id=_candidate_id(request, start_char=start, end_char=end_char),
            source_block_id=block.block_id,
            anchor_text=request.anchor_text,
            start_char=start,
            end_char=end_char,
            context_before=block.text[max(0, start - context_window):start],
            context_after=block.text[end_char:end_char + context_window],
            common_ir_document_id=request.common_ir_document_id,
            common_ir_source_sha256=request.common_ir_source_sha256,
            common_ir_block_id=block.common_ir_block_id,
            common_ir_cell_id=block.common_ir_cell_id,
            common_ir_occurrence_ids=block.common_ir_occurrence_ids,
        )
        for start, end_char in zip(starts, end_chars, strict=True)
    ]
    return AnchorOccurrenceResolution(request=request, status="ambiguous", candidates=candidates)


def materialize_selected_anchor_candidate(
    resolution: AnchorOccurrenceResolution,
    candidate_id: str,
) -> tuple[str, ValueSource]:
    """Turn one server-issued candidate ID into final raw text and span.

    Final structured JSON stores the returned text/span, never ``candidate_id``.
    The caller should use this only after its conditional correction model has
    selected exactly one candidate ID.
    """

    if resolution.status != "ambiguous":
        raise ValueError("only ambiguous resolutions have a candidate to select")
    matches = [candidate for candidate in resolution.candidates if candidate.candidate_id == candidate_id]
    if len(matches) != 1:
        raise ValueError("selected candidate_id is not part of this resolution")
    candidate = matches[0]
    return candidate.anchor_text, ValueSource(
        source_block_id=candidate.source_block_id,
        start_char=candidate.start_char,
        end_char=candidate.end_char,
    )
