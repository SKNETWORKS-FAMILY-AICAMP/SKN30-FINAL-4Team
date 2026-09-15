"""Deterministic continuation candidates with lossless native provenance."""

from __future__ import annotations

import hashlib
import re

from .models import CandidatePack, NativeSourceSpan, SourceBlock


_TERMINAL = re.compile(r"[.!?。！？]|(?:다|함|됨|있음|없음|바람|가능|불가)[.)]?\s*$")
_BARE_NUMBER = re.compile(r"^\s*(?:\d+[.)]|[①-⑳]|[가-하][.)])\s*$")
_CONTINUATION_KINDS = {None, "paragraph", "text", "list_item", "body", "heading_body"}
_LEADING_LAYOUT_MARKER = re.compile(r"^\s*(?:[-·◦□○●▪•]\s*)")
MAX_NATIVE_CONTINUATION_CANDIDATES = 10_000
MAX_NATIVE_CONTINUATION_SPAN_REFERENCES = 30_000
MAX_NATIVE_CONTINUATION_OCCURRENCE_REFERENCES = 100_000
MAX_NATIVE_CONTINUATION_TEXT_BYTES = 32 * 1024 * 1024


def _needs_next(block: SourceBlock, following: SourceBlock) -> bool:
    if block.relation != "candidate" or following.relation != "candidate":
        return False
    text = block.text.rstrip()
    bare_number = bool(_BARE_NUMBER.fullmatch(text))
    if not text or (_TERMINAL.search(text) and not bare_number):
        return False
    # Removing a leading layout marker must leave at least one exact source
    # character.  A marker-only block contributes no lexical evidence and
    # cannot safely become an empty NativeSourceSpan.
    content_start, content_end = _content_bounds(block, first=True)
    if content_end <= content_start:
        return False
    heading_pair = block.block_kind == following.block_kind == "heading"
    if not heading_pair and (
        block.block_kind not in _CONTINUATION_KINDS
        or following.block_kind not in _CONTINUATION_KINDS
    ):
        return False
    if not block.section_id or block.section_id != following.section_id:
        return False
    # Heading joins are exceptional: a visual PDF split must explicitly end
    # at an open punctuation boundary.  Paragraph joins remain lexical only.
    if heading_pair and not text.endswith((",", ":", ";", "/", "(", "[")):
        return False
    if block.source_order is None or following.source_order is None:
        return False
    if not 0 < following.source_order - block.source_order <= 2:
        return False
    return bool(
        bare_number
        or text.endswith(("및", "또는", "위해", "따라", "대하여", "체납", "지원", "제공", "지"))
        or following.text.lstrip().startswith(("*", "※", "중인", "원하여", "하는", "및 "))
        or not _TERMINAL.search(text)
    )


def _content_bounds(block: SourceBlock, *, first: bool) -> tuple[int, int]:
    start = 0
    if first:
        marker = _LEADING_LAYOUT_MARKER.match(block.text)
        if marker:
            start = marker.end()
    return start, len(block.text.rstrip())


def _separator(left: str, right: str) -> str:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    return " "


def _span(block: SourceBlock, *, first: bool, separator_after: str) -> NativeSourceSpan:
    if block.common_ir_block_id is None or not block.common_ir_occurrence_ids:
        raise ValueError("composite candidates require native Common IR block and occurrence provenance")
    start, end = _content_bounds(block, first=first)
    return NativeSourceSpan(
        source_block_id=block.block_id,
        exact_text=block.text[start:end],
        start_char=start,
        end_char=end,
        separator_after=separator_after,
        source_order=block.source_order if block.source_order is not None else 0,
        section_id=block.section_id or "main_notice",
        common_ir_block_id=block.common_ir_block_id,
        common_ir_occurrence_ids=block.common_ir_occurrence_ids,
        common_ir_cell_id=block.common_ir_cell_id,
    )


