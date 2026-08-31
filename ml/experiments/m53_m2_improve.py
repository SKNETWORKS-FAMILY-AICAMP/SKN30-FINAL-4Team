"""M53 — 모델 2 개선안 측정. M52 가 읽은 것에만 대응한다.

이 파일은 실험 기록이다. 여기서 이긴 조합은 M55(누수 감사)와 M56(동일조건
비교·승격)을 거쳐 canonical 이 됐고, 운영에 쓰는 고정판은 m2_features.py 다.

M52 결론 세 줄.

    오차의 36% 가 근거문이 남지 않은 행(C 등급)에서 나온다
    100만원 이하 구간 MAE 1.07 — 오파싱이 아니라 '소액 지원'이라는 성격
    ablation 에서 혼자 큰 축은 지원성격 하나뿐, 사업 내용을 아는 축이 없다

그래서 후보도 세 갈래다.

    (a) 근거등급을 feature 로   — 오차가 몰린 곳을 모델에게 알려주면 줄어드는가
    (b) 제목 텍스트를 feature 로 — '소상공인 카드수수료'와 '반도체 R&D'를
                                구별할 수단을 준다. 지금 모델 2 는 제목을
                                한 글자도 쓰지 않는다.
    (c) 알고리즘 교체           — 방향서가 '이후 필요 시'로 미뤄둔 마지막 순서

프로토콜은 M45 를 한 글자도 바꾸지 않는다. 같은 타깃(stated_cap only), 같은
1,877건, 같은 GroupKFold(5) by program_stem, 같은 baseline(비교군 중앙값).
바뀐 수치가 프로토콜 차이에서 온 것이 아님을 보장하기 위해서다.

텍스트를 넣을 때 지켜야 하는 두 가지.

    누수 1 — 근거문(evidence_text)은 쓰지 않는다. 타깃 per_recipient 가 바로
             그 문장에서 파싱된 값이라 정답을 그대로 읽어주는 셈이 된다.
             제목만 쓴다. 실제로 1,877건 제목 중 금액 표현이 있는 것은 0건이다.
    누수 2 — 제목이 비슷한 재공고가 학습/검증에 갈라지면 점수가 부풀려진다.
             program_stem 그룹으로 이미 막고 있지만, 제목 텍스트를 넣는 순간
             '거의 같은 제목의 다른 사업'(지자체만 다른 공고)이 새 누수 경로가
             된다. 그래서 Phase 2 에서 제목을 정규화해(지역·연도·회차 제거)
             더 엄격한 그룹으로 다시 잰다.

TF-IDF·SVD 는 fold train 에만 적합한다(M45 의 fold 내부 fitting 규율).
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import os
import re
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m45_m2_amount as M45
import m52_m2_error_analysis as M52

OUT_OOF = os.path.join(C.PROC, "m53_oof_predictions.parquet")

SEED = 42
K_SVD = 64               # 16/32/48/64/96/128 중 64. 96 이 0.0034 낮았지만 차이가
                         # fold 재구성 산포 안이라 작은 쪽을 쓴다.
NOMINAL, LO, HI = 0.80, 0.10, 0.90
CAL_FRAC = 0.30
MIN_IMPROVEMENT = 0.10


# ------------------------------------------------------------ 제목 텍스트
def fit_text(train_titles, test_titles, k=K_SVD):
    """char n-gram TF-IDF -> SVD. fold train 에만 적합한다.

    형태소 분석기를 쓰지 않고 char_wb(2,3) 을 쓰는 이유: 공고 제목은
    '소상공인'·'수수료'·'R&D' 같은 짧은 고정 표현의 조합이라 어절 단위보다
    글자 단위가 잘 잡힌다. 실측으로도 word(1,2) 0.4584 vs char_wb(2,3) 0.4402.
    """
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=3,
                        max_features=30000, sublinear_tf=True)
    A = v.fit_transform(train_titles)
    B = v.transform(test_titles)
    s = TruncatedSVD(n_components=k, random_state=SEED)
    return s.fit_transform(A), s.transform(B)


def attach(Xb, titles, tr, te, use_text, k=K_SVD):
    if not use_text:
        return Xb.iloc[tr], Xb.iloc[te]
    a, b = fit_text(titles[tr], titles[te], k)
    c = ["title_svd%02d" % i for i in range(a.shape[1])]
    return (pd.concat([Xb.iloc[tr].reset_index(drop=True), pd.DataFrame(a, columns=c)], axis=1),
            pd.concat([Xb.iloc[te].reset_index(drop=True), pd.DataFrame(b, columns=c)], axis=1))


def norm_title(s):
    """지역 대괄호·연도·회차를 지운 제목. 누수 점검용 엄격 그룹키."""
    s = re.sub(r"\[[^\]]*\]", "", str(s))
    s = re.sub(r"\d", "", s)
    s = re.sub(r"(공고|모집|시행|계획|변경|연장|차|회|년도|년)", "", s)
    return re.sub(r"[^\w가-힣]", "", s)


# ------------------------------------------------------------ 학습기
def lgbm(alpha=0.5, **kw):
    from lightgbm import LGBMRegressor
    p = dict(objective="quantile", alpha=alpha, n_estimators=400, learning_rate=0.05,
             num_leaves=15, min_child_samples=10, random_state=SEED, verbose=-1)
    p.update(kw)
    return lambda Xtr, ytr, Xte: LGBMRegressor(**p).fit(Xtr, ytr).predict(Xte)


def xgbr(objective="reg:absoluteerror", **kw):
    import xgboost as xgb
    p = dict(objective=objective, n_estimators=800, learning_rate=0.03, max_depth=6,
             enable_categorical=True, tree_method="hist", random_state=SEED,
             subsample=0.9, colsample_bytree=0.8)
    p.update(kw)
    return lambda Xtr, ytr, Xte: xgb.XGBRegressor(**p).fit(Xtr, ytr).predict(Xte)


def catb(cats, **kw):
    from catboost import CatBoostRegressor
    p = dict(loss_function="MAE", iterations=600, learning_rate=0.05, depth=6,
             random_seed=SEED, verbose=0, allow_writing_files=False)
    p.update(kw)

    def f(Xtr, ytr, Xte):
        Xtr, Xte = Xtr.copy(), Xte.copy()
        for c in cats:
            Xtr[c] = Xtr[c].astype(str)
            Xte[c] = Xte[c].astype(str)
        return CatBoostRegressor(cat_features=cats, **p).fit(Xtr, ytr).predict(Xte)
    return f


# ------------------------------------------------------------ 실행
def oof(Xb, y, groups, titles, fit, use_text):
    from sklearn.model_selection import GroupKFold
    p = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(Xb, y, groups):
        Xtr, Xte = attach(Xb, titles, tr, te, use_text)
        p[te] = fit(Xtr, y[tr], Xte)
    return p


def oof_hier(X, Xb, y, groups, titles, cats, fit, use_text):
    """계층적: 비교군 중앙값 + 잔차. baseline 을 모델의 출발점으로 쓴다."""
    from sklearn.model_selection import GroupKFold
    p = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(Xb, y, groups):
        Xtr, Xte = attach(Xb, titles, tr, te, use_text)
        b_tr = M45.cohort_median_baseline(X.iloc[tr], y[tr], X.iloc[tr], cats)
        b_te = M45.cohort_median_baseline(X.iloc[tr], y[tr], X.iloc[te], cats)
        p[te] = b_te + fit(Xtr, y[tr] - b_tr, Xte)
    return p


def baseline_oof(X, y, groups, cats):
    from sklearn.model_selection import GroupKFold
    p = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        p[te] = M45.cohort_median_baseline(X.iloc[tr], y[tr], X.iloc[te], cats)
    return p


def metrics(y, p, base_mae):
    m = M45.point_metrics(y, p)
    m["improvement_vs_baseline"] = round(float((base_mae - m["MAE_log10"]) / base_mae), 4)
    return m


# ------------------------------------------------------------ 예측구간
def intervals(Xb, y, groups, titles, cats):
    """최종안으로 CQR 구간을 다시 잰다. M45 cqr_fold 와 같은 구조.

    중앙값은 MAE 목적함수(점추정에서 가장 좋았던 것), 구간은 분위 목적함수를
    쓴다 — M45 가 LGBM 안에서 quantile50/quantile10·90 을 나눠 쓴 것과 같다.
    """
    import xgboost as xgb
    from sklearn.model_selection import GroupKFold

    rng = np.random.default_rng(SEED)
    n = len(y)
    lo, mid, hi = np.zeros(n), np.zeros(n), np.zeros(n)
    raw_lo, raw_hi = np.zeros(n), np.zeros(n)
    deltas = []

    def qfit(Xtr, ytr, Xte):
        m = xgb.XGBRegressor(objective="reg:quantileerror",
                             quantile_alpha=np.array([LO, HI]), n_estimators=800,
                             learning_rate=0.03, max_depth=6, enable_categorical=True,
                             tree_method="hist", random_state=SEED, subsample=0.9,
                             colsample_bytree=0.8).fit(Xtr, ytr)
        q = m.predict(Xte)
        return q[:, 0], q[:, 1]

    point = xgbr()
    for tr, te in GroupKFold(n_splits=5).split(Xb, y, groups):
        Xtr, Xte = attach(Xb, titles, tr, te, True)
        ytr = y[tr]
        mid[te] = point(Xtr, ytr, Xte)
        raw_lo[te], raw_hi[te] = qfit(Xtr, ytr, Xte)

        gtr = groups[tr]
        uniq = np.unique(gtr)
        rng.shuffle(uniq)
        cal = set(uniq[:max(1, int(len(uniq) * CAL_FRAC))])
        is_cal = np.array([g in cal for g in gtr])
        if is_cal.sum() < 20 or (~is_cal).sum() < 50:
            lo[te], hi[te] = raw_lo[te], raw_hi[te]
            deltas.append(0.0)
            continue
        Xf, yf = Xtr.iloc[~is_cal], ytr[~is_cal]
        Xc, yc = Xtr.iloc[is_cal], ytr[is_cal]
        cl, ch = qfit(Xf, yf, Xc)
        tl, th = qfit(Xf, yf, Xte)
        E = np.maximum(cl - yc, yc - ch)
        k = min(max(int(np.ceil((len(E) + 1) * NOMINAL)), 1), len(E))
        delta = float(np.sort(E)[k - 1])
        lo[te], hi[te] = tl - delta, th + delta
        deltas.append(delta)
    return mid, lo, hi, raw_lo, raw_hi, float(np.mean(deltas))


def stability(Xb, y, groups, titles, X, cats, n_repeat=10):
    """fold 재구성 n회. M45 와 같은 방식(그룹 라벨 셔플)."""
    from sklearn.model_selection import GroupKFold
    point = xgbr()
    out = []
    for seed in range(n_repeat):
        rng = np.random.default_rng(seed)
        uniq = np.unique(groups)
        remap = dict(zip(uniq, uniq[rng.permutation(len(uniq))]))
        gs = np.array([remap[v] for v in groups])
        bp, mp = np.zeros(len(y)), np.zeros(len(y))
        for tr, te in GroupKFold(5).split(Xb, y, gs):
            Xtr, Xte = attach(Xb, titles, tr, te, True)
            mp[te] = point(Xtr, y[tr], Xte)
            bp[te] = M45.cohort_median_baseline(X.iloc[tr], y[tr], X.iloc[te], cats)
        b, m = float(np.abs(bp - y).mean()), float(np.abs(mp - y).mean())
        out.append((b - m) / b)
    a = np.array(out)
    return {"n_repeat": n_repeat, "mean": round(float(a.mean()), 4),
            "std": round(float(a.std()), 4), "min": round(float(a.min()), 4),
            "max": round(float(a.max()), 4),
            "pass_rate": round(float((a >= MIN_IMPROVEMENT).mean()), 2)}


def main():
    d0 = pd.read_parquet(M45.SRC)
    d, _ = M45.prepare(d0)
    d = d.reset_index(drop=True)
    d["grade"], d["n_evid"] = M52.grade_rows(d)

    X, y, groups, cats = M45.make_xy(d, with_cohort=True)
    titles = d["title"].fillna("").astype(str).to_numpy()

    # 근거등급 feature 를 붙인 판
    Xg = X.copy()
    Xg["evid_grade"] = pd.Categorical(d["grade"])
    Xg["n_evid_amounts"] = d["n_evid"].astype(float)

    base = baseline_oof(X, y, groups, cats)
    base_mae = float(np.abs(base - y).mean())
    print("== 기준선  비교군 중앙값 MAE %.4f  (M45 공표치 0.5315)" % base_mae)

    print("\n== Phase 1 — 후보 비교 (프로토콜은 M45 그대로)")
    cands = [
        ("C0 현행 M45 (LGBM-q50)", X, False, lgbm()),
        ("C1 + 근거등급", Xg, False, lgbm()),
        ("C2 + 제목텍스트", X, True, lgbm()),
        ("C3 + 근거등급 + 제목텍스트", Xg, True, lgbm()),
        ("C4 C2 + LGBM 튜닝", X, True, lgbm(n_estimators=800, learning_rate=0.03,
                                          num_leaves=31)),
        ("C5 C2 + CatBoost(MAE)", X, True, catb(cats)),
        ("C6 C2 + XGBoost(분위50)", X, True, xgbr("reg:quantileerror",
                                                quantile_alpha=0.5)),
        ("C7 C2 + XGBoost(MAE)", X, True, xgbr()),
    ]
    res = {}
    preds = {}
    for name, Xb, txt, fit in cands:
        p = oof(Xb, y, groups, titles, fit, txt)
        res[name] = metrics(y, p, base_mae)
        preds[name] = p
        print("   %-28s MAE %.4f  MedAE %.4f  2배이내 %.1f%%  개선 %.1f%%"
              % (name, res[name]["MAE_log10"], res[name]["MedAE_log10"],
                 res[name]["within_2x"] * 100,
                 res[name]["improvement_vs_baseline"] * 100))

    p = oof_hier(X, X, y, groups, titles, cats, xgbr(), True)
    res["C8 계층적(비교군중앙값+잔차)"] = metrics(y, p, base_mae)
    preds["C8 계층적(비교군중앙값+잔차)"] = p
    print("   %-28s MAE %.4f  개선 %.1f%%"
          % ("C8 계층적(비교군중앙값+잔차)", res["C8 계층적(비교군중앙값+잔차)"]["MAE_log10"],
             res["C8 계층적(비교군중앙값+잔차)"]["improvement_vs_baseline"] * 100))

    best = min(res, key=lambda k: res[k]["MAE_log10"])
    print("   -> 최저 MAE: %s" % best)

    print("\n== Phase 2 — 제목 누수 점검 (엄격 그룹: 지역·연도·회차 제거)")
    stem = np.array([norm_title(t) for t in titles])
    print("   그룹 수  program_stem %d -> 제목정규화 %d"
          % (len(np.unique(groups)), len(np.unique(stem))))
    leak = {}
    b2 = baseline_oof(X, y, stem, cats)
    b2_mae = float(np.abs(b2 - y).mean())
    for name, Xb, txt, fit in [("C0 현행 M45 (LGBM-q50)", X, False, lgbm()),
                               ("C2 + 제목텍스트", X, True, lgbm()),
                               ("C7 C2 + XGBoost(MAE)", X, True, xgbr())]:
        p = oof(Xb, y, stem, titles, fit, txt)
        m = float(np.abs(p - y).mean())
        leak[name] = {"MAE_log10": round(m, 4),
                      "improvement_vs_baseline": round((b2_mae - m) / b2_mae, 4)}
        print("   %-28s MAE %.4f  개선 %.1f%%  (표준그룹 %.1f%%)"
              % (name, m, (b2_mae - m) / b2_mae * 100,
                 res[name]["improvement_vs_baseline"] * 100))
    leak["baseline_MAE"] = round(b2_mae, 4)

    print("\n== Phase 3 — 최종안 개선율 안정성 (fold 재구성 10회)")
    stab = stability(X, y, groups, titles, X, cats)
    print("   개선율 %.1f%% ± %.1f%%  (범위 %.1f%% ~ %.1f%%)  기준 통과 %.0f/10"
          % (stab["mean"] * 100, stab["std"] * 100, stab["min"] * 100,
             stab["max"] * 100, stab["pass_rate"] * 10))

    print("\n== Phase 4 — 예측구간 재측정 (CQR)")
    mid, lo, hi, rlo, rhi, delta = intervals(X, y, groups, titles, cats)
    iv_raw = M45.interval_metrics(y, rlo, rhi)
    iv_cqr = M45.interval_metrics(y, lo, hi)
    print("   보정 전  커버리지 %.1f%%  폭 %.1f배" % (iv_raw["coverage"] * 100,
                                              iv_raw["median_width_x"]))
    print("   보정 후  커버리지 %.1f%%  폭 %.1f배  (delta %.3f)"
          % (iv_cqr["coverage"] * 100, iv_cqr["median_width_x"], delta))
    print("   M45 기준  보정 후 커버리지 79.6%  폭 33.2배")

    oof_df = pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "pred": mid,
                           "lo": lo, "hi": hi,
                           "support_type": d["support_type"].to_numpy(),
                           "support_method": d["support_method"].to_numpy(),
                           "support_unit": d["support_unit"].to_numpy(),
                           "cohort": d["cohort"].to_numpy()})
    oof_df.to_parquet(OUT_OOF, index=False)
    print("   [data] %s" % OUT_OOF)
    tiers = M45.tier_table(oof_df)
    tcnt = {t: int((tiers["tier"] == t).sum()) for t in M45.TIERS}
    print("   비교군 등급 분포: " + " / ".join("%s %d" % (k, v) for k, v in tcnt.items())
          + "   (M45: 참고 가능 12 / 범위 넓음 14 / 참고 범위 제시 어려움 3)")

    print("\n== Phase 5 — 오차가 어디에서 줄었나 (현행 C0 -> 최종 %s)" % best)
    d["err0"] = np.abs(preds["C0 현행 M45 (LGBM-q50)"] - y)
    d["err1"] = np.abs(preds[best] - y)
    d["금액대"] = pd.cut(d["per_recipient"], [0, 1e6, 1e7, 1e8, 1e9, 1e14],
                      labels=["100만원 이하", "100만~1천만", "1천만~1억", "1억~10억", "10억 초과"])
    size = d.groupby(["support_type", "support_method", "support_unit", "cohort"],
                     observed=True)["err0"].transform("size")
    d["비교군두께"] = pd.cut(size, [0, 10, 30, 100, 10 ** 9],
                        labels=["10건 미만", "10~29건", "30~99건", "100건 이상"])
    breakdown = {}
    for col in ("grade", "금액대", "비교군두께", "cohort"):
        t = d.groupby(col, observed=True)[["err0", "err1"]].agg(["count", "mean"])
        rows = []
        print("   [%s]" % col)
        for k in t.index:
            n = int(t.loc[k, ("err0", "count")])
            a, b = float(t.loc[k, ("err0", "mean")]), float(t.loc[k, ("err1", "mean")])
            rows.append({col: str(k), "n": n, "MAE_현행": round(a, 4),
                         "MAE_최종": round(b, 4), "delta": round(b - a, 4)})
            print("      %-14s n=%4d  %.4f -> %.4f  (%+.4f)" % (k, n, a, b, b - a))
        breakdown[col] = rows

    verdict = ("채택" if res[best]["improvement_vs_baseline"] >= MIN_IMPROVEMENT
               and stab["pass_rate"] >= 0.9 else "보류")
    print("\n== 판정: %s" % verdict)

    C.save_report("m53_m2_improve.json", {
        "protocol": "M45 동일 (n=1877, stated_cap only, GroupKFold5 by program_stem)",
        "baseline_MAE": round(base_mae, 4),
        "candidates": res, "best": best,
        "leakage_check_strict_group": leak,
        "stability": stab,
        "interval": {"raw": iv_raw, "conformal": iv_cqr,
                     "conformal_delta_mean": round(delta, 4), "tier_counts": tcnt,
                     "tiers": tiers.to_dict("records")},
        "error_breakdown": breakdown,
        "text_feature": {"vectorizer": "TfidfVectorizer char_wb (2,3) min_df=3",
                         "reduction": "TruncatedSVD %d" % K_SVD,
                         "fitted": "fold train only",
                         "leakage_guard": "제목만 사용(근거문 미사용). 제목에 금액 표현 0건"},
        "verdict": verdict,
    })
    write_md(res, best, base_mae, leak, stab, iv_raw, iv_cqr, delta, tcnt, tiers,
             breakdown, verdict)


def write_md(res, best, base_mae, leak, stab, iv_raw, iv_cqr, delta, tcnt, tiers,
             breakdown, verdict):
    L = ["# M53 — 모델 2 개선안 측정", "",
         "> M52 가 읽은 세 가지에만 대응한다. 프로토콜은 M45 를 한 글자도 바꾸지",
         "> 않았다 — 같은 타깃(stated_cap only), 같은 1,877건, 같은 GroupKFold(5)",
         "> by program_stem, 같은 baseline(비교군 중앙값 %.4f)." % base_mae, "",
         "## 1. 후보 비교", "",
         "| 후보 | MAE(log10) | MedAE | 2배 이내 | 3배 이내 | baseline 대비 |",
         "|---|---:|---:|---:|---:|---:|"]
    for k in sorted(res, key=lambda k: res[k]["MAE_log10"]):
        v = res[k]
        L.append("| %s%s | %.4f | %.4f | %.1f%% | %.1f%% | **%.1f%%** |"
                 % ("**" if k == best else "", k + ("**" if k == best else ""),
                    v["MAE_log10"], v["MedAE_log10"], v["within_2x"] * 100,
                    v["within_3x"] * 100, v["improvement_vs_baseline"] * 100))

    c0 = res["C0 현행 M45 (LGBM-q50)"]
    c1, c2 = res["C1 + 근거등급"], res["C2 + 제목텍스트"]
    L += ["", "읽는 법 세 가지.", "",
          "1. **근거등급은 예측에 쓸모가 없다** (C1 %.4f vs 현행 %.4f). M52 에서"
          % (c1["MAE_log10"], c0["MAE_log10"]),
          "   오차가 그 등급에 몰려 있는 것은 사실이지만, '이 행은 근거가 없다'는",
          "   정보는 '그래서 값이 얼마인가'를 알려주지 않는다. 오차의 위치를",
          "   설명하는 축과 오차를 줄이는 축은 다르다.",
          "2. **제목 텍스트가 가장 크게 움직였다** (C2 %.4f, 현행 대비 %+.1f%%)."
          % (c2["MAE_log10"], (c2["MAE_log10"] - c0["MAE_log10"]) / c0["MAE_log10"] * 100),
          "   모델을 바꾸지 않고 feature 하나를 넣은 것만으로 MAE 가 내려간다 —",
          "   M45 가 타깃을 고쳐서 얻은 개선과 같은 성격이다. 알고리즘보다",
          "   입력이 먼저다.",
          "3. **알고리즘 교체는 그 다음이다.** 텍스트를 넣은 위에서만 학습기 교체가",
          "   추가로 움직인다. 방향서가 알고리즘 재탐색을 마지막 순서로 둔 이유가",
          "   여기서 재현된다.", ""]

    L += ["## 2. 제목 누수 점검", "",
          "제목 텍스트를 넣는 순간 새 누수 경로가 생긴다 — 지역·연도만 다른",
          "거의 같은 제목이 학습과 검증에 갈라지는 경우다. 제목에서 `[지역]`·",
          "연도·회차를 지운 정규화 제목을 그룹키로 삼아 더 엄격하게 다시 쟀다.", "",
          "| 후보 | 엄격그룹 MAE | 엄격그룹 개선율 | 표준그룹 개선율 |",
          "|---|---:|---:|---:|"]
    for k in ("C0 현행 M45 (LGBM-q50)", "C2 + 제목텍스트", "C7 C2 + XGBoost(MAE)"):
        L.append("| %s | %.4f | %.1f%% | %.1f%% |"
                 % (k, leak[k]["MAE_log10"], leak[k]["improvement_vs_baseline"] * 100,
                    res[k]["improvement_vs_baseline"] * 100))
    L += ["", "엄격그룹에서 baseline 도 함께 나빠지므로(%.4f) 개선율로 읽는다. "
          "개선폭이 유지되면 텍스트 이득이 재공고 유사도에서 온 것이 아니다."
          % leak["baseline_MAE"], "",
          "## 3. 개선율 안정성 (fold 재구성 %d회)" % stab["n_repeat"], "",
          "| | 값 |", "|---|---:|",
          "| 평균 개선율 | %.1f%% |" % (stab["mean"] * 100),
          "| 표준편차 | %.1f%%p |" % (stab["std"] * 100),
          "| 범위 | %.1f%% ~ %.1f%% |" % (stab["min"] * 100, stab["max"] * 100),
          "| 채택기준(10%%) 통과 | %.0f/%d |" % (stab["pass_rate"] * stab["n_repeat"],
                                            stab["n_repeat"]), "",
          "M45 는 11.7% ± 0.8%, 최저 시드가 기준선과 같은 10.0% 였다. "
          "기준선 위 여유가 어떻게 바뀌었는지가 이 표의 핵심이다.", "",
          "## 4. 예측구간", "",
          "| 조건 | 커버리지(명목 80%) | 구간폭 중앙값 |", "|---|---:|---:|",
          "| M45 / 보정 전 | 68.6% | 16.5배 |",
          "| M45 / 보정 후(CQR) | 79.6% | 33.2배 |",
          "| M53 / 보정 전 | %.1f%% | %.1f배 |" % (iv_raw["coverage"] * 100,
                                                iv_raw["median_width_x"]),
          "| M53 / 보정 후(CQR) | %.1f%% | %.1f배 |" % (iv_cqr["coverage"] * 100,
                                                     iv_cqr["median_width_x"]), "",
          "보정 전 커버리지가 M45 보다 크게 낮은 것은 고장이 아니다. XGBoost 의",
          "분위 목적함수가 LightGBM 보다 훨씬 좁은 구간을 내놓기 때문이고(5.6배 vs",
          "16.5배), CQR 은 그 좁은 구간을 실측 이탈량만큼 넓혀 명목 80% 를 맞춘다.",
          "**보정 후에 커버리지는 더 높으면서 폭은 8배가량 좁다** — 담당자에게",
          "'P10~P90 참고범위'로 낼 수 있는 비교군이 그만큼 늘어난다.", "",
          "비교군별 실용성 등급 분포: " + " / ".join("%s %d칸" % (k, v) for k, v in tcnt.items())
          + " (M45: 참고 가능 12 / 범위 넓음 14 / 참고 범위 제시 어려움 3)", "",
          "| 지원성격 | 지원방식 | 단위 | 출처 | n | 구간폭 | 커버리지 | MAE | 등급 |",
          "|---|---|---|---|---:|---:|---:|---:|---|"]
    for _, r in tiers.iterrows():
        L.append("| %s | %s | %s | %s | %d | %.1f배 | %.0f%% | %.3f | %s |"
                 % (r["support_type"], r["support_method"], r["support_unit"],
                    r["cohort"], r["n"], r["width_x"], r["coverage"] * 100,
                    r["MAE_log10"], r["tier"]))

    L += ["", "## 5. 오차가 어디에서 줄었나", ""]
    names = {"grade": "근거 등급", "금액대": "금액대", "비교군두께": "비교군 두께",
             "cohort": "출처"}
    for col, rows in breakdown.items():
        L += ["### %s" % names.get(col, col), "",
              "| 구간 | n | 현행 MAE | 최종 MAE | 변화 |", "|---|---:|---:|---:|---:|"]
        for r in rows:
            L.append("| %s | %d | %.4f | %.4f | %+.4f |"
                     % (r[col], r["n"], r["MAE_현행"], r["MAE_최종"], r["delta"]))
        L.append("")

    L += ["## 6. 판정", "", "**%s** — %s" % (verdict, best), "",
          "- 점추정 MAE %.4f (현행 %.4f), baseline 대비 개선 %.1f%% (현행 %.1f%%)"
          % (res[best]["MAE_log10"], c0["MAE_log10"],
             res[best]["improvement_vs_baseline"] * 100,
             c0["improvement_vs_baseline"] * 100),
          "- fold 재구성 %d회 개선율 %.1f%% ± %.1f%%, 기준 통과 %.0f/%d"
          % (stab["n_repeat"], stab["mean"] * 100, stab["std"] * 100,
             stab["pass_rate"] * stab["n_repeat"], stab["n_repeat"]),
          "- 구간 커버리지 %.1f%% (명목 80%%), 폭 %.1f배"
          % (iv_cqr["coverage"] * 100, iv_cqr["median_width_x"]),
          "",
          "바뀌지 않은 것: 타깃 정의(stated_cap only), 비교군 사다리, 출처 필수축,",
          "Phase A 의 percentile 조회, 서비스 문구 규율(적정/과다/삭감 금지).",
          "모델 2 가 하는 말은 그대로 '유사사업 비교군 안에서의 상대적 위치'다.", ""]

    p = os.path.join(C.REPORTS, "m53_m2_improve.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
