"""Model 3 serving wrapper — 유사사업 비교군 대비 설계 이례성 점수.

기존 canonical scoring(`ml/pipelines/m3_lab.py` 의 `score_pool()`)과
`ml/pipelines/m13_m4_anomaly.py` 의 `prepare()` 를 그대로 호출한다. 별도
학습 weight가 없는 모델이다 — 비교군 reference pool(M66 v3,
`design_features_v3.parquet`)을 매번 다시 스캔해 거리 기반 점수를 낸다.

Freeze 된 구조를 그대로 쓴다(파라미터를 새로 고르지 않는다):
비교군 사다리 A0(성격x방식 -> 성격 -> 전체) · MIN_COHORT=20 ·
mean 대표벡터(n_proto=1) · standard scaling · Euclidean.
ml/docs/02_모델_1_2_3_성능_결과서.md 3장 · ml/evaluation/m66_m3_cohort_supply.py 참조.

주의: "이례적"이지 "잘못됐다"가 아니다. 서비스 문구는 ALLOWED 목록만 쓴다
(m13_m4_anomaly.ALLOWED) — "부적절함"·"지원규모 과다" 같은 판정형 표현은
쓰지 않는다.
"""
import os
import sys

import numpy as np
import pandas as pd

_SERVING_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_ROOT = os.path.abspath(os.path.join(_SERVING_DIR, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = os.path.join(_ML_ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m3_lab as L  # noqa: E402
from m13_m4_anomaly import ALLOWED, MIN_AXES, prepare  # noqa: E402

POOL_PATH = os.path.join(_SERVING_DIR, "design_features_v3.parquet")

# score_pool()/prepare() 가 요구하는 최소 원본 필드. amount_outlier 는
# 없으면 False 로 채운다. row_id 는 없으면 자동 생성한다.
REQUIRED_FIELDS = [
    "support_type", "support_method", "support_unit", "amount_type",
    "per_recipient", "support_count", "project_duration", "support_ratio",
]

_pool = None


def _get_pool():
    """비교군 reference pool. 최초 호출 때만 읽고 캐시한다."""
    global _pool
    if _pool is None:
        raw = pd.read_parquet(POOL_PATH)
        p = prepare(raw)
        _pool = p[p["n_axes"] >= MIN_AXES].reset_index(drop=True)
    return _pool


def predict(records):
    """records: REQUIRED_FIELDS 를 채운 dict 의 list.
    `support_type` 이 비어 있으면 그 행은 채점되지 않는다(원본 prepare() 규칙).

    반환 (pandas.DataFrame, 행 순서는 입력과 동일):
        score        비교군 내부 거리분포에서의 백분위 (0~1, 클수록 이례적)
        level        어떤 비교군 단계에서 채점됐는가
                     (L1 support_type x support_method / L2 support_type / L0 전체)
        cohort_key   그 단계에서의 실제 비교군 키
        cohort_n     비교군 표본수
        top1_axis    가장 크게 벗어난 수치축 (설명용 — ALLOWED 문구와 함께 제시)
    """
    pool = _get_pool()
    df = pd.DataFrame(list(records)).reset_index(drop=True)
    if "row_id" not in df.columns:
        df["row_id"] = ["REQ%05d" % i for i in range(len(df))]
    if "amount_outlier" not in df.columns:
        df["amount_outlier"] = False
    for c in REQUIRED_FIELDS:
        if c not in df.columns:
            df[c] = np.nan

    ap = prepare(df)
    if not len(ap):
        return pd.DataFrame(columns=["row_id", "score", "level", "cohort_key",
                                     "cohort_n", "top1_axis"])

    res = L.score_pool(pool, ap)
    n_num = res["n_num"]
    top1_idx = np.abs(res["D"][:, :n_num]).argmax(axis=1)
    top1 = [L.NUM[i] for i in top1_idx]

    return pd.DataFrame({
        "row_id": ap["row_id"].to_numpy(),
        "score": res["score"].to_numpy(),
        "level": res["level"].to_numpy(),
        "cohort_key": res["cohort_key"].to_numpy(),
        "cohort_n": res["cohort_n"].to_numpy(),
        "top1_axis": top1,
    })


if __name__ == "__main__":
    # support_method/support_unit/amount_type 는 한글 라벨이 아니라 원본
    # 데이터의 영문 코드값이다(예: 보조금 -> grant, 기업당 -> company).
    demo = [{
        "row_id": "DEMO001",
        "support_type": "사업화", "support_method": "grant", "support_unit": "company",
        "amount_type": "per_company", "per_recipient": 500_000_000,
        "support_count": 3, "project_duration": 12, "support_ratio": 70,
    }]
    print(predict(demo))
    print("허용 문구:", ALLOWED)
