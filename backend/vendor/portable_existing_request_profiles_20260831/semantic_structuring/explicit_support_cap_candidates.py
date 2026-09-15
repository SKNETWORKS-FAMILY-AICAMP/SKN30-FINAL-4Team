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
from .support_scale_policy import per_unit_scope_occurrences


EXPLICIT_SUPPORT_CAP_CANDIDATE_VERSION = "explicit-support-cap-candidate-v4"

# Korean large-unit money may omit the final ``원``.  A literal won amount
# may not.  The negative suffix guard prevents a valid-looking KRW prefix of
# a foreign-currency amount from being accepted.
_GROUPED_DECIMAL_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_GROUPED_INTEGER_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_POSITIONAL_COUNT_NUMBER = (
    rf"(?:{_GROUPED_INTEGER_NUMBER}[ \t]*만"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*천)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*백)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*천"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*백)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*백"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER}[ \t]*십"
    rf"(?:[ \t]*{_GROUPED_INTEGER_NUMBER})?"
    rf"|{_GROUPED_INTEGER_NUMBER})"
)
_COUNT_UNIT_FOLLOWED_BY_PARTICLE_OR_BOUNDARY = (
    r"(?=$|[\s,，.;:：)\]}>]|(?:을|를|에|의|이|가|은|는|와|과|도|만|부터|까지))"
)
_MAN_COEFFICIENT_SOURCE = (
    rf"(?:"
    rf"{_GROUPED_DECIMAL_NUMBER}[ \t]*천"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER}[ \t]*백)?"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER})?"
    rf"|{_GROUPED_DECIMAL_NUMBER}[ \t]*백"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER}[ \t]*십)?"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER})?"
    rf"|{_GROUPED_DECIMAL_NUMBER}[ \t]*십"
    rf"(?:[ \t]*{_GROUPED_DECIMAL_NUMBER})?"
    rf"|{_GROUPED_DECIMAL_NUMBER}"
    rf")"
)
_MAN_LIKE_TAIL_SOURCE = (
    r"[0-9,，.]+"
    r"(?:[ \t]*(?:천|백|십)[ \t]*[0-9,，.]*)*"
    r"[ \t]*만(?:[ \t]*원)?"
)
_WON_LIKE_TAIL_SOURCE = (
    r"[0-9,，.]+"
    r"(?:[ \t]*(?:천|백|십)[ \t]*[0-9,，.]*)*"
    r"[ \t]*원"
)
KOREAN_KRW_MONEY_SOURCE = (
    r"(?<![억천백십만원])(?:"
    rf"{_GROUPED_DECIMAL_NUMBER}[ \t]*억"
    rf"(?:"
    rf"[ \t]*{_MAN_COEFFICIENT_SOURCE}[ \t]*만"
    rf"(?:"
    rf"[ \t]*(?:원[ \t]*)?{_MAN_COEFFICIENT_SOURCE}[ \t]*원"
    rf"|(?:[ \t]*원)?"
    rf")"
    rf"|[ \t]*{_MAN_COEFFICIENT_SOURCE}[ \t]*원"
    rf"|[ \t]*원"
    rf")?"
    rf"|{_MAN_COEFFICIENT_SOURCE}[ \t]*만"
    rf"(?:"
    rf"[ \t]*(?:원[ \t]*)?{_MAN_COEFFICIENT_SOURCE}[ \t]*원"
    rf"|(?:[ \t]*원)?"
    rf")"
    rf"|{_GROUPED_DECIMAL_NUMBER}[ \t]*천(?:[ \t]*원)?"
    rf"|{_GROUPED_DECIMAL_NUMBER}[ \t]*원"
    r")"
    # If the optional terminal ``원`` or compound tail is abandoned during
    # regex backtracking, do not emit the valid-looking KRW prefix.
    r"(?![ \t]*원)"
    rf"(?![ \t]*{_MAN_COEFFICIENT_SOURCE}[ \t]*만(?:[ \t]*원)?)"
    rf"(?![ \t]*{_MAN_LIKE_TAIL_SOURCE})"
    rf"(?![ \t]*{_WON_LIKE_TAIL_SOURCE})"
    # ``5천명``/``1만명``/``2천개사`` are selection counts, never KRW.
    # The lookahead deliberately requires a complete count unit so an amount
    # followed by an unrelated Korean word (for example ``만원 사업``) remains
    # valid money.
    rf"(?![ \t]*(?:개사|개팀|개[ \t]*과제|명|팀|사){_COUNT_UNIT_FOLLOWED_BY_PARTICLE_OR_BOUNDARY})"
    r"(?![ \t]*(?:달러|불|USD\b|KRW\b|JPY\b|EUR\b|CNY\b|[\$€¥]))"
)
_MONEY = KOREAN_KRW_MONEY_SOURCE
_RATE = rf"{_GROUPED_DECIMAL_NUMBER}[ \t]*%"
_COUNT = rf"{_POSITIONAL_COUNT_NUMBER}[ \t]*(?:개사|개팀|개[ \t]*과제|명|팀|사)"
_PRECEDING_MALFORMED_COUNT_FRAGMENT = re.compile(
    r"(?:"
    r"[0-9][0-9,，.]*[,，][ \t]+"
    r"|[0-9][0-9,，.]*[ \t]+"
    r"|[0-9][0-9,，.]*[/_][ \t]*"
    r"|[0-9][0-9,，.]*(?:[ \t]*(?:만|천|백|십)[ \t]*[0-9,，.]*)*"
    r"[ \t]*(?:만|천|백|십)[ \t]*"
    r")$"
)
_FOLLOWING_LINE_MONEY_AMOUNT = re.compile(
    rf"^\r?\n[ \t]*(?:{KOREAN_KRW_MONEY_SOURCE})"
)
_BOUND_CAP = re.compile(rf"(?:최대|한도|상한)[ \t]*[:：]?[ \t]*(?:{_MONEY}|{_RATE})")
# A notice can bind the limit after the amount/rate rather than before it:
# ``기업당 지원금 5,000만원 이내`` or ``... 총사업비의 70% 이내``.  This
# scanner is intentionally restricted to immediate limit suffixes so an
# unqualified amount is never promoted to a support cap.
_SUFFIX_BOUND_CAP = re.compile(
    rf"(?:{_MONEY}|{_RATE})[ \t]*(?:이내|내외|한도|상한)"
)
# Selection capacity is also a support-scale fact, but only a literal
# selection/appointment scale heading is safe enough to inventory.  In
# particular, application or reception counts are not a benefit measure.
_SELECTION_CAPACITY_SUFFIX = re.compile(rf"{_COUNT}[ \t]*(?:이내|내외)")
_SELECTION_CAPACITY_HEADING = re.compile(r"(?:선정|선발)[ \t]*규모")
# These are deliberately limited to modifiers immediately adjacent to the
# numeric value.  They preserve source-visible scale semantics without
# swallowing another amount, clause, or ownership phrase.
_DIRECT_BOUND_SUFFIX = re.compile(r"[ \t]*(?:이내|내외|한도|상한)")
_CLAUSE_SEPARATOR = re.compile(
    r"[;；。]|(?<!\d)[,，.]|[,，.](?!\d)"
)