def build_native_continuation_candidates(
    pack: CandidatePack, *, max_spans: int = 3
) -> list[SourceBlock]:
    """Return bounded composites; never rewrite constituent text."""

    if max_spans not in {2, 3}:
        raise ValueError("max_spans must be 2 or 3")
    ordered = sorted(
        (
            block
            for block in pack.blocks
            if not block.source_spans and block.native_parent_block_id is None
        ),
        key=lambda block: (
            block.source_order if block.source_order is not None else 10**12,
            block.block_id,
        ),
    )
    composites: list[SourceBlock] = []
    span_reference_count = 0
    occurrence_reference_count = 0
    derived_text_bytes = 0
    for start in range(len(ordered) - 1):
        group = [ordered[start]]
        while len(group) < max_spans and start + len(group) < len(ordered):
            following = ordered[start + len(group)]
            if not _needs_next(group[-1], following):
                break
            if following.block_kind == "table_cell" or group[-1].block_kind == "table_cell":
                break
            group.append(following)
            if len(composites) >= MAX_NATIVE_CONTINUATION_CANDIDATES:
                raise ValueError("native continuation candidate limit exceeded")
            span_reference_count += len(group)
            occurrence_reference_count += sum(
                len(block.common_ir_occurrence_ids) for block in group
            )
            if span_reference_count > MAX_NATIVE_CONTINUATION_SPAN_REFERENCES:
                raise ValueError("native continuation span reference limit exceeded")
            if (
                occurrence_reference_count
                > MAX_NATIVE_CONTINUATION_OCCURRENCE_REFERENCES
            ):
                raise ValueError(
                    "native continuation occurrence reference limit exceeded"
                )
            # Each constituent slice is retained in ``source_spans`` and once
            # more in the joined composite text.  Account for both copies and
            # inserted separators before constructing the Pydantic objects.
            group_text_bytes = 0
            for index, block in enumerate(group):
                slice_start, slice_end = _content_bounds(
                    block, first=index == 0
                )
                group_text_bytes += len(
                    block.text[slice_start:slice_end].encode("utf-8")
                )
            separator_bytes = sum(
                len(
                    _separator(
                        block.text.rstrip(), group[index + 1].text.lstrip()
                    ).encode("utf-8")
                )
                for index, block in enumerate(group[:-1])
            )
            derived_text_bytes += (2 * group_text_bytes) + separator_bytes
            if derived_text_bytes > MAX_NATIVE_CONTINUATION_TEXT_BYTES:
                raise ValueError("native continuation text byte limit exceeded")
            spans = tuple(
                _span(
                    block,
                    first=index == 0,
                    separator_after=(
                        _separator(block.text.rstrip(), group[index + 1].text.lstrip())
                        if index + 1 < len(group)
                        else ""
                    ),
                )
                for index, block in enumerate(group)
            )
            identity = "\0".join(span.source_block_id for span in spans).encode()
            occurrences = tuple(
                occurrence for span in spans for occurrence in span.common_ir_occurrence_ids
            )
            composites.append(SourceBlock(
                block_id=f"composite:{hashlib.sha256(identity).hexdigest()[:20]}",
                text="".join(span.exact_text + span.separator_after for span in spans),
                relation="candidate",
                block_kind="native_composite",
                section_id=spans[0].section_id,
                source_order=spans[0].source_order,
                source_occurrence_ids=list(occurrences),
                common_ir_block_id=spans[0].common_ir_block_id,
                common_ir_occurrence_ids=occurrences,
                source_spans=spans,
            ))
    by_id: dict[str, SourceBlock] = {}
    for block in composites:
        existing = by_id.get(block.block_id)
        if existing is not None and existing != block:
            raise ValueError("native composite candidate id collision")
        by_id[block.block_id] = block
    return [by_id[key] for key in sorted(by_id)]


def augment_pack_with_native_continuations(pack: CandidatePack) -> CandidatePack:
    """Return ``pack`` plus validated, bounded native composites."""

    composites = build_native_continuation_candidates(pack)
    if not composites:
        return pack
    combined = [*pack.blocks, *composites]
    combined.sort(key=lambda block: (
        block.source_order if block.source_order is not None else 10**12,
        1 if block.source_spans else 0,
        block.block_id,
    ))
    # Keep source-block PrivateAttrs (notably trusted table geometry) on the
    # returned pack while still forcing full graph validation.
    CandidatePack.model_validate({
        **pack.model_dump(mode="python"),
        "blocks": combined,
    })
    return pack.model_copy(update={"blocks": combined})
