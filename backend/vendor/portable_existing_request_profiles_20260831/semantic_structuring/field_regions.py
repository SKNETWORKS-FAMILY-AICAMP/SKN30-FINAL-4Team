"""라벨이 지배하는 원문 구역을 CandidatePack 좌표로 계산한다.

이 모듈은 CPL 을 모른다. 프로파일 필드 이름과 원문 좌표만 다루고, 그 구역을
무엇에 쓸지는 부르는 쪽이 정한다. 후보 생성기와 워커의 누락 감지기가 같은
구역 정의를 보게 하는 것이 목적이다. 두 곳에 라벨 문법을 따로 두면 한쪽이
찾는 구역을 다른 쪽이 못 찾는 상태가 조용히 생긴다.

좌표는 CandidatePack 블록 기준이다. 값 span 후보도 같은 블록 좌표를 쓰므로
"이 후보가 그 구역 안인가" 를 변환 없이 물을 수 있다.

라벨 문법은 닫혀 있다. 서식이 정해 둔 표기라 표현이 열려 있지 않다. 의미
키워드를 늘리는 것이 아니라, 새 필드를 다루려면 그 필드의 라벨 표기를 실제
문서에서 확인하고 여기에 더한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .models import CandidatePack, SourceBlock


# 실문서 30 건의 줄머리 첫 글자를 세어 고른 것이다. ``ㅇ`` 는 한글 이응을
# 글머리로 쓰는 서식(80 회), ``◦`` 는 흰 불릿(120 회), ``‣`` 는 삼각 불릿
# (140 회)이다. ``-`` 와 ``*`` 는 본문에서도 흔해 글머리로 인정하지 않는다.
_LABEL_BULLETS = "○◦□■●▪‣ㅇ·"
# 구역을 끊는 경계. 가운뎃점(``·``)은 ``제조·정보통신`` 처럼 본문 안에 있어서
# 경계가 되면 구역이 단어 중간에서 잘린다. 시작 글머리로만 인정한다.
_REGION_BOUNDARY = "○◦□■●▪‣ㅇ"

_LABEL_GRAMMAR = (
    rf"(?:^|\n|[{_LABEL_BULLETS}])\s*\(?\s*(?:{{label}})\s*\)?\s*[:：)]?"
)
_NEXT_LABEL = re.compile(rf"[\n{_REGION_BOUNDARY}]")
# 서식이 쓰는 다른 항목의 라벨. 여기서 구역을 만들지는 않지만, 만나면 앞 구역의
# 지배가 끝난다. 글머리 기호 없이 이어 쓴 서식(``□ 사업기간 및 전체예산``)에서
# 제목의 뒷부분을 값으로 오인하지 않으려면 낱말만으로도 경계가 돼야 한다.
_BOUNDARY_WORD = (
    r"사업\s*목적|사업\s*기간|사업\s*수행\s*기간|전체\s*추진\s*기간"
    r"|사업\s*예산|전체\s*예산|총\s*사업비|사업\s*필요성"
    r"|지원\s*근거|연계\s*정책|지원\s*대상|지원\s*조건|지원\s*내용"
    r"|지원\s*규모|지원\s*분야|지원\s*기간|수행\s*기관|수행\s*방식"
    r"|사업\s*추진\s*체계|사업\s*추진\s*절차|추진\s*체계|추진\s*절차"
    r"|기대\s*효과|파급\s*효과|성과\s*지표|사업명"
)
# 낱말이 본문에 나왔다는 이유로 구역을 끊으면 안 된다. ``중소기업의 지원대상을
# 넓혀`` 같은 서술에서 사업목적 구역이 세 글자로 잘린다. 낱말이 **라벨 자리**에
# 있을 때만 경계다 — 줄머리·글머리 기호·여는 괄호 뒤이거나, 제목을 잇는
# 접속사(``및``·``과``·``와``·``,``·``·``) 뒤일 때.
# (1) 복합 제목: 라벨 **바로 뒤** 에 접속사로 이어 붙인 다른 항목.
#     ``□ 사업기간 및 전체예산`` · ``지원내용·지원규모``.
#     텍스트 아무 데서나 찾으면 안 된다. ``성과 지원대상`` 의 ``과``,
#     ``산업·지원대상`` 의 ``·`` 가 접속사로 읽혀 본문 한가운데서 구역이
#     잘린다. 그래서 ``label_end`` 에 붙여서만(anchored) 본다.
# 복합 제목의 뒷항목은 앞항목과 겹치는 부분을 줄여 쓰기도 한다
# (``사업추진 체계 및 절차`` = 사업추진체계 + 사업추진절차). 이 축약형은
# 라벨 바로 뒤 접속사 다음에서만 인정한다 — 본문에서 ``절차``·``체계`` 는
# 흔한 낱말이라 아무 데서나 경계로 쓰면 구역이 잘린다.
_COMPOUND_TAIL = rf"{_BOUNDARY_WORD}|절차|체계|예산|규모|대상|내용|기간"
_COMPOUND_HEADING = re.compile(
    # ``match(text, label_end)`` 가 이미 그 자리에 고정한다. ``^`` 를 쓰면
    # 문자열 머리에서만 맞아 라벨 뒤에서는 영영 매치되지 않는다.
    rf"\s*(?:및|과|와|·|,|/)\s*\(?\s*"
    rf"(?P<word>{_COMPOUND_TAIL})\s*\)?\s*[:：)]?"
)
# (2) 줄머리·글머리 뒤의 항목 라벨. 위치가 이미 경계라 검색해도 안전하다.
_BOUNDARY_LABEL_AT_HEAD = re.compile(
    rf"(?:^|[\n{_REGION_BOUNDARY}(（])\s*\(?\s*"
    rf"(?P<word>{_BOUNDARY_WORD})\s*\)?\s*[:：)]?"
)
# (3) 구분자를 달고 있는 항목 라벨: 글머리 없이 이어 붙여도 라벨이다
#     (``사업기간 2024~2028년 사업예산 : 35,300백만원``).
_BOUNDARY_LABEL_WITH_SEPARATOR = re.compile(
    rf"\(?\s*(?P<word>{_BOUNDARY_WORD})\s*\)?\s*[:：)]"
)
# 라벨만 찍히고 비어 있는 구역은 문서가 정말 비운 것이다. 그것을 추출 누락으로
# 표시하면 문서의 빈칸을 우리 결함으로 되돌린다.
_MIN_REGION_CHARS = 2

# 프로파일 필드 이름 -> 그 필드의 라벨 표기들. CPL 코드나 경로를 쓰지 않는다.
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "program_period": (r"사업\s*기간", r"사업\s*수행\s*기간", r"전체\s*추진\s*기간"),
    "purpose_goal": (r"사업\s*목적",),
    # 실제 문서에서 확인한 표기만 넣는다. 새 HWP 는 제목 블록 뒤에 체계도와
    # 절차표가 따로 온다(`□ 사업추진 체계 및 절차` / `ㅇ 사업추진체계` /
    # `ㅇ 사업추진절차`).
    #
    # 체계만 구역으로 만든다. `delivery_relations` 는 기관과 그 역할·행위의
    # 관계인데 절차표는 단계와 주요내용의 행 관계이고 기관이 없다. 절차는
    # `_BOUNDARY_WORD` 에 있어 체계 구역을 끝내는 경계로는 계속 동작한다.
    # 절차를 `delivery_methods` 같은 필드에 잇는 것은 별도 계약이다.
    "delivery_relations": (r"사업\s*추진\s*체계", r"추진\s*체계"),
    # 새 HWP 는 기대효과를 두 곳에 싣는다. 요약표 셀의 ``◦(파급효과)`` 한 줄과
    # 본문 ``□ 기대효과`` 아래 독립 문단들이다. 둘 다 이 라벨로 잡는다.
    # ``성과지표`` 는 이미 ``_BOUNDARY_WORD`` 에 있어 구역을 끝낸다 — 수단·지표가
    # 기대효과 구역으로 딸려 들어가지 않는다.
    "expected_effect": (r"기대\s*효과", r"파급\s*효과"),
}

_PATTERNS: dict[str, re.Pattern[str]] = {
    field: re.compile(_LABEL_GRAMMAR.format(label="|".join(labels)))
    for field, labels in FIELD_LABELS.items()
}
_ANY_LABEL = re.compile(
    _LABEL_GRAMMAR.format(
        label="|".join(
            [label for labels in FIELD_LABELS.values() for label in labels]
        )
        + "|"
        + _BOUNDARY_WORD
    )
)


@dataclass(frozen=True, slots=True)
class TextRegionSpan:
    """텍스트 한 벌 안에서 라벨이 지배하는 구간.

    좌표만 말한다. 어느 블록인지, 어느 occurrence 인지, 그 구간으로 무엇을
    할지는 부르는 쪽이 정한다. 그래서 CandidatePack 블록 텍스트에도 Common IR
    occurrence 텍스트에도 같은 규칙을 적용할 수 있고, 각 소비자는 자기 좌표
    원천과 식별자 생성법을 그대로 유지한다.
    """

    field_name: str
    label_text: str
    label_start: int
    content_start: int
    content_end: int
    # 내용이 비어 있고, 그 이유가 **텍스트가 끝나서** 일 때만 참이다. 다음
    # 라벨이 구역을 끊어 비었다면 거짓 — 그 자리는 문서가 비운 것이고 뒤따르는
    # 내용은 남의 값이다. 다음 블록으로 지배를 넘길지는 이 값만 보고 정한다.
    continues_to_sibling: bool = False


def find_text_regions(
    text: str, *, field_name: str, include_empty: bool = False
) -> list[TextRegionSpan]:
    """한 텍스트 안에서 그 필드의 라벨이 지배하는 구간을 찾는다.

    기본값은 내용이 있는 구간만 돌려준다. 라벨만 찍히고 비어 있는 구간은 문서가
    비운 것이지 구조화가 놓친 것이 아니라서, 그것을 추출 누락으로 되돌리지 않기
    위해서다.

    ``include_empty=True`` 는 빈 구간도 돌려준다. 라벨이 아예 없는 것과 라벨은
    있는데 비어 있는 것은 다른 사실이고, 다음 블록으로 지배가 넘어가는지는 그
    구분에서만 나온다. 넘어갈 수 있는지는 ``continues_to_sibling`` 이 말한다.

    이 함수는 주어진 텍스트 안만 본다. 다음 블록을 찾아가는 일은 부르는 쪽이다.
    """

    pattern = _PATTERNS.get(field_name)
    if pattern is None:
        return []
    spans: list[TextRegionSpan] = []
    for match in pattern.finditer(text):
        end = _region_end(text, match.end())
        empty = len(text[match.end() : end].strip()) < _MIN_REGION_CHARS
        if empty and not include_empty:
            continue
        spans.append(
            TextRegionSpan(
                field_name=field_name,
                label_text=match.group().strip(),
                label_start=match.start(),
                content_start=match.end(),
                content_end=end,
                # 뒤에 남은 것이 공백뿐이면 텍스트가 끝나서 빈 것이다.
                continues_to_sibling=empty and not text[end:].strip(),
            )
        )
    return spans


@dataclass(frozen=True, slots=True)
class RequestFieldRegion:
    """한 필드의 라벨이 지배하는 원문 구역.

    ``block_id`` 와 좌표는 **내용이 있는 블록** 기준이다. 라벨이 앞 블록에만
    있고 내용이 뒤따라오는 서식이면 ``label_block_id`` 가 라벨 쪽을 가리킨다.
    후보가 구역 안인지 보려면 언제나 ``block_id`` 와 content 좌표를 쓴다.
    """

    field_name: str
    label_text: str
    block_id: str
    label_block_id: str
    common_ir_block_id: str | None
    common_ir_cell_id: str | None
    common_ir_occurrence_ids: tuple[str, ...]
    content_start: int
    content_end: int
    content_text: str
    raw_text: str

    def contains(self, start: int, end: int) -> bool:
        """블록 좌표 [start, end) 가 이 구역 안에 들어오는지."""

        return self.content_start <= start and end <= self.content_end


def _region_end(text: str, label_end: int) -> int:
    """구역의 끝. 글머리·줄 경계나 다른 항목 라벨에서 끊는다.

    구역을 블록 전체로 잡으면 400 자 한 문단에 여러 항목이 이어진 서식에서
    한 구역이 뒤따르는 항목까지 삼킨다. 글머리 없이 낱말로만 이어 쓴 제목
    (``□ 사업기간 및 전체예산``)도 뒷부분이 다른 항목이므로 여기서 끊는다.
    """

    stop = len(text)
    bullet = _NEXT_LABEL.search(text, label_end)
    if bullet is not None:
        stop = bullet.start()
    compound = _COMPOUND_HEADING.match(text, label_end)
    if compound is not None:
        stop = min(stop, compound.start("word"))
    for boundary in (_BOUNDARY_LABEL_AT_HEAD, _BOUNDARY_LABEL_WITH_SEPARATOR):
        word = boundary.search(text, label_end)
        if word is not None and word.start("word") < stop:
            stop = word.start("word")
    return stop


def starts_with_form_label(text: str) -> bool:
    """텍스트 머리에 서식 항목 라벨이 오는가.

    구역을 만드는 필드뿐 아니라 경계로만 쓰는 항목 라벨(``지원대상``,
    ``사업예산`` …)도 포함한다. 앞 라벨의 지배는 여기서 끝난다 — 남의 값을
    가져오지 않는다. 부르는 쪽이 구역을 만드는 필드 목록만 알아서는 이 판단을
    할 수 없으므로 공개한다.
    """

    match = _ANY_LABEL.search(text)
    return match is not None and not text[: match.start()].strip()


def _sibling_content(
    blocks: list[SourceBlock], index: int
) -> tuple[SourceBlock, int, int] | None:
    """라벨 블록 다음에서 그 라벨이 지배하는 내용 블록을 찾는다.

    ``□ 사업추진 체계 및 절차`` 처럼 제목만 있는 블록 뒤에 내용이 따라오는
    서식이 있다. 다음 라벨을 만나면 지배가 끝난다 — 남의 값을 가져오지 않는다.
    """

    for following in blocks[index + 1 :]:
        text = following.text or ""
        if not text.strip():
            continue
        if starts_with_form_label(text):
            return None
        return following, 0, len(text)
    return None


def build_field_regions(
    pack: CandidatePack, *, field_name: str | None = None
) -> list[RequestFieldRegion]:
    """CandidatePack 안에서 라벨이 지배하는 구역을 모은다.

    값을 만들지 않는다. 어느 원문이 어느 필드의 자리인지만 말한다.
    """

    fields = (
        [field_name] if field_name is not None else list(FIELD_LABELS)
    )
    blocks = sorted(
        pack.blocks, key=lambda b: (b.source_order if b.source_order is not None else 0)
    )
    found: list[RequestFieldRegion] = []
    for index, block in enumerate(blocks):
        text = block.text or ""
        for field in fields:
            if field not in _PATTERNS:
                continue
            spans = find_text_regions(text, field_name=field, include_empty=True)
            filled = [s for s in spans if not s.continues_to_sibling]
            filled = [
                s
                for s in filled
                if len(text[s.content_start : s.content_end].strip())
                >= _MIN_REGION_CHARS
            ]
            if filled:
                spans, host = filled, block
            else:
                # 라벨은 있는데 텍스트가 끝나서 비었다면 지배가 다음 블록으로
                # 넘어간다. 다음 라벨 때문에 비었다면 넘기지 않는다.
                carry = [s for s in spans if s.continues_to_sibling]
                if not carry:
                    continue
                sibling = _sibling_content(blocks, index)
                if sibling is None:
                    continue
                host, start, stop = sibling
                spans = [
                    TextRegionSpan(
                        field_name=field,
                        label_text=carry[0].label_text,
                        label_start=carry[0].label_start,
                        content_start=start,
                        content_end=stop,
                    )
                ]
            for span in spans:
                start, stop = span.content_start, span.content_end
                region = RequestFieldRegion(
                    field_name=field,
                    label_text=span.label_text,
                    block_id=host.block_id,
                    label_block_id=block.block_id,
                    common_ir_block_id=host.common_ir_block_id,
                    common_ir_cell_id=host.common_ir_cell_id,
                    common_ir_occurrence_ids=tuple(host.common_ir_occurrence_ids),
                    content_start=start,
                    content_end=stop,
                    content_text=(host.text or "")[start:stop],
                    raw_text=(host.text or "")[
                        span.label_start if host is block else start : stop
                    ],
                )
                found.append(region)
    return _drop_nested_duplicates(found)


def _drop_nested_duplicates(
    regions: list[RequestFieldRegion],
) -> list[RequestFieldRegion]:
    """표 계층이 같은 자리를 다시 실은 것만 접는다.

    표는 한 본문을 부모·셀·문단으로 세 번 싣는다(``t4`` / ``t4#r3c1`` /
    ``t4#r3c1p0``). 같은 자리를 계층 수만큼 내보내면 같은 원문을 여러 벌로
    세게 된다.

    문구가 같다는 이유만으로 접지 않는다. 한 문서에 같은 문구가 서로 다른
    자리에 두 번 나올 수 있고 그 둘은 서로 다른 근거다. 한 블록 안의 두 자리도
    마찬가지다. 블록 id 가 **서로 다르면서** 한쪽이 다른 쪽의 접두사일 때 —
    곧 같은 자리를 계층만 달리해 가리킬 때 — 만 좁은 쪽을 남긴다.
    """

    kept: list[RequestFieldRegion] = []
    for region in sorted(regions, key=lambda r: -len(r.block_id)):
        nested = any(
            other.field_name == region.field_name
            and other.content_text == region.content_text
            and other.block_id != region.block_id
            and other.block_id.startswith(region.block_id)
            for other in kept
        )
        if not nested:
            kept.append(region)
    return kept


__all__ = [
    "FIELD_LABELS",
    "RequestFieldRegion",
    "TextRegionSpan",
    "build_field_regions",
    "find_text_regions",
    "starts_with_form_label",
]