_REJECT_PARTICIPANT_PAID = re.compile(r"(?:참가\s*비|수수료|자부담|부담금)")
_REJECT_AGGREGATE_TOTAL = re.compile(r"(?:총\s*사업비|총\s*예산|총\s*액)")
_ELIGIBILITY_METRIC = re.compile(
    r"(?:연\s*매출|매출(?:액)?|영업\s*이익|자산(?:액)?|자본(?:금)?|투자(?:금|액)|출자|재무)"
)
_ELIGIBILITY_ROLE = re.compile(
    r"(?:신청\s*(?:자격|대상)|지원\s*대상|모집\s*대상|참가\s*자격|(?:이하|이상)\s*(?:기업|업체|기관|대상|자)\b)"
)
_SUFFIX_ELIGIBILITY_SIGNAL = re.compile(
    r"(?:수행\s*실적|사업\s*실적|지원\s*실적|실적|보유)"
)
_POSITIVE_SUPPORT_OWNERSHIP = re.compile(
    r"(?:추가\s*지원|지원(?:금|비|한도|금액|규모)|지급|제공|보조(?:금)?|융자(?:금|한도)?|보증(?:금|한도)?)"
)
_DIRECT_SUPPORT_BASIS_RELATION = re.compile(
    r"(?:지원(?:금|비|율|액)|보조(?:금)?|융자|보증).*?"
    r"(?:연\s*매출|매출(?:액)?|영업\s*이익)\s*(?:의|대비)\s*"
    r"(?:최대|한도|상한)?\s*$"
)
_PER_RECIPIENT_BASIS = re.compile(
    r"(?:기업|업체|회사|과제|프로젝트|팀|기관|참여자|수혜자)\s*(?:당|별)"
)
_POST_CAP_SUPPORT_OWNERSHIP = re.compile(
    r"^\s*(?:(?:까지|이내|한도)\s*)?(?:(?:을|를)\s*)?"
    r"(?:추가\s*지원|지원(?!\s*(?:대상|자격|사업|분야|요건|조건|실적|이력))|지급|제공|보조|융자|보증)"
)
_POST_CAP_SUPPORT_RECEIPT = re.compile(
    r"^\s*(?:(?:까지|이내|한도)\s*)?(?:(?:을|를|의)\s*)?"
    r"(?:규모(?:로|의)?\s*)?(?:(?:기\s*)?지원|지급)\s*(?:을\s*)?"
    r"받(?:은|았(?:던)?|던|지\s*않은)"
)
_POST_CAP_EXPLICIT_PAST_RECEIPT = re.compile(
    r"^\s*(?:(?:까지|이내|한도)\s*)?(?:(?:을|를|의)\s*)?"
    r"(?:규모(?:로|의)?\s*)?"
    r"(?:"
    r"(?:이미|최근(?:에)?|과거(?:에)?|기존(?:에)?|이전(?:에)?|종전(?:에)?|"
    r"(?:19|20)\d{2}년(?:도)?(?:에)?)\s*"
    r"(?:(?:기\s*)?지원|지급)\s*(?:을\s*)?받(?:은|았(?:던)?|던)"
    r"|(?:(?:기\s*)?지원|지급)\s*(?:을\s*)?받(?:았던|던)"
    r")"
)
_POST_CAP_HISTORICAL_OR_EXCLUSION_SIGNAL = re.compile(
    r"(?:"
    r"기\s*지원|기\s*수혜|받\s*지\s*않|받은\s*적(?:이)?\s*없|"
    r"받은\s*(?:경험|사실|바)[은는이가]?\s*(?:있|없|보유)|"
    r"(?:지원\s*|수혜\s*)?(?:이력|실적)[은는이가을를]?\s*(?:보유|있|없|않)|"
    r"(?:기업|업체|기관|대상|자)?[은는이가]?\s*(?:제외|불가)|"
    r"수혜\s*(?:기업|업체|기관|자).*?(?:제외|불가)"
    r")"
)
_POST_CAP_PRIOR_SUPPORT_HISTORY = re.compile(
    r"^\s*(?:(?:까지|이내|한도)\s*)?(?:(?:을|를)\s*)?"
    r"(?:지원|수혜|지급)\s*(?:이력|실적)[은는이가을를]?\s*"
    r"(?:보유|있|없|않|(?:기업|업체|기관|대상|자)[은는이가]?\s*(?:제외|불가))"
)
_PREFIX_EXPLICIT_PAST = re.compile(
    r"(?:^|[\s:：,(（])(?:이미|최근(?:에)?|과거(?:에)?|지난해|전년도|"
    r"기존(?:에)?|이전(?:에)?|종전(?:에)?|(?:19|20)\d{2}년(?:도)?(?:에)?)(?=$|\s)"
)
_PREFIX_HISTORICAL_CAP_CONTEXT = re.compile(
    r"(?:과거(?:에)?|지난해|전년도|기\s*지원(?:금)?|"
    r"지원\s*(?:이력|실적))"
)
_PREFIX_CURRENT_CAP_RESET = re.compile(
    r"(?:달리|반면|금년도|올해|현재|향후|금후|지원\s*예정|지원\s*가능)"
)
_PREFIX_SUPPORT_EXCLUSION = re.compile(
    r"(?:지원\s*(?:대상\s*)?제외|지원\s*불가|제외\s*대상)"
)
_HISTORICAL_ELIGIBILITY_CONDITION = re.compile(
    r"(?:이상|이하|미만|초과|증가|감소|달성|충족|경우|시)"
)
_DIRECT_SCOPE_OWNER_BRIDGE = re.compile(
    r"[ \t]*(?:(?:정부|국비|지방비|사업화|기술개발|연구개발|창업)[ \t]*)?(?:지원(?:금|비|한도|금액|규모)|보조(?:금)?|융자(?:금|한도)?|"
    r"보증(?:금|한도)?|지급액)[은는이가]?[ \t]*"
)
_NON_EXACT_EVIDENCE_KINDS = frozenset({"table", "layout", "diagram", "image", "ocr"})
_PDF_PROSE_ONLY_KINDS = _NON_EXACT_EVIDENCE_KINDS | frozenset({"table_cell", "cell", "table-cell"})


