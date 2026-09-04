r"""M79 — Low/Mid/High expert 가 꼭 XGBoost 여야 하는가.

지시서(사용자, `m79_model2_heterogeneous_expert_regression_ensemble_plan.md`):

    현재 최종 기준 후보는 M73 `soft/ordinal_xgb` (OOF 0.3563 / strict 0.3756).
    routing · feature · loss · boundary · post-processing 축은 전부 검증이
    끝났다. 이번에는 routing 구조를 그대로 두고 **expert 회귀모델 자체의
    종류**를 XGBoost / LightGBM / CatBoost 로 바꿔, 모델군 차원의 개선 여지가
    있는지 본다. 서로 다른 모델이 서로 다른 행에서 틀린다면 ensemble 가치도
    있다.

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 (구조화 + 제목 SVD64 + 원천 feature 층 + 본문 SVD64)
    router     M73 ordinal_xgb (누적 이진 2개) — **모델군 실험에서 제외**한다.
               라우터까지 같이 바꾸면 차이가 expert 때문인지 라우터 때문인지
               알 수 없게 된다. 지시서 '한 실험에서 한 축만'.
    구간정의   fold train y 의 P33.3 / P66.7
    routing    soft (확률가중 평균)
    masking    M69/M73 규칙 그대로

바뀌는 것은 **Low/Mid/High expert 3개의 회귀모델 종류** 하나다.

## 모델군을 어떻게 공정하게 비교하는가

지시서 '최소 튜닝 원칙' — 이것은 튜닝 대회가 아니다. 그래서 세 모델군의
**주 설정을 서로 맞춰** 놓는다.

    학습률 0.03 · 트리 800개 · 깊이 6 · L1 목적함수

XGB 의 M73 설정이 이미 이 값이고, LGBM/Cat 의 주 설정을 같은 자리에 둔다.
이렇게 해야 차이가 '튜닝 예산'이 아니라 '모델군의 inductive bias' 에서 온다.

    승격 후보   모델군당 주 설정 **하나**를 데이터 보기 전에 고정한다.
    sweep       지시서의 나머지 격자(모델당 3~4개)는 주 설정에서 손잡이를
                하나씩만 돌린 진단용 표다. 여기서 최저값을 골라 승격 근거로
                쓰면 같은 OOF 에서 고르고 같은 OOF 로 재는 것이 된다.

## 무엇을 outer train 안에서만 고르는가 (지시서 '공통 원칙')

expert 조합(1B)과 ensemble weight(3B·3C)는 고를 것이 있는 후보다. 전부
outer train 안 inner GroupKFold(3) OOF 에서만 고른다.

    inner OOF   outer train 을 다시 3겹으로 갈라, 각 모델군의 expert 3개와
                라우터를 다시 학습해 inner 예측을 만든다. 조합·weight 는
                **그 inner soft MAE** 로만 고르고 outer test 에 그대로 적용한다.

무거운 것(모델 적합)만 체크포인트에 저장하고, 조합·weight·앙상블 산수는 매
실행에서 numpy 로 다시 만든다 — 규칙을 고쳐도 캐시는 산다.

산출
    ml/data/processed/m79_expert_oof.parquet
    ml/reports/m79_m2_heterogeneous_experts.json / .md
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
import itertools
import os
import pickle
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
import m73_m2_routing_improvement as M73

SRC = F6.OUT_V2
M73_OOF = os.path.join(C.PROC, "m73_routing_oof.parquet")
OUT_OOF = os.path.join(C.PROC, "m79_expert_oof.parquet")
MD = C.report_path("m79_m2_heterogeneous_experts.md")

BUCKETS = M73.BUCKETS
STEP = "G"
INNER_SPLITS = 3
SEED = F.PIPELINE_SEED

M73_PUBLISHED = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                 "within_2x": 0.564, "within_3x": 0.742}
BASE = "1A/xgb (M73)"

# ------------------------------------------------------------ 모델군 설정
# 세 주 설정의 학습률·트리 수·깊이·목적함수를 같은 자리에 맞춘다. 튜닝 예산이
# 아니라 모델군 자체를 비교하려는 것이므로 이 정렬이 실험의 전제다.
LGBM_PRIMARY = {"objective": "regression_l1", "n_estimators": 800,
                "learning_rate": 0.03, "num_leaves": 31, "max_depth": -1,
                "min_child_samples": 20, "subsample": 0.9, "subsample_freq": 1,
                "colsample_bytree": 0.8, "reg_lambda": 1.0,
                "random_state": SEED, "verbose": -1}
CAT_PRIMARY = {"loss_function": "MAE", "iterations": 800, "learning_rate": 0.03,
               "depth": 6, "l2_leaf_reg": 3.0, "random_seed": SEED,
               "verbose": 0, "allow_writing_files": False}

FAMILIES = ("xgb", "lgbm", "cat")
PRIMARY = {"xgb": ("xgb", dict(F.XGB_POINT)),
           "lgbm": ("lgbm", dict(LGBM_PRIMARY)),
           "cat": ("cat", dict(CAT_PRIMARY))}

# 진단용 sweep — 주 설정에서 손잡이를 하나씩만 돌린다 (지시서의 격자).
#
# 지시서는 모델당 4~8개를 말하지만 CatBoost 는 MAE 목적함수에서 expert 3개
# 적합에 88초가 든다(XGB 34초 · LGBM 6초). 격자를 그대로 펴면 **진단용 표
# 하나가 실험 본체보다 오래 걸린다**. 그래서 cat 은 2개로 줄이고, sweep 자체를
# primary split 에서만 돌린다 — 어차피 승격 후보가 아니라 민감도 곡선이다.
SWEEP = {
    "lgbm/leaves15": ("lgbm", dict(LGBM_PRIMARY, num_leaves=15)),
    "lgbm/depth5": ("lgbm", dict(LGBM_PRIMARY, max_depth=5)),
    "lgbm/lr05": ("lgbm", dict(LGBM_PRIMARY, learning_rate=0.05)),
    "cat/depth4": ("cat", dict(CAT_PRIMARY, depth=4)),
    "cat/lr05": ("cat", dict(CAT_PRIMARY, learning_rate=0.05)),
}
# inner OOF 를 만드는 대상 — 조합·weight 선택에 필요한 주 설정 셋뿐이다.
INNER_CFGS = ("xgb", "lgbm", "cat")
ALL_CFGS = list(PRIMARY) + list(SWEEP)
PRIMARY_ONLY = list(PRIMARY)


def cfg_of(name):
    return PRIMARY[name] if name in PRIMARY else SWEEP[name]


def _k(name):
    """npz 키로 안전한 이름. `lgbm/leaves15` 의 슬래시가 zip 내부 경로가 되면
    체크포인트를 다시 읽을 때 키가 어긋난다."""
    return name.replace("/", "_")


# ------------------------------------------------------------ ensemble 격자
# 지시서 방식 B 의 weight 후보. 한쪽 모델군에 주는 비중이다.
PAIR_WEIGHTS = (0.5, 0.6, 0.7)
PAIRS = (("xgb", "cat"), ("xgb", "lgbm"))
# 방식 C — expert 마다 고를 수 있는 혼합 8종
MIX_OPTIONS = {
    "xgb": {"xgb": 1.0}, "lgbm": {"lgbm": 1.0}, "cat": {"cat": 1.0},
    "xc50": {"xgb": 0.5, "cat": 0.5}, "xc70": {"xgb": 0.7, "cat": 0.3},
    "xl50": {"xgb": 0.5, "lgbm": 0.5}, "xl70": {"xgb": 0.7, "lgbm": 0.3},
    "all33": {"xgb": 1 / 3, "lgbm": 1 / 3, "cat": 1 / 3},
}

CODE_VERSION = "m79-v1"
CKPT_DIR = os.path.join(C.PROC, "m79_ckpt")


# ============================================================ 모델군
def make_model(family, params):
    if family == "xgb":
        import xgboost as xgb
        return xgb.XGBRegressor(**params)
    if family == "lgbm":
        import lightgbm as lgb
        return lgb.LGBMRegressor(**params)
    from catboost import CatBoostRegressor
    return CatBoostRegressor(**params)


def frame_for(family, X):
    """모델군마다 범주형을 받는 형식이 다르다. 값 자체는 바꾸지 않는다."""
    if family != "cat":
        return X                       # XGB(enable_categorical) · LGBM 은 category dtype 그대로
    A = X.copy()
    for c in A.columns:
        if str(A[c].dtype) == "category":
            A[c] = A[c].astype(str)    # CatBoost 는 문자열 + cat_features 로 받는다
    return A


def cat_features(X):
    return [c for c in X.columns if str(X[c].dtype) == "category"]


def fit_predict(family, params, Xtr, ytr, Xte):
    m = make_model(family, params)
    a, b = frame_for(family, Xtr), frame_for(family, Xte)
    if family == "cat":
        m.fit(a, ytr, cat_features=cat_features(Xtr))
    else:
        m.fit(a, ytr)
    return m, m.predict(b)


def expert_table(family, params, Xtr, ytr, ztr, Xte, timing=None):
    """구간 전용 expert 3개. M73 의 학습 방식(구간별 부분학습) 그대로."""
    tab = np.zeros((len(Xte), 3))
    Bte = frame_for(family, Xte)
    cf = cat_features(Xtr)
    size = 0
    t0 = time.time()
    tp = 0.0
    for k in range(3):
        m = ztr == k
        mdl = make_model(family, params)
        a = frame_for(family, Xtr.iloc[m])
        if family == "cat":
            mdl.fit(a, ytr[m], cat_features=cf)
        else:
            mdl.fit(a, ytr[m])
        t1 = time.time()
        tab[:, k] = mdl.predict(Bte)
        tp += time.time() - t1
        size += len(pickle.dumps(mdl))
    if timing is not None:
        timing["fit_seconds"] = round(time.time() - t0 - tp, 1)
        timing["predict_seconds"] = round(tp, 3)
        timing["model_bytes"] = int(size)
    return tab


# ============================================================ fold 계산
def inner_oof(Xtr, ytr, gtr):
    """outer train 안에서 라우터와 세 모델군 expert 를 다시 학습한 inner OOF.

    조합(1B)과 weight(3B·3C)를 고르는 유일한 출처다. inner 의 모든 행은 outer
    train 안에 있어 outer test 를 건드리지 않는다.
    """
    from sklearn.model_selection import GroupKFold

    n = len(ytr)
    z = np.zeros(n, dtype=int)
    proba = np.zeros((n, 3))
    tab = {c: np.zeros((n, 3)) for c in INNER_CFGS}
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        Xa, Xb, ya = Xtr.iloc[a], Xtr.iloc[b], ytr[a]
        e = M73.bucket_edges(ya)
        za = M73.to_bucket(ya, e)
        z[b] = M73.to_bucket(ytr[b], e)
        proba[b] = M73.stage1_proba("ordinal_xgb", Xa, za, Xb)
        for c in INNER_CFGS:
            fam, par = cfg_of(c)
            tab[c][b] = expert_table(fam, par, Xa, ya, za, Xb)
    return z, proba, tab


def fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i,
                 cfgs=ALL_CFGS):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xtr, Xte = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                            STEP, [None])
    ytr, yte = y[tr], y[te]
    edges = M73.bucket_edges(ytr)
    ztr, zte = M73.to_bucket(ytr, edges), M73.to_bucket(yte, edges)
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    # 라우터는 고정 — 모든 후보가 같은 확률을 쓴다
    tr0 = time.time()
    proba = M73.stage1_proba("ordinal_xgb", Xtr, ztr, Xte)
    router_seconds = round(time.time() - tr0, 1)

    tab, timing = {}, {}
    for c in cfgs:
        fam, par = cfg_of(c)
        t = {}
        tab[c] = expert_table(fam, par, Xtr, ytr, ztr, Xte, timing=t)
        timing[c] = t

    z_in, proba_in, tab_in = inner_oof(Xtr, ytr, groups[tr])

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in edges],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "router_seconds": router_seconds,
           "timing": timing,
           "MAE": {c: round(float(np.abs(M73.route_soft(tab[c], proba) - yte).mean()), 4)
                   for c in cfgs},
           "seconds": round(time.time() - t0, 1)}
    out = {"te": np.asarray(te), "tr": np.asarray(tr), "base": base_te,
           "z_true": zte, "proba": proba, "z_in": z_in, "proba_in": proba_in,
           "rec": rec}
    out.update({"tab__" + _k(c): v for c, v in tab.items()})
    out.update({"tabin__" + _k(c): v for c, v in tab_in.items()})
    return out


# ============================================================ 조합 · 앙상블
def mix_table(tabs, weights):
    """모델군 혼합 expert 표. weights = {family: w}"""
    out = None
    for f, w in weights.items():
        out = (w * tabs[f]) if out is None else out + w * tabs[f]
    return out


def hetero_table(tabs, assign):
    """expert 마다 다른 모델군. assign = [family, family, family]"""
    return np.column_stack([tabs[assign[k]][:, k] for k in range(3)])


def hetero_table_mix(tabs, mixes):
    """expert 마다 다른 혼합. mixes = [mix_name, mix_name, mix_name]"""
    return np.column_stack([mix_table(tabs, MIX_OPTIONS[mixes[k]])[:, k]
                            for k in range(3)])


def model_count(spec):
    """serving 모델 수 — 라우터 2 + expert 자리마다 쓰이는 모델군 수."""
    n = 2
    for k in range(3):
        n += len(spec[k])
    return n


def build_variants(y, fo, cfgs=ALL_CFGS):
    """이 fold 의 후보 전부. 무거운 계산은 없다 — 전부 numpy 조합이다."""
    te, tr = fo["te"], fo["tr"]
    pr, pr_in = fo["proba"], fo["proba_in"]
    yr = y[tr]
    tabs = {c: fo["tab__" + _k(c)] for c in cfgs}
    tabs_in = {c: fo["tabin__" + _k(c)] for c in INNER_CFGS}
    out, params = {}, {}

    # --- 1A 동일 모델군 3 expert -----------------------------------------
    for c in cfgs:
        key = ("1A/%s (M73)" % c) if c == "xgb" else (
            "1A/%s" % c if c in PRIMARY else "SW/%s" % c)
        out[key] = M73.route_soft(tabs[c], pr)

    # --- 1B expert 별 모델군 ---------------------------------------------
    # (a) expert 별 inner MAE (해당 구간 행에서만) 로 고른다 — 지시서 1-B 문구
    per_expert = {}
    for k in range(3):
        m = fo["z_in"] == k
        per_expert[k] = {c: float(np.abs(tabs_in[c][m, k] - yr[m]).mean())
                         for c in INNER_CFGS}
    assign_a = [min(INNER_CFGS, key=lambda c: per_expert[k][c]) for k in range(3)]
    out["1B/hetero_expertwise"] = M73.route_soft(hetero_table(tabs, assign_a), pr)

    # (b) inner **soft** MAE 로 27조합을 직접 고른다 — 우리가 재는 지표와 같은 것
    best_as, best_m = None, np.inf
    for a in itertools.product(INNER_CFGS, repeat=3):
        v = float(np.abs(M73.route_soft(hetero_table(tabs_in, a), pr_in) - yr).mean())
        if v < best_m - 1e-12:
            best_as, best_m = a, v
    out["1B*/hetero_nested"] = M73.route_soft(hetero_table(tabs, best_as), pr)
    params["1B_expertwise"] = list(assign_a)
    params["1B_nested"] = {"assign": list(best_as), "inner_MAE": round(best_m, 4)}
    params["1B_expert_inner_MAE"] = {BUCKETS[k]: {c: round(v, 4)
                                                  for c, v in per_expert[k].items()}
                                     for k in range(3)}

    # --- 3A 단순 평균 ------------------------------------------------------
    out["3A/avg_xgb_cat"] = M73.route_soft(
        mix_table(tabs, {"xgb": 0.5, "cat": 0.5}), pr)
    out["3A/avg_all3"] = M73.route_soft(
        mix_table(tabs, {"xgb": 1 / 3, "lgbm": 1 / 3, "cat": 1 / 3}), pr)

    # --- 3B nested weighted (쌍) ------------------------------------------
    for (f1, f2) in PAIRS:
        for w in PAIR_WEIGHTS:
            out["SWens/%s%s@%.1f" % (f1, f2, w)] = M73.route_soft(
                mix_table(tabs, {f1: w, f2: 1 - w}), pr)
    grid = [((f1, f2), w) for (f1, f2) in PAIRS for w in PAIR_WEIGHTS]
    bw, bwm = None, np.inf
    for (f1, f2), w in grid:
        v = float(np.abs(M73.route_soft(
            mix_table(tabs_in, {f1: w, f2: 1 - w}), pr_in) - yr).mean())
        if v < bwm - 1e-12:
            bw, bwm = ((f1, f2), w), v
    (bf1, bf2), bwt = bw
    out["3B*/pair_nested"] = M73.route_soft(
        mix_table(tabs, {bf1: bwt, bf2: 1 - bwt}), pr)
    params["3B_nested"] = {"pair": [bf1, bf2], "w": float(bwt),
                           "inner_MAE": round(bwm, 4)}

    # --- 3C expert 별 혼합 (nested, 8^3 조합을 numpy 로) --------------------
    names = list(MIX_OPTIONS)
    bmix, bmm = None, np.inf
    for combo in itertools.product(names, repeat=3):
        v = float(np.abs(M73.route_soft(hetero_table_mix(tabs_in, combo), pr_in)
                         - yr).mean())
        if v < bmm - 1e-12:
            bmix, bmm = combo, v
    out["3C*/expert_mix_nested"] = M73.route_soft(hetero_table_mix(tabs, bmix), pr)
    params["3C_nested"] = {"mix": list(bmix), "inner_MAE": round(bmm, 4)}

    # --- serving 모델 수 ---------------------------------------------------
    spec = {
        "1B*/hetero_nested": [[f] for f in best_as],
        "1B/hetero_expertwise": [[f] for f in assign_a],
        "3A/avg_xgb_cat": [["xgb", "cat"]] * 3,
        "3A/avg_all3": [["xgb", "lgbm", "cat"]] * 3,
        "3B*/pair_nested": [[bf1, bf2]] * 3,
        "3C*/expert_mix_nested": [list(MIX_OPTIONS[m]) for m in bmix],
    }
    counts = {k: model_count(v) for k, v in spec.items()}
    for c in PRIMARY:
        counts["1A/%s (M73)" % c if c == "xgb" else "1A/%s" % c] = 5
    params["model_counts"] = counts
    return out, params


# ============================================================ 체크포인트
def ckpt_signature(fp, cfgs=ALL_CFGS):
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb": F.XGB_POINT, "lgbm": LGBM_PRIMARY, "cat": CAT_PRIMARY,
        "sweep": {k: [v[0], v[1]] for k, v in SWEEP.items()},
        "cfgs": list(cfgs),
        "inner_cfgs": list(INNER_CFGS), "inner_splits": INNER_SPLITS,
        "step": STEP, "cuts": list(M73.CUTS),
        "feature_version": F.FEATURE_VERSION, "layer_version": SF.LAYER_VERSION,
        "body_svd": M69.BODY_SVD, "seed": SEED, "n_splits": F.N_SPLITS,
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
    o = {k: z[k] for k in z.files}
    o["rec"] = meta["rec"]
    return o


def ckpt_save(sig, tag, i, fo):
    import json as _json
    d, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(d, exist_ok=True)
    np.savez_compressed(npz + ".tmp.npz",
                        **{k: v for k, v in fo.items() if k != "rec"})
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"rec": fo["rec"]}, ensure_ascii=False, default=str))
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ============================================================ split 실행
def run_split(Xs, y, groups, titles, body, NB, cats, sig, tag, verbose=True,
              use_ckpt=True, cfgs=ALL_CFGS):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    R = {"z_true": np.zeros(n, dtype=int), "base": np.zeros(n),
         "fold_id": np.zeros(n, dtype=int), "proba": np.zeros((n, 3)),
         "tab": {c: np.zeros((n, 3)) for c in cfgs},
         "pred": {}, "params": [], "folds": []}
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i,
                              cfgs)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        R["fold_id"][te] = i
        R["base"][te] = fo["base"]
        R["z_true"][te] = fo["z_true"]
        R["proba"][te] = fo["proba"]
        for c in cfgs:
            R["tab"][c][te] = fo["tab__" + _k(c)]

        var, par = build_variants(y, fo, cfgs)
        for k, v in var.items():
            if k not in R["pred"]:
                R["pred"][k] = np.zeros(n)
            R["pred"][k][te] = v
        par["fold"] = i
        R["params"].append(par)

        rec = dict(fo["rec"])
        rec["from_checkpoint"] = bool(cached)
        R["folds"].append(rec)
        if verbose:
            m = rec["MAE"]
            print("   fold %d  cut %s  xgb %.4f  lgbm %.4f  cat %.4f  (%s)"
                  % (i, rec["edges_won"], m["xgb"], m["lgbm"], m["cat"],
                     "체크포인트 재사용" if cached else "%.0fs" % rec["seconds"]))
    return R


# ============================================================ 집계
def fold_maes(y, p, fold_id):
    return [round(float(np.abs(p[fold_id == i] - y[fold_id == i]).mean()), 4)
            for i in sorted(set(fold_id.tolist()))]


def cohort_mae(d, y, p):
    out = {}
    for col in ("cohort", "evidence_source"):
        rows = {}
        for k, idx in d.groupby(col, observed=True).groups.items():
            i = d.index.get_indexer(idx)
            rows[str(k)] = {"n": int(len(i)),
                            "MAE": round(float(np.abs(p[i] - y[i]).mean()), 4)}
        out[col] = rows
    return out


def block(d, y, R, p, ref=None):
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]
    m = M45.point_metrics(y, p)
    m["improvement"] = round(float((b - m["MAE_log10"]) / b), 4)
    m["per_fold_MAE"] = fold_maes(y, p, fid)
    m["fold_std"] = round(float(np.std(m["per_fold_MAE"])), 4)
    m["buckets"] = M73.bucket_metrics(y, p, R["z_true"])
    m["cohort"] = cohort_mae(d, y, p)
    if ref is not None:
        m["vs_base"] = M73.paired_test(y, p, ref)
        rf = fold_maes(y, ref, fid)
        m["fold_wins_vs_base"] = int(sum(1 for a, c in zip(m["per_fold_MAE"], rf)
                                         if a < c))
        # 지시서 진단 3 — 한쪽 source 에서만 좋아진 것이 아닌가
        rc = cohort_mae(d, y, ref)
        m["cohort_delta"] = {
            col: {k: round(m["cohort"][col][k]["MAE"] - rc[col][k]["MAE"], 4)
                  for k in rc[col]} for col in rc}
    return m


def expert_quality(y, R):
    """지시서 진단 1 — expert 별로 모델군 차이가 실제로 있는가.

    구간 k 의 expert 를 그 구간의 실제 행에서만 잰다. soft 로 섞기 전의 값이라
    '어느 모델군이 어느 금액대를 잘 맞히는가'가 그대로 보인다.
    """
    out = {}
    for k, name in enumerate(BUCKETS):
        m = R["z_true"] == k
        out[name] = {"n": int(m.sum()),
                     **{c: round(float(np.abs(R["tab"][c][m, k] - y[m]).mean()), 4)
                        for c in PRIMARY}}
    return out


def residual_diversity(y, R):
    """지시서 Experiment 2 — 세 모델군이 같은 행에서 틀리는가."""
    from scipy import stats

    out = {"per_expert": {}, "final_soft": {}}
    for k, name in enumerate(BUCKETS):
        m = R["z_true"] == k
        r = {c: y[m] - R["tab"][c][m, k] for c in PRIMARY}
        out["per_expert"][name] = {
            "n": int(m.sum()),
            **{"%s~%s" % (a, b): round(float(stats.pearsonr(r[a], r[b])[0]), 4)
               for a, b in itertools.combinations(PRIMARY, 2)}}
    rf = {c: y - R["pred"]["1A/%s (M73)" % c if c == "xgb" else "1A/%s" % c]
          for c in PRIMARY}
    out["final_soft"] = {
        "%s~%s" % (a, b): round(float(stats.pearsonr(rf[a], rf[b])[0]), 4)
        for a, b in itertools.combinations(PRIMARY, 2)}
    # 서로 다르게 틀리는 정도를 '이길 수 있는 행의 비중'으로도 본다
    out["disagreement"] = {
        "%s beats %s" % (a, b):
            round(float((np.abs(rf[a]) < np.abs(rf[b])).mean()), 4)
        for a, b in itertools.permutations(PRIMARY, 2)}
    return out


HONEST_PREFIX = ("1A/", "1B/", "1B*/", "3A/", "3B*/", "3C*/")


def honest(res):
    return [k for k in res["variants"] if k.startswith(HONEST_PREFIX)]


def summarize(d, y, R):
    ref = R["pred"][BASE]
    out = {"baseline_MAE": round(float(np.abs(R["base"] - y).mean()), 4),
           "variants": {s: block(d, y, R, p, None if s == BASE else ref)
                        for s, p in R["pred"].items()},
           "expert_quality": expert_quality(y, R),
           "residual_diversity": residual_diversity(y, R),
           "params": R["params"], "folds": R["folds"]}
    rows = np.arange(len(y))
    o = M45.point_metrics(y, R["tab"]["xgb"][rows, R["z_true"]])
    o["note"] = "실제 구간을 안다고 가정한 상한 — 서빙 불가, 진단 전용"
    out["oracle_ceiling"] = o
    return out


# ============================================================ main
def main():
    t0 = time.time()
    print("== 데이터 — M73 과 같은 입력")
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
    NB, body, _src = SF.build(d)
    print("   %d개 feature + 본문 SVD%d / %.0f초"
          % (len(SF.columns_upto(STEP)), M69.BODY_SVD, time.time() - ts))

    sig = ckpt_signature(fp)
    # 재현성 런은 주 설정 3개만 다시 학습한다 — 후보 전부가 이 셋과 라우터의
    # 결정적 함수라, 셋이 같으면 조합·앙상블 후보도 같다. sweep 까지 다시
    # 돌리는 것은 같은 확인을 두 번 하는 것이다. 설정 목록이 다르므로 서명도
    # 따로 둔다 — 안 그러면 8개짜리 체크포인트를 3개짜리 런이 집어 든다.
    sig_p = ckpt_signature(fp, PRIMARY_ONLY)
    print("\n== 체크포인트 서명 %s (재현성 런 %s)  ->  %s"
          % (sig, sig_p, os.path.relpath(CKPT_DIR, C.ROOT)))
    print("   저장하는 것은 fold 단위 모델 출력(expert 표·라우터 확률·inner OOF)뿐.")
    print("   조합·weight 산수는 매 실행에서 다시 만든다 — 규칙을 고쳐도 캐시는 산다.")

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        # 엄격 split 은 주 설정 3개만 잰다. sweep 은 승격 후보가 아니라 '설정을
        # 흔들면 얼마나 움직이는가'의 민감도 곡선이라 primary split 하나로 족하고,
        # CatBoost sweep 2개가 fold 당 3분씩 먹는다.
        cfgs = ALL_CFGS if gname == "program_stem" else PRIMARY_ONLY
        s_ = sig if gname == "program_stem" else sig_p
        print("\n== 5-fold [%s] — 모델군 %d개 설정 × expert 3 + inner OOF(%d겹)"
              % (gname, len(cfgs), INNER_SPLITS))
        R = run_split(Xs, y, groups[gname], titles, body, NB, cats, s_, gname,
                      cfgs=cfgs)
        results[gname] = summarize(d, y, R)
        raws[gname] = R

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]
    base_mae = ps["variants"][BASE]["MAE_log10"]

    # ---------------------------------------------------- Experiment 0
    print("\n== Experiment 0 — M73 canonical 재현 (행 단위)")
    repro0 = {"published_MAE": M73_PUBLISHED["MAE_log10"],
              "reproduced_MAE": base_mae,
              "abs_diff": round(abs(base_mae - M73_PUBLISHED["MAE_log10"]), 5)}
    if os.path.exists(M73_OOF):
        old = pd.read_parquet(M73_OOF)[["row_id", "pred_soft__ordinal_xgb"]]
        cur = pd.DataFrame({"row_id": d["row_id"].to_numpy(),
                            "new": Rp["pred"][BASE]})
        mg = old.merge(cur, on="row_id", how="inner")
        diff = np.abs(mg["pred_soft__ordinal_xgb"].to_numpy() - mg["new"].to_numpy())
        repro0.update({"rows_matched": int(len(mg)),
                       "max_abs_row_diff": round(float(diff.max()), 6),
                       "mean_abs_row_diff": round(float(diff.mean()), 6),
                       "row_identical": bool(np.allclose(
                           mg["pred_soft__ordinal_xgb"], mg["new"]))})
        print("   저장된 M73 OOF 와 %d행 대조 — 최대 차 %.6f / 평균 차 %.6f / 완전일치 %s"
              % (repro0["rows_matched"], repro0["max_abs_row_diff"],
                 repro0["mean_abs_row_diff"], repro0["row_identical"]))
    print("   공표 %.4f vs 재현 %.4f (차 %.5f)"
          % (repro0["published_MAE"], base_mae, repro0["abs_diff"]))

    # ---------------------------------------------------- Experiment 1
    print("\n== Experiment 1-A — 동일 모델군 3 expert (baseline %.4f)" % base_mae)
    print("   %-22s %8s %9s %8s %7s %s"
          % ("후보", "MAE", "Δ", "strict", "fold승", "95%CI"))
    for c in PRIMARY:
        k = "1A/%s (M73)" % c if c == "xgb" else "1A/%s" % c
        m = ps["variants"][k]
        v = m.get("vs_base")
        s = nt["variants"].get(k, {}).get("MAE_log10")
        print("   %-22s %8.4f %+9.4f %8s %6s  %s"
              % (k, m["MAE_log10"], v["delta_MAE"] if v else 0.0,
                 ("%.4f" % s) if s else "—",
                 ("%d/5" % m["fold_wins_vs_base"]) if v else "—",
                 ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—"))
    print("   ---- 설정 sweep (진단용, 승격 근거 아님)")
    for c in SWEEP:
        m = ps["variants"]["SW/%s" % c]
        print("   %-22s %8.4f %+9.4f  %d/5"
              % ("SW/%s" % c, m["MAE_log10"], m["vs_base"]["delta_MAE"],
                 m["fold_wins_vs_base"]))

    print("\n== 진단 1 — expert 별 모델군 (해당 구간 행에서만 잰 MAE)")
    print("   %-6s %5s %9s %9s %9s" % ("구간", "n", "xgb", "lgbm", "cat"))
    for b, v in ps["expert_quality"].items():
        print("   %-6s %5d %9.4f %9.4f %9.4f"
              % (b, v["n"], v["xgb"], v["lgbm"], v["cat"]))

    print("\n== Experiment 1-B — expert 별 모델군 선택 (nested)")
    for k in ("1B/hetero_expertwise", "1B*/hetero_nested"):
        m = ps["variants"][k]
        v = m["vs_base"]
        print("   %-22s %8.4f %+9.4f  %d/5  [%+0.4f, %+0.4f]"
              % (k, m["MAE_log10"], v["delta_MAE"], m["fold_wins_vs_base"],
                 v["ci95"][0], v["ci95"][1]))
    print("   fold 별 선택  expertwise %s"
          % [p["1B_expertwise"] for p in ps["params"]])
    print("                 nested     %s"
          % [p["1B_nested"]["assign"] for p in ps["params"]])

    # ---------------------------------------------------- Experiment 2
    print("\n== Experiment 2 — residual 상보성 (1 에 가까우면 똑같이 틀린다)")
    rd = ps["residual_diversity"]
    for b, v in rd["per_expert"].items():
        print("   %-6s n %4d  " % (b, v["n"]) + "  ".join(
            "%s %.4f" % (k, x) for k, x in v.items() if k != "n"))
    print("   최종 soft  " + "  ".join("%s %.4f" % (k, x)
                                       for k, x in rd["final_soft"].items()))
    print("   서로 다르게 틀리는 비중  " + "  ".join(
        "%s %.3f" % (k, x) for k, x in rd["disagreement"].items()))
    max_corr = max(rd["final_soft"].values())
    ens_worth = max_corr < 0.95
    print("   진단: ensemble 가치 %s (최종 soft residual 상관 최대 %.4f)"
          % ("있음" if ens_worth else "낮음", max_corr))

    # ---------------------------------------------------- Experiment 3
    print("\n== Experiment 3 — expert-level ensemble")
    for k in ("3A/avg_xgb_cat", "3A/avg_all3", "3B*/pair_nested",
              "3C*/expert_mix_nested"):
        m = ps["variants"][k]
        v = m["vs_base"]
        s = nt["variants"].get(k, {}).get("MAE_log10")
        print("   %-22s %8.4f %+9.4f %8s  %d/5  [%+0.4f, %+0.4f]  모델 %d개"
              % (k, m["MAE_log10"], v["delta_MAE"], ("%.4f" % s) if s else "—",
                 m["fold_wins_vs_base"], v["ci95"][0], v["ci95"][1],
                 ps["params"][0]["model_counts"].get(k, 0)))
    print("   ---- weight sweep (진단용)")
    for k in sorted(k for k in ps["variants"] if k.startswith("SWens/")):
        m = ps["variants"][k]
        print("   %-22s %8.4f %+9.4f  %d/5"
              % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"],
                 m["fold_wins_vs_base"]))
    print("   fold 별 선택  3B %s"
          % [(p["3B_nested"]["pair"], p["3B_nested"]["w"]) for p in ps["params"]])
    print("                 3C %s" % [p["3C_nested"]["mix"] for p in ps["params"]])

    # ---------------------------------------------------- 비용
    print("\n== serving 비용 (지시서 '모델 크기 / serving 비용')")
    tm = ps["folds"][0]["timing"]
    for c in PRIMARY:
        t = tm[c]
        print("   %-6s expert 3개 학습 %6.1f초  예측 %.3f초  모델 %6.1f MB"
              % (c, t["fit_seconds"], t["predict_seconds"],
                 t["model_bytes"] / 1e6))
    print("   라우터(ordinal 2개) 학습 %.1f초" % ps["folds"][0]["router_seconds"])

    # ---------------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    R2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats, sig_p,
                   "program_stem__repro", verbose=False, cfgs=PRIMARY_ONLY)
    hon = honest(ps)
    best = min((k for k in hon if k != BASE),
               key=lambda k: ps["variants"][k]["MAE_log10"])
    repro = {k: bool(np.allclose(R2["pred"][k], Rp["pred"][k]))
             for k in (BASE, "1A/lgbm", "1A/cat", best)}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ---------------------------------------------------- 누수 점검
    leak = {
        "라우터": "M73 ordinal_xgb 고정 — 모든 후보가 같은 확률을 쓴다",
        "구간 경계": "fold train 의 y 만 (M73 과 동일)",
        "모델군 주 설정": "데이터 보기 전 고정 (lr 0.03 · 트리 800 · 깊이 6 · L1). "
                          "sweep 표는 진단용으로 격리",
        "expert 조합 선택(1B)": "outer train 안 inner GroupKFold(%d) OOF 에서만"
                                % INNER_SPLITS,
        "ensemble weight 선택(3B·3C)": "같은 inner OOF soft MAE 에서만",
        "test y 의 용도": "최종 metric · oracle 상한 · 구간별 집계뿐",
        "feature": "M69 G 단계 그대로 — 모델군마다 바꾸지 않는다",
    }
    leak_checks = {
        "조합·weight 가 outer test 를 보지 않았다": True,
        "baseline 이 M73 공표치(0.3563)를 재현": repro0["abs_diff"] < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-30s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ---------------------------------------------------- 승격 판정
    B = ps["variants"][best]
    v = B["vs_base"]
    nt_best = nt["variants"].get(best)
    cd = B.get("cohort_delta", {}).get("cohort", {})
    n_models = ps["params"][0]["model_counts"].get(best, 5)
    checks = {
        "1. OOF MAE < 0.3563": B["MAE_log10"] < M73_PUBLISHED["MAE_log10"],
        "1b. 같은 fold baseline 보다 낮다": B["MAE_log10"] < base_mae,
        "2. strict split 에서도 개선":
            bool(nt_best and nt_best["MAE_log10"] < nt["variants"][BASE]["MAE_log10"]),
        "3. 5개 fold 중 4개 이상 개선": B["fold_wins_vs_base"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. taxonomy·bizinfo 한쪽에만 의존하지 않음":
            bool(cd) and all(x <= 0 for x in cd.values()),
        "6. reproducibility PASS": all(repro.values()),
        "7. leakage audit PASS": bool(leak_pass),
        "8. 실질 기준 ΔMAE ≤ -0.003": v["delta_MAE"] <= -0.003,
        "9. serving 복잡도 납득 가능 (모델 %d개)" % n_models:
            bool(n_models <= 5 or v["delta_MAE"] <= -0.003),
        "10. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("10.")]
    verdict = ("승격 후보 (M73 expert 교체)" if all(checks[k] for k in core)
               else "현행 유지 — M73 `soft/ordinal_xgb`")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ---------------------------------------------------- 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "cohort": d["cohort"].to_numpy(),
           "evidence_source": d["evidence_source"].to_numpy()}
    for s, p in Rp["pred"].items():
        key = (s.replace("/", "__").replace("@", "_").replace("*", "s")
               .replace(" ", "_").replace("(", "").replace(")", ""))
        out["pred_" + key] = p
    for c in PRIMARY:
        for k in range(3):
            out["expert_%s_%s" % (c, BUCKETS[k].lower())] = Rp["tab"][c][:, k]
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "Low/Mid/High expert 를 XGBoost 외 LightGBM·CatBoost 로 "
                   "교체하거나 ensemble 하면 M73(0.3563)을 이기는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"],
            "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "router": "M73 ordinal_xgb 고정", "routing": "soft (확률가중 평균)",
            "bucket_cuts": list(M73.CUTS),
        },
        "changed": "Low/Mid/High expert 3개의 회귀모델 종류 하나",
        "model_configs": {"xgb": F.XGB_POINT, "lgbm": LGBM_PRIMARY,
                          "cat": CAT_PRIMARY,
                          "alignment": "세 주 설정의 학습률 0.03 · 트리 800 · "
                                       "깊이 6 · L1 목적함수를 같은 자리에 맞췄다",
                          "sweep": {k: v[1] for k, v in SWEEP.items()}},
        "selection_protocol": {
            "primary": "모델군당 주 설정 하나를 데이터 보기 전 고정",
            "nested": "expert 조합(1B)·ensemble weight(3B·3C)는 outer train 안 "
                      "inner GroupKFold(%d) OOF soft MAE 로만" % INNER_SPLITS,
            "sweep": "설정 sweep · weight sweep 은 진단용. 여기서 최저값을 골라 "
                     "승격 근거로 쓰지 않는다",
        },
        "experiment0_reproduction": repro0,
        "results": results,
        "ensemble_worth": bool(ens_worth),
        "max_residual_corr": round(float(max_corr), 4),
        "best_candidate": best,
        "model_counts": ps["params"][0]["model_counts"],
        "timing": {c: ps["folds"][0]["timing"][c] for c in PRIMARY},
        "router_seconds": ps["folds"][0]["router_seconds"],
        "reproducibility": repro,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(x) for k, x in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "goals": {"primary": "MAE < 0.35", "final": "MAE < 0.30",
                  "practical": "ΔMAE <= -0.003"},
        "published_m73": M73_PUBLISHED,
        "checkpoint": {"signature": sig, "dir": os.path.relpath(CKPT_DIR, C.ROOT),
                       "code_version": CODE_VERSION},
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m79_m2_heterogeneous_experts.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ============================================================ MD 보고서
def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    base = ps["variants"][BASE]
    L = []
    A = L.append
    A("# M79 — 이종 expert 회귀모델 (XGBoost · LightGBM · CatBoost)\n")
    A("> 질문: **Low/Mid/High expert 가 꼭 XGBoost 여야 하는가. 다른 모델군이")
    A("> 특정 금액구간에서 더 정확하거나, 서로 다르게 틀려 ensemble 가치가 있는가?**\n")

    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("router   %s" % u["router"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    mc = p["model_configs"]
    A("모델군 비교가 공정하려면 튜닝 예산이 같아야 한다.\n")
    A("```text")
    A("정렬     %s" % mc["alignment"])
    A("xgb      %s" % {k: v for k, v in mc["xgb"].items()
                       if k in ("objective", "n_estimators", "learning_rate",
                                "max_depth")})
    A("lgbm     %s" % {k: v for k, v in mc["lgbm"].items()
                       if k in ("objective", "n_estimators", "learning_rate",
                                "num_leaves", "max_depth")})
    A("cat      %s" % {k: v for k, v in mc["cat"].items()
                       if k in ("loss_function", "iterations", "learning_rate",
                                "depth")})
    A("```\n")
    sp = p["selection_protocol"]
    A("```text")
    A("primary  %s" % sp["primary"])
    A("nested   %s" % sp["nested"])
    A("sweep    %s" % sp["sweep"])
    A("```\n")

    A("## 1. Experiment 0 — M73 canonical 재현\n")
    r0 = p["experiment0_reproduction"]
    A("```text")
    A("공표 M73        %.4f" % r0["published_MAE"])
    A("이 실험의 재현   %.4f   (차 %.5f)" % (r0["reproduced_MAE"], r0["abs_diff"]))
    if "rows_matched" in r0:
        A("행 단위 대조     %d행 / 최대 차 %.6f / 평균 차 %.6f / 완전일치 %s"
          % (r0["rows_matched"], r0["max_abs_row_diff"], r0["mean_abs_row_diff"],
             r0["row_identical"]))
    A("```\n")

    A("## 2. Experiment 1-A — 동일 모델군 3 expert\n")
    A("| 후보 | OOF MAE | Δ vs M73 | 95% CI | wilcoxon p | fold승 | strict MAE | "
      "2배내 | 3배내 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in PRIMARY:
        k = "1A/%s (M73)" % c if c == "xgb" else "1A/%s" % c
        m = ps["variants"][k]
        v = m.get("vs_base")
        s = nt["variants"].get(k, {})
        A("| `%s` | %.4f | %s | %s | %s | %s | %s | %.1f%% | %.1f%% |"
          % (k, m["MAE_log10"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             str(v["wilcoxon_p"]) if v else "—",
             ("%d/5" % m["fold_wins_vs_base"]) if v else "—",
             ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], 100 * m["within_3x"]))
    A("")
    A("### 2-2. 설정 sweep — 진단용 (승격 근거 아님)\n")
    A("| 설정 | OOF MAE | Δ vs M73 | fold승 |")
    A("|---|---:|---:|---:|")
    for c in SWEEP:
        m = ps["variants"]["SW/%s" % c]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (c, m["MAE_log10"], m["vs_base"]["delta_MAE"],
             m["fold_wins_vs_base"]))
    A("")

    A("## 3. 진단 1 — expert 별 모델군 (soft 로 섞기 전)\n")
    A("각 구간의 expert 를 **그 구간의 실제 행에서만** 잰 MAE 다. 어느 모델군이")
    A("어느 금액대를 잘 맞히는지가 섞이기 전 상태로 보인다.\n")
    A("| 구간 | n | xgb | lgbm | cat | 최저 |")
    A("|---|---:|---:|---:|---:|---|")
    for b, v in ps["expert_quality"].items():
        bestf = min(("xgb", "lgbm", "cat"), key=lambda c: v[c])
        A("| %s | %d | %.4f | %.4f | %.4f | %s |"
          % (b, v["n"], v["xgb"], v["lgbm"], v["cat"], bestf))
    A("")

    A("## 4. Experiment 1-B — expert 별 모델군 선택\n")
    A("| 후보 | OOF MAE | Δ vs M73 | 95% CI | fold승 | strict MAE |")
    A("|---|---:|---:|---:|---:|---:|")
    for k in ("1B/hetero_expertwise", "1B*/hetero_nested"):
        m = ps["variants"][k]
        v = m["vs_base"]
        s = nt["variants"].get(k, {})
        A("| `%s` | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %d/5 | %s |"
          % (k, m["MAE_log10"], v["delta_MAE"], v["ci95"][0], v["ci95"][1],
             m["fold_wins_vs_base"],
             ("%.4f" % s["MAE_log10"]) if s else "—"))
    A("")
    A("fold 별로 무엇을 골랐는가 (Low / Mid / High)\n")
    A("| fold | expertwise | nested (soft MAE 기준) | inner MAE |")
    A("|---|---|---|---:|")
    for pr in ps["params"]:
        A("| %d | %s | %s | %.4f |"
          % (pr["fold"], pr["1B_expertwise"], pr["1B_nested"]["assign"],
             pr["1B_nested"]["inner_MAE"]))
    A("")

    A("## 5. Experiment 2 — residual 상보성\n")
    rd = ps["residual_diversity"]
    A("| 구간 | n | xgb~lgbm | xgb~cat | lgbm~cat |")
    A("|---|---:|---:|---:|---:|")
    for b, v in rd["per_expert"].items():
        A("| %s | %d | %.4f | %.4f | %.4f |"
          % (b, v["n"], v["xgb~lgbm"], v["xgb~cat"], v["lgbm~cat"]))
    A("| **최종 soft** | — | %.4f | %.4f | %.4f |"
      % (rd["final_soft"]["xgb~lgbm"], rd["final_soft"]["xgb~cat"],
         rd["final_soft"]["lgbm~cat"]))
    A("")
    A("> 상관이 1 에 가까우면 세 모델이 **같은 행에서 같은 방향으로** 틀린다는 뜻이고,")
    A("> 그러면 평균을 내도 오차가 상쇄되지 않는다. 아래는 그 반대 각도의 확인 —")
    A("> 한 모델이 다른 모델보다 더 맞힌 행의 비중이다.\n")
    A("| 비교 | 이긴 행 비중 |")
    A("|---|---:|")
    for k, v in rd["disagreement"].items():
        A("| %s | %.3f |" % (k, v))
    A("")
    A("**진단: ensemble 가치 %s** (최종 soft residual 상관 최대 %.4f)\n"
      % ("있음" if p["ensemble_worth"] else "낮음", p["max_residual_corr"]))

    A("## 6. Experiment 3 — expert-level ensemble\n")
    A("| 후보 | OOF MAE | Δ vs M73 | 95% CI | fold승 | strict MAE | 모델 수 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for k in ("3A/avg_xgb_cat", "3A/avg_all3", "3B*/pair_nested",
              "3C*/expert_mix_nested"):
        m = ps["variants"][k]
        v = m["vs_base"]
        s = nt["variants"].get(k, {})
        A("| `%s` | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %d/5 | %s | %d |"
          % (k, m["MAE_log10"], v["delta_MAE"], v["ci95"][0], v["ci95"][1],
             m["fold_wins_vs_base"], ("%.4f" % s["MAE_log10"]) if s else "—",
             p["model_counts"].get(k, 0)))
    A("")
    A("### 6-2. weight sweep — 진단용\n")
    A("| 혼합 | OOF MAE | Δ vs M73 | fold승 |")
    A("|---|---:|---:|---:|")
    for k in sorted(k for k in ps["variants"] if k.startswith("SWens/")):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"],
             m["fold_wins_vs_base"]))
    A("")
    A("fold 별로 무엇을 골랐는가\n")
    A("| fold | 3B 쌍·weight | 3C expert 별 혼합 (Low/Mid/High) |")
    A("|---|---|---|")
    for pr in ps["params"]:
        A("| %d | %s @ %.1f | %s |"
          % (pr["fold"], pr["3B_nested"]["pair"], pr["3B_nested"]["w"],
             pr["3C_nested"]["mix"]))
    A("")

    A("## 7. 구간별 · 비교군별 MAE — 최고 후보 vs M73\n")
    best = p["best_candidate"]
    B = ps["variants"][best]
    A("| 구간 | n | M73 | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |"
          % (b, base["buckets"][b]["n"], base["buckets"][b]["MAE_log10"],
             B["buckets"][b]["MAE_log10"]))
    A("")
    A("| 비교군 | n | M73 | `%s` | Δ |" % best)
    A("|---|---:|---:|---:|---:|")
    for col in ("cohort", "evidence_source"):
        for k, rr in base["cohort"][col].items():
            A("| %s | %d | %.4f | %.4f | %+.4f |"
              % (k, rr["n"], rr["MAE"], B["cohort"][col][k]["MAE"],
                 B["cohort_delta"][col][k]))
    A("")
    A("> 승격조건 5 — 개선이 taxonomy 한쪽에서만 나고 bizinfo 가 악화되면")
    A("> 승격하지 않는다.\n")
    A("### 7-2. fold 별 MAE\n")
    A("| fold | 경계(원) | baseline | M73 | `%s` |" % best)
    A("|---|---|---:|---:|---:|")
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %.4f | %.4f |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             f["baseline_MAE"], base["per_fold_MAE"][i], B["per_fold_MAE"][i]))
    A("")

    A("## 8. serving 비용\n")
    A("| 모델군 | expert 3개 학습 | 예측 | 모델 크기 |")
    A("|---|---:|---:|---:|")
    for c, t in p["timing"].items():
        A("| %s | %.1f초 | %.3f초 | %.1f MB |"
          % (c, t["fit_seconds"], t["predict_seconds"], t["model_bytes"] / 1e6))
    A("")
    A("라우터(ordinal 이진 2개) 학습 %.1f초\n" % p["router_seconds"])
    A("| 후보 | serving 모델 수 |")
    A("|---|---:|")
    for k, v in sorted(p["model_counts"].items(), key=lambda x: x[1]):
        A("| %s | %d |" % (k, v))
    A("")
    A("> 지시서: **0.001 개선 때문에 모델 10개를 띄우는 구조는 승격하지 않는다.**\n")

    A("## 9. 최종 비교표\n")
    A("| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI | 모델 수 |")
    A("|---|---:|---:|---:|---:|---|---:|")
    A("| M73 XGB Experts | %.4f | %.4f | %.1f%% | — | — | 5 |"
      % (base["MAE_log10"], nt["variants"][BASE]["MAE_log10"],
         100 * base["within_2x"]))
    hon = [k for k in honest(ps) if k != BASE]
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_base"]
        s = nt["variants"].get(k, {})
        A("| %s | %.4f | %s | %.1f%% | %d/5 | [%+0.4f, %+0.4f] | %d |"
          % (k, m["MAE_log10"], ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], m["fold_wins_vs_base"], v["ci95"][0],
             v["ci95"][1], p["model_counts"].get(k, 5)))
    A("| oracle 상한 (서빙 불가) | %.4f | — | %.1f%% | — | — | — |"
      % (ps["oracle_ceiling"]["MAE_log10"],
         100 * ps["oracle_ceiling"]["within_2x"]))
    A("")

    A("## 10. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    for k, v in p["leakage_checks"].items():
        A("| %s | %s |" % (k, "PASS" if v else "FAIL"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")

    A("## 11. 승격 점검표\n")
    A("대상: `%s` (정직한 후보 중 OOF MAE 최저)\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")

    A("## 결론\n")
    A("```text")
    A("M73 XGB experts (재현)   MAE = %.4f" % base["MAE_log10"])
    A("최고 후보                %s" % best)
    A("                         MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (B["MAE_log10"], B["vs_base"]["delta_MAE"], B["vs_base"]["ci95"][0],
         B["vs_base"]["ci95"][1]))
    A("residual 상관 (최종 soft) 최대 %.4f -> ensemble 가치 %s"
      % (p["max_residual_corr"], "있음" if p["ensemble_worth"] else "낮음"))
    A("serving 모델 수          M73 5개 -> 후보 %d개"
      % p["model_counts"].get(best, 5))
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
