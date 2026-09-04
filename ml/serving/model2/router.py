"""Model 2 serving — M73 ordinal soft routing.

학습의 `m73_block` 과 같은 구조다.

    global            전 구간 XGB 회귀 1개
    expert Low/Mid/High  fold train y 의 P33.3/P66.7 로 나눈 구간별 회귀 3개
    stage 1           누적 이진 2개 (P(y>Low경계), P(y>Mid경계)) -> 3-class 확률
    soft              구간 확률로 expert 예측을 가중 평균 (hard 라우팅 아님)

hard 가 아니라 soft 인 이유는 M67 이 실측했다 — 구간 오분류 비용이 라우팅
이득보다 컸다. M73 이 soft 로 바꿔 0.3719 -> 0.3563 을 만들었다.
"""
import os
import sys

import numpy as np

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

import m2_features as F                        # noqa: E402
import m73_m2_routing_improvement as M73       # noqa: E402


def fit(X, y):
    """global + expert 3 + ordinal stage1 2 = 모델 6개를 적합한다."""
    import xgboost as xgb

    edges = M73.bucket_edges(y)
    z = M73.to_bucket(y, edges)
    glob = F.make_point_model().fit(X, y)
    experts = []
    for k in range(3):
        m = z == k
        experts.append(F.make_point_model().fit(X.iloc[m], y[m]))
    p = M73._xgb_params()
    ord_a = xgb.XGBClassifier(objective="binary:logistic",
                              eval_metric="logloss", **p).fit(X, (z >= 1).astype(int))
    ord_b = xgb.XGBClassifier(objective="binary:logistic",
                              eval_metric="logloss", **p).fit(X, (z >= 2).astype(int))
    return {"edges": [float(e) for e in edges], "global": glob,
            "experts": experts, "ordinal": (ord_a, ord_b)}


def proba(model, X):
    """3-class 구간 확률. 학습의 `stage1_proba('ordinal_xgb', ...)` 와 같은 식."""
    a, b = model["ordinal"]
    p1 = a.predict_proba(X)[:, 1]
    p2 = np.minimum(p1, b.predict_proba(X)[:, 1])
    pr = np.column_stack([1 - p1, p1 - p2, p2])
    return np.clip(pr, 1e-9, None) / np.clip(pr, 1e-9, None).sum(1, keepdims=True)


def predict(model, X):
    """soft 라우팅 최종 예측(log10) + 진단값."""
    tab = np.column_stack([e.predict(X) for e in model["experts"]])
    pr = proba(model, X)
    soft = M73.route_soft(tab, pr)
    return {"pred_log10": soft, "bucket_proba": pr, "expert_table": tab,
            "global": model["global"].predict(X),
            "edges_won": [int(round(10 ** e)) for e in model["edges"]]}
