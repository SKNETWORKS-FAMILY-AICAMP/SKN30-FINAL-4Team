"""Conservative completeness checks for explicitly headed prose lists.

This is intentionally a small lexical/provenance audit.  It only inventories
items which a Common-IR main-notice prose block visibly marks as a bullet or
numbered list entry.  It does not infer facts, parse tables, or attempt to
turn ordinary prose into a list.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .models import CandidatePack, FactField, SourceBlock, SourceRelation
from .native_provenance import project_value_source_to_atomic_ranges


_OPEN_HEADINGS = {
    "모집대상": "eligibility",
    "지원대상": "eligibility",
    "신청대상": "eligibility",
    "신청자격": "eligibility",
    "세부신청자격": "eligibility",
    "지원자격": "eligibility",
    "참가자격": "eligibility",
    "기본요건": "eligibility",
    "지원제외": "restriction",
    "지원제외대상": "restriction",
    "제외대상": "restriction",
    "신청제외": "restriction",
    "지원제한": "restriction",
    "참여제한": "restriction",
    "중복지원": "restriction",
    "사업내용": "support",
    "지원내용": "support",
    "지원항목": "support",
    "지원항목및내용": "support",
}
_GROUP_FIELDS = {
    "eligibility": frozenset({
        FactField.APPLICANT_ELIGIBILITY, FactField.SUPPORT_TARGET,
        FactField.ELIGIBILITY_CONDITIONS, FactField.BENEFICIARY,
        FactField.APPLICABLE_ENTITY, FactField.PARTICIPATION_REQUIREMENTS,
    }),
    "restriction": frozenset({
        FactField.EXCLUSIONS, FactField.DUPLICATE_SUPPORT_CONDITIONS,
    }),
    "support": frozenset({
        FactField.SUPPORT_METHODS, FactField.SUPPORT_ACTIVITIES,
        FactField.SUPPORT_ITEMS, FactField.SUPPORT_CONTENT,
        FactField.SUPPORT_SCALE, FactField.SUPPORT_PERIOD,
        FactField.TOTAL_BUDGET, FactField.COST_SHARING, FactField.PAYMENT_TERMS,
    }),
}
_PROSE_KINDS = frozenset({None, "paragraph", "text", "list_item", "body", "heading_body"})
_UNSAFE_KINDS = frozenset({"table", "table_cell", "cell", "table-cell", "ocr", "layout", "diagram", "image"})
# A visible marker plus non-whitespace content. A star is excluded because
# Korean notices routinely use it for explanatory footnotes under a list.
# We intentionally do not accept a bare dash or a continuation line as an
# item either.
# A dash remains space-required because hyphenated prose is common.  The
# other listed markers are unambiguous enough to accept compact forms such as
# ``①창업기업`` and ``1.창업기업``.  Compact decimal/version forms are still
# excluded by requiring non-digit content after a compact numeric marker.
_ITEM_PREFIX = re.compile(
    r"^[ \t]*(?:"
    r"-[ \t]+"
    r"|[•◦○▪·](?:[ \t]+|(?=\S))"
    r"|\d{1,3}[.)](?:[ \t]+|(?=[^\s\d]))"
    r"|\(\d{1,3}\)(?:[ \t]+|(?=\S))"
    r"|[①-⑳](?:[ \t]+|(?=\S))"
    r"|[가나다라마바사아자차카타파하][.)](?:[ \t]+|(?=\S))"
    r")"
)
_ITEM = re.compile(_ITEM_PREFIX.pattern + r"\S")


@dataclass(frozen=True, slots=True)
class ExplicitListItemRegion:
    """One exact list item retained only for an in-memory repair attempt."""

    source_block_id: str
    start_char: int
    end_char: int
    field_group: str
    item_text: str
    replace_fact_ids: tuple[str, ...] = ()

    def repair_payload(self) -> dict[str, object]:
        return {
            "source_block_id": self.source_block_id,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "field_group": self.field_group,
            "item_text": self.item_text,
            "allowed_field_names": sorted(
                field.value for field in _GROUP_FIELDS[self.field_group]
            ),
            "replace_fact_ids": list(self.replace_fact_ids),
        }


class ExplicitListCompletenessError(ValueError):
    """Typed repair signal whose persistent representation contains no source text."""

    error_classification = "missing_explicit_list_item"

    def __init__(
        self,
        required_list_item_regions: list[ExplicitListItemRegion],
        do_not_restore_fact_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        if not required_list_item_regions:
            raise ValueError("explicit list completeness requires a missing item")
        self._required_list_item_regions = tuple(required_list_item_regions)
        self._do_not_restore_fact_ids = frozenset(do_not_restore_fact_ids)
        groups = sorted({item.field_group for item in self._required_list_item_regions})
        block_ids = sorted({item.source_block_id for item in self._required_list_item_regions})
        super().__init__(
            "explicit list completeness failed: "
            f"classification={self.error_classification} "
            f"missing_item_count={len(self._required_list_item_regions)} "
            f"field_groups={groups} source_block_ids={block_ids}"
        )

    @property
    def required_list_item_regions(self) -> list[dict[str, object]]:
        return [item.repair_payload() for item in self._required_list_item_regions]

    @property
    def do_not_restore_fact_ids(self) -> frozenset[str]:
        return self._do_not_restore_fact_ids


def _normalized_heading(text: str) -> str | None:
    """Normalize only a standalone heading, never a phrase inside prose."""

    if not isinstance(text, str) or "※" in text:
        return None
    value = unicodedata.normalize("NFKC", text).strip()
    value = re.sub(r"^#{1,6}[ \t]+", "", value)
    value = re.sub(r"[：:]$", "", value).strip()
    # Heading labels sometimes arrive with internal spacing; retain exactness
    # after that presentation-only normalization.
    value = re.sub(r"\s+", "", value)
    return value or None


def _normalized_block_kind(block: SourceBlock) -> str | None:
    """Keep omitted block kinds distinct from an unknown empty-string kind."""

    return block.block_kind.casefold() if block.block_kind is not None else None


def _is_atomic_main_notice_block(block: SourceBlock, pack: CandidatePack) -> bool:
    kind = _normalized_block_kind(block)
    return (
        pack.common_ir_document_id is not None
        and block.relation == SourceRelation.CANDIDATE
        and block.common_ir_block_id is not None
        and bool(block.common_ir_occurrence_ids)
        and block.common_ir_cell_id is None
        and block.source_spans == ()
        and block.native_parent_block_id is None
        and (block.section_id == "main_notice" or (block.section_id or "").startswith("main_notice_resume_"))
        and kind in _PROSE_KINDS | {"heading"}
        and kind not in _UNSAFE_KINDS
    )


def _heading_kind(block: SourceBlock) -> bool:
    # Common IR gives headings an explicit kind.  ``heading_body`` is accepted
    # only if its full text is an allowlisted label, which remains conservative.
    return _normalized_block_kind(block) in {"heading", "heading_body"}


def _is_indented_continuation(line: str) -> bool:
    """Recognize only a visibly indented continuation of the preceding item.

    A prose line without indentation closes the conservative list scope.  This
    distinction matters because a Common-IR prose block can keep wrapped
    bullet text and an unrelated paragraph in the same immutable block.
    """

    return bool(re.match(r"^[ \t]+\S", line))


def _marked_item_regions_in_text(
    text: str,
    *,
    initial_offset: int = 0,
) -> tuple[list[tuple[int, int]], bool]:
    """Return marked item spans and whether unindented prose closed the scope.

    An item span includes its visibly indented wrapped lines.  The next marker
    starts the next item; only an unindented non-marker line closes the scope.
    Coordinates stay in the source block, not in a reconstructed string.
    """

    result: list[tuple[int, int]] = []
    current_start: int | None = None
    current_end: int | None = None
    offset = initial_offset
    for line in text.splitlines(keepends=True):
        visible_text = line.rstrip("\r\n")
        visible_end = offset + len(visible_text)
        if not visible_text.strip():
            offset += len(line)
            continue
        if _ITEM.match(line):
            if current_start is not None and current_end is not None:
                result.append((current_start, current_end))
            current_start = offset
            current_end = visible_end
        elif current_start is not None and _is_indented_continuation(visible_text):
            current_end = visible_end
        else:
            if current_start is not None and current_end is not None:
                result.append((current_start, current_end))
            return result, True
        offset += len(line)
    if current_start is not None and current_end is not None:
        result.append((current_start, current_end))
    return result, False


def discover_explicit_list_item_regions(pack: CandidatePack) -> list[ExplicitListItemRegion]:
    """Inventory only visibly marked list lines under exact allowed headings."""

    active_group: str | None = None
    active_section_id: str | None = None
    last_atomic_source_order: int | None = None
    result: list[ExplicitListItemRegion] = []
    ordered = sorted(enumerate(pack.blocks), key=lambda item: (
        item[1].source_order if item[1].source_order is not None else item[0], item[0]
    ))
    for _, block in ordered:
        if not _is_atomic_main_notice_block(block, pack):
            # Never carry a semantic list scope across a section or an
            # excluded structural heading. Derived candidates with the same
            # section are ignored because their atomic parent is audited.
            if (
                active_group is not None
                and (
                    block.section_id != active_section_id
                    or _heading_kind(block)
                )
            ):
                active_group = None
                active_section_id = None
            continue
        if active_group is not None and (
            last_atomic_source_order is None
            or block.source_order is None
            or block.source_order != last_atomic_source_order + 1
        ):
            # A routed A-pack may omit an unrelated heading. A source-order
            # gap therefore closes the scope instead of assuming invisible
            # intervening blocks preserve it.
            active_group = None
            active_section_id = None
        if active_section_id is not None and block.section_id != active_section_id:
            active_group = None
            active_section_id = None
        last_atomic_source_order = block.source_order
        heading = _normalized_heading(block.text) if _heading_kind(block) else None
        if _heading_kind(block):
            # Any explicit heading ends the preceding scope. Only a complete,
            # exact allowlist match may open a new one; an unrelated heading
            # must not let earlier semantics leak into later lists.
            inline_lines: list[tuple[int, int]] = []
            inline_scope_closed = False
            if heading not in _OPEN_HEADINGS and block.block_kind == "heading_body":
                # Some Common-IR producers retain a heading and its visibly
                # marked body lines in one immutable block. Accept that shape
                # only when the *first line alone* is an exact allowlisted
                # heading; a keyword embedded in ordinary prose still cannot
                # open a semantic scope.
                offset = 0
                lines = block.text.splitlines(keepends=True)
                first_text = lines[0].rstrip("\r\n") if lines else ""
                heading = _normalized_heading(first_text)
                if heading in _OPEN_HEADINGS:
                    inline_lines, inline_scope_closed = _marked_item_regions_in_text(
                        "".join(lines[1:]), initial_offset=len(lines[0]),
                    )
            opened_group = _OPEN_HEADINGS.get(heading)
            if opened_group is not None:
                result.extend(
                    ExplicitListItemRegion(
                        source_block_id=block.block_id,
                        start_char=start,
                        end_char=end,
                        field_group=opened_group,
                        item_text=block.text[start:end],
                    )
                    for start, end in inline_lines
                )
            active_group = None if inline_scope_closed else opened_group
            active_section_id = block.section_id if active_group is not None else None
            continue
        if active_group is None or _normalized_block_kind(block) not in _PROSE_KINDS:
            continue
        item_spans, body_scope_closed = _marked_item_regions_in_text(block.text)
        result.extend(
            ExplicitListItemRegion(
                source_block_id=block.block_id,
                start_char=start,
                end_char=end,
                field_group=active_group,
                item_text=block.text[start:end],
            )
            for start, end in item_spans
        )
        if body_scope_closed:
            active_group = None
            active_section_id = None
        # splitlines() returns no line for an empty string, but SourceBlock
        # already prohibits that.  One non-newline line is handled above.
    return result


def _substantive_item_bounds(
    *, start_char: int, end_char: int, item_text: str,
) -> tuple[int, int] | None:
    """Return the non-marker, non-whitespace coordinates of one list item."""

    prefix = _ITEM_PREFIX.match(item_text)
    if prefix is None:
        return None
    substantive_end = start_char + len(item_text.rstrip())
    substantive_start = start_char + prefix.end()
    if substantive_end <= substantive_start or substantive_end > end_char:
        return None
    return substantive_start, substantive_end


def _covers_full_substantive_item(
    *, item_start: int, item_end: int, item_text: str, claim_start: int, claim_end: int,
) -> bool:
    """Allow the full line or just its content, never a noun/qualifier fragment."""

    bounds = _substantive_item_bounds(
        start_char=item_start, end_char=item_end, item_text=item_text,
    )
    if bounds is None:
        return False
    substantive_start, substantive_end = bounds
    return (
        item_start <= claim_start <= substantive_start
        and substantive_end <= claim_end <= item_end
    )


_EXPLICIT_SUPPORT_SIGNALS = ("지원", "제공", "혜택")
_EXACT_CAP_QUALIFIERS = ("최대", "한도", "상한", "이내", "까지", "매칭", "정액", "정률")
_AMOUNT_OR_RATE = re.compile(
    r"(?:[\d일이삼사오육칠팔구십백천만억]+[\d일이삼사오육칠팔구십백천만억,]*(?:\.\d+)?"
    r"\s*(?:원|만원|천만원|백만원|억원|%|퍼센트))"
)


def _is_exact_support_cap_fragment(*, field_name: FactField, text: str) -> bool:
    """Keep an already-extracted, narrow cap beside a repaired list item.

    This intentionally requires both a scale field and a visible numeric cap
    qualifier.  A noun such as ``창업기업`` can therefore never masquerade as
    a support-scale subfact merely because it was assigned a support field.
    """

    return (
        field_name == FactField.SUPPORT_SCALE
        and any(token in text for token in _EXACT_CAP_QUALIFIERS)
        and _AMOUNT_OR_RATE.search(text) is not None
    )


def _is_item_local_supplemental_fact(
    *,
    item_source_block_id: str,
    item_start: int,
    item_end: int,
    item_text: str,
    projected: list[tuple[str, int, int]],
    fully_projected: bool,
    field_name: FactField,
    field_group: str,
) -> bool:
    """Preserve a narrow item-local fact while requiring full item coverage.

    One bullet can state several facts: eligibility plus a benefit, or broad
    support content plus an exact amount/period.  Such a contained fact must
    not discharge list completeness by itself, but repair must not delete it
    merely because a new full-item fact is required.  Cross-item, overbroad,
    and whole-item wrong-field claims remain invalid overlaps.
    """

    if not fully_projected or len(projected) != 1:
        return False
    bounds = _substantive_item_bounds(
        start_char=item_start,
        end_char=item_end,
        item_text=item_text,
    )
    if bounds is None:
        return False
    substantive_start, substantive_end = bounds
    projected_block_id, start, end = projected[0]
    if projected_block_id != item_source_block_id:
        return False
    is_narrow_local_span = (
        substantive_start <= start
        and end <= substantive_end
        and (start > substantive_start or end < substantive_end)
    )
    if not is_narrow_local_span:
        return False

    # A subfact in the same semantic group may be retained, but cannot cover
    # the complete bullet (that invariant is enforced separately above).
    if field_name in _GROUP_FIELDS[field_group]:
        return True

    # Eligibility bullets can legitimately combine a target/condition with a
    # separately stated benefit.  Preserve only explicit benefit language;
    # otherwise a wrong-field fragment such as support_content="창업기업" is
    # discarded during repair.  An exact support cap is the one narrow
    # scale exception even when the short cap phrase omits "지원" itself.
    if field_group == "eligibility" and field_name in _GROUP_FIELDS["support"]:
        local_text = item_text[start - item_start:end - item_start]
        return (
            any(signal in local_text for signal in _EXPLICIT_SUPPORT_SIGNALS)
            or _is_exact_support_cap_fragment(field_name=field_name, text=local_text)
        )
    return False


def validate_explicit_list_completeness_v02(
    pack: CandidatePack,
    materialized_evidence,
) -> None:
    """Require one compatible exact fact per inventoried list item.

    A fact covers an item only when its fully projected native coordinates lie
    in that one item.  This means a parent-block anchor spanning two bullets
    cannot discharge either/both as a shortcut.
    """

    regions = discover_explicit_list_item_regions(pack)
    if not regions:
        return
    claims: dict[ExplicitListItemRegion, list[tuple[str, FactField]]] = {
        item: [] for item in regions
    }
    overlaps: dict[ExplicitListItemRegion, set[str]] = {item: set() for item in regions}
    for row in materialized_evidence:
        if row.value_source is None:
            continue
        projected, fully_projected = project_value_source_to_atomic_ranges(
            pack, row.value_source
        )
        for item in regions:
            matching_ranges = [
                (start, end)
                for block_id, start, end in projected
                if block_id == item.source_block_id
            ]
            if (
                not _is_item_local_supplemental_fact(
                    item_source_block_id=item.source_block_id,
                    item_start=item.start_char,
                    item_end=item.end_char,
                    item_text=item.item_text,
                    projected=projected,
                    fully_projected=fully_projected,
                    field_name=row.field_name,
                    field_group=item.field_group,
                )
                and any(
                    start < item.end_char and end > item.start_char
                    for start, end in matching_ranges
                )
            ):
                overlaps[item].add(row.fact_id)
            if (
                fully_projected
                and len(projected) == 1
                and matching_ranges
                and _covers_full_substantive_item(
                    item_start=item.start_char,
                    item_end=item.end_char,
                    item_text=item.item_text,
                    claim_start=matching_ranges[0][0],
                    claim_end=matching_ranges[0][1],
                )
            ):
                claims[item].append((row.fact_id, row.field_name))
    missing: list[ExplicitListItemRegion] = []
    blocked: set[str] = set()
    for item in regions:
        compatible = _GROUP_FIELDS[item.field_group]
        item_claims = claims[item]
        if not any(field in compatible for _, field in item_claims):
            replace_fact_ids = tuple(sorted(overlaps[item]))
            missing.append(ExplicitListItemRegion(
                source_block_id=item.source_block_id,
                start_char=item.start_char,
                end_char=item.end_char,
                field_group=item.field_group,
                item_text=item.item_text,
                replace_fact_ids=replace_fact_ids,
            ))
            blocked.update(replace_fact_ids)
    if missing:
        raise ExplicitListCompletenessError(missing, blocked)


def explicit_list_repair_has_invalid_overlaps_v02(
    pack: CandidatePack,
    materialized_evidence,
    required_regions: list[dict[str, object]],
) -> bool:
    """Detect bad facts retained beside a repaired exact list-item fact.

    This runs only for regions emitted by a prior typed list error.  It uses
    materialized native coordinates, so changing a fact id, anchor spelling,
    or the particular 2/3-span composite cannot preserve an overbroad or
    wrong-field claim.  A compatible fact wholly contained in the one target
    item is the only accepted overlap.
    """

    blocks_by_id = {block.block_id: block for block in pack.blocks}
    targets: list[tuple[str, int, int, str, str, frozenset[FactField]]] = []
    for region in required_regions:
        block_id = region.get("source_block_id")
        start = region.get("start_char")
        end = region.get("end_char")
        group = region.get("field_group")
        block = blocks_by_id.get(block_id) if isinstance(block_id, str) else None
        if (
            not isinstance(block_id, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or not isinstance(group, str)
            or group not in _GROUP_FIELDS
            or block is None
            or end > len(block.text)
        ):
            raise ValueError("invalid explicit-list repair region")
        item_text = block.text[start:end]
        if _substantive_item_bounds(
            start_char=start, end_char=end, item_text=item_text,
        ) is None:
            raise ValueError("invalid explicit-list repair item region")
        targets.append((block_id, start, end, item_text, group, _GROUP_FIELDS[group]))

    for row in materialized_evidence:
        if row.value_source is None:
            continue
        projected, fully_projected = project_value_source_to_atomic_ranges(
            pack, row.value_source
        )
        for block_id, item_start, item_end, item_text, field_group, compatible in targets:
            matching = [
                (start, end)
                for projected_block_id, start, end in projected
                if projected_block_id == block_id
                and start < item_end
                and end > item_start
            ]
            if not matching:
                continue
            valid_item_fact = (
                fully_projected
                and len(projected) == 1
                and len(matching) == 1
                and _covers_full_substantive_item(
                    item_start=item_start,
                    item_end=item_end,
                    item_text=item_text,
                    claim_start=matching[0][0],
                    claim_end=matching[0][1],
                )
                and row.field_name in compatible
            )
            supplemental_item_fact = (
                len(matching) == 1
                and _is_item_local_supplemental_fact(
                    item_source_block_id=block_id,
                    item_start=item_start,
                    item_end=item_end,
                    item_text=item_text,
                    projected=projected,
                    fully_projected=fully_projected,
                    field_name=row.field_name,
                    field_group=field_group,
                )
            )
            if not valid_item_fact and not supplemental_item_fact:
                return True
    return False


__all__ = [
    "ExplicitListCompletenessError",
    "ExplicitListItemRegion",
    "discover_explicit_list_item_regions",
    "explicit_list_repair_has_invalid_overlaps_v02",
    "validate_explicit_list_completeness_v02",
]
