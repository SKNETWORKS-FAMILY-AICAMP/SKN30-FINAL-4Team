"""Model 2 serving — 입력 정규화.

## 서빙 계약 (무엇을 받고 무엇을 받지 않는가)

받는다 — **F06 스키마 한 행** + 공고문 원문 텍스트.

    row_id, title, cohort, support_type, support_unit, support_method,
    support_rate, self_burden_rate, selected_count, project_duration,
    region, year, program_stem, normalized_title, evidence_text,
    evidence_source ...

받지 않는다 — HWP/PDF 파일 자체. 원문 -> 텍스트 -> F06 스키마는 기존
수집·전처리 파이프라인(D04 -> E01 -> F04 -> F05 -> F06)의 일이고 M82 가
바꾸지 않았다. 여기서 다시 구현하면 두 벌이 된다.

`support_type` 은 Model 1 의 출력이다. 비어 있으면 그대로 결측으로 둔다 —
서빙에서 임의로 채우면 Model 1 을 우회하는 셈이 된다.
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

import m45_m2_amount as M45                # noqa: E402


def training_frame(src_parquet=None):
    """번들을 적합할 때 쓰는 학습 프레임 (M45.prepare 통과, 1,877행)."""
    import f06_design_features as F6
    path = src_parquet or F6.OUT_V2
    d, _ = M45.prepare(pd.read_parquet(path))
    return d.reset_index(drop=True), path


def to_frame(records, template=None):
    """추론 입력(dict 또는 dict 목록) -> 학습과 같은 컬럼 스키마의 DataFrame.

    `template` 은 학습 프레임 한 행이다. F06 스키마에는 모델이 직접 쓰지 않는
    컬럼도 있어서, 빠진 칸을 결측으로 채우는 기준으로 쓴다. 값을 베끼는 것이
    아니라 **컬럼 집합과 dtype 만** 맞춘다.
    """
    if isinstance(records, dict):
        records = [records]
    df = pd.DataFrame(list(records))
    if template is not None:
        for c in template.columns:
            if c not in df.columns:
                df[c] = pd.NA
        df = df[list(template.columns)]
        for c in df.columns:
            want = template[c].dtype
            if str(want) == "category":
                df[c] = df[c].astype("object")
            else:
                try:
                    df[c] = df[c].astype(want)
                except (TypeError, ValueError):
                    pass
    return df.reset_index(drop=True)


REQUIRED = ("title", "evidence_text")


def validate(df):
    """최소 입력 점검. 없으면 예측이 조용히 나빠지므로 여기서 막는다."""
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError("필수 입력 누락: %s" % missing)
    empty = [c for c in REQUIRED if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all()]
    if empty:
        raise ValueError("필수 입력이 비어 있음: %s" % empty)
    return df
