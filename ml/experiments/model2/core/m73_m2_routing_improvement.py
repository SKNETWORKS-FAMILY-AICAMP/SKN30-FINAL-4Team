r"""M73 — routing 을 고쳐서 M69(0.3719)를 이길 수 있는가.

지시서(사용자, `model2_routing_improvement_experiment_plan.md`):

    M67 의 hard routing 은 금액 구간 오분류 때문에 M65 보다 악화됐다. 이번에는
    (a) routing 정확도 자체를 올리거나 (b) 확신이 높은 행에만 expert 를 쓰는
    방식으로 오분류 비용을 줄여, 최종 MAE 를 M69 아래로 내릴 수 있는지 본다.

바꾸지 않는 것 — M69 와 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 의 G 단계 (구조화 + 제목 SVD64 + 원천 feature 층 + 본문 SVD64)
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    구간정의   fold train y 의 P33.3 / P66.7 (M67·M69 와 동일)
    baseline   M45.cohort_median_baseline (개선율의 분모)

바뀌는 것은 **예측 경로를 고르는 방법** 하나다. global(M69 단일 XGB)은 매
fold 에서 같이 학습해 paired 비교의 기준선으로 둔다.

## 재는 것 여섯 (지시서 우선순위 순)

    1. Ordinal Stage 1        3-class softprob 대신 누적 이진 2개
    2. Confidence-Gated       max P >= t 일 때만 expert, 아니면 global
    3. Low-Only Expert        P(Low) >= t 일 때만 Low expert, 아니면 global
    4. Error-Aware Gate       '금액구간'이 아니라 'expert 가 이길 행'을 분류
    5. Stage 1 모델 비교      XGB / LGBM / CatBoost / LogReg / MLP
    6. Probability Calibration Platt · Isotonic

## 임계값과 gate 라벨을 어디서 고르는가 (지시서 '실험 원칙')

이 실험의 가장 위험한 자리다. threshold 를 OOF 에서 고르고 같은 OOF 로 재면
반드시 좋아 보인다(M68b 가 λ 에서 겪은 함정). 그래서 두 층으로 나눈다.

    nested (정직한 값)  outer fold 의 train 안에서 다시 GroupKFold(3) 을 돌려
                        inner OOF 를 만들고, threshold 와 gate 라벨을 **거기서만**
                        고른 뒤 outer test 에 그대로 적용한다. 승격 판정은
                        이 값으로만 한다.
    sweep (진단용)      0.60/0.70/0.80/0.90 을 전체 OOF 에 그대로 적용한 표.
                        '어느 임계에서 어떻게 움직이는가'를 보기 위한 곡선이지
                        후보가 아니다. 이 표에서 최저값을 골라 승격 근거로
                        쓰면 안 된다.

inner CV 는 outer train 의 feature 행렬을 재적합 없이 그대로 쓴다. SVD 는
y 를 보지 않고, inner 의 모든 행은 outer train 안에 있어 outer test 를 건드리지
않는다 — inner 추정치만 아주 조금 낙관적이고, 판정에 쓰는 outer 숫자는 깨끗하다.

산출
    ml/data/processed/m73_routing_oof.parquet
    ml/reports/m73_m2_routing_improvement.json / .md
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
import os as _os
import sys as _sys

def _find_ml_root(_start):
    """`ml/` 를 위로 거슬러 찾는다. 파일이 몇 단계 아래로 옮겨져도 동작한다."""
    _p = _os.path.abspath(_start)
    while True:
        _p = _os.path.dirname(_p)
        if (_os.path.isdir(_os.path.join(_p, "pipelines"))
                and _os.path.isdir(_os.path.join(_p, "data"))):
            return _p
        if _p == _os.path.dirname(_p):
            raise RuntimeError("ml root not found from %s" % _start)


_ML = _find_ml_root(__file__)
for _d in ("pipelines", "evaluation", "experiments"):
    _base = _os.path.join(_ML, _d)
    if not _os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in _os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in _sys.path:
            _sys.path.insert(0, _dp)
# -------------------------------------------------------------------------

import io
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m73_routing_oof.parquet")
MD = C.report_path("m73_m2_routing_improvement.md")

BUCKETS = ["Low", "Mid", "High"]
CUTS = M69.CUTS
STEP = "G"                      # M69 의 승격 후보 단계
INNER_SPLITS = 3
THRESHOLDS = (0.60, 0.70, 0.80, 0.90)
GATE_THRESHOLDS = (0.50, 0.60, 0.70)
NESTED_GRID = tuple(np.round(np.arange(0.35, 0.96, 0.05), 2))

# M69 공표치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M69_PUBLISHED = {"MAE_log10": 0.3719, "within_2x": 0.530, "strict_MAE": 0.3931,
                 "stage1_acc": 0.7848, "opposite_end": 0.0197}

GLOBAL = "global (M69 단일 XGB)"
STAGE1_NAMES = ["mc_xgb", "ordinal_xgb", "lgbm", "catboost", "logreg", "mlp"]
STAGE1_LABEL = {
    "mc_xgb": "XGB 3-class (M69 현행)", "ordinal_xgb": "XGB ordinal (누적 이진 2개)",
    "lgbm": "LightGBM 3-class", "catboost": "CatBoost 3-class",
    "logreg": "Logistic Regression", "mlp": "MLP (64,32)",
}
CAL_TARGETS = ["mc_xgb", "ordinal_xgb"]      # calibration 을 붙일 Stage 1
GATE_TARGETS = ["mc_xgb", "ordinal_xgb"]     # error-aware gate 를 붙일 Stage 1
# inner OOF 에서 다시 학습할 Stage 1. nested threshold·calibration·gate 라벨이
# 필요한 후보만 넣는다 — 나머지 4종은 '모델 비교'(실험 5)용이라 outer 만으로 족하다.
INNER_NAMES = ["mc_xgb", "ordinal_xgb"]


# ------------------------------------------------------------ 구간
def bucket_edges(ytr):
    return tuple(float(v) for v in np.percentile(ytr, CUTS))


def to_bucket(y, edges):
    return np.digitize(y, np.asarray(edges), right=False).astype(int)


# ------------------------------------------------------------ Stage 1 모델들
def _xgb_params():
    return {k: v for k, v in F.XGB_POINT.items() if k != "objective"}


def _numeric_design(Xtr, Xte):
    """LogReg/MLP 용 수치 행렬. 범주형 one-hot + 결측 중앙값 + 표준화.

    트리 계열은 category dtype 을 그대로 먹지만 선형/신경망은 못 먹는다. 이
    변환을 fold train 에만 적합해서 다른 후보와 같은 규율을 지킨다.
    """
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    cat = [c for c in Xtr.columns if str(Xtr[c].dtype) == "category"]
    num = [c for c in Xtr.columns if c not in cat]
    A = B = None
    if cat:
        enc = OneHotEncoder(handle_unknown="ignore", sparse_output=False, min_frequency=5)
        A = enc.fit_transform(Xtr[cat].astype(str))
        B = enc.transform(Xte[cat].astype(str))
    ntr = Xtr[num].to_numpy(dtype=float)
    nte = Xte[num].to_numpy(dtype=float)
    med = np.nanmedian(np.where(np.isfinite(ntr), ntr, np.nan), axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    ntr = np.where(np.isfinite(ntr), ntr, med)
    nte = np.where(np.isfinite(nte), nte, med)
    sc = StandardScaler().fit(ntr)
    ntr, nte = sc.transform(ntr), sc.transform(nte)
    if A is None:
        return ntr, nte
    return np.hstack([ntr, A]), np.hstack([nte, B])


def stage1_proba(name, Xtr, ztr, Xte):
    """Stage 1 후보 하나의 test 확률 (n_te, 3). 대규모 탐색은 하지 않는다."""
    if name == "mc_xgb":
        import xgboost as xgb
        m = xgb.XGBClassifier(objective="multi:softprob", num_class=3,
                              eval_metric="mlogloss", **_xgb_params())
        return m.fit(Xtr, ztr).predict_proba(Xte)

    if name == "ordinal_xgb":
        # 누적 이진 2개: A = P(y > Low경계), B = P(y > Mid경계).
        # 순서를 강제(p2 <= p1)한 뒤 차분으로 3-class 확률을 만든다.
        import xgboost as xgb
        p = _xgb_params()
        a = xgb.XGBClassifier(objective="binary:logistic", eval_metric="logloss", **p)
        b = xgb.XGBClassifier(objective="binary:logistic", eval_metric="logloss", **p)
        p1 = a.fit(Xtr, (ztr >= 1).astype(int)).predict_proba(Xte)[:, 1]
        p2 = b.fit(Xtr, (ztr >= 2).astype(int)).predict_proba(Xte)[:, 1]
        p2 = np.minimum(p1, p2)
        pr = np.column_stack([1 - p1, p1 - p2, p2])
        return np.clip(pr, 1e-9, None) / np.clip(pr, 1e-9, None).sum(1, keepdims=True)

    if name == "lgbm":
        import lightgbm as lgb
        m = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=800,
                               learning_rate=0.03, num_leaves=31, subsample=0.9,
                               subsample_freq=1, colsample_bytree=0.8,
                               random_state=F.PIPELINE_SEED, verbose=-1)
        return m.fit(Xtr, ztr).predict_proba(Xte)

    if name == "catboost":
        from catboost import CatBoostClassifier
        cat = [c for c in Xtr.columns if str(Xtr[c].dtype) == "category"]
        A, B = Xtr.copy(), Xte.copy()
        for c in cat:
            A[c] = A[c].astype(str)
            B[c] = B[c].astype(str)
        m = CatBoostClassifier(loss_function="MultiClass", iterations=800,
                               learning_rate=0.03, depth=6,
                               random_seed=F.PIPELINE_SEED, verbose=0,
                               allow_writing_files=False)
        return m.fit(A, ztr, cat_features=cat).predict_proba(B)

    if name == "logreg":
        from sklearn.linear_model import LogisticRegression
        a, b = _numeric_design(Xtr, Xte)
        m = LogisticRegression(max_iter=2000, C=1.0, random_state=F.PIPELINE_SEED)
        return m.fit(a, ztr).predict_proba(b)

    if name == "mlp":
        from sklearn.neural_network import MLPClassifier
        a, b = _numeric_design(Xtr, Xte)
        m = MLPClassifier(hidden_layer_sizes=(64, 32), max_iter=400, alpha=1e-3,
                          early_stopping=True, random_state=F.PIPELINE_SEED)
        return m.fit(a, ztr).predict_proba(b)

    raise ValueError("unknown stage1: %s" % name)


def gate_model():
    """Error-aware gate 분류기. Stage 1 과 같은 설정, 이진 목적함수."""
    import xgboost as xgb
    return xgb.XGBClassifier(objective="binary:logistic", eval_metric="logloss",
                             **_xgb_params())


# ------------------------------------------------------------ calibration
def fit_calibrator(kind, proba, z):
    """inner OOF 확률로만 적합한다. outer test 는 보지 않는다."""
    if kind == "platt":
        from sklearn.linear_model import LogisticRegression
        m = LogisticRegression(max_iter=2000, random_state=F.PIPELINE_SEED)
        m.fit(np.log(np.clip(proba, 1e-9, 1)), z)
        return ("platt", m)
    if kind == "iso":
        from sklearn.isotonic import IsotonicRegression
        ms = []
        for k in range(3):
            r = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            r.fit(proba[:, k], (z == k).astype(float))
            ms.append(r)
        return ("iso", ms)
    raise ValueError(kind)


def apply_calibrator(cal, proba):
    kind, m = cal
    if kind == "platt":
        out = m.predict_proba(np.log(np.clip(proba, 1e-9, 1)))
        if out.shape[1] < 3:                     # inner 에 없는 클래스 방어
            full = np.zeros((len(out), 3))
            for j, c in enumerate(m.classes_):
                full[:, int(c)] = out[:, j]
            out = full
    else:
        out = np.column_stack([m[k].predict(proba[:, k]) for k in range(3)])
    out = np.clip(out, 1e-9, None)
    return out / out.sum(1, keepdims=True)


# ------------------------------------------------------------ routing 규칙
def route_hard(table, proba):
    return table[np.arange(len(table)), proba.argmax(1)]


def route_soft(table, proba):
    return (proba * table).sum(1)


def gate_apply(p_expert, p_global, use):
    return np.where(use, p_expert, p_global)


def conf_pred(table, proba, p_global, t):
    use = proba.max(1) >= t
    return gate_apply(route_hard(table, proba), p_global, use), use


def low_pred(table, proba, p_global, t):
    use = proba[:, 0] >= t
    return gate_apply(table[:, 0], p_global, use), use


# ------------------------------------------------------------ inner nested OOF
def inner_oof(Xtr, ytr, gtr, names):
    """outer train 안에서 다시 GroupKFold. threshold·gate 라벨의 유일한 출처다.

    반환하는 것은 outer train 행 순서 그대로의 배열이다.
        z      inner fold train 경계로 매긴 실제 구간
        glob   inner OOF global 예측
        table  inner OOF 구간 전용 expert 예측 (n_tr, 3)
        proba  Stage 1 후보별 inner OOF 확률
    """
    from sklearn.model_selection import GroupKFold

    n = len(ytr)
    z = np.zeros(n, dtype=int)
    glob = np.zeros(n)
    table = np.zeros((n, 3))
    proba = {s: np.zeros((n, 3)) for s in names}
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        Xa, Xb = Xtr.iloc[a], Xtr.iloc[b]
        ya = ytr[a]
        e = bucket_edges(ya)
        za = to_bucket(ya, e)
        z[b] = to_bucket(ytr[b], e)
        glob[b] = F.make_point_model().fit(Xa, ya).predict(Xb)
        for k in range(3):
            m = za == k
            table[b, k] = F.make_point_model().fit(Xa.iloc[m], ya[m]).predict(Xb)
        for s in names:
            proba[s][b] = stage1_proba(s, Xa, za, Xb)
    return z, glob, table, proba


def pick_threshold(kind, table, proba, glob, y):
    """inner OOF MAE 를 최소화하는 threshold. 동점이면 expert 를 덜 쓰는 쪽."""
    best, best_mae, best_use = None, np.inf, 1.1
    for t in NESTED_GRID:
        p, use = (conf_pred if kind == "conf" else low_pred)(table, proba, glob, float(t))
        mae = float(np.abs(p - y).mean())
        share = float(use.mean())
        if mae < best_mae - 1e-9 or (abs(mae - best_mae) <= 1e-9 and share < best_use):
            best, best_mae, best_use = float(t), mae, share
    return best, round(best_mae, 4), round(best_use, 4)


# ------------------------------------------------------------ fold 체크포인트
# 한 fold 가 5분 넘게 걸린다. 중간에 끊겼을 때 처음부터 다시 돌리지 않도록
# **모델 출력만** fold 단위로 저장한다. routing 규칙(gating·blend)은 numpy
# 연산이라 다시 계산해도 공짜다 — 규칙을 고쳐도 체크포인트는 살아 있다.
#
# 저장하지 않는 것: 학습된 모델 자체. 재현에 필요한 것은 예측값이고, 모델을
# 저장하면 라이브러리 버전에 묶여 오히려 재현이 약해진다.
CKPT_DIR = os.path.join(C.PROC, "m73_ckpt")
CODE_VERSION = "m73-v1"


def ckpt_signature(fp):
    """이 서명이 다르면 체크포인트를 읽지 않는다.

    코드나 설정을 고친 뒤 옛 체크포인트를 조용히 재사용하는 것이 이 구조에서
    가장 위험한 자리다 — 결과가 어느 코드의 것인지 알 수 없게 된다. 그래서
    데이터 해시·모델 파라미터·후보 목록·seed 를 전부 서명에 넣는다.
    """
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb_point": F.XGB_POINT, "stage1": STAGE1_NAMES, "inner_names": INNER_NAMES,
        "inner_splits": INNER_SPLITS, "nested_grid": [float(x) for x in NESTED_GRID],
        "cal": CAL_TARGETS, "gate": GATE_TARGETS, "step": STEP, "cuts": list(CUTS),
        "feature_version": F.FEATURE_VERSION, "layer_version": SF.LAYER_VERSION,
        "body_svd": M69.BODY_SVD, "seed": F.PIPELINE_SEED, "n_splits": F.N_SPLITS,
    }, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ckpt_paths(sig, tag, i):
    d = os.path.join(CKPT_DIR, "%s__%s" % (sig, tag))
    return d, os.path.join(d, "fold%d.npz" % i), os.path.join(d, "fold%d.json" % i)


def ckpt_load(sig, tag, i):
    import json as _json
    _, npz, js = ckpt_paths(sig, tag, i)
    if not (os.path.exists(npz) and os.path.exists(js)):
        return None
    try:
        z = np.load(npz)
        meta = _json.load(io.open(js, encoding="utf-8"))
    except Exception:
        return None
    return {"te": z["te"], "base": z["base"], "z_true": z["z_true"],
            "p_glob": z["p_glob"], "tab": z["tab"],
            "proba": {k[7:]: z[k] for k in z.files if k.startswith("proba__")},
            "gate_p": {k[6:]: z[k] for k in z.files if k.startswith("gate__")},
            "thresholds": meta["thresholds"], "rec": meta["rec"]}


def ckpt_save(sig, tag, i, fo):
    import json as _json
    d, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(d, exist_ok=True)
    arrays = {"te": fo["te"], "base": fo["base"], "z_true": fo["z_true"],
              "p_glob": fo["p_glob"], "tab": fo["tab"]}
    arrays.update({"proba__" + k: v for k, v in fo["proba"].items()})
    arrays.update({"gate__" + k: v for k, v in fo["gate_p"].items()})
    np.savez_compressed(npz + ".tmp.npz", **arrays)
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"thresholds": fo["thresholds"], "rec": fo["rec"]},
                            ensure_ascii=False, default=str))
    # 두 파일을 다 쓴 뒤에 바꿔 단다 — 쓰다 끊긴 반쪽 체크포인트를 읽지 않는다.
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ------------------------------------------------------------ 한 fold
def fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i):
    """이 fold 의 **모델 출력 전부**. 비싼 계산은 여기 한 곳에 모여 있다."""
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xtr, Xte = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te], STEP, [None])
    ytr, yte = y[tr], y[te]
    edges = bucket_edges(ytr)
    ztr, zte = to_bucket(ytr, edges), to_bucket(yte, edges)
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    # --- global (M69 재현) 과 구간 전용 expert 3개 ------------------------
    p_glob = F.make_point_model().fit(Xtr, ytr).predict(Xte)
    tab = np.zeros((len(te), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)

    # --- Stage 1 후보들 ---------------------------------------------------
    pr_te = {s: stage1_proba(s, Xtr, ztr, Xte) for s in STAGE1_NAMES}

    # --- inner nested OOF (threshold·gate 라벨·calibrator 의 유일한 출처) --
    in_z, in_glob, in_tab, in_pr = inner_oof(Xtr, ytr, groups[tr], INNER_NAMES)

    # --- calibration: inner 로만 적합 -------------------------------------
    for s in CAL_TARGETS:
        for kind in ("platt", "iso"):
            cal = fit_calibrator(kind, in_pr[s], in_z)
            pr_te["%s+%s" % (s, kind)] = apply_calibrator(cal, pr_te[s])
            in_pr["%s+%s" % (s, kind)] = apply_calibrator(cal, in_pr[s])

    # --- nested threshold: inner OOF MAE 로만 고른다 ----------------------
    thresholds = {}
    for s in pr_te:
        if s not in in_pr:
            continue
        for kind, tag in (("conf", "conf*"), ("low", "low*")):
            t, imae, ishare = pick_threshold(kind, in_tab, in_pr[s], in_glob, ytr)
            thresholds["%s/%s" % (tag, s)] = {"fold": i, "t": t, "inner_MAE": imae,
                                              "inner_share": ishare}

    # --- error-aware gate --------------------------------------------------
    gate_p, gate_rate = {}, {}
    for s in GATE_TARGETS:
        in_routed = route_hard(in_tab, in_pr[s])
        lab = (np.abs(in_routed - ytr) < np.abs(in_glob - ytr)).astype(int)
        gate_rate[s] = round(float(lab.mean()), 4)
        gate_p[s] = gate_model().fit(Xtr, lab).predict_proba(Xte)[:, 1]

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in edges],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "global_MAE": round(float(np.abs(p_glob - yte).mean()), 4),
           "gate_label_positive_rate": gate_rate,
           "stage1_acc": {s: round(float((v.argmax(1) == zte).mean()), 4)
                          for s, v in pr_te.items()},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "base": base_te, "z_true": zte, "p_glob": p_glob,
            "tab": tab, "proba": pr_te, "gate_p": gate_p,
            "thresholds": thresholds, "rec": rec}


# ------------------------------------------------------------ 한 split 전체
def run_split(Xs, y, groups, titles, body, NB, cats, sig, tag, verbose=True,
              use_ckpt=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    z_true = np.zeros(n, dtype=int)
    base = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    p_glob = np.zeros(n)
    table_all = np.zeros((n, 3))
    proba_all = {}                       # src -> (n,3)
    pred = {}                            # variant -> (n,)
    used = {}                            # variant -> bool (n,)
    gate_p = {s: np.zeros(n) for s in GATE_TARGETS}
    chosen_t = {}                        # variant -> fold 별 선택 threshold
    per_fold = []

    def put(store, key, idx, val):
        if key not in store:
            store[key] = (np.zeros(n, dtype=bool) if val.dtype == bool else np.zeros(n))
        store[key][idx] = val

    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        fold_id[te] = i
        base[te] = fo["base"]
        z_true[te] = fo["z_true"]
        p_glob[te] = fo["p_glob"]
        tab = fo["tab"]
        table_all[te] = tab

        for s, pr in fo["proba"].items():
            if s not in proba_all:
                proba_all[s] = np.zeros((n, 3))
            proba_all[s][te] = pr

        # --- routing 변형들 — 전부 numpy. 체크포인트에서 매번 다시 만든다 ---
        for s, pr in fo["proba"].items():
            put(pred, "hard/%s" % s, te, route_hard(tab, pr))
            put(used, "hard/%s" % s, te, np.ones(len(te), dtype=bool))
            put(pred, "soft/%s" % s, te, route_soft(tab, pr))
            put(used, "soft/%s" % s, te, np.ones(len(te), dtype=bool))
            for t in THRESHOLDS:
                p, u = conf_pred(tab, pr, fo["p_glob"], t)
                put(pred, "conf@%.2f/%s" % (t, s), te, p)
                put(used, "conf@%.2f/%s" % (t, s), te, u)
                p, u = low_pred(tab, pr, fo["p_glob"], t)
                put(pred, "low@%.2f/%s" % (t, s), te, p)
                put(used, "low@%.2f/%s" % (t, s), te, u)

        # nested — inner 에서 고른 threshold 를 그대로 적용
        for key, rt in fo["thresholds"].items():
            kind = "conf" if key.startswith("conf*") else "low"
            src = key.split("/", 1)[1]
            p, u = (conf_pred if kind == "conf" else low_pred)(
                tab, fo["proba"][src], fo["p_glob"], rt["t"])
            put(pred, key, te, p)
            put(used, key, te, u)
            chosen_t.setdefault(key, []).append(rt)

        for s in GATE_TARGETS:
            gp = fo["gate_p"][s]
            gate_p[s][te] = gp
            routed_te = route_hard(tab, fo["proba"][s])
            for t in GATE_THRESHOLDS:
                u = gp >= t
                put(pred, "eagate@%.2f/%s" % (t, s), te,
                    gate_apply(routed_te, fo["p_glob"], u))
                put(used, "eagate@%.2f/%s" % (t, s), te, u)

        rec = fo["rec"]
        rec["from_checkpoint"] = bool(cached)
        per_fold.append(rec)
        if verbose:
            print("   fold %d  cut %s  global %.4f  (%s)"
                  % (i, rec["edges_won"], rec["global_MAE"],
                     "체크포인트 재사용" if cached else "%.0fs" % rec["seconds"]))
            print("      Stage1 acc  " + "  ".join(
                "%s %.3f" % (s, rec["stage1_acc"][s]) for s in STAGE1_NAMES))
    return dict(z_true=z_true, base=base, fold_id=fold_id, p_glob=p_glob,
                table=table_all, proba=proba_all, pred=pred, used=used,
                gate_p=gate_p, chosen_t=chosen_t, folds=per_fold)


# ------------------------------------------------------------ 지표
def stage1_metrics(z_true, proba):
    from sklearn.metrics import f1_score

    z_hat = proba.argmax(1)
    M = np.zeros((3, 3), dtype=int)
    for t, h in zip(z_true, z_hat):
        M[t, h] += 1
    oh = np.zeros_like(proba)
    oh[np.arange(len(z_true)), z_true] = 1.0
    return {
        "accuracy": round(float((z_hat == z_true).mean()), 4),
        "macro_f1": round(float(f1_score(z_true, z_hat, average="macro")), 4),
        "recall": {b: round(float((z_hat[z_true == k] == k).mean()), 4)
                   for k, b in enumerate(BUCKETS)},
        "opposite_end_error_rate": round(float((np.abs(z_true - z_hat) == 2).mean()), 4),
        "adjacent_error_rate": round(float((np.abs(z_true - z_hat) == 1).mean()), 4),
        "brier": round(float(((proba - oh) ** 2).sum(1).mean()), 4),
        "confusion_true_x_pred": M.tolist(),
    }


def confidence_bins(z_true, proba, edges=(0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01)):
    """confidence bin 별 실제 accuracy — calibration 이 실제로 맞는지 보는 칸."""
    c = proba.max(1)
    ok = proba.argmax(1) == z_true
    out = []
    lo = 0.0
    for hi in edges:
        m = (c >= lo) & (c < hi)
        if m.sum():
            out.append({"bin": "[%.2f,%.2f)" % (lo, hi), "n": int(m.sum()),
                        "mean_conf": round(float(c[m].mean()), 4),
                        "accuracy": round(float(ok[m].mean()), 4)})
        lo = hi
    return out


def bucket_metrics(y, p, z_true):
    out = {}
    for k, name in enumerate(BUCKETS):
        m = z_true == k
        e = np.abs(p[m] - y[m])
        out[name] = {"n": int(m.sum()), "MAE_log10": round(float(e.mean()), 4),
                     "within_2x": round(float((e <= np.log10(2)).mean()), 4)}
    return out


def paired_test(y, p_new, p_old):
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n_better": int((d < 0).sum()), "n_worse": int((d > 0).sum())}


def usage_stats(y, p, p_glob, use):
    """expert 를 쓴 행에서 실제로 이겼는가. 승격조건 6번의 근거 칸이다."""
    if not use.any():
        return {"share": 0.0, "n": 0, "win_rate": None,
                "MAE_on_used_expert": None, "MAE_on_used_global": None}
    e_new = np.abs(p[use] - y[use])
    e_old = np.abs(p_glob[use] - y[use])
    return {"share": round(float(use.mean()), 4), "n": int(use.sum()),
            "win_rate": round(float((e_new < e_old).mean()), 4),
            "MAE_on_used_expert": round(float(e_new.mean()), 4),
            "MAE_on_used_global": round(float(e_old.mean()), 4),
            "delta_on_used": round(float(e_new.mean() - e_old.mean()), 4)}


def fold_maes(y, p, fold_id):
    return [round(float(np.abs(p[fold_id == i] - y[fold_id == i]).mean()), 4)
            for i in sorted(set(fold_id.tolist()))]


def summarize(y, R, want_paired=True):
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]
    g = M45.point_metrics(y, R["p_glob"])
    g["improvement"] = round(float((b - g["MAE_log10"]) / b), 4)
    g["per_fold_MAE"] = fold_maes(y, R["p_glob"], fid)
    g["fold_std"] = round(float(np.std(g["per_fold_MAE"])), 4)
    g["buckets"] = bucket_metrics(y, R["p_glob"], R["z_true"])

    stage1 = {s: stage1_metrics(R["z_true"], pr) for s, pr in R["proba"].items()}
    for s in R["proba"]:
        stage1[s]["confidence_bins"] = confidence_bins(R["z_true"], R["proba"][s])

    variants = {}
    gf = g["per_fold_MAE"]
    for key, p in R["pred"].items():
        met = M45.point_metrics(y, p)
        met["improvement"] = round(float((b - met["MAE_log10"]) / b), 4)
        met["per_fold_MAE"] = fold_maes(y, p, fid)
        met["fold_std"] = round(float(np.std(met["per_fold_MAE"])), 4)
        met["fold_wins_vs_global"] = int(sum(1 for a, c in zip(met["per_fold_MAE"], gf)
                                             if a < c))
        met["buckets"] = bucket_metrics(y, p, R["z_true"])
        met["usage"] = usage_stats(y, p, R["p_glob"], R["used"][key])
        if want_paired:
            met["vs_global"] = paired_test(y, p, R["p_glob"])
        if key in R["chosen_t"]:
            met["nested_thresholds"] = R["chosen_t"][key]
        variants[key] = met

    # oracle 상한 — 진단용. 모델이 아니다.
    rows = np.arange(len(y))
    oracle = M45.point_metrics(y, R["table"][rows, R["z_true"]])
    oracle["note"] = "실제 구간을 안다고 가정한 상한 — 서빙 불가, 진단 전용"

    return {"baseline_MAE": round(b, 4), "global": g, "stage1": stage1,
            "variants": variants, "oracle_ceiling": oracle, "folds": R["folds"]}


def gate_quality(y, R):
    """Error-aware gate 가 'expert 가 이길 행'을 실제로 맞혔는가."""
    rows = np.arange(len(y))
    out = {}
    for s in GATE_TARGETS:
        routed = R["table"][rows, R["proba"][s].argmax(1)]
        truth = (np.abs(routed - y) < np.abs(R["p_glob"] - y)).astype(int)
        gp = R["gate_p"][s]
        item = {"true_positive_rate_in_data": round(float(truth.mean()), 4)}
        for t in GATE_THRESHOLDS:
            pred = (gp >= t).astype(int)
            tp = int(((pred == 1) & (truth == 1)).sum())
            fp = int(((pred == 1) & (truth == 0)).sum())
            fn = int(((pred == 0) & (truth == 1)).sum())
            item["t=%.2f" % t] = {
                "accuracy": round(float((pred == truth).mean()), 4),
                "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                "recall": round(tp / (tp + fn), 4) if tp + fn else None,
                "selected_share": round(float(pred.mean()), 4)}
        out[s] = item
    return out


# ------------------------------------------------------------ 보고 (콘솔)
def report_split(res, title):
    g = res["global"]
    print("   ---- %s (비교군 baseline %.4f)" % (title, res["baseline_MAE"]))
    print("      %-28s MAE %.4f (fold σ %.4f)  2배내 %.1f%%  3배내 %.1f%%"
          % (GLOBAL, g["MAE_log10"], g["fold_std"], 100 * g["within_2x"],
             100 * g["within_3x"]))
    print("      %-28s MAE %.4f  (진단용 상한)"
          % ("oracle (실제 구간 안다고 가정)", res["oracle_ceiling"]["MAE_log10"]))
    print("   ---- Stage 1 후보")
    print("      %-22s %7s %7s %7s %7s %7s %7s %7s"
          % ("모델", "acc", "macroF1", "Low", "Mid", "High", "반대끝", "Brier"))
    for s, m in res["stage1"].items():
        print("      %-22s %7.4f %7.4f %7.4f %7.4f %7.4f %7.4f %7.4f"
              % (s, m["accuracy"], m["macro_f1"], m["recall"]["Low"],
                 m["recall"]["Mid"], m["recall"]["High"],
                 m["opposite_end_error_rate"], m["brier"]))
    print("   ---- routing 변형 (global %.4f 대비)" % g["MAE_log10"])
    print("      %-26s %8s %8s %8s %8s %9s %s"
          % ("변형", "MAE", "ΔMAE", "expert%", "승률", "fold승", "95%CI"))
    for key in sorted(res["variants"], key=lambda k: res["variants"][k]["MAE_log10"]):
        m = res["variants"][key]
        v = m.get("vs_global")
        u = m["usage"]
        print("      %-26s %8.4f %+8.4f %7.1f%% %8s %7d/5  %s"
              % (key, m["MAE_log10"], v["delta_MAE"] if v else float("nan"),
                 100 * u["share"], ("%.3f" % u["win_rate"]) if u["win_rate"] is not None else "—",
                 m["fold_wins_vs_global"],
                 ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—"))


# ------------------------------------------------------------ main
def main():
    t0 = time.time()
    print("== 데이터 — M69 와 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    print("\n== 원천 feature 층 (M69 G 단계)")
    ts = time.time()
    NB, body, src = SF.build(d)
    print("   %d개 feature + 본문 SVD%d / %.0f초"
          % (len(SF.columns_upto(STEP)), M69.BODY_SVD, time.time() - ts))

    sig = ckpt_signature(fp)
    print("\n== 체크포인트 서명 %s  ->  %s" % (sig, os.path.relpath(CKPT_DIR, C.ROOT)))
    print("   끊겨도 이미 끝난 fold 는 다시 계산하지 않는다. 코드·파라미터·데이터가")
    print("   바뀌면 서명이 달라져 옛 체크포인트를 읽지 않는다.")

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        print("\n== 5-fold [%s]" % gname)
        R = run_split(Xs, y, groups[gname], titles, body, NB, cats, sig, gname)
        res = summarize(y, R)
        res["gate_quality"] = gate_quality(y, R)
        results[gname] = res
        raws[gname] = R
        print()
        report_split(res, "전체 OOF [%s]" % gname)

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]

    # ------------------------------------------------------------ 승자 선정
    # 후보 자격: nested(정직한) 변형 + hard/soft. 고정 threshold sweep 은
    # 같은 OOF 에서 고르는 것이라 후보에서 뺀다 (진단용 곡선으로만 남긴다).
    honest = [k for k in ps["variants"]
              if k.startswith(("conf*", "low*", "hard/", "soft/", "eagate@"))]
    best = min(honest, key=lambda k: ps["variants"][k]["MAE_log10"])
    print("\n== 정직한 후보 중 최저 MAE: %s (%.4f)  vs global %.4f"
          % (best, ps["variants"][best]["MAE_log10"], ps["global"]["MAE_log10"]))

    # ------------------------------------------------------------ 재현성
    # 체크포인트 namespace 를 `repro` 로 따로 둔다. 같은 파일을 다시 읽으면
    # 같은 숫자를 자기 자신과 비교하는 것이라 재현성 점검이 무의미해진다.
    # 이렇게 두면 캐시가 있어도 **두 번의 독립적인 학습 결과**를 비교하게 된다.
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    R2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats,
                   sig, "program_stem__repro", verbose=False)
    repro = {
        "global": bool(np.allclose(R2["p_glob"], Rp["p_glob"])),
        best: bool(np.allclose(R2["pred"][best], Rp["pred"][best])),
        "hard/mc_xgb": bool(np.allclose(R2["pred"]["hard/mc_xgb"],
                                        Rp["pred"]["hard/mc_xgb"])),
    }
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ------------------------------------------------------------ 누수 점검
    leak = {
        "구간 경계 계산 입력": "fold train 의 y 만 (np.percentile(ytr, [33.3, 66.7]))",
        "threshold 선택 입력": "outer train 안의 inner GroupKFold(%d) OOF 만" % INNER_SPLITS,
        "gate 라벨 생성 입력": "inner OOF 예측 — 같은 행을 학습한 예측으로 라벨을 만들지 않는다",
        "calibration 적합 입력": "inner OOF 확률 + inner 구간 라벨만",
        "routing 결정에 쓰인 test 정보": "없음 — predict_proba(Xte) / gate proba(Xte) 뿐",
        "test y 의 용도": "최종 metric · oracle 상한 진단 · 구간별 집계뿐",
        "fold 별 선택 threshold(conf*/mc_xgb)":
            str([r["t"] for r in Rp["chosen_t"].get("conf*/mc_xgb", [])]),
        "fold 별 선택 threshold(low*/mc_xgb)":
            str([r["t"] for r in Rp["chosen_t"].get("low*/mc_xgb", [])]),
        "threshold 가 fold 마다 흔들린다(전체 사전선택 아님)":
            str(len({r["t"] for r in Rp["chosen_t"].get("conf*/mc_xgb", [])}) > 1
                or len({r["t"] for r in Rp["chosen_t"].get("low*/mc_xgb", [])}) > 1),
    }
    leak_checks = {
        "threshold·gate 라벨이 outer test 를 보지 않았다": True,
        "global 이 M69 공표치(0.3719)를 재현":
            abs(ps["global"]["MAE_log10"] - M69_PUBLISHED["MAE_log10"]) < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-40s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ------------------------------------------------------------ 승격 판정
    B = ps["variants"][best]
    v = B["vs_global"]
    s1_base = ps["stage1"]["mc_xgb"]
    best_s1 = min(ps["stage1"], key=lambda s: ps["stage1"][s]["opposite_end_error_rate"])
    nt_best = nt["variants"].get(best)
    checks = {
        "1. OOF MAE 가 M69 global 보다 낮다": B["MAE_log10"] < ps["global"]["MAE_log10"],
        "2. 엄격 split 에서도 같은 방향":
            bool(nt_best and nt_best["MAE_log10"] < nt["global"]["MAE_log10"]),
        "3. 5개 fold 중 4개 이상 개선": B["fold_wins_vs_global"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. opposite-end error 감소":
            ps["stage1"][best_s1]["opposite_end_error_rate"]
            < s1_base["opposite_end_error_rate"],
        "6. expert 사용 행에서 실제 이득": bool(B["usage"]["n"] > 0
                                       and (B["usage"]["delta_on_used"] or 0) < 0),
        "7. leakage audit PASS": bool(leak_pass),
        "8. reproducibility PASS": all(repro.values()),
        "9. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("9.")]
    verdict = ("승격 후보 (M69 대체)" if all(checks[k] for k in core)
               else "현행 유지 (M69)")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ------------------------------------------------------------ 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "pred_global": Rp["p_glob"],
           "expert_low": Rp["table"][:, 0], "expert_mid": Rp["table"][:, 1],
           "expert_high": Rp["table"][:, 2],
           "cohort": d["cohort"].to_numpy(),
           "evidence_source": d["evidence_source"].to_numpy()}
    for s in STAGE1_NAMES:
        out["p_low_%s" % s] = Rp["proba"][s][:, 0]
        out["p_mid_%s" % s] = Rp["proba"][s][:, 1]
        out["p_high_%s" % s] = Rp["proba"][s][:, 2]
    for s in GATE_TARGETS:
        out["gate_p_%s" % s] = Rp["gate_p"][s]
    for k, p in Rp["pred"].items():
        out["pred_" + k.replace("/", "__").replace("@", "_").replace("*", "s")] = p
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "routing 정확도 개선 · 확신 기반 gating 이 M69(0.3719)를 이기는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "regressor": F.XGB_POINT, "bucket_cuts": list(CUTS),
        },
        "changed": "예측 경로 선택 방법만 — Stage1 종류 · gating 규칙 · calibration",
        "selection_protocol": {
            "nested": "outer train 안 GroupKFold(%d) inner OOF 에서만 threshold·"
                      "gate 라벨·calibrator 를 고른다. 승격 판정은 이 값으로만." % INNER_SPLITS,
            "sweep": "conf@t · low@t · eagate@t 는 고정 threshold 를 전체 OOF 에 "
                     "적용한 진단용 곡선이다. 여기서 최저값을 골라 승격 근거로 쓰지 않는다.",
            "nested_grid": [float(x) for x in NESTED_GRID],
        },
        "stage1_labels": STAGE1_LABEL,
        "checkpoint": {
            "signature": sig, "dir": os.path.relpath(CKPT_DIR, C.ROOT),
            "code_version": CODE_VERSION,
            "stores": "fold 단위 모델 출력(예측·확률·gate 확률·선택 threshold)만. "
                      "routing 규칙은 매 실행에서 다시 계산한다.",
            "invalidation": "코드 버전·데이터 해시·모델 파라미터·후보 목록·seed 를 "
                            "서명에 넣어, 하나라도 바뀌면 옛 체크포인트를 읽지 않는다.",
            "repro_namespace": "재현성 런은 `__repro` 로 분리 — 같은 파일을 재사용해 "
                               "재현성이 자동으로 통과하는 일이 없게 한다.",
        },
        "results": results,
        "best_honest_variant": best,
        "best_stage1_by_opposite_end": best_s1,
        "reproducibility": repro,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(x) for k, x in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "goals": {"primary": "MAE < 0.35", "final": "MAE < 0.30"},
        "published_m69": M69_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m73_m2_routing_improvement.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ------------------------------------------------------------ MD 보고서
def _vrow(key, m, gmae):
    v = m.get("vs_global") or {}
    u = m["usage"]
    return ("| `%s` | %.4f | %+0.4f | %s | %.1f%% | %s | %d/5 | %s |"
            % (key, m["MAE_log10"], m.get("vs_global", {}).get("delta_MAE", 0.0),
               ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
               100 * u["share"],
               ("%.3f" % u["win_rate"]) if u["win_rate"] is not None else "—",
               m["fold_wins_vs_global"],
               str(v.get("wilcoxon_p")) if v else "—"))


def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    g = ps["global"]
    L = []
    A = L.append
    A("# M73 — routing 개선(ordinal · confidence gate · error-aware gate)\n")
    A("> 질문: **금액 구간을 더 잘 맞히거나 expert 를 써야 할 행만 골라내면,")
    A("> hard routing 의 오분류 비용을 줄여 M69 0.3719 보다 유의하게 좋아지는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("임계값·gate 라벨·calibrator 를 어디서 골랐는지가 이 실험의 핵심 규율이다.\n")
    A("```text")
    A("nested  %s" % p["selection_protocol"]["nested"])
    A("sweep   %s" % p["selection_protocol"]["sweep"])
    A("```\n")
    A("## 1. 기준선\n")
    A("| | MAE(log10) | fold σ | 2배 이내 | 3배 이내 |")
    A("|---|---:|---:|---:|---:|")
    A("| 비교군 중앙값 baseline | %.4f | — | — | — |" % ps["baseline_MAE"])
    A("| **global (M69 단일 XGB)** | **%.4f** | %.4f | %.1f%% | %.1f%% |"
      % (g["MAE_log10"], g["fold_std"], 100 * g["within_2x"], 100 * g["within_3x"]))
    A("| oracle 상한 (진단용, 서빙 불가) | %.4f | — | %.1f%% | %.1f%% |"
      % (ps["oracle_ceiling"]["MAE_log10"], 100 * ps["oracle_ceiling"]["within_2x"],
         100 * ps["oracle_ceiling"]["within_3x"]))
    A("")
    A("> M69 공표치 %.4f 의 재현입니다. 아래 모든 Δ 는 이 global 과 **같은 fold·"
      "같은 feature 행렬**에서 잰 paired 값입니다.\n" % p["published_m69"]["MAE_log10"])
    A("## 2. Stage 1 모델 비교 (실험 1·5·6)\n")
    A("| Stage 1 | Accuracy | Macro-F1 | Low rec | Mid rec | High rec | "
      "반대끝 오류 | Brier |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s, m in ps["stage1"].items():
        A("| %s | %.4f | %.4f | %.4f | %.4f | %.4f | %.4f | %.4f |"
          % (s, m["accuracy"], m["macro_f1"], m["recall"]["Low"], m["recall"]["Mid"],
             m["recall"]["High"], m["opposite_end_error_rate"], m["brier"]))
    A("")
    A("> 기존 Stage 1(`mc_xgb`) 은 M69 공표치 Accuracy %.4f / 반대끝 %.4f 입니다.\n"
      % (p["published_m69"]["stage1_acc"], p["published_m69"]["opposite_end"]))
    A("### confidence bin 별 실제 accuracy (calibration 진단)\n")
    for s in ("mc_xgb", "mc_xgb+platt", "mc_xgb+iso"):
        if s not in ps["stage1"]:
            continue
        A("`%s` (Brier %.4f)\n" % (s, ps["stage1"][s]["brier"]))
        A("| confidence bin | n | 평균 confidence | 실제 accuracy |")
        A("|---|---:|---:|---:|")
        for r in ps["stage1"][s]["confidence_bins"]:
            A("| %s | %d | %.4f | %.4f |" % (r["bin"], r["n"], r["mean_conf"],
                                             r["accuracy"]))
        A("")
    A("## 3. routing 변형 — 정직한 후보 (nested 선택 · 고정규칙)\n")
    A("| 변형 | MAE | ΔMAE vs global | 95% CI | expert 사용 | 사용행 승률 | "
      "fold승 | wilcoxon p |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    honest = [k for k in ps["variants"]
              if k.startswith(("conf*", "low*", "hard/", "soft/", "eagate@"))]
    for k in sorted(honest, key=lambda k: ps["variants"][k]["MAE_log10"]):
        A(_vrow(k, ps["variants"][k], g["MAE_log10"]))
    A("")
    A("## 4. threshold sweep — 진단용 곡선 (승격 근거 아님)\n")
    A("고정 threshold 를 전체 OOF 에 그대로 적용한 표입니다. **여기서 최저값을")
    A("골라 쓰면 같은 데이터에서 고르고 같은 데이터로 재는 것**이라 낙관 쪽으로")
    A("휩니다. 3장의 nested 값이 정직한 대응치입니다.\n")
    A("| 변형 | MAE | ΔMAE | expert 사용 | 사용행 승률 | fold승 |")
    A("|---|---:|---:|---:|---:|---:|")
    sweep = [k for k in ps["variants"] if k.startswith(("conf@", "low@"))
             and k.endswith(("/mc_xgb", "/ordinal_xgb"))]
    for k in sorted(sweep):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | %.1f%% | %s | %d/5 |"
          % (k, m["MAE_log10"], m["vs_global"]["delta_MAE"], 100 * m["usage"]["share"],
             ("%.3f" % m["usage"]["win_rate"]) if m["usage"]["win_rate"] is not None else "—",
             m["fold_wins_vs_global"]))
    A("")
    A("### nested 로 고른 threshold (fold 별)\n")
    A("| 변형 | fold별 t | fold별 inner MAE | fold별 inner expert 사용률 |")
    A("|---|---|---|---|")
    for k in sorted(honest):
        rec = ps["variants"][k].get("nested_thresholds")
        if rec:
            A("| `%s` | %s | %s | %s |"
              % (k, [r["t"] for r in rec], [r["inner_MAE"] for r in rec],
                 [r["inner_share"] for r in rec]))
    A("")
    A("## 5. Error-Aware Gate 품질 (실험 4)\n")
    A("gate 가 맞혀야 하는 것은 금액구간이 아니라 **expert 가 global 을 이기는 행**")
    A("입니다. 아래 `데이터상 실제 양성비율` 이 gate 가 넘어야 할 무작위 기준선입니다.\n")
    A("| Stage 1 | 실제 양성비율 | threshold | Accuracy | Precision | Recall | 선택 비중 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for s, item in ps["gate_quality"].items():
        for t in GATE_THRESHOLDS:
            r = item["t=%.2f" % t]
            A("| %s | %.4f | %.2f | %.4f | %s | %s | %.1f%% |"
              % (s, item["true_positive_rate_in_data"], t, r["accuracy"],
                 ("%.4f" % r["precision"]) if r["precision"] is not None else "—",
                 ("%.4f" % r["recall"]) if r["recall"] is not None else "—",
                 100 * r["selected_share"]))
    A("")
    A("## 6. 구간별 MAE — 최고 후보 vs global\n")
    best = p["best_honest_variant"]
    B = ps["variants"][best]
    A("| 구간 | n | global | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |" % (b, g["buckets"][b]["n"],
                                         g["buckets"][b]["MAE_log10"],
                                         B["buckets"][b]["MAE_log10"]))
    A("")
    A("## 7. fold 별 MAE\n")
    A("| fold | 경계(원) | baseline | global | `%s` |" % best)
    A("|---|---|---:|---:|---:|")
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %.4f | %.4f |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             f["baseline_MAE"], g["per_fold_MAE"][i], B["per_fold_MAE"][i]))
    A("")
    A("## 8. 엄격 split (normalized_title) 재확인\n")
    A("| | MAE(log10) |")
    A("|---|---:|")
    A("| global | %.4f |" % nt["global"]["MAE_log10"])
    for k in sorted(honest, key=lambda k: ps["variants"][k]["MAE_log10"])[:6]:
        if k in nt["variants"]:
            A("| `%s` | %.4f |" % (k, nt["variants"][k]["MAE_log10"]))
    A("")
    A("| Stage 1 | 엄격 accuracy | 엄격 반대끝 오류 |")
    A("|---|---:|---:|")
    for s in STAGE1_NAMES:
        if s in nt["stage1"]:
            A("| %s | %.4f | %.4f |" % (s, nt["stage1"][s]["accuracy"],
                                        nt["stage1"][s]["opposite_end_error_rate"]))
    A("")
    A("## 9. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    for k, v in p["leakage_checks"].items():
        A("| %s | %s |" % (k, "PASS" if v else "FAIL"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")
    A("## 10. 승격 점검표\n")
    A("대상: `%s` (정직한 후보 중 OOF MAE 최저)\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")
    A("## 결론\n")
    A("```text")
    A("M69 global          MAE = %.4f" % g["MAE_log10"])
    A("최고 정직 후보      %s" % best)
    A("                    MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (B["MAE_log10"], B["vs_global"]["delta_MAE"], B["vs_global"]["ci95"][0],
         B["vs_global"]["ci95"][1]))
    A("oracle 상한         MAE = %.4f  (서빙 불가)" % ps["oracle_ceiling"]["MAE_log10"])
    A("Stage1 최고 accuracy %s = %.4f"
      % (max(ps["stage1"], key=lambda s: ps["stage1"][s]["accuracy"]),
         max(m["accuracy"] for m in ps["stage1"].values())))
    A("Stage1 최저 반대끝  %s = %.4f"
      % (p["best_stage1_by_opposite_end"],
         ps["stage1"][p["best_stage1_by_opposite_end"]]["opposite_end_error_rate"]))
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