@dataclass(frozen=True, slots=True)
class ExplicitSupportCapCandidate:
    """One exact source-position support cap candidate.

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
    """Return whether one atomic block may inventory a cap exactly once."""

    return (
        not block.source_spans
        and block.native_parent_block_id is None
        and (block.block_kind or "").casefold() not in _NON_EXACT_EVIDENCE_KINDS
    )


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
    separators = list(_CLAUSE_SEPARATOR.finditer(text, line_start, line_end))
    previous = [match.end() for match in separators if match.end() <= start]
    following = [match.start() for match in separators if match.start() >= end]
    segment_start = previous[-1] if previous else line_start
    segment_end = following[0] if following else line_end
    return text[segment_start:segment_end]


def _direct_support_aggregate_relation(prefix: str) -> bool:
    """Accept only an explicit support-value to aggregate-basis relation."""

    positive = list(_POSITIVE_SUPPORT_OWNERSHIP.finditer(prefix))
    aggregate = list(_REJECT_AGGREGATE_TOTAL.finditer(prefix))
    if not positive or not aggregate:
        return False
    owner = positive[-1]
    basis = aggregate[-1]
    if owner.end() > basis.start():
        return False
    bridge = prefix[owner.end():basis.start()].strip()
    basis_suffix = prefix[basis.end():].strip()
    return (
        bridge in {"", "은", "는", "이", "가", ":", "："}
        and basis_suffix in {"의", "대비", "중", "기준"}
    )


def _prefix_describes_historical_cap(prefix: str) -> bool:
    """Reject a cap owned by an explicitly historical same-clause context."""

    historical = list(_PREFIX_HISTORICAL_CAP_CONTEXT.finditer(prefix))
    if not historical:
        return False
    latest_history_end = historical[-1].end()
    if any(
        match.start() >= latest_history_end
        for match in _PREFIX_CURRENT_CAP_RESET.finditer(prefix)
    ):
        return False

    # ``전년도 매출액 ... 증가 시 지원금 ... 지급`` describes a current
    # benefit whose *eligibility condition* happens to reference last year.
    # The history marker therefore owns the financial condition, not the
    # later support amount.  Require the condition and an explicit benefit
    # owner after it so a true ``지난해 지원금 최대 ...`` remains historical.
    history_tail = prefix[latest_history_end:]
    eligibility = list(_ELIGIBILITY_METRIC.finditer(history_tail))
    support_owners = list(_POSITIVE_SUPPORT_OWNERSHIP.finditer(history_tail))
    if eligibility and support_owners:
        latest_eligibility_end = eligibility[-1].end()
        current_owners = [
            owner for owner in support_owners
            if owner.start() >= latest_eligibility_end
        ]
        if current_owners and _HISTORICAL_ELIGIBILITY_CONDITION.search(
            history_tail[latest_eligibility_end:current_owners[-1].start()]
        ):
            return False
    return True


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
    # contains ``지원``. An aggregate calculation basis is accepted only
    # through an explicit relation such as ``지원금은 총사업비의 최대 80%``,
    # or when a distinct support owner appears after the aggregate value.
    if _REJECT_PARTICIPANT_PAID.search(prefix):
        return False
    if _PREFIX_SUPPORT_EXCLUSION.search(prefix):
        return False
    if _prefix_describes_historical_cap(prefix):
        return False
    has_aggregate_total = bool(_REJECT_AGGREGATE_TOTAL.search(prefix))
    positive_support_owners = list(_POSITIVE_SUPPORT_OWNERSHIP.finditer(prefix))
    eligibility_signals = [
        *_ELIGIBILITY_METRIC.finditer(prefix),
        *_ELIGIBILITY_ROLE.finditer(prefix),
    ]
    has_prior_positive_support_owner = bool(positive_support_owners)
    latest_positive_end = max(
        (match.end() for match in positive_support_owners), default=-1
    )
    latest_eligibility_end = max(
        (match.end() for match in eligibility_signals), default=-1
    )
    latest_aggregate_end = max(
        (match.end() for match in _REJECT_AGGREGATE_TOTAL.finditer(prefix)),
        default=-1,
    )
    # A heading such as ``지원금 신청자격:`` contains the word 지원금 but
    # changes the following clause into an eligibility rule.  Only a support
    # owner appearing *after* that eligibility signal may own the cap.
    eligibility_overrides_prior_owner = (
        latest_eligibility_end > latest_positive_end
    )
    post_cap_support_owner = bool(_POST_CAP_SUPPORT_OWNERSHIP.match(suffix))
    # A completed-form ``지원받은`` may also describe the current workflow
    # (for example, ``지원받은 후 정산``). Reject it only when the same suffix
    # carries a concrete prior-support, exclusion, or negative signal.
    if _POST_CAP_SUPPORT_RECEIPT.match(suffix):
        if (
            _POST_CAP_HISTORICAL_OR_EXCLUSION_SIGNAL.search(suffix)
            or _PREFIX_EXPLICIT_PAST.search(prefix)
        ):
            return False
    if _POST_CAP_EXPLICIT_PAST_RECEIPT.match(suffix):
        return False
    if _POST_CAP_PRIOR_SUPPORT_HISTORY.match(suffix):
        return False
    strong_suffix_eligibility_signal = bool(
        _SUFFIX_ELIGIBILITY_SIGNAL.search(suffix)
    )
    suffix_eligibility_signal = bool(
        _ELIGIBILITY_METRIC.search(suffix) or _ELIGIBILITY_ROLE.search(suffix)
    )
    if eligibility_overrides_prior_owner:
        return post_cap_support_owner or bool(
            _DIRECT_SUPPORT_BASIS_RELATION.search(prefix)
        )
    if has_aggregate_total:
        return (
            _direct_support_aggregate_relation(prefix)
            # ``총사업비 5억원 중 지원금 최대 1억원`` has two
            # independent values: the support owner following the aggregate
            # amount owns this later cap. A trailing generic verb cannot
            # change ``총사업비 최대 ... 지급`` into a support cap.
            or latest_positive_end > latest_aggregate_end
        )
    if strong_suffix_eligibility_signal and not post_cap_support_owner:
        return False
    if (
        suffix_eligibility_signal
        and not post_cap_support_owner
    ):
        return False

    # A per-unit phrase says how a value is scoped, not whether the value is
    # a public benefit. Applicant financial criteria keep their eligibility
    # meaning unless this same bounded clause independently says support or
    # puts an immediate support verb after the cap.
    if (
        eligibility_signals
        and not has_prior_positive_support_owner
    ):
        return post_cap_support_owner

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
    signals.extend(
        (occurrence.end, "support")
        for occurrence in per_unit_scope_occurrences(prefix)
    )
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


def _clause_start(text: str, position: int) -> int:
    """Return the nearest same-line lexical-clause prefix boundary."""

    line_start = text.rfind("\n", 0, position) + 1
    separators = list(_CLAUSE_SEPARATOR.finditer(text, line_start, position))
    return separators[-1].end() if separators else line_start


def _expand_bound_cap_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Preserve only immediately bound per-unit and limit qualifiers.

    The scanner's base match establishes that this is a benefit cap.  This
    helper does not infer new ownership: it merely retains a literal
    ``기업당``/``과제별`` prefix or ``이내``/``내외`` suffix when no other text
    intervenes.
    """

    clause_start = _clause_start(text, start)
    prefix = text[clause_start:start]
    direct_scopes = [
        occurrence
        for occurrence in per_unit_scope_occurrences(prefix)
        if (
            not prefix[occurrence.end:].strip()
            or _DIRECT_SCOPE_OWNER_BRIDGE.fullmatch(prefix[occurrence.end:])
        )
    ]
    if direct_scopes:
        start = clause_start + min(occurrence.start for occurrence in direct_scopes)

    suffix_match = _DIRECT_BOUND_SUFFIX.match(text, end)
    if suffix_match is not None:
        end = suffix_match.end()
    return start, end


