"""Model 2 serving — feature 조립 (M82/P3, 211열).

학습 때의 조립 순서를 그대로 재현한다. 순서가 어긋나면 XGBoost 는 조용히
틀린 답을 낸다 — 그래서 번들에 **컬럼 순서를 통째로 저장**하고 추론 때마다
대조한다(`assert_schema`).

    구조화 필드 (M45.make_xy)
    + 제목 TF-IDF/SVD 64        (m2_features.fit_title_features)
    + 원천 feature 층 m2-source-v1 (m2_source_features.build)
    + 본문 마스킹 TF-IDF/SVD 64  (m69.fit_body_svd)
    + explicit proximity 24      (m82 P1)
    + masked proximity SVD 16    (m82 P2)
    = 211열
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

import m2_features as F                    # noqa: E402
import m2_source_features as SF            # noqa: E402
import m45_m2_amount as M45                # noqa: E402
import m69_m2_source_features as M69       # noqa: E402

import proximity as PX                     # noqa: E402  (같은 폴더)

STEP = "G"                                  # M69 승격 단계


# ------------------------------------------------------------ 적합 (번들 생성)
def fit(d):
    """전체 학습행으로 변환기를 적합하고, 학습 설계행렬과 함께 돌려준다."""
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    NB, body, src = SF.build(d)

    idx = np.arange(len(d))
    # 학습·서빙이 같은 함수로 적합되도록 fold 함수를 tr=te=전체 로 호출한다.
    Xa, _Xb, title_fitted = F.build_features(Xs, titles, idx, idx, True, True)
    body_a, _body_b, body_fitted = M69.fit_body_svd(body, body, return_objects=True)
    P = PX.build(src)
    prox_a, _prox_b, prox_fitted = M82_fit_prox(P)

    X = _assemble(Xa, NB, idx, body_a, PX.explicit(P), prox_a)
    bundle = {
        "title": title_fitted,             # (TfidfVectorizer, TruncatedSVD)
        "body": body_fitted,               # (TfidfVectorizer, TruncatedSVD)
        "prox": prox_fitted,               # (TfidfVectorizer, TruncatedSVD, k)
        "columns": list(map(str, X.columns)),
        "structured_cols": list(map(str, Xs.columns)),
        "nb_cols": list(SF.columns_upto(STEP)),
        "cat_levels": {c: list(map(str, Xs[c].cat.categories))
                       for c in Xs.columns if str(Xs[c].dtype) == "category"},
        # 학습 때의 구조화 컬럼 dtype. 신규 1건은 결측만 있는 칸이 object 로
        # 굳어 XGBoost 가 거부한다 — 추론에서 이 표로 되돌린다.
        "structured_dtypes": {c: str(Xs[c].dtype) for c in Xs.columns},
        # 원천 feature 층(NB)의 범주 레벨. NB 는 매 호출 새로 만들어지므로
        # 1건만 넣으면 레벨이 학습과 달라진다 — XGBoost 가 즉시 거부한다.
        "nb_cat_levels": {c: list(map(str, NB[c].cat.categories))
                          for c in SF.columns_upto(STEP)
                          if str(NB[c].dtype) == "category"},
        "cats": cats,
    }
    return X, y, bundle


def M82_fit_prox(P):
    import m82_m2_proximity_features as M82
    txt = PX.context_text(P)
    return M82.fit_prox_svd(txt, txt, return_objects=True)


def _assemble(Xbase, NB, idx, body_svd, prox_explicit, prox_svd):
    """M69.assemble 과 같은 순서로 붙인다."""
    a = Xbase.reset_index(drop=True)
    cols = SF.columns_upto(STEP)
    if cols:
        a = pd.concat([a, NB.iloc[idx][cols].reset_index(drop=True)], axis=1)
    bn = ["%s%02d" % (M69.BODY_PREFIX, i) for i in range(body_svd.shape[1])]
    a = pd.concat([a, pd.DataFrame(body_svd, columns=bn)], axis=1)
    a = pd.concat([a, prox_explicit.reset_index(drop=True)], axis=1)
    pn = ["proxsvd%02d" % i for i in range(prox_svd.shape[1])]
    a = pd.concat([a, pd.DataFrame(prox_svd, columns=pn)], axis=1)
    return a


# ------------------------------------------------------------ 변환 (추론)
def transform(d, bundle):
    """새 공고 프레임 -> 학습과 같은 열·순서의 설계행렬."""
    Xs, _y, _, _ = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    NB, body, src = SF.build(d)
    idx = np.arange(len(d))

    # 학습 때의 dtype 으로 되돌린다. 범주형은 레벨까지 맞추고(새 값은 NaN),
    # 수치형은 to_numeric 으로 강제한다 — 신규 1건은 값이 없어 object 가 되기 쉽다.
    for c, want in bundle.get("structured_dtypes", {}).items():
        if c not in Xs.columns:
            continue
        if want == "category":
            levels = bundle["cat_levels"].get(c, [])
            Xs[c] = pd.Categorical(Xs[c].astype(str), categories=levels)
        elif want != "object":
            v = pd.to_numeric(Xs[c], errors="coerce")
            # 정수형인데 결측이 있으면 float 로 둔다 — XGBoost 는 NaN 을 분기로
            # 처리하므로 임의값으로 채우는 것보다 낫다.
            if want.startswith("int") and v.isna().any():
                want = "float64"
            Xs[c] = v.astype(want)

    tv, tsvd = bundle["title"]
    ta = tsvd.transform(tv.transform(titles))
    Xa = pd.concat([Xs.reset_index(drop=True),
                    pd.DataFrame(ta, columns=F.title_columns(ta.shape[1]))], axis=1)

    bv, bsvd = bundle["body"]
    body_a = bsvd.transform(bv.transform(body))

    P = PX.build(src)
    if bundle["prox"] is None:
        prox_a = np.zeros((len(d), 0))
    else:
        pv, psvd, k = bundle["prox"]
        prox_a = psvd.transform(pv.transform(PX.context_text(P)))
        if prox_a.shape[1] < k:
            prox_a = np.pad(prox_a, ((0, 0), (0, k - prox_a.shape[1])))

    for c, levels in bundle.get("nb_cat_levels", {}).items():
        if c in NB.columns:
            NB[c] = pd.Categorical(NB[c].astype(str), categories=levels)

    X = _assemble(Xa, NB, idx, body_a, PX.explicit(P), prox_a)
    return assert_schema(X, bundle), P


def assert_schema(X, bundle):
    """열 이름·순서가 학습과 정확히 같아야 한다. 다르면 여기서 멈춘다."""
    want = bundle["columns"]
    got = list(map(str, X.columns))
    if got != want:
        missing = [c for c in want if c not in got]
        extra = [c for c in got if c not in want]
        raise ValueError(
            "feature schema mismatch — 학습 %d열 / 추론 %d열, 누락 %s, 추가 %s"
            % (len(want), len(got), missing[:5], extra[:5]))
    return X[want]
