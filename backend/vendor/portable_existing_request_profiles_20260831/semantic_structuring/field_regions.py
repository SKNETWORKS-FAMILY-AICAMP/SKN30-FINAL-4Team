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
_BOUNDARY_WORDS = re.compile(
    r"(?:사업\s*목적|사업\s*기간|사업\s*수행\s*기간|전체\s*추진\s*기간"
    r"|사업\s*예산|전체\s*예산|총\s*사업비|사업\s*필요성"
    r"|지원\s*근거|연계\s*정책|지원\s*대상|지원\s*조건|지원\s*내용"
    r"|지원\s*규모|지원\s*분야|지원\s*기간|수행\s*기관|수행\s*방식"
    r"|사업\s*추진\s*체계|사업\s*추진\s*절차|추진\s*체계|추진\s*절차"
    r"|기대\s*효과|파급\s*효과|성과\s*지표|사업명)"
)
# 라벨만 찍히고 비어 있는 구역은 문서가 정말 비운 것이다. 그것을 추출 누락으로
# 표시하면 문서의 빈칸을 우리 결함으로 되돌린다.
_MIN_REGION_CHARS = 2

# 프로파일 필드 이름 -> 그 필드의 라벨 표기들. CPL 코드나 경로를 쓰지 않는다.
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "program_period": (r"사업\s*기간", r"사업\s*수행\s*기간", r"전체\s*추진\s*기간"),
    "purpose_goal": (r"사업\s*목적",),
}

_PATTERNS: dict[str, re.Pattern[str]] = {
    field: re.compile(_LABEL_GRAMMAR.format(label="|".join(labels)))
    for field, labels in FIELD_LABELS.items()
}
_ANY_LABEL = re.compile(
    _LABEL_GRAMMAR.format(
        label="|".join(
            label for labels in FIELD_LABELS.values() for label in labels
        )
    )
)


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
    word = _BOUNDARY_WORDS.search(text, label_end)
    if word is not None and word.start() < stop:
        stop = word.start()
    return stop


def _starts_with_any_label(text: str) -> bool:
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
        if _starts_with_any_label(text):
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
            pattern = _PATTERNS.get(field)
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                end = _region_end(text, match.end())
                content = text[match.end() : end]
                host, start, stop = block, match.end(), end
                if len(content.strip()) < _MIN_REGION_CHARS:
                    sibling = _sibling_content(blocks, index)
                    if sibling is None:
                        continue
                    host, start, stop = sibling
                region = RequestFieldRegion(
                    field_name=field,
                    label_text=match.group().strip(),
                    block_id=host.block_id,
                    label_block_id=block.block_id,
                    common_ir_block_id=host.common_ir_block_id,
                    common_ir_cell_id=host.common_ir_cell_id,
                    common_ir_occurrence_ids=tuple(host.common_ir_occurrence_ids),
                    content_start=start,
                    content_end=stop,
                    content_text=(host.text or "")[start:stop],
                    raw_text=(host.text or "")[
                        match.start() if host is block else start : stop
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
    자리에 두 번 나올 수 있고 그 둘은 서로 다른 근거다. 블록 id 가 서로의
    접두사일 때 — 곧 같은 자리를 계층만 달리해 가리킬 때 — 만 좁은 쪽을 남긴다.
    """

    kept: list[RequestFieldRegion] = []
    for region in sorted(regions, key=lambda r: -len(r.block_id)):
        nested = any(
            other.field_name == region.field_name
            and other.content_text == region.content_text
            and other.block_id.startswith(region.block_id)
            for other in kept
        )
        if not nested:
            kept.append(region)
    return kept


__all__ = ["FIELD_LABELS", "RequestFieldRegion", "build_field_regions"]
