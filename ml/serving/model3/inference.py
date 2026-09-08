"""Model 3 serving wrapper — 유사사업 비교군 대비 설계 이례성 점수.

기존 canonical scoring(`ml/pipelines/model3/m3_lab.py` 의 `score_pool()`)과
`ml/pipelines/model3/m13_m3_anomaly.py` 의 `prepare()` 를 그대로 호출한다. 별도
학습 weight가 없는 모델이다 — 비교군 reference pool(M66 v3,
`design_features_v3.parquet`)을 매번 다시 스캔해 거리 기반 점수를 낸다.

Freeze 된 구조를 그대로 쓴다(파라미터를 새로 고르지 않는다):
비교군 사다리 A0(성격x방식 -> 성격 -> 전체) · MIN_COHORT=20 ·
mean 대표벡터(n_proto=1) · standard scaling · Euclidean.
ml/docs/02_모델_1_2_3_성능_결과서.md 3장 · ml/evaluation/model3/m66_m3_cohort_supply.py 참조.

주의: "이례적"이지 "잘못됐다"가 아니다. 서비스 문구는 ALLOWED 목록만 쓴다
(m13_m3_anomaly.ALLOWED) — "부적절함"·"지원규모 과다" 같은 판정형 표현은
쓰지 않는다.
"""
import os
import sys

import numpy as np
import pandas as pd

_SERVING_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_ROOT = os.path.abspath(os.path.join(_SERVING_DIR, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML_ROOT, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):          # 모델별 하위 폴더까지
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)

import m3_lab as L  # noqa: E402
from m13_m3_anomaly import ALLOWED, MIN_AXES, prepare  # noqa: E402

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


# 결측이 None 으로 들어오면 그 열이 object dtype 이 되어 prepare() 의
# np.log10 이 터진다. 서빙 입력은 JSON 에서 오므로 None 이 정상 경로다 —
# 여기서 NaN 으로 되돌린다. prepare() 는 NaN 을 '축 결측' 으로 세도록 짜여 있다.
NUMERIC_FIELDS = ["per_recipient", "support_count", "project_duration",
                  "support_ratio"]


def _frame(records):
    """서빙 입력(dict 목록) -> prepare() 가 받는 프레임. 결측을 NaN 으로 맞춘다."""
    df = pd.DataFrame(list(records)).reset_index(drop=True)
    if "row_id" not in df.columns:
        df["row_id"] = ["REQ%05d" % i for i in range(len(df))]
    if "amount_outlier" not in df.columns:
        df["amount_outlier"] = False
    df["amount_outlier"] = df["amount_outlier"].fillna(False).astype(bool)
    for c in REQUIRED_FIELDS:
        if c not in df.columns:
            df[c] = np.nan
    for c in NUMERIC_FIELDS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def axis_report(records):
    """채점 전에 각 행의 유효 수치축 수를 돌려준다 — 왜 빠졌는지 보이게.

    반환: dict 의 list — row_id · n_axes · scorable · axes_present · axes_missing
    """
    ap = prepare(_frame(records))
    from m13_m3_anomaly import NUM_FEATS       # noqa: PLC0415
    out = []
    for _, r in ap.iterrows():
        present = [a for a in NUM_FEATS if pd.notna(r.get(a))]
        out.append({
            "row_id": r["row_id"],
            "n_axes": int(r["n_axes"]),
            "scorable": int(r["n_axes"]) >= MIN_AXES,
            "axes_present": present,
            "axes_missing": [a for a in NUM_FEATS if a not in present],
        })
    return out


def predict(records, enforce_min_axes=True):
    """records: REQUIRED_FIELDS 를 채운 dict 의 list.
    `support_type` 이 비어 있으면 그 행은 채점되지 않는다(원본 prepare() 규칙).

    `enforce_min_axes=True`(기본)면 유효 수치축이 `MIN_AXES` 미만인 행도 함께
    제외한다. **이 검사를 여기 둔 이유**: 비교군 pool 에는 원래
    `n_axes >= MIN_AXES` 필터가 걸려 있는데 채점 대상 행에는 걸려 있지
    않았다. 그래서 축이 하나뿐인 요청도 나머지 축이 비교군 중앙값으로 채워진
    채 거리 점수를 받았다 — 근거가 없어서 평범해 보이는 것과 실제로 평범한
    것이 구별되지 않는다. 상위 래퍼(`score.score_document`)에만 두면 이
    함수를 직접 부르는 코드가 그대로 우회하므로 진입 함수에서 막는다.

    반환 (pandas.DataFrame, 행 순서는 입력과 동일):
        score        비교군 내부 거리분포에서의 백분위 (0~1, 클수록 이례적)
        level        어떤 비교군 단계에서 채점됐는가
                     (L1 support_type x support_method / L2 support_type / L0 전체)
        cohort_key   그 단계에서의 실제 비교군 키
        cohort_n     비교군 표본수
        top1_axis    가장 크게 벗어난 수치축 (설명용 — ALLOWED 문구와 함께 제시)
    """
    pool = _get_pool()
    ap = prepare(_frame(records))
    if enforce_min_axes and len(ap):
        ap = ap[ap["n_axes"] >= MIN_AXES].reset_index(drop=True)
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
