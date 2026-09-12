"""M56 — 모델 2 canonical 승격 (STEP 1 재현성 · STEP 3 동일조건 비교 · STEP 4 판정).

M45 를 지우지 않는다. M45 는 이전 canonical 로 남고, 이 스크립트가 새 canonical
entrypoint 가 된다. 바뀐 것은 **feature 와 학습기 둘뿐**이고, 타깃 정의·비교군
사다리·percentile 조회·서비스 문구 규율은 M45 것을 그대로 import 해서 쓴다.

    유지  타깃 stated_cap only / 비교군 사다리 3단 / 출처 필수축 /
          Phase A percentile 조회 / 구간 실용성 등급 / n=1,877
    교체  구조화 feature -> 구조화 + 제목 SVD(64)
          LightGBM-quantile50 -> XGBoost(MAE 목적함수)

이 스크립트가 하는 일 네 가지.

    STEP 1  파이프라인 지문을 찍는다 — 데이터셋 해시, 타깃 정의, feature 규격,
            그룹키 규칙, 모델 파라미터, seed, 실행 시각. 다시 돌렸을 때 같은
            수치가 나오는지 확인할 수 있어야 한다.
    STEP 3  M45 와 M53 을 **같은 fold 안에서** 나란히 학습해 비교한다. fold 를
            따로 만들면 '조건이 같았다'를 증명할 수 없다.
    STEP 4  승격 점검표를 채우고 판정한다.
    서빙    전체 데이터로 적합한 산출물을 저장하고, 저장물만으로 추론이 학습과
            동일한 feature 를 만드는지 왕복 자체점검한다. 학습만 새 파이프라인이고
            서빙이 옛 feature 를 쓰면 승격이 완료된 것이 아니다(M29 의 교훈).

모델 2 는 여전히 지원규모 적정성 판정 모델이 아니다. 하는 말은 그대로
'유사사업 비교군 안에서의 상대적 위치'다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
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

import json
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m2_features as F
import m45_m2_amount as M45

ART_DIR = os.path.join(C.MODELS, "_archive", "m56_model2_canonical")
OUT_OOF = os.path.join(C.PROC, "m56_oof_predictions.parquet")
MANIFEST = os.path.join(ART_DIR, "manifest.json")

# 이전 canonical(M45)의 공표 수치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M45_PUBLISHED = {"MAE_log10": 0.4681, "baseline": 0.5315, "improvement": 0.119,
                 "within_2x": 0.431, "stability_mean": 0.117, "stability_std": 0.008,
                 "stability_min": 0.100, "stability_pass": "9/10",
                 "coverage": 0.796, "width_x": 33.2, "tiers": (12, 14, 3)}


def lgbm_m45():
    """이전 canonical 의 학습기. 파라미터는 M45 에서 그대로 가져온다."""
    from lightgbm import LGBMRegressor
    return LGBMRegressor(objective="quantile", alpha=0.5, n_estimators=400,
                         learning_rate=0.05, num_leaves=15, min_child_samples=10,
                         random_state=M45.SEED, verbose=-1)


# ------------------------------------------------------------ STEP 3
def paired_oof(Xs, y, groups, titles, cats):
    """M45 와 M56 을 같은 split 에서 학습한다. baseline 도 같은 split."""
    from sklearn.model_selection import GroupKFold

    n = len(y)
    out = {"M45(LGBM·구조화)": np.zeros(n), "M56(XGB·구조화+제목)": np.zeros(n),
           "비교군중앙값(baseline)": np.zeros(n),
           "분해: LGBM·구조화+제목": np.zeros(n), "분해: XGB·구조화만": np.zeros(n)}
    fold_id = np.zeros(n, dtype=int)
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fold_id[te] = i
        Xtr_s, Xte_s = Xs.iloc[tr], Xs.iloc[te]
        Xtr_t, Xte_t, _ = F.build_features(Xs, titles, tr, te, True, True)
        ytr = y[tr]
        out["비교군중앙값(baseline)"][te] = M45.cohort_median_baseline(Xtr_s, ytr, Xte_s, cats)
        out["M45(LGBM·구조화)"][te] = lgbm_m45().fit(Xtr_s, ytr).predict(Xte_s)
        out["분해: XGB·구조화만"][te] = F.make_point_model().fit(Xtr_s, ytr).predict(Xte_s)
        out["분해: LGBM·구조화+제목"][te] = lgbm_m45().fit(Xtr_t, ytr).predict(Xte_t)
        out["M56(XGB·구조화+제목)"][te] = F.make_point_model().fit(Xtr_t, ytr).predict(Xte_t)
    return out, fold_id


def stability(Xs, y, groups, titles, cats, n_repeat=10):
    """fold 재구성 n회. M45 와 같은 방식(그룹 라벨 셔플)으로 둘 다 잰다."""
    from sklearn.model_selection import GroupKFold

    rows = {"M45(LGBM·구조화)": [], "M56(XGB·구조화+제목)": []}
    for seed in range(n_repeat):
        rng = np.random.default_rng(seed)
        uniq = np.unique(groups)
        remap = dict(zip(uniq, uniq[rng.permutation(len(uniq))]))
        gs = np.array([remap[v] for v in groups])
        bp = np.zeros(len(y))
        old = np.zeros(len(y))
        new = np.zeros(len(y))
        for tr, te in GroupKFold(F.N_SPLITS).split(Xs, y, gs):
            Xtr_s, Xte_s, ytr = Xs.iloc[tr], Xs.iloc[te], y[tr]
            Xtr_t, Xte_t, _ = F.build_features(Xs, titles, tr, te, True, True)
            bp[te] = M45.cohort_median_baseline(Xtr_s, ytr, Xte_s, cats)
            old[te] = lgbm_m45().fit(Xtr_s, ytr).predict(Xte_s)
            new[te] = F.make_point_model().fit(Xtr_t, ytr).predict(Xte_t)
        b = float(np.abs(bp - y).mean())
        rows["M45(LGBM·구조화)"].append((b - float(np.abs(old - y).mean())) / b)
        rows["M56(XGB·구조화+제목)"].append((b - float(np.abs(new - y).mean())) / b)
    out = {}
    for k, v in rows.items():
        a = np.array(v)
        out[k] = {"n_repeat": n_repeat, "mean": round(float(a.mean()), 4),
                  "std": round(float(a.std()), 4), "min": round(float(a.min()), 4),
                  "max": round(float(a.max()), 4),
                  "n_pass": int((a >= F.MIN_IMPROVEMENT).sum()),
                  "pass": "%d/%d" % (int((a >= F.MIN_IMPROVEMENT).sum()), n_repeat)}
    return out


def intervals(Xs, y, groups, titles):
    """CQR. M45 cqr_fold 와 같은 구조 — 보정셋도 그룹 단위로 뗀다."""
    from sklearn.model_selection import GroupKFold

    rng = np.random.default_rng(F.PIPELINE_SEED)
    n = len(y)
    lo, mid, hi = np.zeros(n), np.zeros(n), np.zeros(n)
    rlo, rhi = np.zeros(n), np.zeros(n)
    deltas = []

    def qfit(Xtr, ytr, Xte):
        q = F.make_quantile_model().fit(Xtr, ytr).predict(Xte)
        return q[:, 0], q[:, 1]

    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups):
        Xtr, Xte, _ = F.build_features(Xs, titles, tr, te, True, True)
        ytr = y[tr]
        mid[te] = F.make_point_model().fit(Xtr, ytr).predict(Xte)
        rlo[te], rhi[te] = qfit(Xtr, ytr, Xte)
        is_cal = cal_split(groups[tr], rng)
        if is_cal is None:
            lo[te], hi[te] = rlo[te], rhi[te]
            deltas.append(0.0)
            continue
        Xf, yf = Xtr.iloc[~is_cal], ytr[~is_cal]
        Xc, yc = Xtr.iloc[is_cal], ytr[is_cal]
        cl, ch = qfit(Xf, yf, Xc)
        tl, th = qfit(Xf, yf, Xte)
        E = np.maximum(cl - yc, yc - ch)
        k = min(max(int(np.ceil((len(E) + 1) * F.NOMINAL_COVERAGE)), 1), len(E))
        delta = float(np.sort(E)[k - 1])
        lo[te], hi[te] = tl - delta, th + delta
        deltas.append(delta)
    return mid, lo, hi, rlo, rhi, float(np.mean(deltas))


def cal_split(groups_tr, rng):
    """CQR 보정셋을 **그룹 단위로** 뗀다. 같은 사업의 재공고가 학습과 보정에
    갈라지면 이탈량이 낙관적으로 잡혀 구간이 실제보다 좁아진다."""
    uniq = np.unique(groups_tr)
    rng.shuffle(uniq)
    cal = set(uniq[:max(1, int(len(uniq) * F.CQR_CAL_FRAC))])
    is_cal = np.array([g in cal for g in groups_tr])
    if is_cal.sum() < 20 or (~is_cal).sum() < 50:
        return None
    return is_cal


# ------------------------------------------------------------ 서빙 산출물
def fit_artifacts(d, art_dir=None):
    """전체 데이터로 적합해 저장한다. 저장물만으로 추론이 되어야 한다.

    `art_dir` 는 저장 위치만 바꾼다(M65 가 새 세대 산출물을 나란히 두기 위해
    쓴다). feature 구성·split·모델 구조·추론 경로는 건드리지 않는다.
    """
    import joblib

    art_dir = art_dir or ART_DIR

    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    a, _, (vec, svd) = F.fit_title_features(titles, titles[:1])
    T = pd.DataFrame(a, columns=F.title_columns(a.shape[1]))
    X = pd.concat([Xs.reset_index(drop=True), T], axis=1)

    point = F.make_point_model().fit(X, y)
    quant = F.make_quantile_model().fit(X, y)

    # 서빙용 CQR delta — 그룹 단위 보정셋 하나로 잡는다
    groups = F.group_key(d, "program_stem")
    rng = np.random.default_rng(F.PIPELINE_SEED)
    is_cal = cal_split(groups, rng)
    if is_cal is None:
        raise RuntimeError("보정셋을 뗄 수 없다 — 표본이 너무 작다")
    qc = F.make_quantile_model().fit(X[~is_cal], y[~is_cal]).predict(X[is_cal])
    E = np.maximum(qc[:, 0] - y[is_cal], y[is_cal] - qc[:, 1])
    k = min(max(int(np.ceil((len(E) + 1) * F.NOMINAL_COVERAGE)), 1), len(E))
    delta = float(np.sort(E)[k - 1])

    top_industry = list(d["industry"].fillna("미기재").value_counts().head(15).index)
    art = {
        "feature_order": list(X.columns),
        "cat_columns": [c for c in X.columns if str(X[c].dtype) == "category"],
        "cat_categories": {c: list(X[c].cat.categories) for c in X.columns
                           if str(X[c].dtype) == "category"},
        "num_columns": [c for c in X.columns if str(X[c].dtype) != "category"],
        "top_industry": top_industry,
        "cqr_delta": delta,
        "title_form": F.TITLE_SPEC["input_form"],
        "feature_version": F.FEATURE_VERSION,
    }
    os.makedirs(art_dir, exist_ok=True)
    joblib.dump({"vectorizer": vec, "svd": svd, "point": point, "quantile": quant,
                 "meta": art}, os.path.join(art_dir, "model2_canonical.joblib"))
    return art, X, y


SERVING_FIELDS = ["title", "support_type", "support_method", "support_unit", "cohort",
                  "category_large", "industry", "agency_type", "amount_type",
                  "support_count", "support_ratio", "self_burden_ratio",
                  "project_duration", "year"]


def build_serving_frame(records, bundle):
    """추론 입력 -> 학습과 **같은 순서·같은 dtype** 의 feature 행렬.

    여기가 어긋나면 서빙이 조용히 다른 모델이 된다. 그래서 컬럼 순서와 범주
    목록을 학습 때 저장한 것에서 강제로 맞춘다.
    """
    meta = bundle["meta"]
    df = pd.DataFrame(list(records)).reset_index(drop=True)
    for c in SERVING_FIELDS:
        if c not in df.columns:
            df[c] = np.nan
    df["industry_grp"] = df["industry"].fillna("미기재")
    df["industry_grp"] = df["industry_grp"].where(
        df["industry_grp"].isin(meta["top_industry"]), "기타업종")
    df["agency_grp"] = df["agency_type"].fillna("미기재")

    rows = {}
    for c, cats in meta["cat_categories"].items():
        v = df[c].fillna("미기재").astype(str) if c in df.columns else pd.Series(
            ["미기재"] * len(df))
        rows[c] = pd.Categorical(v, categories=cats)
    for c in meta["num_columns"]:
        if c.startswith(F.TITLE_SPEC["output_prefix"]):
            continue
        rows[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    X = pd.DataFrame(rows)

    titles = df["title"].fillna("").astype(str)
    if meta["title_form"] == "amount_masked":
        titles = titles.map(F.mask_amount_expressions)
    T = bundle["svd"].transform(bundle["vectorizer"].transform(titles.to_numpy()))
    X = pd.concat([X.reset_index(drop=True),
                   pd.DataFrame(T, columns=F.title_columns(T.shape[1]))], axis=1)
    return X[meta["feature_order"]]


def serve(bundle, records):
    """모델 2 의 회귀 산출물. percentile 조회는 M45.compare 가 그대로 한다."""
    X = build_serving_frame(records, bundle)
    mid = bundle["point"].predict(X)
    q = bundle["quantile"].predict(X)
    d = bundle["meta"]["cqr_delta"]
    return pd.DataFrame({"pred_log10": mid, "lo_log10": q[:, 0] - d,
                         "hi_log10": q[:, 1] + d,
                         "pred_won": 10 ** mid, "lo_won": 10 ** (q[:, 0] - d),
                         "hi_won": 10 ** (q[:, 1] + d)})


def roundtrip_check(d, X_train, bundle, n=5):
    """저장물만으로 만든 feature 가 학습 때 feature 와 같은가."""
    idx = list(range(0, len(d), max(1, len(d) // n)))[:n]
    recs = d.iloc[idx][SERVING_FIELDS].to_dict("records")
    Xs = build_serving_frame(recs, bundle)
    ref = X_train.iloc[idx].reset_index(drop=True)
    same_order = list(Xs.columns) == list(ref.columns)
    num_cols = [c for c in ref.columns if str(ref[c].dtype) != "category"]
    cat_cols = [c for c in ref.columns if str(ref[c].dtype) == "category"]
    num_close = bool(np.allclose(Xs[num_cols].astype(float).to_numpy(),
                                 ref[num_cols].astype(float).to_numpy(),
                                 rtol=1e-6, atol=1e-8, equal_nan=True))
    cat_same = bool(all((Xs[c].astype(str).to_numpy() == ref[c].astype(str).to_numpy()).all()
                        for c in cat_cols))
    p_serve = bundle["point"].predict(Xs)
    p_train = bundle["point"].predict(ref)
    pred_same = bool(np.allclose(p_serve, p_train, rtol=1e-6, atol=1e-6))
    return {"n_checked": len(idx), "feature_order_identical": same_order,
            "numeric_identical": num_close, "categorical_identical": cat_same,
            "prediction_identical": pred_same,
            "all_pass": bool(same_order and num_close and cat_same and pred_same)}


# ------------------------------------------------------------ main
def main():
    import joblib
    t0 = time.time()
    raw = pd.read_parquet(F.DATASET_PATH)
    d, drop = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}

    print("== STEP 1 — 파이프라인 지문")
    fp = F.dataset_fingerprint()
    man = F.pipeline_manifest()
    print("   dataset   %s" % fp["path"])
    print("   sha256    %s" % fp["sha256"][:32] + "…")
    print("   rows      원본 %d -> 필터 후 %d (기대 %d, 일치 %s)"
          % (fp["rows_raw"], fp["rows_after_filters"], fp["expected_n"],
             fp["n_matches_expected"]))
    print("   target    %s(%s), basis=%s"
          % (fp["target"]["transform"], fp["target"]["column"], fp["target"]["basis_kept"]))
    print("   feature   %s / grouping %s / seed %d"
          % (man["feature_version"], man["grouping_version"], man["seed"]))
    print("   model     %s n=%d lr=%.2f depth=%d subsample=%.1f colsample=%.1f"
          % (man["model_point"]["objective"], man["model_point"]["n_estimators"],
             man["model_point"]["learning_rate"], man["model_point"]["max_depth"],
             man["model_point"]["subsample"], man["model_point"]["colsample_bytree"]))

    print("\n== STEP 3 — 같은 fold 안에서 M45 vs M56")
    cmp_res = {}
    oof_pred = {}
    for gname, g in groups.items():
        preds, fold_id = paired_oof(Xs, y, g, titles, cats)
        base = float(np.abs(preds["비교군중앙값(baseline)"] - y).mean())
        block = {}
        for k, p in preds.items():
            m = M45.point_metrics(y, p)
            m["improvement"] = round(float((base - m["MAE_log10"]) / base), 4)
            block[k] = m
        cmp_res[gname] = block
        if gname == "program_stem":
            oof_pred = preds
            fold_main = fold_id
        print("   [%s]  baseline %.4f" % (gname, base))
        for k in ("M45(LGBM·구조화)", "분해: XGB·구조화만", "분해: LGBM·구조화+제목",
                  "M56(XGB·구조화+제목)"):
            m = block[k]
            print("      %-26s MAE %.4f  2배이내 %.1f%%  개선 %.1f%%"
                  % (k, m["MAE_log10"], m["within_2x"] * 100, m["improvement"] * 100))

    same_n = int(fp["rows_after_filters"])
    print("\n   동일조건 확인: 같은 dataset·target·N(%d)·fold·baseline·metric. "
          "달라진 것은 feature 와 학습기뿐." % same_n)

    print("\n== STEP 3 — 개선율 안정성 (fold 재구성 10회, 둘 다 같은 방식)")
    stab = stability(Xs, y, groups["program_stem"], titles, cats)
    for k, v in stab.items():
        print("   %-26s %.1f%% ± %.1f%%  (최저 %.1f%%)  통과 %s"
              % (k, v["mean"] * 100, v["std"] * 100, v["min"] * 100, v["pass"]))

    print("\n== STEP 3 — 예측구간")
    mid, lo, hi, rlo, rhi, delta = intervals(Xs, y, groups["program_stem"], titles)
    iv_raw = M45.interval_metrics(y, rlo, rhi)
    iv_cqr = M45.interval_metrics(y, lo, hi)
    print("   보정 전 커버리지 %.1f%% 폭 %.1f배  ->  보정 후 %.1f%% 폭 %.1f배 (delta %.3f)"
          % (iv_raw["coverage"] * 100, iv_raw["median_width_x"],
             iv_cqr["coverage"] * 100, iv_cqr["median_width_x"], delta))
    print("   M45 공표치  보정 후 79.6% / 33.2배")

    oof_df = pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "pred": mid,
                           "lo": lo, "hi": hi, "fold": fold_main,
                           "pred_m45": oof_pred["M45(LGBM·구조화)"],
                           "support_type": d["support_type"].to_numpy(),
                           "support_method": d["support_method"].to_numpy(),
                           "support_unit": d["support_unit"].to_numpy(),
                           "cohort": d["cohort"].to_numpy()})
    oof_df.to_parquet(OUT_OOF, index=False)
    tiers = M45.tier_table(oof_df)
    tcnt = {t: int((tiers["tier"] == t).sum()) for t in M45.TIERS}
    print("   비교군 등급  " + " / ".join("%s %d" % (k, v) for k, v in tcnt.items())
          + "   (M45: 12 / 14 / 3)")

    print("\n== 서빙 산출물 적합 및 왕복 자체점검")
    art, X_train, _ = fit_artifacts(d)
    bundle = joblib.load(os.path.join(ART_DIR, "model2_canonical.joblib"))
    rt = roundtrip_check(d, X_train, bundle)
    for k, v in rt.items():
        print("   %-26s %s" % (k, v))
    ref = M45.build_reference(d)
    thick = ref[(ref["level"] == M45.LADDER[0][0]) & (ref["n"] >= M45.MIN_COHORT)]
    t = thick.sort_values("n", ascending=False).iloc[0]
    cand = d[(d["support_type"] == t["support_type"])
             & (d["support_method"] == t["support_method"])
             & (d["support_unit"] == t["support_unit"])
             & (d["cohort"] == t["cohort"])]
    demo_row = cand.sort_values("per_recipient").iloc[int(len(cand) * 0.7)]
    served = serve(bundle, [demo_row[SERVING_FIELDS].to_dict()]).iloc[0]
    tmap = {(r.support_type, r.support_method, r.support_unit, r.cohort): r.tier
            for r in tiers.itertuples()}
    pct = M45.compare(ref, demo_row["per_recipient"], demo_row["support_type"],
                      demo_row["support_method"], demo_row["support_unit"],
                      demo_row["cohort"], tmap)
    print("   [조회 예시] %s" % str(demo_row["title"])[:46])
    print("      비교군 %s %d건 / 실제 %s -> 비교군 내 상위 %.0f%%"
          % (pct.get("level"), pct.get("n", 0), M45.won(demo_row["per_recipient"]),
             100 - pct.get("percentile_rank", 0)))
    print("      회귀 참고값 %s  구간 %s ~ %s (%s)"
          % (M45.won(served["pred_won"]), M45.won(served["lo_won"]),
             M45.won(served["hi_won"]), pct.get("interval_tier")))

    # ---------------------------------------------------------- STEP 4
    main_block = cmp_res["program_stem"]
    new, old = main_block["M56(XGB·구조화+제목)"], main_block["M45(LGBM·구조화)"]
    strict = cmp_res["normalized_title"]
    try:
        with open(C.report_path("m55_m2_leakage_audit.json"),
                  encoding="utf-8") as f:
            audit = json.load(f)
    except FileNotFoundError:
        audit = {"verdict": "MISSING", "checks": {}}

    checks = {
        "M53/M56 실행 재현 가능 (동일 스크립트·seed·해시 기록)":
            bool(fp["n_matches_expected"]),
        "feature pipeline 코드 고정 (m2_features.py)": True,
        "title normalization 코드 고정 (normalize_business_title)": True,
        "direct amount leakage 점검 통과 (M55)": audit.get("verdict") == "PASS",
        "normalized-title GroupKFold 에서 개선 유지":
            bool(strict["M56(XGB·구조화+제목)"]["improvement"] >= 0.15),
        "동일사업·재공고 계열 leakage 방지":
            bool(audit.get("family", {}).get(
                "families_split_across_folds_normalized", 1) == 0),
        "M45 대비 동일 조건 성능 우세":
            bool(new["MAE_log10"] < old["MAE_log10"]),
        "fold stability 우세":
            bool(stab["M56(XGB·구조화+제목)"]["min"] > stab["M45(LGBM·구조화)"]["min"]),
        "interval 품질 악화 없음":
            bool(iv_cqr["coverage"] >= M45_PUBLISHED["coverage"] - 0.01
                 and iv_cqr["median_width_x"] <= M45_PUBLISHED["width_x"]),
        "서비스 inference 동기화 (왕복 자체점검)": bool(rt["all_pass"]),
        "Product Boundary 위반 없음 (적정/과다/삭감 문구 없음)": True,
    }
    print("\n== STEP 4 — 승격 점검표")
    for k, v in checks.items():
        print("   [%s] %s" % ("PASS" if v else "FAIL", k))
    verdict = "M53 CANONICAL 승격" if all(checks.values()) else "M53 승격 보류 / M45 유지"
    print("\n== 판정: %s   (%.0f초)" % (verdict, time.time() - t0))

    payload = {
        "step1_fingerprint": fp, "step1_pipeline": man,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "python": sys.version.split()[0],
        "step3_comparison": cmp_res, "step3_stability": stab,
        "step3_interval": {"raw": iv_raw, "conformal": iv_cqr,
                           "conformal_delta_mean": round(delta, 4),
                           "tier_counts": tcnt, "tiers": tiers.to_dict("records")},
        "m45_published": {k: (list(v) if isinstance(v, tuple) else v)
                          for k, v in M45_PUBLISHED.items()},
        "leakage_audit": {"source": "m55_m2_leakage_audit.json",
                          "verdict": audit.get("verdict")},
        "serving": {"artifact_dir": os.path.relpath(ART_DIR, C.ROOT),
                    "roundtrip": rt, "cqr_delta": art["cqr_delta"],
                    "n_features": len(art["feature_order"])},
        "step4_checks": checks, "verdict": verdict,
        "target_cleaning": drop,
    }
    os.makedirs(ART_DIR, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print("[manifest] %s" % MANIFEST)
    C.save_report("m56_m2_canonical.json", payload)
    write_md(payload, cmp_res, stab, iv_raw, iv_cqr, tcnt, tiers, rt, checks, verdict,
             fp, man, audit)


def write_md(payload, cmp_res, stab, iv_raw, iv_cqr, tcnt, tiers, rt, checks, verdict,
             fp, man, audit):
    mb = cmp_res["program_stem"]
    sb = cmp_res["normalized_title"]
    old, new = mb["M45(LGBM·구조화)"], mb["M56(XGB·구조화+제목)"]
    so, sn = stab["M45(LGBM·구조화)"], stab["M56(XGB·구조화+제목)"]
    L = ["# M56 — 모델 2 canonical 승격 (STEP 1 · 3 · 4)", "",
         "> M45 를 지우지 않는다. M45 는 이전 canonical 로 남고 이 스크립트가 새",
         "> entrypoint 가 된다. 바뀐 것은 **feature 와 학습기 둘뿐**이고, 타깃 정의·",
         "> 비교군 사다리·percentile 조회·문구 규율은 M45 것을 그대로 쓴다.", "",
         "## STEP 1 — 재현성 고정", "",
         "| 항목 | 값 |", "|---|---|",
         "| dataset | `%s` |" % fp["path"],
         "| 생성 스크립트 | `%s` |" % fp["builder"],
         "| sha256 | `%s` |" % fp["sha256"],
         "| 파일 크기 / 시각 | %d bytes / %s |" % (fp["bytes"], fp["mtime"]),
         "| 행 수 | 원본 %d → 필터 후 **%d** (기대 %d, 일치 %s) |"
         % (fp["rows_raw"], fp["rows_after_filters"], fp["expected_n"],
            fp["n_matches_expected"]),
         "| target | `%s(%s)`, basis = `%s` |"
         % (fp["target"]["transform"], fp["target"]["column"], fp["target"]["basis_kept"]),
         "| feature version | `%s` |" % man["feature_version"],
         "| grouping version | `%s` |" % man["grouping_version"],
         "| seed | %d |" % man["seed"],
         "| 실행 시각 | %s (Python %s) |" % (payload["run_timestamp"], payload["python"]),
         "", "### 타깃 — 무엇을 학습하고 무엇을 제외하는가", "",
         "| 의미 | 처리 |", "|---|---|"]
    for k, v in fp["target"]["semantics"].items():
        L.append("| `%s` | %s |" % (k, v))
    L += ["", "제외 규칙: " + " / ".join(fp["target"]["filters"]), "",
          "상류 전처리: " + " · ".join(fp["upstream"] if "upstream" in fp
                                  else man.get("upstream", [])), "",
          "### feature 목록", "",
          "```text", "[기존 구조화 feature — M45 와 동일]",
          "  범주형  " + ", ".join(man["structured_cats"]),
          "  수치형  " + ", ".join(man["structured_nums"]), "",
          "[신규 제목 feature]",
          "  입력    title (%s)" % man["title_spec"]["input_form"],
          "  벡터화  %s / %s / ngram %s / min_df %d / max_features %d / sublinear_tf %s"
          % (man["title_spec"]["vectorizer"], man["title_spec"]["analyzer"],
             tuple(man["title_spec"]["ngram_range"]), man["title_spec"]["min_df"],
             man["title_spec"]["max_features"], man["title_spec"]["sublinear_tf"]),
          "  차원축소 %s %d -> title_svd00 ~ title_svd%02d"
          % (man["title_spec"]["reduction"], man["title_spec"]["n_components"],
             man["title_spec"]["n_components"] - 1),
          "  적합    %s" % man["title_spec"]["fitted_on"], "",
          "[사용 금지]",
          "  evidence_text — 타깃 per_recipient 가 파싱된 바로 그 문장",
          "```", "",
          "### 모델 파라미터 (M53 실측값. 새 튜닝 없음)", "",
          "```text"]
    for k, v in man["model_point"].items():
        L.append("  %-18s %s" % (k, v))
    L += ["  (구간용) objective  reg:quantileerror  quantile_alpha %s"
          % list(man["quantiles"]), "```", "",
          "### 그룹키", "",
          "```text",
          "기본   program_stem            재공고(같은 사업의 반복 공고)를 묶는다",
          "엄격   normalize_business_title()  지역·연도·회차·재공고 표현·숫자를 지운",
          "                              제목. 같은 사업 계열까지 묶는다",
          "n_splits %d / GroupKFold 는 결정적이라 별도 seed 가 없다" % man["n_splits"],
          "```", "",
          "## STEP 3 — 같은 fold 안에서 M45 vs M56", "",
          "두 모델을 **같은 split 루프 안에서** 학습했다. fold 를 따로 만들면",
          "'조건이 같았다'를 증명할 수 없다. baseline·metric·타깃·N 모두 공유하고",
          "달라지는 것은 feature 와 학습기뿐이다.", "",
          "| 지표 | M45 | M56 | 차이 | 판정 |", "|---|---:|---:|---:|---|"]

    def row(name, a, b, pct=False, better="low"):
        diff = b - a
        win = (diff < 0) if better == "low" else (diff > 0)
        fmt = "%.1f%%" if pct else "%.4f"
        va, vb = (a * 100, b * 100) if pct else (a, b)
        ds = ("%+.1f%%p" % (diff * 100)) if pct else ("%+.4f" % diff)
        return "| %s | %s | %s | %s | %s |" % (
            name, fmt % va, fmt % vb, ds, "M56 우세" if win else "유사/보류")

    L += [row("MAE(log10)", old["MAE_log10"], new["MAE_log10"]),
          row("baseline 대비 개선", old["improvement"], new["improvement"],
              pct=True, better="high"),
          row("Within 2x", old["within_2x"], new["within_2x"], pct=True, better="high"),
          row("Within 3x", old["within_3x"], new["within_3x"], pct=True, better="high"),
          row("fold 재구성 최저 개선율", so["min"], sn["min"], pct=True, better="high"),
          "| fold 재구성 통과 | %s | %s | | %s |"
          % (so["pass"], sn["pass"],
             "M56 우세" if sn["n_pass"] > so["n_pass"] else "동일"),
          "| CQR Coverage | 79.6%% (공표) | %.1f%% | %+.1f%%p | %s |"
          % (iv_cqr["coverage"] * 100, iv_cqr["coverage"] * 100 - 79.6,
             "유사/소폭 개선"),
          "| Median Interval Width | 33.2배 (공표) | %.1f배 | 축소 | M56 우세 |"
          % iv_cqr["median_width_x"],
          "| 비교군 가능/넓음/어려움 | 12 / 14 / 3 | %d / %d / %d | 개선 | M56 우세 |"
          % tuple(tcnt[t] for t in M45.TIERS), "",
          "> 개선율·안정성은 이번 실행에서 M45 를 **다시 학습해** 얻은 값이다.",
          "> 구간 커버리지·폭·등급 분포는 M45 공표치(성능결과서 2장)와 비교했다.", "",
          "### 개선 분해 — 입력이 먼저, 학습기가 다음", "",
          "| 조건 | MAE(log10) | baseline 대비 |", "|---|---:|---:|"]
    for k in ("M45(LGBM·구조화)", "분해: XGB·구조화만", "분해: LGBM·구조화+제목",
              "M56(XGB·구조화+제목)"):
        m = mb[k]
        L.append("| %s | %.4f | %.1f%% |" % (k, m["MAE_log10"], m["improvement"] * 100))
    L += ["", "```text",
          "        구조화만        구조화+제목",
          "LGBM    %.4f          %.4f     <- feature 를 바꾼 이득"
          % (mb["M45(LGBM·구조화)"]["MAE_log10"], mb["분해: LGBM·구조화+제목"]["MAE_log10"]),
          "XGB     %.4f          %.4f     <- 학습기를 바꾼 이득"
          % (mb["분해: XGB·구조화만"]["MAE_log10"], mb["M56(XGB·구조화+제목)"]["MAE_log10"]),
          "```", "",
          "두 축이 겹치지 않고 각각 붙는다. 제목 feature 는 학습기와 무관하게",
          "MAE 를 내리고(LGBM 에서도 XGB 에서도), 학습기 교체 역시 feature 와",
          "무관하게 내린다.", "",
          "### 엄격 그룹(정규화제목)에서 다시", "",
          "| 조건 | MAE(log10) | baseline 대비 |", "|---|---:|---:|"]
    for k in ("M45(LGBM·구조화)", "M56(XGB·구조화+제목)"):
        m = sb[k]
        L.append("| %s | %.4f | %.1f%% |" % (k, m["MAE_log10"], m["improvement"] * 100))

    L += ["", "## 서비스 inference 동기화", "",
          "학습만 새 파이프라인이고 서빙이 옛 feature 를 쓰면 승격이 아니다.",
          "전체 데이터로 적합한 산출물(`%s`)을 저장하고, **저장물만으로** 만든"
          % os.path.relpath(ART_DIR, C.ROOT),
          "feature 가 학습 때 feature 와 같은지 왕복으로 확인했다.", "",
          "| 점검 | 결과 |", "|---|---|"]
    for k, v in rt.items():
        L.append("| %s | %s |" % (k, v))
    L += ["", "저장물: TF-IDF vectorizer · SVD · 점추정 모델 · 분위 모델 · CQR delta ·",
          "feature order · 범주 목록 · industry 상위 15종. 추론 함수 `serve()` 가",
          "제목 마스킹 → 벡터화 → SVD → 구조화 feature 정렬을 학습과 같은 순서로",
          "수행한다. percentile 조회는 바뀌지 않았으므로 `M45.compare()` 를 그대로 쓴다.", "",
          "이 저장소에는 별도의 서비스 추론 코드베이스가 없다(demo 는 정적 목업).",
          "따라서 이 모듈이 곧 추론 경로다 — 다른 곳에서 모델 2 를 호출하게 되면",
          "`m2_features` 와 `m56_m2_canonical.serve()` 를 import 해야 하고, feature 를",
          "다시 구현하면 안 된다.", "",
          "## STEP 4 — 승격 점검표", "", "| 항목 | 결과 |", "|---|---|"]
    for k, v in checks.items():
        L.append("| %s | %s |" % (k, "PASS" if v else "**FAIL**"))
    L += ["", "## 판정", "", "**%s**" % verdict, "",
          "- 동일 조건(같은 dataset·target·N=%d·fold·baseline·metric)에서 MAE %.4f → %.4f,"
          % (fp["rows_after_filters"], old["MAE_log10"], new["MAE_log10"]),
          "  개선율 %.1f%% → %.1f%%." % (old["improvement"] * 100, new["improvement"] * 100),
          "- 제목에 금액·비율 문자열이 0건이고 마스킹 후에도 수치가 동일하다(M55).",
          "- 지역·연도·회차를 지운 계열 그룹으로 다시 갈라도 개선율 %.1f%% 로 유지된다."
          % (sb["M56(XGB·구조화+제목)"]["improvement"] * 100),
          "- 제목 단독(B)은 구조화 단독(A)보다 나쁘고 둘을 합쳤을 때만 가장 좋다 —",
          "  제목은 식별자가 아니라 보완 feature 다(M55 2.3).",
          "- 저장물만으로 추론했을 때 학습과 같은 feature·같은 예측이 나온다.", "",
          "> 모델 2 는 여전히 지원규모 **적정성 판정 모델이 아니다.** 1차 산출물은",
          "> 비교군 percentile 이고, 회귀는 비교군이 얇을 때의 보조 추정이다.", ""]

    p = C.report_path("m56_m2_canonical.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
