"""Model 2 서빙 — 비교군 사다리 확장.

무엇을 바꾸고 무엇을 안 바꾸는가
------------------------------
바꾸는 것은 **percentile 비교군을 고르는 순서**뿐이다. XGBoost 가중치·feature
조립·예측값은 건드리지 않는다. 학습 경로(`m45_m2_amount.LADDER`)도 그대로 둔다 —
여기서 서빙용 사다리를 따로 만들고 서빙 참조표만 새로 굽는다.

왜 필요한가
----------
기존 사다리는 support_type 을 너무 빨리 버린다.

    성격x방식x단위x출처  →  성격x단위x출처  →  단위x출처

마지막 단계에 support_type 이 없다. 실측: 실제 존재하는 42개 조합 중 19개(45%)가
여기로 떨어져, 백분위가 지원성격을 전혀 반영하지 않는다. 예외가 아니라 절반이다.

단위를 섞으면 안 된다 — 새 단계 설계의 제약
-----------------------------------------
`m45.lookup` 이 맨 앞에서 단위·출처가 없으면 비교를 포기한다. 이유가 주석에
적혀 있다: **기업당 금액을 과제당 분포와 견주면 안 된다.** 같은 이유로 사다리
중간 단계에서도 `support_unit` 을 빼면 기업당/과제당/인당이 한 분포에 섞인다.

그래서 두 가지를 다 만들어 두고 실측으로 고르게 한다.

    UNIT_SAFE  성격x단위 를 추가한다. 단위를 유지한 채 출처만 버린다.
    WIDE       계획서 원안. 성격x출처·성격 을 추가한다. 단위가 섞인다.

기본값은 UNIT_SAFE 다. 유지율을 올리는 것이 목적인데, 단위를 섞어 올린 유지율은
"지원성격은 맞지만 단위가 다른 분포와 비교한 값"이라 이름만 정확해진다.
"""
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)

import m45_m2_amount as M45                    # noqa: E402

ST, SM, SU, CO = "support_type", "support_method", "support_unit", "cohort"

# 단위를 끝까지 유지한다. support_type 을 버리기 전에 출처만 먼저 버린다.
UNIT_SAFE_LADDER = [
    ("성격x방식x단위x출처", [ST, SM, SU, CO]),
    ("성격x단위x출처", [ST, SU, CO]),
    ("성격x단위", [ST, SU]),                    # 신규 — 출처만 버린다
    ("단위x출처", [SU, CO]),
]

# 계획서 원안. 단위를 버리는 단계가 둘 들어간다.
WIDE_LADDER = [
    ("성격x방식x단위x출처", [ST, SM, SU, CO]),
    ("성격x단위x출처", [ST, SU, CO]),
    ("성격x단위", [ST, SU]),
    ("성격x출처", [ST, CO]),                    # 신규 — 단위가 섞인다
    ("성격", [ST]),                             # 신규 — 단위가 섞인다
    ("단위x출처", [SU, CO]),
]

LADDERS = {"unit_safe": UNIT_SAFE_LADDER, "wide": WIDE_LADDER,
           "legacy": M45.LADDER}
DEFAULT_LADDER = "unit_safe"

# 단위를 키에 쓰지 않는 단계. 여기서 나온 백분위는 기업당/과제당이 섞여 있다.
UNIT_MIXED_LEVELS = frozenset(("성격x출처", "성격"))

# 서빙 artifact 는 하나뿐이다. 파일명·경로를 그대로 두고 **내용만** unit_safe
# 사다리 기준으로 교체했다 — 새 파일명을 하나 더 만들면 배포 대상이 늘어난다.
# 기존 3단계의 값은 한 행도 바뀌지 않았고 `성격x단위` 25행이 더해졌을 뿐이다
# (교체 전 확인: 컬럼·dtype 동일, 공통 단계 bit 일치).
REFERENCE = os.path.join(_HERE, "cohort_reference.parquet")

_CACHE = {}


def _with_ladder(ladder, fn, *args, **kwargs):
    """m45 의 사다리를 잠시 갈아끼우고 그 함수를 그대로 쓴다.

    build_reference·lookup 이 모듈 전역 LADDER 를 읽는다. 같은 계산을 여기서
    다시 구현하면 백분위 산식이 두 벌이 되어 조용히 갈라진다 — 그래서 코드를
    베끼지 않고 전역만 잠깐 바꾼 뒤 되돌린다.
    """
    orig = M45.LADDER
    try:
        M45.LADDER = ladder
        return fn(*args, **kwargs)
    finally:
        M45.LADDER = orig


def build_reference(d, ladder=DEFAULT_LADDER):
    """학습 프레임 → 확장 참조표. 백분위 산식은 m45 것을 그대로 쓴다."""
    return _with_ladder(LADDERS[ladder], M45.build_reference, d)


def lookup(ref, support_type, support_method, unit, cohort,
           ladder=DEFAULT_LADDER):
    """확장 사다리로 비교군을 찾는다. 반환은 m45.lookup 과 같다."""
    return _with_ladder(LADDERS[ladder], M45.lookup, ref, support_type,
                        support_method, unit, cohort)


def load_reference(path=None):
    """서빙 참조표. 한 번만 읽고 캐시한다."""
    p = path or REFERENCE
    if p not in _CACHE:
        _CACHE[p] = pd.read_parquet(p)
    return _CACHE[p]


def uses_support_type(level):
    """이 단계가 support_type 을 비교 키로 썼는가."""
    return bool(level) and str(level).startswith("성격")


def uses_support_unit(level):
    """이 단계가 support_unit 을 유지했는가. False 면 단위가 섞인 분포다."""
    return bool(level) and str(level) not in UNIT_MIXED_LEVELS


def compare(value, support_type, support_method, unit, cohort,
            ref=None, ladder=DEFAULT_LADDER):
    """m45.compare 와 같은 반환 + 그 단계가 무엇을 썼는지 두 플래그."""
    r = ref if ref is not None else load_reference()
    out = _with_ladder(LADDERS[ladder], M45.compare, r, value, support_type,
                       support_method, unit, cohort)
    if isinstance(out, dict):
        level = out.get("level")
        out["uses_support_type"] = uses_support_type(level)
        out["uses_support_unit"] = uses_support_unit(level)
    return out


if __name__ == "__main__":
    # 참조표를 굽는다. 학습 데이터에서 만들고 서빙 폴더에 둔다.
    import preprocessing as PP
    sys.path.insert(0, _HERE)
    d, src = PP.training_frame()
    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LADDER
    out = build_reference(d, name)
    out.to_parquet(REFERENCE, index=False)
    print("사다리 %s · 원천 %s · %d행 -> %s"
          % (name, os.path.basename(src), len(out), REFERENCE))