def _confirmed_selection_capacity_in_segment(text: str, start: int, end: int) -> bool:
    """Accept only a literal selection/appointment scale near one count.

    ``신청 20개사`` and ``접수 인원 20명`` are operational volumes rather
    than a support result.  A named ``선정규모``/``선발규모`` heading is the
    deliberately narrow positive signal for this inventory.
    """

    segment = _bounded_line_segment(text, start, end)
    segment_start = text.find(segment, max(0, start - len(segment)))
    if segment_start < 0:
        return False
    relative_start = start - segment_start
    return bool(_SELECTION_CAPACITY_HEADING.search(segment[:relative_start]))


def _overlaps(
    start: int,
    end: int,
    spans: Iterable[tuple[int, int]],
) -> bool:
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def explicit_support_cap_spans_in_text(text: str) -> list[tuple[int, int]]:
    """Discover confirmed support-scale spans for one immutable source string.

    The compatibility name is retained because callers use this as their
    bounded completeness catalog.  Besides explicit support limits, it holds
    the narrow ``선정규모 N개사 내외`` selection-capacity form.
    """

    if not isinstance(text, str):
        return []
    primary = [
        _expand_bound_cap_span(text, match.start(), match.end())
        for match in _BOUND_CAP.finditer(text)
        if not _FOLLOWING_LINE_MONEY_AMOUNT.match(text[match.end():])
        and _confirmed_in_segment(text, match.start(), match.end())
    ]
    spans = list(primary)

    # Add suffix-only forms only when they do not duplicate a prefix-bound
    # cap.  ``최대 5천만원 이내`` is therefore one occurrence, not two.
    for match in _SUFFIX_BOUND_CAP.finditer(text):
        if _overlaps(match.start(), match.end(), primary):
            continue
        if _confirmed_in_segment(text, match.start(), match.end()):
            spans.append(_expand_bound_cap_span(text, match.start(), match.end()))

    # Counts require their own high-confidence semantic owner.  They cannot
    # pass the monetary ownership predicate because selection capacity is not
    # a payment or rate.
    for match in _SELECTION_CAPACITY_SUFFIX.finditer(text):
        if _PRECEDING_MALFORMED_COUNT_FRAGMENT.search(text[:match.start()]):
            continue
        if _confirmed_selection_capacity_in_segment(text, match.start(), match.end()):
            spans.append(_expand_bound_cap_span(text, match.start(), match.end()))

    # Prefix/suffix expansion can converge to the same exact source span;
    # preserve source order while inventorying it once.
    unique: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for span in sorted(spans):
        if span not in seen:
            seen.add(span)
            unique.append(span)
    return unique


