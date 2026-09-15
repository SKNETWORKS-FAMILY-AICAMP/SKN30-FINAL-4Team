"""Exact native-text candidates for explicit Korean support amount/rate caps.

This is deliberately a lexical discovery module only.  It never decides that
an arbitrary amount is an offer: the bounded source segment must establish
support ownership (or an explicit recipient basis), and callers still perform
their structural/evidence validation before materialising a Raw Fact.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re

from .models import CandidatePack, SourceBlock


EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION = "explicit-support-cap-candidate-v2"

# Korean large-unit money may omit the final ``원``.  A literal won amount
# may not.  The negative suffix guard prevents a valid-looking KRW prefix of
# a foreign-currency amount from being accepted.
KOREAN_KRW_MONEY_SOURCE = (
    r"(?:"
    r"\d[\d,]*(?:\.\d+)?\s*억(?:\s*\d[\d,]*(?:\.\d+)?\s*(?:천|백)?\s*만)?\s*원?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:(?:천|백)?만|천)\s*원?"
    r"|\d[\d,]*\s*원"
    r")"
    r"(?!\s*(?:달러|불|USD\b|KRW\b|JPY\b|EUR\b|CNY\b|[\$€¥]))"
)
_MONEY = KOREAN_KRW_MONEY_SOURCE
_RATE = r"\d[\d,]*(?:\.\d+)?\s*%"
_BOUND_CAP = re.compile(rf"(?:최대|한도|상한)\s*[:：]?\s*(?:{_MONEY}|{_RATE})")
_REJECT_PARTICIPANT_PAID = re.compile(r"(?:참가\s*비|수수료|자부담|부담금)")
_REJECT_AGGREGATE_TOTAL = re.compile(r"(?:총\s*사업비|총\s*예산|총\s*액)")
_ELIGIBILITY_METRIC = re.compile(
    r"(?:연\s*매출|매출(?:액)?|자산(?:액)?|자본(?:금)?|투자(?:금|액)|출자|재무)"
)
_ELIGIBILITY_ROLE = re.compile(
    r"(?:신청\s*(?:자격|대상)|지원\s*대상|모집\s*대상|참가\s*자격|(?:이하|이상)\s*(?:기업|업체|기관|대상|자)\b)"
)
_POSITIVE_SUPPORT_OWNERSHIP = re.compile(
    r"(?:추가\s*지원|지원(?:금|비|한도|금액|규모)|지급|제공|보조(?:금)?|융자(?:금|한도)?|보증(?:금|한도)?)"
)
_PER_RECIPIENT_BASIS = re.compile(
    r"(?:기업|업체|회사|과제|프로젝트|팀|기관|참여자|수혜자)\s*(?:당|별)"
)
_POST_CAP_SUPPORT_OWNERSHIP = re.compile(
    r"^\s*(?:(?:까지|이내|한도)\s*)?(?:(?:을|를)\s*)?"
    r"(?:추가\s*지원|지원(?!\s*(?:대상|자격|사업|분야|요건|조건))|지급|제공|보조|융자|보증)"
)
_NON_EXACT_EVIDENCE_KINDS = frozenset({"table", "layout", "diagram", "image", "ocr"})
_PDF_PROSE_ONLY_KINDS = _NON_EXACT_EVIDENCE_KINDS | frozenset({"table_cell", "cell", "table-cell"})


@dataclass(frozen=True, slots=True)
class ExplicitSupportCapCandidate:
    """One exact source-position cap candidate.

    Equal text at two locations intentionally remains two candidates.  The
    finalizer must materialise the corresponding occurrence before treating
    either as covered.
    """

    source_block_id: str
    start_char: int
    end_char: int
    anchor_text: str
    version: str = EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION


def is_exact_support_cap_evidence_block(block: SourceBlock) -> bool:
    """Return whether a block is immutable native text usable for a cap."""

    return (block.block_kind or "").casefold() not in _NON_EXACT_EVIDENCE_KINDS


def is_pdf_prose_support_cap_block(block: SourceBlock) -> bool:
    """Return whether a block is safe for PDF prose-only planning."""

    return (
        "#" not in block.block_id
        and block.common_ir_cell_id is None
        and (block.block_kind or "").casefold() not in _PDF_PROSE_ONLY_KINDS
    )


# Compatibility name: generic exact evidence includes trusted table cells.
is_native_text_source_block = is_exact_support_cap_evidence_block


def _bounded_line_segment(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    segment_start = max(
        line_start,
        text.rfind(";", line_start, start) + 1,
        text.rfind("；", line_start, start) + 1,
    )
    stops = [position for position in (text.find(";", end, line_end), text.find("；", end, line_end), line_end) if position >= 0]
    return text[segment_start:min(stops)]


def _confirmed_in_segment(text: str, start: int, end: int) -> bool:
    segment = _bounded_line_segment(text, start, end)
    segment_start = text.find(segment, max(0, start - len(segment)))
    if segment_start < 0:
        return False
    relative_start = start - segment_start
    relative_end = end - segment_start
    prefix = segment[:relative_start]
    suffix = segment[relative_end:]

    # Participant payment and aggregate totals retain precedence over a
    # trailing verb.  For example, ``자부담 최대 1억원 지원`` must not turn a
    # payer obligation into a public benefit merely because the line later
    # contains ``지원``.  An aggregate calculation basis is accepted only
    # when a positive owner already appears *before* the cap (the existing
    # ``지원금은 총사업비의 최대 80%`` case).
    if _REJECT_PARTICIPANT_PAID.search(prefix):
        return False
    has_aggregate_total = bool(_REJECT_AGGREGATE_TOTAL.search(prefix))
    has_prior_support_owner = bool(
        _POSITIVE_SUPPORT_OWNERSHIP.search(prefix)
        or _PER_RECIPIENT_BASIS.search(prefix)
    )
    if has_aggregate_total:
        if has_prior_support_owner:
            return True
        return False

    post_cap_support_owner = bool(_POST_CAP_SUPPORT_OWNERSHIP.match(suffix))

    # Decide ownership from the nearest explicit phrase before the cap.  A
    # positive word elsewhere in the same line must not make an unrelated
    # applicant threshold or participant payment look like a benefit.
    signals: list[tuple[int, str]] = []
    for pattern, kind in (
        (_POSITIVE_SUPPORT_OWNERSHIP, "support"),
        (_PER_RECIPIENT_BASIS, "support"),
        (_REJECT_PARTICIPANT_PAID, "reject"),
        (_REJECT_AGGREGATE_TOTAL, "reject"),
        (_ELIGIBILITY_METRIC, "reject"),
        (_ELIGIBILITY_ROLE, "reject"),
    ):
        signals.extend((match.end(), kind) for match in pattern.finditer(prefix))
    if signals:
        _, nearest_kind = max(signals, key=lambda item: item[0])
        if nearest_kind == "support":
            return True
        # An applicant-financial phrase before the number can be a condition
        # rather than the cap's owner: ``연매출 ... 기업에 최대 1억원 지급``.
        # Once payer/aggregate guards above have ruled out false positives,
        # only an immediate post-cap benefit verb may establish ownership.
        if post_cap_support_owner:
            return True
        return False

    # Korean notices also put the owning verb after the numeric expression
    # (``최대 1억원 지원``).  Only an immediate suffix may establish this;
    # a later support phrase cannot bless an earlier eligibility amount.
    return post_cap_support_owner


def explicit_support_cap_spans_in_text(text: str) -> list[tuple[int, int]]:
    """Discover confirmed bound-cap spans for one immutable source string."""

    if not isinstance(text, str):
        return []
    return [
        (match.start(), match.end())
        for match in _BOUND_CAP.finditer(text)
        if _confirmed_in_segment(text, match.start(), match.end())
    ]


def _extract(
    blocks: Iterable[SourceBlock],
    *,
    accepted: Callable[[SourceBlock], bool],
) -> list[ExplicitSupportCapCandidate]:
    candidates: list[ExplicitSupportCapCandidate] = []
    for block in blocks:
        if not isinstance(block, SourceBlock) or not accepted(block):
            continue
        for start, end in explicit_support_cap_spans_in_text(block.text):
            candidates.append(ExplicitSupportCapCandidate(
                source_block_id=block.block_id,
                start_char=start,
                end_char=end,
                anchor_text=block.text[start:end],
            ))
    return candidates


def extract_explicit_support_cap_candidates(
    pack_or_blocks: CandidatePack | Iterable[SourceBlock],
) -> list[ExplicitSupportCapCandidate]:
    blocks = pack_or_blocks.blocks if isinstance(pack_or_blocks, CandidatePack) else pack_or_blocks
    return _extract(blocks, accepted=is_exact_support_cap_evidence_block)


def extract_pdf_prose_explicit_support_cap_candidates(
    pack_or_blocks: CandidatePack | Iterable[SourceBlock],
) -> list[ExplicitSupportCapCandidate]:
    blocks = pack_or_blocks.blocks if isinstance(pack_or_blocks, CandidatePack) else pack_or_blocks
    return _extract(blocks, accepted=is_pdf_prose_support_cap_block)


def explicit_support_cap_spans_by_block(
    pack_or_blocks: CandidatePack | Iterable[SourceBlock],
) -> dict[str, set[tuple[int, int]]]:
    result: dict[str, set[tuple[int, int]]] = {}
    for candidate in extract_explicit_support_cap_candidates(pack_or_blocks):
        result.setdefault(candidate.source_block_id, set()).add((candidate.start_char, candidate.end_char))
    return result


__all__ = [
    "EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION",
    "KOREAN_KRW_MONEY_SOURCE",
    "ExplicitSupportCapCandidate",
    "explicit_support_cap_spans_by_block",
    "explicit_support_cap_spans_in_text",
    "extract_explicit_support_cap_candidates",
    "extract_pdf_prose_explicit_support_cap_candidates",
    "is_exact_support_cap_evidence_block",
    "is_native_text_source_block",
    "is_pdf_prose_support_cap_block",
]
