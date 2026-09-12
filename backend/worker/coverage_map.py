"""한 라벨 구역에서 어디가 선택됐고 어디가 안 됐는지 좌표로만 센다.

여기서는 아무것도 판정하지 않는다. 비율도, occurrence 개수도, 문자열 유사도도
실패의 근거로 쓰지 않는다. 덮인 구간과 안 덮인 구간을 내놓을 뿐이고, 그것을
누락으로 볼지 중복으로 볼지는 받는 쪽이 정한다.

그렇게 나눈 이유는 관측된 두 형태가 서로 다른 지표를 갖기 때문이다. mockup_08
사업목적은 occurrence 하나 안에서 73자 중 21자만 선택됐고 (occurrence 기준으로는
1/1 이라 멀쩡해 보인다), 새 HWP 기대효과는 본문 occurrence 세 개가 통째로 빠졌다
(각 occurrence 안에서는 100% 다). 한쪽 지표로 다른 쪽을 못 잡는다.

표는 한 본문을 부모·셀·문단 occurrence 로 다시 싣는다
(``occ:rhwp:t4`` / ``occ:rhwp:t4:c5`` / ``occ:rhwp:t4:c5:p0``). 그래서 구역과
선택이 같은 자리를 서로 다른 계층으로 가리킬 수 있다. 계층 관계는 id 경계로
결정적으로 판정하지만, 좌표 옮기기는 매핑이 **전체 구역 집합에서 유일할 때만**
한다. 한 구역 안에 두 번 나와도, 서로 다른 두 구역에 한 번씩 나와도 옮기지
않는다 — 여기서 고르면 그건 계산이 아니라 추측이다.
"""

from __future__ import annotations

from dataclasses import dataclass


Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class RegionSpan:
    """구역 하나. Common IR occurrence 좌표다."""

    occurrence_id: str
    text: str
    content_start: int
    content_end: int


@dataclass(frozen=True, slots=True)
class Selection:
    """구조화가 고른 값 하나. occurrence 와 그 안의 좌표, 그리고 고른 본문.

    본문은 계층이 다른 선택을 구역 좌표로 옮길 때만 쓴다. 프로파일이 이미
    ``value_raw`` 로 싣고 있어 따로 만들어 낼 필요가 없다.
    """

    occurrence_id: str
    start: int
    end: int
    text: str = ""


@dataclass(frozen=True, slots=True)
class RegionCoverage:
    """구역 하나의 계산 결과."""

    occurrence_id: str
    content_start: int
    content_end: int
    covered: tuple[Span, ...]
    uncovered: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class CoverageMap:
    regions: tuple[RegionCoverage, ...]
    # 어느 구역에도 붙지 않은 선택. 구역 밖에서 값을 가져왔다는 뜻이고,
    # 그 자체로는 결함이 아니다 (요약표처럼 같은 내용이 다른 자리에 또 있다).
    unattributed: tuple[Selection, ...]
    # 계층은 맞는데 좌표를 옮길 수 없던 선택. 자식 본문이 부모 안에 여러 번
    # 나오는 경우다.
    unresolved: tuple[Selection, ...]


def build_coverage_map(
    regions: list[RegionSpan], selections: list[Selection]
) -> CoverageMap:
    """구역별 덮인/안 덮인 구간을 센다. 상태를 바꾸지 않는다."""

    by_region: dict[str, list[Span]] = {r.occurrence_id: [] for r in regions}
    unattributed: list[Selection] = []
    unresolved: list[Selection] = []
    index = {r.occurrence_id: r for r in regions}

    for selection in selections:
        region = index.get(selection.occurrence_id)
        if region is not None:
            by_region[region.occurrence_id].append((selection.start, selection.end))
            continue
        related = [
            region for region in regions if _is_hierarchy(region, selection)
        ]
        if not related:
            unattributed.append(selection)
            continue
        moved = [
            (region, span)
            for region, span in ((r, _translate(r, selection)) for r in related)
            if span is not None
        ]
        # 여러 구역이 같은 문자열을 담고 있으면 어느 자리인지 문서가 말해 주지
        # 않는다. 구역마다 한 번씩 나온다는 이유로 양쪽에 넣으면 같은 근거를
        # 두 번 세게 된다.
        if len(moved) != 1:
            unresolved.append(selection)
            continue
        region, span = moved[0]
        by_region[region.occurrence_id].append(span)

    return CoverageMap(
        regions=tuple(
            _cover(region, by_region[region.occurrence_id]) for region in regions
        ),
        unattributed=tuple(unattributed),
        unresolved=tuple(unresolved),
    )


def _cover(region: RegionSpan, spans: list[Span]) -> RegionCoverage:
    clipped = [
        (max(a, region.content_start), min(b, region.content_end))
        for a, b in spans
    ]
    covered = _merge([(a, b) for a, b in clipped if a < b])
    return RegionCoverage(
        occurrence_id=region.occurrence_id,
        content_start=region.content_start,
        content_end=region.content_end,
        covered=covered,
        uncovered=_complement(covered, region.content_start, region.content_end),
    )


def _merge(spans: list[Span]) -> tuple[Span, ...]:
    """겹치거나 맞닿은 구간을 합친다."""

    out: list[Span] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return tuple(out)


def _complement(covered: tuple[Span, ...], start: int, end: int) -> tuple[Span, ...]:
    out: list[Span] = []
    cursor = start
    for a, b in covered:
        if cursor < a:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < end:
        out.append((cursor, end))
    return tuple(out)


def _is_nested(outer: str, inner: str) -> bool:
    """표 계층인가. 구분자 경계까지 봐야 한다.

    맨 글자 접두사로 보면 ``occ:…:p1`` 이 ``occ:…:p10`` 의 부모가 되고
    ``occ:…:c5`` 가 ``occ:…:c50`` 의 부모가 된다. 둘 다 형제다.

    문구가 같다는 이유로는 묶지 않는다. 한 문서에 같은 문구가 서로 다른 자리에
    두 번 나올 수 있고 그 둘은 서로 다른 근거다
    (``cpl_coverage._drop_nested_duplicates`` 와 같은 규칙).
    """

    return outer != inner and inner.startswith(outer + ":")


def _is_hierarchy(region: RegionSpan, selection: Selection) -> bool:
    return _is_nested(selection.occurrence_id, region.occurrence_id) or _is_nested(
        region.occurrence_id, selection.occurrence_id
    )


def _translate(region: RegionSpan, selection: Selection) -> Span | None:
    """선택 좌표를 구역 occurrence 좌표로 옮긴다. 유일할 때만.

    계층이 다르면 같은 글자라도 좌표가 다르다. 고른 본문이 구역 안에 정확히 한
    번 나올 때만 그 자리로 옮기고, 없거나 두 번 이상이면 옮기지 않는다. 여기서
    하나를 고르면 계산이 아니라 추측이다.
    """

    if not selection.text:
        return None
    first = region.text.find(selection.text)
    if first < 0 or region.text.find(selection.text, first + 1) >= 0:
        return None
    return (first, first + len(selection.text))