def _is_scope_heading(block: SourceBlock) -> bool:
    """Return whether a pack block starts a new heading-owned scope."""

    kind = (block.block_kind or "").casefold()
    return kind in {"heading", "heading_body"}


def _is_historical_support_heading(text: str) -> bool:
    """Recognize a heading that owns the following body as past support."""

    return bool(_PREFIX_HISTORICAL_CAP_CONTEXT.search(text))


def is_historical_support_cap_context(text: str) -> bool:
    """Return whether one selected support-scale clause is explicitly past.

    Discovery uses the same predicate before it inventories a current cap.
    Finalization also calls this on a model-selected source clause, so a
    historical value cannot bypass the inventory simply by being selected as
    a generic ``support_scale`` fact.
    """

    return _prefix_describes_historical_cap(text)


def _crosses_native_composite_sources(
    block: SourceBlock,
    start: int,
    end: int,
) -> bool:
    """Return whether a literal candidate uses two immutable constituents."""

    if block.block_kind != "native_composite" or not block.source_spans:
        return False
    cursor = 0
    source_hits = 0
    for span in block.source_spans:
        span_end = cursor + len(span.exact_text)
        if start < span_end and cursor < end:
            source_hits += 1
        cursor = span_end + len(span.separator_after)
    return source_hits >= 2


