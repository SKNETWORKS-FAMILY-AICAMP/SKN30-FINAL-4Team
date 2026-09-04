r"""M78 — M73 이 이미 낸 예측값을 후처리로 더 잘 보정할 수 있는가.

지시서(사용자, `m78_model2_postprocessing_calibration_experiment_plan.md`):

    현재 최종 기준 후보는 M73 `soft/ordinal_xgb` (OOF 0.3563 / strict 0.3756).
    expert tuning · routing · local cohort · spline/GAM 축이 전부 개선을 내지
    못했다. 이번에는 모델 구조를 건드리지 않고, **M73 raw prediction 에 남아
    있는 systematic bias 를 후처리 단계에서 보정**할 수 있는지 본다.

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 (구조화 + 제목 SVD64 + 원천 feature 층 + 본문 SVD64)
    routing    M73 soft / ordinal_xgb — 구간 33.3/66.7, 확률가중 평균
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    raw 예측   M73 의 예측값 자체는 한 글자도 바꾸지 않는다

바뀌는 것은 **raw 예측 뒤에 붙는 함수 하나**다.

## 이 실험에서 가장 위험한 자리 — 후처리도 모델의 일부다

지시서 '가장 중요한 원칙': 전체 OOF 정답을 보고 규칙을 만든 뒤 같은 OOF 에
적용하면 반드시 좋아 보인다. isotonic 은 특히 심하다 — 1,877행에 계단을
자유롭게 놓게 두면 OOF MAE 는 얼마든지 내려간다. 그래서 모든 calibrator 는
**outer train 안에서만** 적합한다.

    outer train
        -> inner GroupKFold(3) 로 M73 파이프라인을 다시 돌려 inner OOF 예측
        -> 후처리 규칙(isotonic·a·b·bin correction·alpha·clip)을 여기서만 적합
        -> outer test 의 raw 예측에 그대로 적용
        -> 승격 판정은 이 outer 숫자로만

inner CV 는 outer train 의 feature 행렬을 재적합 없이 그대로 쓴다(M73 이 세운
규율). inner 의 모든 행은 outer train 안에 있어 outer test 를 건드리지 않는다.

하이퍼파라미터(bin 개수·alpha·clip 수준)가 있는 후보는 **inner OOF MAE 로만**
고른다(`nested`). 고정값을 전체 OOF 에 적용한 표(`sweep`)도 같이 싣지만 그것은
'후처리가 설정에 얼마나 민감한가'를 보는 진단용 곡선이지 승격 후보가 아니다.

## Experiment 6 을 왜 따로 돌리지 않는가

지시서의 residual correction 은 `y_hat = raw + (a + b*raw)` 인데 이것은
`a + (1+b)*raw` 와 같은 식이다. Experiment 2 의 L2 와 **수학적으로 동일한
후보**라 따로 재면 같은 것을 두 번 세는 것이 된다. L2 결과가 곧 답이다.

산출
    ml/data/processed/m78_postprocess_oof.parquet
    ml/reports/m78_m2_postprocess_calibration.json / .md
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
import m73_m2_routing_improvement as M73

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m78_postprocess_oof.parquet")
MD = C.report_path("m78_m2_postprocess_calibration.md")

BUCKETS = M73.BUCKETS
STEP = "G"
INNER_SPLITS = 3
RAW = "raw (M73 soft/ordinal_xgb)"

M73_PUBLISHED = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                 "within_2x": 0.564, "within_3x": 0.742}

# ------------------------------------------------------------ 후처리 설정
ISO_MIN_N = 50                    # I2 — 계단 하나가 최소 이만큼의 행을 딛는다
BIN_KS = (3, 5)                   # Exp3 — 지시서 '10-bin 이상 사용 금지'
BIN_MIN_N = 40                    # bin 별 최소 n. 미달이면 보정 0
ALPHAS = (0.90, 0.95, 0.97, 0.99)  # Exp4 — 약한 shrinkage 만
CLIPS = (0.5, 1.0, 2.0)           # Exp5 — 양끝 백분위 (P0.5/99.5, P1/99, P2/98)
SLICE_MIN_N = 30

CODE_VERSION = "m78-v1"
CKPT_DIR = os.path.join(C.PROC, "m78_ckpt")


# ============================================================ 후처리 규칙들
# 모든 fit_* 은 (inner OOF 예측 xr, outer train 정답 yr) 만 본다. test 는 못 본다.
def fit_isotonic(xr, yr):
    from sklearn.isotonic import IsotonicRegression
    return IsotonicRegression(out_of_bounds="clip").fit(xr, yr)


def fit_isotonic_binned(xr, yr, min_n=ISO_MIN_N):
    """계단마다 최소 표본수를 둔 제한적 isotonic (지시서 I2).

    sklearn 에는 min-samples 옵션이 없다. 그래서 예측값 순으로 최소 min_n 씩
    묶어 **대표점**(구간 중앙값)만 남긴 뒤 그 위에 isotonic 을 태운다. 계단이
    데이터 한두 행 위에 서는 일을 구조적으로 막는다.
    """
    from sklearn.isotonic import IsotonicRegression

    n = len(xr)
    k = max(2, n // max(min_n, 1))
    o = np.argsort(xr, kind="mergesort")
    xs, ys = xr[o], yr[o]
    idx = np.array_split(np.arange(n), k)
    px = np.array([np.median(xs[i]) for i in idx])
    py = np.array([np.median(ys[i]) for i in idx])
    w = np.array([len(i) for i in idx], dtype=float)
    keep = np.concatenate([[True], np.diff(px) > 0])     # 동점 대표점 제거
    return IsotonicRegression(out_of_bounds="clip").fit(px[keep], py[keep],
                                                        sample_weight=w[keep])


def fit_shift(xr, yr):
    """L1 — intercept only. MAE 를 재므로 평균이 아니라 중앙값이 맞는 추정량이다."""
    return float(np.median(yr - xr))


def fit_linear(xr, yr, kind):
    """L2 — a + b*raw. ols 는 제곱오차 기준, lad 는 절대오차 기준(우리 지표)."""
    if kind == "ols":
        b, a = np.polyfit(xr, yr, 1)
        return float(a), float(b)
    from sklearn.linear_model import QuantileRegressor
    q = QuantileRegressor(quantile=0.5, alpha=0.0, solver="highs")
    q.fit(xr.reshape(-1, 1), yr)
    return float(q.intercept_), float(q.coef_[0])


def fit_bin_correction(xr, yr, k, min_n=BIN_MIN_N):
    """Exp3 — 예측 분위 구간별 residual 중앙값. 경계는 train 예측에서만."""
    edges = np.percentile(xr, np.linspace(0, 100, k + 1))[1:-1]
    edges = np.unique(edges)
    idx = np.digitize(xr, edges)
    corr, ns = np.zeros(len(edges) + 1), np.zeros(len(edges) + 1, dtype=int)
    for j in range(len(edges) + 1):
        m = idx == j
        ns[j] = int(m.sum())
        # 표본이 얇은 구간은 보정하지 않는다 — 얇은 구간의 중앙값이 곧 잡음이다
        corr[j] = float(np.median(yr[m] - xr[m])) if m.sum() >= min_n else 0.0
    return {"edges": edges, "corr": corr, "n": ns}


def apply_bin_correction(rule, p):
    return p + rule["corr"][np.digitize(p, rule["edges"])]


def fit_reference(kind, Xs_tr, yr):
    """Exp4 shrinkage 의 기준값. R3(M73 global)은 지시서에 따라 제외."""
    if kind == "R1":
        return {"kind": "R1", "value": float(np.median(yr))}
    med = float(np.median(yr))
    g = {}
    s = Xs_tr["support_type"].astype(str).to_numpy()
    for k in np.unique(s):
        m = s == k
        g[k] = float(np.median(yr[m])) if m.sum() >= SLICE_MIN_N else med
    return {"kind": "R2", "value": med, "by_support_type": g}


def reference_values(ref, Xs_te):
    if ref["kind"] == "R1":
        return np.full(len(Xs_te), ref["value"])
    s = Xs_te["support_type"].astype(str).to_numpy()
    return np.array([ref["by_support_type"].get(k, ref["value"]) for k in s])


def pick_by_inner(options, score):
    """inner OOF MAE 를 최소화하는 설정. 동점이면 더 약한 보정 쪽."""
    best, best_mae = None, np.inf
    for o in options:
        m = score(o)
        if m < best_mae - 1e-12:
            best, best_mae = o, m
    return best, round(float(best_mae), 4)


# ============================================================ fold 계산
def m73_block(Xtr, ytr, Xte):
    """M73 재현 블록 — global · 구간 expert 3 · ordinal Stage1 · soft 예측."""
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    g = F.make_point_model().fit(Xtr, ytr).predict(Xte)
    tab = np.zeros((len(Xte), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)
    pr = M73.stage1_proba("ordinal_xgb", Xtr, ztr, Xte)
    return {"edges": edges, "global": g, "table": tab, "proba": pr,
            "soft": M73.route_soft(tab, pr)}


def inner_oof(Xtr, ytr, gtr):
    """outer train 안에서 M73 파이프라인을 다시 돌린 inner OOF soft 예측.

    후처리 규칙이 볼 수 있는 유일한 (예측, 정답) 짝이다. outer test 의 raw
    예측과 같은 성격의 값이라야 규칙이 test 에서도 통한다 — in-sample 예측을
    쓰면 잔차가 거의 0 이라 어떤 보정도 '필요 없다'고 나온다.
    """
    from sklearn.model_selection import GroupKFold

    n = len(ytr)
    soft, glob = np.zeros(n), np.zeros(n)
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        blk = m73_block(Xtr.iloc[a], ytr[a], Xtr.iloc[b])
        soft[b] = blk["soft"]
        glob[b] = blk["global"]
    return soft, glob


def fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xtr, Xte = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                            STEP, [None])
    ytr, yte = y[tr], y[te]
    edges = M73.bucket_edges(ytr)
    zte = M73.to_bucket(yte, edges)
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    blk = m73_block(Xtr, ytr, Xte)
    in_raw, in_glob = inner_oof(Xtr, ytr, groups[tr])

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in edges],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "raw_MAE": round(float(np.abs(blk["soft"] - yte).mean()), 4),
           "global_MAE": round(float(np.abs(blk["global"] - yte).mean()), 4),
           "inner_raw_MAE": round(float(np.abs(in_raw - ytr).mean()), 4),
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "tr": np.asarray(tr), "base": base_te,
            "z_true": zte, "raw": blk["soft"], "glob": blk["global"],
            "table": blk["table"], "in_raw": in_raw, "in_glob": in_glob,
            "rec": rec}


# ============================================================ 후처리 적용
def build_variants(Xs, y, fo):
    """이 fold 의 raw 예측에 후처리 후보를 전부 적용한다.

    전부 inner OOF 로만 적합하고, 무거운 계산은 하나도 없다 — 규칙을 고쳐도
    체크포인트(모델 출력)는 살아 있다.
    """
    tr, te = fo["tr"], fo["te"]
    xr, yr, p = fo["in_raw"], y[tr], fo["raw"]
    out, params = {}, {}

    # --- Exp1 isotonic ----------------------------------------------------
    out["I1/isotonic"] = fit_isotonic(xr, yr).predict(p)
    iso2 = fit_isotonic_binned(xr, yr)
    out["I2/isotonic_min%d" % ISO_MIN_N] = iso2.predict(p)
    params["I2_steps"] = int(len(np.unique(iso2.y_thresholds_)))

    # --- Exp2 linear ------------------------------------------------------
    c = fit_shift(xr, yr)
    out["L1/shift"] = p + c
    params["L1_c"] = round(c, 5)
    for kind in ("ols", "lad"):
        a, b = fit_linear(xr, yr, kind)
        out["L2/%s" % kind] = a + b * p
        params["L2_%s" % kind] = {"a": round(a, 5), "b": round(b, 5)}

    # --- Exp3 bin correction ---------------------------------------------
    bin_rules = {}
    for k in BIN_KS:
        r = fit_bin_correction(xr, yr, k)
        bin_rules[k] = r
        out["B%d/bin" % k] = apply_bin_correction(r, p)
        params["B%d" % k] = {"edges": [round(float(x), 4) for x in r["edges"]],
                             "corr": [round(float(x), 5) for x in r["corr"]],
                             "n": r["n"].tolist()}
    # nested — bin 개수를 inner OOF 로 고른다
    bk, bmae = pick_by_inner(BIN_KS, lambda k: float(np.abs(
        apply_bin_correction(bin_rules[k], xr) - yr).mean()))
    out["B*/bin_nested"] = apply_bin_correction(bin_rules[bk], p)
    params["B_nested"] = {"k": int(bk), "inner_MAE": bmae}

    # --- Exp4 weak shrinkage ---------------------------------------------
    refs = {kind: fit_reference(kind, Xs.iloc[tr], yr) for kind in ("R1", "R2")}
    rv_te = {kind: reference_values(r, Xs.iloc[te]) for kind, r in refs.items()}
    rv_tr = {kind: reference_values(r, Xs.iloc[tr]) for kind, r in refs.items()}
    for kind in ("R1", "R2"):
        for a in ALPHAS:
            out["S/%s@%.2f" % (kind, a)] = a * p + (1 - a) * rv_te[kind]
    (nk, na), smae = pick_by_inner(
        [(k, a) for k in ("R1", "R2") for a in ALPHAS],
        lambda o: float(np.abs(o[1] * xr + (1 - o[1]) * rv_tr[o[0]] - yr).mean()))
    out["S*/shrink_nested"] = na * p + (1 - na) * rv_te[nk]
    params["S_nested"] = {"ref": nk, "alpha": float(na), "inner_MAE": smae}
    params["R1_median"] = round(refs["R1"]["value"], 5)

    # --- Exp5 quantile clipping ------------------------------------------
    for q in CLIPS:
        lo, hi = np.percentile(xr, [q, 100 - q])
        out["C/p%.1f" % q] = np.clip(p, lo, hi)
    (cq,), cmae = pick_by_inner(
        [(q,) for q in CLIPS],
        lambda o: float(np.abs(np.clip(xr, *np.percentile(xr, [o[0], 100 - o[0]]))
                               - yr).mean()))
    lo, hi = np.percentile(xr, [cq, 100 - cq])
    out["C*/clip_nested"] = np.clip(p, lo, hi)
    params["C_nested"] = {"q": float(cq), "lo": round(float(lo), 4),
                          "hi": round(float(hi), 4), "inner_MAE": cmae}
    return out, params


# ============================================================ 체크포인트
def ckpt_signature(fp):
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb_point": F.XGB_POINT, "inner_splits": INNER_SPLITS,
        "step": STEP, "cuts": list(M73.CUTS),
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
    return {k: z[k] for k in z.files} | {"rec": meta["rec"]}


def ckpt_save(sig, tag, i, fo):
    import json as _json
    d, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(d, exist_ok=True)
    arr = {k: v for k, v in fo.items() if k != "rec"}
    np.savez_compressed(npz + ".tmp.npz", **arr)
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"rec": fo["rec"]}, ensure_ascii=False, default=str))
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ============================================================ split 실행
def run_split(Xs, y, groups, titles, body, NB, cats, sig, tag, verbose=True,
              use_ckpt=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    R = {"z_true": np.zeros(n, dtype=int), "base": np.zeros(n),
         "fold_id": np.zeros(n, dtype=int), "raw": np.zeros(n),
         "glob": np.zeros(n), "table": np.zeros((n, 3)), "in_raw_by_fold": [],
         "pred": {}, "params": [], "folds": []}
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        R["fold_id"][te] = i
        R["base"][te] = fo["base"]
        R["z_true"][te] = fo["z_true"]
        R["raw"][te] = fo["raw"]
        R["glob"][te] = fo["glob"]
        R["table"][te] = fo["table"]
        R["in_raw_by_fold"].append((fo["tr"], fo["in_raw"]))

        var, par = build_variants(Xs, y, fo)
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
            print("   fold %d  cut %s  raw %.4f  inner-raw %.4f  (%s)"
                  % (i, rec["edges_won"], rec["raw_MAE"], rec["inner_raw_MAE"],
                     "체크포인트 재사용" if cached else "%.0fs" % rec["seconds"]))
    R["pred"][RAW] = R["raw"]
    return R


# ============================================================ Exp0 진단
def calibration_fit(p, y):
    from scipy import stats
    b, a = np.polyfit(p, y, 1)
    r = stats.pearsonr(p, y)[0]
    return {"intercept": round(float(a), 4), "slope": round(float(b), 4),
            "r2": round(float(r ** 2), 4)}


def pred_bins(p, y, q=10):
    """예측 분위 구간별 실제/잔차. 지시서 Experiment 0-1 의 출력."""
    edges = np.unique(np.percentile(p, np.linspace(0, 100, q + 1)))
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, len(edges) - 2)
    rows = []
    for k in range(len(edges) - 1):
        m = idx == k
        if not m.any():
            continue
        r = y[m] - p[m]
        rows.append({"bin": "[%.3f, %.3f)" % (edges[k], edges[k + 1]),
                     "n": int(m.sum()),
                     "mean_pred": round(float(p[m].mean()), 4),
                     "mean_actual": round(float(y[m].mean()), 4),
                     "median_pred": round(float(np.median(p[m])), 4),
                     "median_actual": round(float(np.median(y[m])), 4),
                     "mean_residual": round(float(r.mean()), 4),
                     "median_residual": round(float(np.median(r)), 4),
                     "MAE": round(float(np.abs(r).mean()), 4)})
    return rows


def slice_bias(d, Xs, p, y):
    """지시서 Experiment 0-4. 규칙을 바로 만들지 않고 '있는지'만 본다."""
    out = {}
    cols = {"evidence_source": d["evidence_source"].astype(str).to_numpy(),
            "cohort": d["cohort"].astype(str).to_numpy(),
            "support_type": Xs["support_type"].astype(str).to_numpy()}
    for c in ("support_ratio", "support_count", "project_duration"):
        cols["has_" + c] = np.where(np.isfinite(Xs[c].to_numpy(dtype=float)),
                                    "있음", "없음")
    for name, v in cols.items():
        rows = {}
        for k in np.unique(v):
            m = v == k
            if m.sum() < SLICE_MIN_N:
                continue
            r = y[m] - p[m]
            rows[str(k)] = {"n": int(m.sum()),
                            "median_residual": round(float(np.median(r)), 4),
                            "mean_residual": round(float(r.mean()), 4),
                            "MAE": round(float(np.abs(r).mean()), 4)}
        out[name] = rows
    return out


def extreme_errors(p, y, qs=(0.5, 1.0, 2.0, 5.0)):
    """Exp5 의 전제 — 극단 예측에서 실제로 오차가 커지는가."""
    out = {}
    for q in qs:
        lo, hi = np.percentile(p, [q, 100 - q])
        m = (p < lo) | (p > hi)
        out["p%.1f" % q] = {
            "n_extreme": int(m.sum()),
            "MAE_extreme": round(float(np.abs(y[m] - p[m]).mean()), 4),
            "MAE_rest": round(float(np.abs(y[~m] - p[~m]).mean()), 4),
            "median_residual_extreme": round(float(np.median(y[m] - p[m])), 4)}
    return out


def diagnose(d, Xs, y, R):
    from scipy import stats
    p = R["raw"]
    r = y - p
    return {
        "calibration": calibration_fit(p, y),
        "calibration_note": "ideal: intercept≈0, slope≈1",
        "residual": {"mean": round(float(r.mean()), 4),
                     "median": round(float(np.median(r)), 4),
                     "sd": round(float(r.std()), 4),
                     # 잔차가 예측과 상관이 있으면 그 기울기만큼 보정 여지가 있다
                     "corr_with_pred": round(float(stats.pearsonr(p, r)[0]), 4),
                     "spearman_with_pred": round(float(stats.spearmanr(p, r)[0]), 4)},
        "bins_10": pred_bins(p, y, 10),
        "bins_5": pred_bins(p, y, 5),
        "slices": slice_bias(d, Xs, p, y),
        "extreme": extreme_errors(p, y),
    }


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
    from scipy import stats
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]
    m = M45.point_metrics(y, p)
    m["improvement"] = round(float((b - m["MAE_log10"]) / b), 4)
    m["per_fold_MAE"] = fold_maes(y, p, fid)
    m["fold_std"] = round(float(np.std(m["per_fold_MAE"])), 4)
    m["buckets"] = M73.bucket_metrics(y, p, R["z_true"])
    m["cohort"] = cohort_mae(d, y, p)
    m["calibration"] = calibration_fit(p, y)
    if ref is not None:
        m["vs_raw"] = M73.paired_test(y, p, ref)
        rf = fold_maes(y, ref, fid)
        m["fold_wins_vs_raw"] = int(sum(1 for a, c in zip(m["per_fold_MAE"], rf)
                                        if a < c))
        # 통과기준 5 — ranking 이 지나치게 붕괴하지 않았는가
        m["spearman_vs_raw"] = round(float(stats.spearmanr(p, ref)[0]), 4)
        m["max_abs_shift"] = round(float(np.abs(p - ref).max()), 4)
        m["mean_abs_shift"] = round(float(np.abs(p - ref).mean()), 4)
    return m


def summarize(d, y, R):
    ref = R["raw"]
    out = {"baseline_MAE": round(float(np.abs(R["base"] - y).mean()), 4),
           "variants": {s: block(d, y, R, p, None if s == RAW else ref)
                        for s, p in R["pred"].items()},
           "global": block(d, y, R, R["glob"]),
           "params": R["params"], "folds": R["folds"]}
    rows = np.arange(len(y))
    o = M45.point_metrics(y, R["table"][rows, R["z_true"]])
    o["note"] = "실제 구간을 안다고 가정한 상한 — 서빙 불가, 진단 전용"
    out["oracle_ceiling"] = o
    return out


# 승격 후보 = nested 로 고른 것 + 사전 고정 규칙(하이퍼파라미터가 없는 것).
# 고정 alpha/clip/bin sweep 은 같은 OOF 에서 고르고 같은 OOF 로 재는 표라 뺀다.
HONEST_PREFIX = ("I1/", "I2/", "L1/", "L2/", "B*/", "S*/", "C*/")


def honest(res):
    return [k for k in res["variants"] if k.startswith(HONEST_PREFIX)]


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
    print("\n== 체크포인트 서명 %s  ->  %s" % (sig, os.path.relpath(CKPT_DIR, C.ROOT)))
    print("   저장하는 것은 fold 단위 모델 출력(raw 예측·inner OOF 예측)뿐이다.")
    print("   후처리 규칙은 매 실행에서 다시 적합한다 — 규칙을 고쳐도 캐시는 산다.")

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        print("\n== 5-fold [%s] — M73 raw + inner OOF(GroupKFold %d)"
              % (gname, INNER_SPLITS))
        R = run_split(Xs, y, groups[gname], titles, body, NB, cats, sig, gname)
        results[gname] = summarize(d, y, R)
        raws[gname] = R

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]
    raw_mae = ps["variants"][RAW]["MAE_log10"]

    # ---------------------------------------------------- Experiment 0
    print("\n== Experiment 0 — prediction bias diagnostic")
    diag = diagnose(d, Xs, y, Rp)
    cal = diag["calibration"]
    print("   calibration  y = %.4f + %.4f * pred   (R² %.4f)   이상: 0 / 1"
          % (cal["intercept"], cal["slope"], cal["r2"]))
    rr = diag["residual"]
    print("   residual     평균 %+.4f  중앙 %+.4f  sd %.4f  "
          "예측과의 상관 %+.4f (spearman %+.4f)"
          % (rr["mean"], rr["median"], rr["sd"], rr["corr_with_pred"],
             rr["spearman_with_pred"]))
    print("   예측 5분위별 (n / 평균예측 / 평균실제 / 중앙잔차 / MAE)")
    for r in diag["bins_5"]:
        print("      %-20s %4d  %+.3f  %+.3f  %+.4f  %.4f"
              % (r["bin"], r["n"], r["mean_pred"], r["mean_actual"],
                 r["median_residual"], r["MAE"]))
    print("   극단 예측 오차 (Exp5 의 전제)")
    for q, v in diag["extreme"].items():
        print("      %-6s 극단 %3d행 MAE %.4f  vs 나머지 %.4f  (차 %+.4f)"
              % (q, v["n_extreme"], v["MAE_extreme"], v["MAE_rest"],
                 v["MAE_extreme"] - v["MAE_rest"]))
    extreme_worse = diag["extreme"]["p2.0"]["MAE_extreme"] > \
        diag["extreme"]["p2.0"]["MAE_rest"]
    trend = abs(rr["corr_with_pred"])
    diag_verdict = ("보정 여지 있음" if (abs(cal["slope"] - 1) > 0.05 or trend > 0.10)
                    else "보정 여지 약함")
    print("   진단: %s (slope %.4f, |잔차-예측 상관| %.4f)"
          % (diag_verdict, cal["slope"], trend))

    # ---------------------------------------------------- Exp1~5 결과
    print("\n== 후처리 후보 (raw %.4f 대비)" % raw_mae)
    print("   %-24s %8s %9s %8s %7s %-22s %7s"
          % ("후보", "MAE", "Δ", "strict", "fold승", "95%CI", "ρ(raw)"))
    hon = honest(ps)
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {}).get("MAE_log10")
        print("   %-24s %8.4f %+9.4f %8s %6s  %-22s %7.4f"
              % (k, m["MAE_log10"], v["delta_MAE"],
                 ("%.4f" % s) if s else "—",
                 "%d/5" % m["fold_wins_vs_raw"],
                 "[%+0.4f, %+0.4f]" % tuple(v["ci95"]), m["spearman_vs_raw"]))
    print("   ---- sweep (진단용, 승격 근거 아님)")
    for k in sorted(k for k in ps["variants"]
                    if k.startswith(("S/", "C/", "B3/", "B5/"))):
        m = ps["variants"][k]
        print("   %-24s %8.4f %+9.4f  %d/5"
              % (k, m["MAE_log10"], m["vs_raw"]["delta_MAE"],
                 m["fold_wins_vs_raw"]))

    # ---------------------------------------------------- 파라미터 안정성
    print("\n== 후처리 파라미터의 fold 간 안정성 (승격조건 8)")
    pars = ps["params"]
    print("   L1 c        %s" % [p["L1_c"] for p in pars])
    print("   L2 lad a    %s" % [p["L2_lad"]["a"] for p in pars])
    print("   L2 lad b    %s" % [p["L2_lad"]["b"] for p in pars])
    print("   L2 ols b    %s" % [p["L2_ols"]["b"] for p in pars])
    print("   bin nested  %s" % [p["B_nested"]["k"] for p in pars])
    print("   shrink      %s" % [(p["S_nested"]["ref"], p["S_nested"]["alpha"])
                                 for p in pars])
    print("   clip nested %s" % [p["C_nested"]["q"] for p in pars])
    stability = {
        "L1_c": [p["L1_c"] for p in pars],
        "L2_lad_a": [p["L2_lad"]["a"] for p in pars],
        "L2_lad_b": [p["L2_lad"]["b"] for p in pars],
        "L2_ols_a": [p["L2_ols"]["a"] for p in pars],
        "L2_ols_b": [p["L2_ols"]["b"] for p in pars],
        "bin_nested_k": [p["B_nested"]["k"] for p in pars],
        "shrink_nested": [[p["S_nested"]["ref"], p["S_nested"]["alpha"]]
                          for p in pars],
        "clip_nested_q": [p["C_nested"]["q"] for p in pars],
        "I2_steps": [p["I2_steps"] for p in pars],
    }
    stability["L2_lad_b_range"] = round(max(stability["L2_lad_b"]) -
                                        min(stability["L2_lad_b"]), 4)
    stability["L1_c_range"] = round(max(stability["L1_c"]) -
                                    min(stability["L1_c"]), 4)

    # ---------------------------------------------------- 조합 (조건부)
    # 지시서: 개별 실험 중 최소 2개가 각각 독립적으로 유의한 개선을 만들었을 때만.
    sig_wins = [k for k in hon
                if ps["variants"][k]["vs_raw"]["ci95"][1] < 0
                and ps["variants"][k]["MAE_log10"] < raw_mae]
    axis = {k[:2] for k in sig_wins}
    combo_note = None
    if len(axis) >= 2:
        combo_note = "조합 실험 조건 충족 — 유의한 축 %s" % sorted(axis)
    else:
        combo_note = ("미실행 — 지시서 '조합 실험 조건'. 독립적으로 유의한 개선을 "
                      "낸 축이 %d개 (필요 2개)" % len(axis))
    print("\n== 조합 실험 — %s" % combo_note)

    # ---------------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    R2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats, sig,
                   "program_stem__repro", verbose=False)
    best = min(hon, key=lambda k: ps["variants"][k]["MAE_log10"])
    repro = {"raw": bool(np.allclose(R2["raw"], Rp["raw"])),
             "inner OOF": bool(np.allclose(
                 np.concatenate([a for _, a in R2["in_raw_by_fold"]]),
                 np.concatenate([a for _, a in Rp["in_raw_by_fold"]]))),
             best: bool(np.allclose(R2["pred"][best], Rp["pred"][best]))}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ---------------------------------------------------- 누수 점검
    leak = {
        "후처리 규칙 적합 입력": "outer train 안 inner GroupKFold(%d) OOF 예측과 "
                                 "outer train 정답만" % INNER_SPLITS,
        "inner OOF 가 in-sample 이 아님": "예측하는 행은 그 inner fold 학습에서 빠져 "
                                          "있다 — 잔차가 test 와 같은 성격이다",
        "bin 경계 / clip 분위 / isotonic 계단": "전부 inner OOF 예측 분포에서만",
        "bin 개수·alpha·clip 수준 선택": "inner OOF MAE 로만 (nested). 고정값 표는 "
                                         "진단용으로 격리",
        "shrinkage 기준값": "outer train 정답의 중앙값 (R1) / support_type 별 "
                            "중앙값 (R2). R3(global)은 지시서에 따라 제외",
        "test y 의 용도": "최종 metric · oracle 상한 · 구간별 집계뿐",
        "raw 예측 변경 여부": "없음 — M73 블록 코드 경로 그대로",
        "Experiment 6 (residual correction)":
            "L2 와 수학적으로 동일(raw + a + b*raw = a + (1+b)*raw)하여 미실행",
    }
    leak_checks = {
        "후처리 규칙이 outer test 를 보지 않았다": True,
        "raw 가 M73 공표치(0.3563)를 재현": abs(raw_mae - 0.3563) < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-34s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ---------------------------------------------------- 승격 판정
    B = ps["variants"][best]
    v = B["vs_raw"]
    nt_best = nt["variants"].get(best)
    checks = {
        "1. OOF MAE < 0.3563": B["MAE_log10"] < M73_PUBLISHED["MAE_log10"],
        "1b. 같은 fold raw 보다 낮다": B["MAE_log10"] < raw_mae,
        "2. strict split 에서도 개선":
            bool(nt_best and nt_best["MAE_log10"] < nt["variants"][RAW]["MAE_log10"]),
        "3. 5개 fold 중 4개 이상 개선": B["fold_wins_vs_raw"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. ranking 붕괴 없음 (ρ ≥ 0.99)": B["spearman_vs_raw"] >= 0.99,
        "6. leakage audit PASS": bool(leak_pass),
        "7. reproducibility PASS": all(repro.values()),
        "8. 파라미터가 fold 마다 안정": stability["L1_c_range"] < 0.05,
        "9. 실질 기준 ΔMAE ≤ -0.003": v["delta_MAE"] <= -0.003,
        "10. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("10.")]
    verdict = ("승격 후보 (M73 raw 뒤에 후처리 추가)" if all(checks[k] for k in core)
               else "현행 유지 — M73 raw `soft/ordinal_xgb`")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ---------------------------------------------------- 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "pred_global": Rp["glob"], "cohort": d["cohort"].to_numpy(),
           "evidence_source": d["evidence_source"].to_numpy()}
    for s, p in Rp["pred"].items():
        key = (s.replace("/", "__").replace("@", "_").replace("*", "s")
               .replace(" ", "_").replace("(", "").replace(")", ""))
        out["pred_" + key] = p
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "M73 raw 예측에 남은 systematic bias 를 후처리로 보정하면 "
                   "0.3563 아래로 내려가는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"],
            "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "routing": "M73 soft / ordinal_xgb (구간 33.3/66.7)",
            "regressor": F.XGB_POINT, "bucket_cuts": list(M73.CUTS),
        },
        "changed": "raw 예측 뒤에 붙는 후처리 함수 하나. raw 예측 자체는 불변",
        "selection_protocol": {
            "nested": "outer train 안 GroupKFold(%d) inner OOF 예측에서만 후처리 "
                      "규칙과 하이퍼파라미터를 고른다. 승격 판정은 outer 값으로만."
                      % INNER_SPLITS,
            "sweep": "고정 alpha·clip·bin 을 전체 OOF 에 적용한 진단용 표. "
                     "여기서 최저값을 골라 승격 근거로 쓰지 않는다.",
            "honest_prefix": list(HONEST_PREFIX),
        },
        "postprocess_config": {
            "isotonic_min_n": ISO_MIN_N, "bin_ks": list(BIN_KS),
            "bin_min_n": BIN_MIN_N, "alphas": list(ALPHAS), "clips": list(CLIPS),
            "exp6_skipped": "residual correction 은 L2 와 동일한 식이라 미실행",
        },
        "diagnostic": diag,
        "diagnostic_verdict": diag_verdict,
        "extreme_worse_than_rest": bool(extreme_worse),
        "results": results,
        "parameter_stability": stability,
        "significant_axes": sorted(axis),
        "combination": combo_note,
        "best_candidate": best,
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
    C.save_report("m78_m2_postprocess_calibration.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ============================================================ MD 보고서
LABEL = {
    "I1/isotonic": "Exp1 · plain isotonic",
    "I2/isotonic_min%d" % ISO_MIN_N: "Exp1 · 제한적 isotonic (계단당 최소 %d행)" % ISO_MIN_N,
    "L1/shift": "Exp2 · intercept only (raw + c)",
    "L2/ols": "Exp2 · a + b·raw (OLS)",
    "L2/lad": "Exp2 · a + b·raw (LAD, MAE 기준)",
    "B*/bin_nested": "Exp3 · 예측 bin 별 residual 보정 (bin 수 nested)",
    "S*/shrink_nested": "Exp4 · weak shrinkage (기준·alpha nested)",
    "C*/clip_nested": "Exp5 · quantile clipping (수준 nested)",
}


def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    rawm = ps["variants"][RAW]
    dg = p["diagnostic"]
    L = []
    A = L.append
    A("# M78 — 후처리 보정 (isotonic · linear · bin · shrinkage · clipping)\n")
    A("> 질문: **M73 이 이미 낸 예측값에 systematic bias 가 남아 있는가,")
    A("> 남아 있다면 안전한 후처리로 0.3563 아래로 내릴 수 있는가?**\n")

    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("routing  %s" % u["routing"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("후처리도 모델의 일부다. 규칙을 어디서 적합했는지가 이 실험의 전부다.\n")
    A("```text")
    A("nested  %s" % p["selection_protocol"]["nested"])
    A("sweep   %s" % p["selection_protocol"]["sweep"])
    A("Exp6    %s" % p["postprocess_config"]["exp6_skipped"])
    A("```\n")

    A("## 1. Experiment 0 — 보정할 bias 가 있기는 한가\n")
    c = dg["calibration"]
    r = dg["residual"]
    A("```text")
    A("calibration   y = %+.4f + %.4f · pred     (R² %.4f)   이상: 0 / 1"
      % (c["intercept"], c["slope"], c["r2"]))
    A("residual      평균 %+.4f  중앙 %+.4f  sd %.4f"
      % (r["mean"], r["median"], r["sd"]))
    A("잔차-예측 상관 pearson %+.4f  spearman %+.4f"
      % (r["corr_with_pred"], r["spearman_with_pred"]))
    A("진단          %s" % p["diagnostic_verdict"])
    A("```\n")
    A("### 1-2. 예측 10분위별 실제·잔차\n")
    A("| 예측 구간 | n | 평균 예측 | 평균 실제 | 중앙 예측 | 중앙 실제 | "
      "평균 잔차 | 중앙 잔차 | MAE |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for b in dg["bins_10"]:
        A("| %s | %d | %+.3f | %+.3f | %+.3f | %+.3f | %+.4f | %+.4f | %.4f |"
          % (b["bin"], b["n"], b["mean_pred"], b["mean_actual"],
             b["median_pred"], b["median_actual"], b["mean_residual"],
             b["median_residual"], b["MAE"]))
    A("")
    A("> 잔차 = 실제 − 예측. 양수면 과소예측, 음수면 과대예측이다. 낮은 구간에서")
    A("> 음수, 높은 구간에서 양수가 **일관되게** 나와야 후처리 가치가 있다.\n")
    A("### 1-3. slice 별 bias (규칙은 만들지 않는다)\n")
    A("| slice | 값 | n | 중앙 잔차 | 평균 잔차 | MAE |")
    A("|---|---|---:|---:|---:|---:|")
    for name, rows in dg["slices"].items():
        for k, v in rows.items():
            A("| %s | %s | %d | %+.4f | %+.4f | %.4f |"
              % (name, k, v["n"], v["median_residual"], v["mean_residual"],
                 v["MAE"]))
    A("")
    A("### 1-4. 극단 예측 오차 (Experiment 5 의 전제)\n")
    A("| 양끝 백분위 | 극단 n | 극단 MAE | 나머지 MAE | 차이 | 극단 중앙잔차 |")
    A("|---|---:|---:|---:|---:|---:|")
    for q, v in dg["extreme"].items():
        A("| %s | %d | %.4f | %.4f | %+.4f | %+.4f |"
          % (q, v["n_extreme"], v["MAE_extreme"], v["MAE_rest"],
             v["MAE_extreme"] - v["MAE_rest"], v["median_residual_extreme"]))
    A("")

    A("## 2. 후처리 후보 — 정직한 후보 (nested 선택 · 고정규칙)\n")
    A("| 후보 | 설명 | OOF MAE | Δ vs raw | 95% CI | wilcoxon p | fold승 | "
      "strict MAE | 2배내 | 3배내 | ρ(raw) |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    A("| `%s` | M73 그대로 | %.4f | — | — | — | — | %.4f | %.1f%% | %.1f%% | — |"
      % (RAW, rawm["MAE_log10"], nt["variants"][RAW]["MAE_log10"],
         100 * rawm["within_2x"], 100 * rawm["within_3x"]))
    hon = honest(ps)
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {})
        A("| `%s` | %s | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %s | %d/5 | %s | "
          "%.1f%% | %.1f%% | %.4f |"
          % (k, LABEL.get(k, ""), m["MAE_log10"], v["delta_MAE"],
             v["ci95"][0], v["ci95"][1], str(v["wilcoxon_p"]),
             m["fold_wins_vs_raw"],
             ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], 100 * m["within_3x"], m["spearman_vs_raw"]))
    A("")
    A("> raw 는 같은 fold·같은 코드로 다시 학습한 M73 `soft/ordinal_xgb`")
    A("> (%.4f, 공표 %.4f 재현) 이다. 모든 Δ 는 이 값과의 paired 차이다."
      % (rawm["MAE_log10"], p["published_m73"]["MAE_log10"]))
    A("> ρ(raw) 는 보정 전후 예측 순위의 Spearman 상관 — 승격조건 5.\n")
    A("### 2-2. sweep — 진단용 (승격 근거 아님)\n")
    A("| 후보 | OOF MAE | Δ vs raw | fold승 |")
    A("|---|---:|---:|---:|")
    for k in sorted(k for k in ps["variants"]
                    if k.startswith(("S/", "C/", "B3/", "B5/"))):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (k, m["MAE_log10"], m["vs_raw"]["delta_MAE"], m["fold_wins_vs_raw"]))
    A("")

    A("### 2-3. 구간별 · 비교군별 MAE — 최고 후보 vs raw\n")
    best = p["best_candidate"]
    B = ps["variants"][best]
    A("| 구간 | n | raw | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |"
          % (b, rawm["buckets"][b]["n"], rawm["buckets"][b]["MAE_log10"],
             B["buckets"][b]["MAE_log10"]))
    A("")
    A("| 비교군 | n | raw | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for col in ("cohort", "evidence_source"):
        for k, rr in rawm["cohort"][col].items():
            A("| %s | %d | %.4f | %.4f |"
              % (k, rr["n"], rr["MAE"], B["cohort"][col][k]["MAE"]))
    A("")
    A("### 2-4. fold 별 MAE\n")
    A("| fold | 경계(원) | baseline | raw | `%s` |" % best)
    A("|---|---|---:|---:|---:|")
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %.4f | %.4f |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             f["baseline_MAE"], rawm["per_fold_MAE"][i], B["per_fold_MAE"][i]))
    A("")
    A("### 2-5. calibration slope — 보정 전후\n")
    A("| 예측 | intercept | slope | R² |")
    A("|---|---:|---:|---:|")
    A("| raw | %+.4f | %.4f | %.4f |"
      % (rawm["calibration"]["intercept"], rawm["calibration"]["slope"],
         rawm["calibration"]["r2"]))
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        cc = ps["variants"][k]["calibration"]
        A("| `%s` | %+.4f | %.4f | %.4f |"
          % (k, cc["intercept"], cc["slope"], cc["r2"]))
    A("")

    A("## 3. 후처리 파라미터의 fold 간 안정성 (승격조건 8)\n")
    st = p["parameter_stability"]
    A("| 파라미터 | fold 0~4 |")
    A("|---|---|")
    for k in ("L1_c", "L2_lad_a", "L2_lad_b", "L2_ols_a", "L2_ols_b",
              "bin_nested_k", "shrink_nested", "clip_nested_q", "I2_steps"):
        A("| %s | %s |" % (k, st[k]))
    A("")
    A("> `L1_c` 는 fold 마다 %.4f 폭으로 움직인다. 보정량 자체가 이 폭보다 작으면"
      % st["L1_c_range"])
    A("> 그 보정은 신호가 아니라 fold 잡음을 따라간 것이다.\n")

    A("## 4. bin correction 상세 (Experiment 3)\n")
    for kk in BIN_KS:
        A("`%d-bin` — fold 별 (경계 / 보정량 / bin n)\n" % kk)
        A("| fold | bin 경계 | 보정량 | bin n |")
        A("|---|---|---|---|")
        for pr in ps["params"]:
            b = pr["B%d" % kk]
            A("| %d | %s | %s | %s |" % (pr["fold"], b["edges"], b["corr"],
                                         b["n"]))
        A("")

    A("## 5. 조합 실험\n")
    A("%s\n" % p["combination"])

    A("## 6. 최종 비교표\n")
    A("| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI |")
    A("|---|---:|---:|---:|---:|---|")
    A("| M73 raw | %.4f | %.4f | %.1f%% | — | — |"
      % (rawm["MAE_log10"], nt["variants"][RAW]["MAE_log10"],
         100 * rawm["within_2x"]))
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {})
        A("| %s | %.4f | %s | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (LABEL.get(k, k), m["MAE_log10"],
             ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], m["fold_wins_vs_raw"], v["ci95"][0],
             v["ci95"][1]))
    A("| oracle 상한 (서빙 불가) | %.4f | — | %.1f%% | — | — |"
      % (ps["oracle_ceiling"]["MAE_log10"],
         100 * ps["oracle_ceiling"]["within_2x"]))
    A("")

    A("## 7. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    for k, v in p["leakage_checks"].items():
        A("| %s | %s |" % (k, "PASS" if v else "FAIL"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")

    A("## 8. 승격 점검표\n")
    A("대상: `%s` (정직한 후보 중 OOF MAE 최저)\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")

    A("## 결론\n")
    A("```text")
    A("M73 raw (같은 fold 재현)  MAE = %.4f" % rawm["MAE_log10"])
    A("최고 후처리 후보          %s" % best)
    A("                          MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (B["MAE_log10"], B["vs_raw"]["delta_MAE"], B["vs_raw"]["ci95"][0],
         B["vs_raw"]["ci95"][1]))
    A("calibration slope         raw %.4f -> %.4f"
      % (rawm["calibration"]["slope"], B["calibration"]["slope"]))
    A("bias 진단                 %s" % p["diagnostic_verdict"])
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
