"""Model 2 serving — 진입점 (M82/P3).

    신규 공고(F06 스키마 + 원문)
      -> preprocessing.to_frame
      -> masking / proximity
      -> feature_builder.transform   (211열, 순서 검증)
      -> router.predict              (ordinal soft routing)
      -> 지원규모(원)

산출물 두 가지는 서로 독립이다.

    predict()     지원규모 회귀 추정값 (M82/P3)
    percentile()  비교군 안에서의 위치 — 모델 2 의 1차 산출물.
                  M65 세대와 같은 `m45_m2_amount.build_reference()` 표를 쓴다.

번들: ml/models/model2_canonical/model2_p3_bundle.joblib
"""
import os
import sys

import numpy as np
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
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import feature_builder as FB               # noqa: E402
import preprocessing as PP                 # noqa: E402
import router as RT                        # noqa: E402
import masking as MK                       # noqa: E402

BUNDLE_PATH = os.path.join(_ML, "models", "model2_canonical",
                           "model2_p3_bundle.joblib")
COHORT_REF = os.path.join(_HERE, "cohort_reference.parquet")

_CACHE = {}


def load(path=None):
    """번들을 한 번만 읽어 캐시한다."""
    import joblib
    p = path or BUNDLE_PATH
    if p not in _CACHE:
        if not os.path.exists(p):
            raise FileNotFoundError(
                "번들이 없다: %s — `python build_bundle.py` 로 먼저 만든다." % p)
        _CACHE[p] = joblib.load(p)
    return _CACHE[p]


def predict(records, path=None):
    """지원규모 추정. 반환 단위는 원(KRW)과 log10 둘 다."""
    B = load(path)
    df = PP.validate(PP.to_frame(records, B["template"]))
    X, P = FB.transform(df, B["features"])
    out = RT.predict(B["model"], X)

    # 마스킹 감사 — 서빙에서도 매번 확인한다(학습에서만 확인하면 의미가 없다)
    residue = int(sum(MK.has_digit_residue(t) for t in P["prox_context_text"]))
    rows = []
    for i in range(len(df)):
        pr = out["bucket_proba"][i]
        rows.append({
            "row_id": (df["row_id"].iloc[i] if "row_id" in df.columns else None),
            "pred_log10": round(float(out["pred_log10"][i]), 4),
            "pred_won": int(10 ** out["pred_log10"][i]),
            "bucket_proba": {"Low": round(float(pr[0]), 4),
                             "Mid": round(float(pr[1]), 4),
                             "High": round(float(pr[2]), 4)},
            "bucket": ["Low", "Mid", "High"][int(np.argmax(pr))],
            "bucket_edges_won": out["edges_won"],
            "proximity": {c: (None if pd.isna(P[c].iloc[i]) else float(P[c].iloc[i]))
                          for c in ("prox_support_rate", "prox_self_burden_rate",
                                    "prox_selected_count", "prox_duration_months")},
        })
    return {"model": B["meta"]["name"], "n": len(rows),
            "context_digit_residue": residue, "predictions": rows}


def percentile(value_won, support_type, support_method, unit, cohort):
    """모델 2 의 1차 산출물 — 비교군 안에서 이 금액이 어디쯤인가.

    회귀(`predict`)와 독립이다. 참조표는 M65 세대와 같은
    `m45_m2_amount.build_reference()` 결과를 그대로 쓴다 — M82 는 회귀만
    바꿨고 비교군 사다리는 바꾸지 않았다.
    """
    import m45_m2_amount as M45
    if not os.path.exists(COHORT_REF):
        raise FileNotFoundError("cohort_reference.parquet 이 없다: %s" % COHORT_REF)
    ref = pd.read_parquet(COHORT_REF)
    return M45.compare(ref, value_won, support_type, support_method, unit, cohort)


def info(path=None):
    """번들 메타 — 어떤 실험의 어떤 성능인지 백엔드가 바로 확인할 수 있게."""
    return load(path)["meta"]


if __name__ == "__main__":
    import json
    print(json.dumps(info(), ensure_ascii=False, indent=2, default=str))