def _extract(
    blocks: Iterable[SourceBlock],
    *,
    accepted: Callable[[SourceBlock], bool],
    allow_cross_native_composite: bool = False,
) -> list[ExplicitSupportCapCandidate]:
    candidates: list[ExplicitSupportCapCandidate] = []
    ordered_blocks = list(blocks)
    # CandidatePack generators normally already preserve document order, but
    # use the immutable source order where supplied so a heading scope cannot
    # be accidentally applied backwards by a caller's iterable construction.
    ordered_blocks = [
        block
        for _index, block in sorted(
            enumerate(ordered_blocks),
            key=lambda item: (
                item[1].source_order is None,
                item[1].source_order if item[1].source_order is not None else item[0],
                item[0],
            ),
        )
    ]
    historical_heading_scope = False
    for block in ordered_blocks:
        if isinstance(block, SourceBlock) and _is_scope_heading(block):
            # Any subsequent heading resets the prior heading's ownership;
            # only an explicit historical heading starts a new exclusion
            # scope.  This prevents a past-support section from suppressing
            # the next, unrelated current-support section.
            historical_heading_scope = _is_historical_support_heading(block.text)
        if not isinstance(block, SourceBlock):
            continue
        if historical_heading_scope:
            continue
        spans = explicit_support_cap_spans_in_text(block.text)
        if not accepted(block):
            if not allow_cross_native_composite:
                continue
            spans = [
                (start, end)
                for start, end in spans
                if _crosses_native_composite_sources(block, start, end)
            ]
        for start, end in spans:
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
    return _extract(
        blocks,
        accepted=is_exact_support_cap_evidence_block,
        allow_cross_native_composite=True,
    )


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
    "is_historical_support_cap_context",
    "is_native_text_source_block",
    "is_pdf_prose_support_cap_block",
]
