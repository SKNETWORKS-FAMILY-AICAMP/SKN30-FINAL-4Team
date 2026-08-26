"""M45 — 모델 2 최종: 유사 비교군 안에서의 기업당 지원규모.

모델 2 는 두 질문 중 하나만 답한다.
    (버림)  최근 지원규모가 오르는가 내리는가              -> 2-A
    (채택)  이 사업의 기업당 지원규모가 유사군에서 어디쯤인가  -> 2-B

2-A 를 버린 근거는 이 저장소에 이미 있다(a02_ts_stl_diagnosis / a03_ts_trend_test,
커밋 dcbbf90). STL 추세강도 0.146, 지원성격 6종 전부 '추세없음'(BH q>=0.79),
예측 벤치마크 최고가 Last Value. 시간 구조가 없는 축에 상승/하락 라벨을 붙이면
라벨이 아니라 잡음이 된다. per_company 관측이 2019~2025 통틀어 487건뿐이라
연도x지원성격 칸이 대부분 한 자릿수인 것도 같은 이유로 치명적이다.

M12 대비 이 스크립트가 고치는 것 세 가지.

1. 타깃 정제 — per_recipient 는 두 가지 다른 값을 섞고 있었다.
       stated_cap        원문에 적힌 기업당 '한도'
       budget_div_count  총예산 / 지원건수 = '평균'
   전체로 재면 1.20배라 안 갈리는 것처럼 보인다(p=0.79). 그러나 실제로 쓰는
   비교군 안에서 재면 갈린다 — 사업화xgrantxcompany 에서 cap 5,000만원 vs
   div 501만원 = 9.97배 (p=0.001). 전체에서 상쇄된 이유는 두 basis 가 서로
   다른 비교군에 몰려 있었기 때문이다. 한도와 평균을 같은 분포에 넣으면
   percentile 이 두 의미의 가중평균을 가리킨다. div 는 뺀다.

2. 출처를 필수 축으로 승격 — M12 는 출처를 사다리 '안쪽'에 두고 30건에 못
   미치면 섞었다. 그런데 섞으면 안 되는 정도가 측정된 것보다 크다.
       연구개발xgrantxcompany  40.00배 (p<1e-4)
       사업화xgrantxproject    20.00배 (p<1e-4)
       사업화xgrantxcompany    10.00배 (p<1e-4)
   10칸 중 8칸이 유의하게 갈린다. 더 나쁜 것은 이 차이를 관측 가능한 특성으로
   대리할 수 없다는 점이다 — agency_type 을 축에 넣어 통제해도
   사업화xgrantxcompanyxcentral 에서 taxonomy 315건 vs bizinfo 18건이 여전히
   10배(p<1e-4)다. 수행기관 차이가 아니라 기재 관행 차이라는 뜻이다.
   그래서 출처는 support_unit 과 동급의 '후퇴로 없앨 수 없는 축'이 된다.
   신규 사업은 어느 모집단과 견줄지를 골라야 하고, 못 고르면 비교를 포기한다.

3. 예측구간에 실용성 등급 — 구간을 내는 것과 쓸 수 있는 구간을 내는 것은
   다르다. 368배짜리 구간을 'P10~P90 참고범위'라고 내보내면 담당자는 근거가
   있다고 오해한다. 비교군별 구간폭으로 3단계 판정을 붙이고, 최하 등급은
   숫자 대신 '편차가 커 제시하기 어렵다'를 낸다.

이 모델이 하지 않는 말: 적정/과다/과소/삭감. 상대적 위치만 말한다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SRC = os.path.join(C.PROC, "design_features.parquet")
OUT_REF = os.path.join(C.PROC, "m45_cohort_reference.parquet")
OUT_OOF = os.path.join(C.PROC, "m45_oof_predictions.parquet")

SEED = 42
MIN_COHORT = 30          # 이 아래면 한 단계 넓힌다
MIN_REPORT = 5           # 참고분포 테이블에 아예 싣지 않는 하한
MIN_IMPROVEMENT = 0.10   # ML 이 baseline 을 이겼다고 말하려면 이만큼
NOMINAL = 0.80           # 명목 예측구간
LO, HI = 0.10, 0.90
CAL_FRAC = 0.30          # fold 안에서 보정셋으로 떼는 그룹 비율
PCTS = [1, 5, 10, 25, 50, 75, 90, 95, 99]

# 구간폭 실용성 등급 (M26 의 실측 분포에서 잡은 경계를 그대로 쓴다)
TIER_OK, TIER_WIDE = 30.0, 100.0
TIERS = ("참고 가능", "범위 넓음", "참고 범위 제시 어려움")

# 비교군 사다리. 앞쪽이 좁다.
# support_unit 과 cohort 는 모든 단계에 고정으로 들어간다 — 후퇴로 없앨 수 없다.
#   단위: 기업당 1억원과 과제당 1억원은 같은 숫자지만 다른 값이다.
#   출처: 위 docstring 2번. 최대 40배까지 갈리고 관측 특성으로 대리되지 않는다.
LADDER = [
    ("성격x방식x단위x출처", ["support_type", "support_method", "support_unit", "cohort"]),
    ("성격x단위x출처", ["support_type", "support_unit", "cohort"]),
    ("단위x출처", ["support_unit", "cohort"]),
]
FIXED = ["support_unit", "cohort"]


# ------------------------------------------------------------ Phase 0
def prepare(df):
    """타깃 정제. 뺀 건수를 전부 세어 돌려준다 — 조용히 지우지 않는다."""
    d = df.copy()
    drop = {"전체": int(len(d))}

    d = d[d["support_type"].notna()]
    drop["지원성격_결측_제외"] = drop["전체"] - len(d)

    n = len(d)
    d = d[d["per_recipient"].notna() & (d["per_recipient"] > 0)]
    drop["기업당지원액_결측_제외"] = n - len(d)

    n = len(d)
    d = d[~d["amount_outlier"]]
    drop["파싱오류범위_제외"] = n - len(d)

    # 핵심 정제: 한도와 평균을 섞지 않는다.
    n = len(d)
    d = d[d["per_recipient_basis"] == "stated_cap"]
    drop["총예산나눗셈(평균)_제외"] = n - len(d)

    n = len(d)
    d = d[d["support_unit"].notna() & d["cohort"].notna()]
    drop["필수축_결측_제외"] = n - len(d)

    d = d.copy()
    d["y"] = np.log10(d["per_recipient"].astype(float))
    d["industry_grp"] = d["industry"].fillna("미기재")
    top = d["industry_grp"].value_counts().head(15).index
    d["industry_grp"] = d["industry_grp"].where(d["industry_grp"].isin(top), "기타업종")
    d["agency_grp"] = d["agency_type"].fillna("미기재")
    d["group_key"] = d["program_stem"].fillna(d["title"]).astype(str)
    drop["최종"] = int(len(d))
    return d, drop


# ------------------------------------------------------------ Phase A
def build_reference(d):
    """사다리의 모든 단계를 미리 만들어 둔다. 조회는 여기서 찾아 쓴다."""
    rows = []
    for level, keys in LADDER:
        for key, g in d.groupby(keys, dropna=True, observed=True):
            if len(g) < MIN_REPORT:
                continue
            key = key if isinstance(key, tuple) else (key,)
            kv = dict(zip(keys, key))
            v = g["per_recipient"].astype(float).to_numpy()
            r = {"level": level,
                 "support_type": kv.get("support_type"),
                 "support_method": kv.get("support_method"),
                 "support_unit": kv.get("support_unit"),
                 "cohort": kv.get("cohort"),
                 "n": int(len(v))}
            for p in PCTS:
                r["p%d" % p] = float(np.percentile(v, p))
            r["iqr"] = r["p75"] - r["p25"]
            r["spread_x"] = float(r["p90"] / r["p10"]) if r["p10"] > 0 else np.nan
            rows.append(r)
    return pd.DataFrame(rows)


def lookup(ref, support_type, support_method, unit, cohort):
    """비교군을 찾는다. 후퇴했으면 후퇴 사실을 함께 돌려준다.

    단위나 출처를 모르면 비교를 포기한다. 모르는 채로 넓은 분포와 대보면
    기업당 금액을 과제당 분포와, 중앙부처 관행을 지자체 관행과 견주게 된다.
    """
    if not unit:
        return None, None, "지원단위 미확정 — 기업당/과제당/인당을 섞을 수 없다"
    if not cohort:
        return None, None, "비교 모집단 미선택 — 출처별로 최대 40배 갈린다"

    want = {"support_type": support_type, "support_method": support_method,
            "support_unit": unit, "cohort": cohort}
    fallback = None
    for level, keys in LADDER:
        cond = pd.Series(True, index=ref.index)
        for col in ("support_type", "support_method", "support_unit", "cohort"):
            cond &= (ref[col] == want[col]) if col in keys else ref[col].isna()
        c = ref[(ref["level"] == level) & cond]
        if not len(c):
            continue
        row = c.iloc[0]
        if int(row["n"]) >= MIN_COHORT:
            return row, level, None
        if fallback is None:
            fallback = (row, level)
    if fallback:
        return (fallback[0], fallback[1],
                "비교군 부족(%d건 < %d)" % (int(fallback[0]["n"]), MIN_COHORT))
    return None, None, "비교군 없음"


def percentile_rank(row, value):
    """비교군의 몇 %가 이 값 이하인가. 저장된 분위 격자에서 보간한다."""
    ps = np.array([row["p%d" % p] for p in PCTS], dtype=float)
    return float(np.interp(value, ps, PCTS, left=0.0, right=100.0))


def compare(ref, value, support_type, support_method, unit, cohort, tier_map=None):
    row, level, warn = lookup(ref, support_type, support_method, unit, cohort)
    if row is None:
        return {"status": "비교불가", "reason": warn}
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return {"status": "비교불가", "reason": "신규 사업 금액 미기재"}
    rank = percentile_rank(row, float(value))
    key = (row["support_type"], row["support_method"], row["support_unit"], row["cohort"])
    tier = (tier_map or {}).get(key)
    out = {"status": "비교군_부족" if warn else "비교가능",
           "value": float(value), "level": level, "n": int(row["n"]),
           "distribution": {"p%d" % p: float(row["p%d" % p]) for p in PCTS},
           "percentile_rank": round(rank, 1),
           "spread_x": None if pd.isna(row["spread_x"]) else round(float(row["spread_x"]), 1),
           "interval_tier": tier,
           "statement": "유사사업 %d건 중 약 %.0f%%가 이 값 이하다." % (int(row["n"]), rank)}
    if warn:
        out["reason"] = warn
    return out


# ------------------------------------------------------------ Phase B/C
CATS = ["support_type", "support_method", "support_unit", "category_large",
        "industry_grp", "agency_grp", "amount_type"]
NUMS = ["support_count", "support_ratio", "project_duration", "self_burden_ratio", "year"]


def make_xy(d, with_cohort):
    """with_cohort=True 면 사용자가 비교 모집단을 고른 상태를 가정한다.

    M12 는 cohort 를 넣지 않았다 — '신규 사업 조회 시점에 없는 정보'라는 이유였다.
    이 스크립트는 출처를 필수 축으로 올렸으므로 조회 시점에 존재한다고 본다.
    다만 그 가정이 점수를 얼마나 부풀리는지 알아야 하므로 두 조건 다 잰다.
    """
    cats = CATS + (["cohort"] if with_cohort else [])
    t = d.copy()
    for c in cats:
        t[c] = t[c].fillna("미기재").astype("category")
    return t[cats + NUMS], t["y"].to_numpy(), t["group_key"].to_numpy(), cats


def fit_quantiles(Xtr, ytr, Xte, alphas=(LO, 0.5, HI)):
    from lightgbm import LGBMRegressor
    p = {}
    for a in alphas:
        p[a] = LGBMRegressor(objective="quantile", alpha=a, n_estimators=400,
                             learning_rate=0.05, num_leaves=15, min_child_samples=10,
                             random_state=SEED, verbose=-1).fit(Xtr, ytr).predict(Xte)
    return p


def cqr_fold(Xtr, ytr, Xte, groups_tr, rng):
    """fold 안에서 학습셋을 다시 학습/보정으로 쪼개 CQR 을 적용한다.

    보정셋도 그룹 단위로 뗀다. 같은 사업의 재공고가 학습과 보정에 갈라지면
    이탈량이 낙관적으로 잡혀 구간이 실제보다 좁아진다.
    """
    uniq = np.unique(groups_tr)
    rng.shuffle(uniq)
    cal = set(uniq[:max(1, int(len(uniq) * CAL_FRAC))])
    is_cal = np.array([g in cal for g in groups_tr])
    Xf, yf = Xtr.iloc[~is_cal], ytr[~is_cal]
    Xc, yc = Xtr.iloc[is_cal], ytr[is_cal]
    if len(Xc) < 20 or len(Xf) < 50:
        p = fit_quantiles(Xtr, ytr, Xte)
        return p[LO], p[0.5], p[HI], 0.0
    pc = fit_quantiles(Xf, yf, Xc)
    pt = fit_quantiles(Xf, yf, Xte)
    E = np.maximum(pc[LO] - yc, yc - pc[HI])
    k = min(max(int(np.ceil((len(E) + 1) * NOMINAL)), 1), len(E))
    delta = float(np.sort(E)[k - 1])
    return pt[LO] - delta, pt[0.5], pt[HI] + delta, delta


def cohort_median_baseline(Xtr, ytr, Xte, cats):
    """비교군 중앙값. 사다리와 같은 순서로 후퇴한다 — 공정한 baseline 이 되려면
    ML 과 같은 정보만 써야 한다."""
    tr = Xtr.copy()
    tr["_y"] = ytr
    keys = [k for k in ["support_type", "support_method", "support_unit", "cohort"]
            if k in cats]
    tables = []
    for i in range(len(keys), 0, -1):
        tables.append((keys[:i], tr.groupby(keys[:i], observed=True)["_y"].median()))
    gm = float(np.median(ytr))
    out = []
    for _, r in Xte.iterrows():
        v = np.nan
        for ks, tbl in tables:
            k = tuple(r[c] for c in ks)
            v = tbl.get(k[0] if len(k) == 1 else k, np.nan)
            if not pd.isna(v):
                break
        out.append(gm if pd.isna(v) else float(v))
    return np.array(out)


def point_metrics(y, p):
    e = np.abs(p - y)
    return {"MAE_log10": round(float(e.mean()), 4),
            "MedAE_log10": round(float(np.median(e)), 4),
            "geo_mean_error_x": round(float(10 ** e.mean()), 3),
            "within_2x": round(float((e <= np.log10(2)).mean()), 4),
            "within_3x": round(float((e <= np.log10(3)).mean()), 4)}


def interval_metrics(y, lo, hi):
    w = hi - lo
    cov = float(((y >= lo) & (y <= hi)).mean())
    a = 1 - NOMINAL
    score = w + (2 / a) * (lo - y) * (y < lo) + (2 / a) * (y - hi) * (y > hi)
    return {"coverage": round(cov, 4), "nominal": NOMINAL,
            "median_width_log10": round(float(np.median(w)), 4),
            "median_width_x": round(float(10 ** np.median(w)), 1),
            "mean_width_log10": round(float(w.mean()), 4),
            "interval_score_median": round(float(np.median(score)), 4),
            "crossing_rate": round(float((hi < lo).mean()), 5)}


def run(d, with_cohort):
    from lightgbm import LGBMRegressor
    from sklearn.model_selection import GroupKFold

    X, y, g, cats = make_xy(d, with_cohort)
    rng = np.random.default_rng(SEED)
    n = len(y)
    preds = {k: np.zeros(n) for k in
             ["전체중앙값", "비교군중앙값(baseline)", "LightGBM", "LGBM-quantile50"]}
    lo = np.zeros(n)
    hi = np.zeros(n)
    mid = np.zeros(n)
    raw_lo = np.zeros(n)
    raw_hi = np.zeros(n)
    deltas = []

    for tr, te in GroupKFold(n_splits=5).split(X, y, g):
        Xtr, Xte, ytr = X.iloc[tr], X.iloc[te], y[tr]
        preds["전체중앙값"][te] = np.median(ytr)
        preds["비교군중앙값(baseline)"][te] = cohort_median_baseline(Xtr, ytr, Xte, cats)
        preds["LightGBM"][te] = LGBMRegressor(
            n_estimators=400, learning_rate=0.05, num_leaves=15, min_child_samples=10,
            random_state=SEED, verbose=-1).fit(Xtr, ytr).predict(Xte)
        p = fit_quantiles(Xtr, ytr, Xte)
        preds["LGBM-quantile50"][te] = p[0.5]
        raw_lo[te], raw_hi[te] = p[LO], p[HI]
        l, m, h, dl = cqr_fold(Xtr, ytr, Xte, g[tr], rng)
        lo[te], mid[te], hi[te] = l, m, h
        deltas.append(dl)

    pts = {k: point_metrics(y, p) for k, p in preds.items()}
    base = pts["비교군중앙값(baseline)"]["MAE_log10"]
    best = min((k for k in pts if k not in ("전체중앙값", "비교군중앙값(baseline)")),
               key=lambda k: pts[k]["MAE_log10"])
    imp = (base - pts[best]["MAE_log10"]) / base
    oof = pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "pred": mid,
                        "lo": lo, "hi": hi,
                        "support_type": d["support_type"].to_numpy(),
                        "support_method": d["support_method"].to_numpy(),
                        "support_unit": d["support_unit"].to_numpy(),
                        "cohort": d["cohort"].to_numpy()})
    return {
        "with_cohort_feature": with_cohort, "n": int(n),
        "features": cats + NUMS,
        "point": pts, "baseline_MAE": base, "best_ml": best,
        "best_ml_MAE": pts[best]["MAE_log10"], "improvement": round(float(imp), 4),
        "min_required": MIN_IMPROVEMENT, "adopt": bool(imp >= MIN_IMPROVEMENT),
        "interval_raw": interval_metrics(y, raw_lo, raw_hi),
        "interval_conformal": interval_metrics(y, lo, hi),
        "conformal_delta_mean": round(float(np.mean(deltas)), 4),
    }, oof


def stability(d, with_cohort, n_repeat=10):
    """fold 재구성 10회로 개선율이 채택 기준 위에 안정적으로 있는지 본다.

    GroupKFold 는 결정적이라 시드가 없다. 그래서 그룹 라벨을 섞어 fold 구성을
    바꾼다. 개선율이 기준선 바로 위에 있을 때 한 번 재고 '채택'이라고 쓰면
    다음 사람이 다른 순서로 돌렸을 때 뒤집힌다.
    """
    from lightgbm import LGBMRegressor
    from sklearn.model_selection import GroupKFold

    X, y, g, cats = make_xy(d, with_cohort)
    out = []
    for seed in range(n_repeat):
        rng = np.random.default_rng(seed)
        uniq = np.unique(g)
        remap = dict(zip(uniq, uniq[rng.permutation(len(uniq))]))
        gs = np.array([remap[v] for v in g])
        bp = np.zeros(len(y))
        mp = np.zeros(len(y))
        for tr, te in GroupKFold(5).split(X, y, gs):
            Xtr, Xte, ytr = X.iloc[tr], X.iloc[te], y[tr]
            bp[te] = cohort_median_baseline(Xtr, ytr, Xte, cats)
            mp[te] = LGBMRegressor(
                objective="quantile", alpha=0.5, n_estimators=400, learning_rate=0.05,
                num_leaves=15, min_child_samples=10, random_state=SEED,
                verbose=-1).fit(Xtr, ytr).predict(Xte)
        b, m = float(np.abs(bp - y).mean()), float(np.abs(mp - y).mean())
        out.append((b - m) / b)
    a = np.array(out)
    return {"n_repeat": n_repeat, "mean": round(float(a.mean()), 4),
            "std": round(float(a.std()), 4), "min": round(float(a.min()), 4),
            "max": round(float(a.max()), 4),
            "pass_rate": round(float((a >= MIN_IMPROVEMENT).mean()), 2)}


def tier_table(oof):
    """비교군별 구간폭 -> 실용성 등급. 최하 등급은 숫자를 내보내지 않는다."""
    rows = []
    keys = ["support_type", "support_method", "support_unit", "cohort"]
    for k, g in oof.groupby(keys, observed=True):
        if len(g) < 10:
            continue
        w = float(np.median(g["hi"] - g["lo"]))
        x = 10 ** w
        tier = TIERS[0] if x <= TIER_OK else TIERS[1] if x <= TIER_WIDE else TIERS[2]
        cov = float(((g["y"] >= g["lo"]) & (g["y"] <= g["hi"])).mean())
        mae = float(np.abs(g["pred"] - g["y"]).mean())
        rows.append({"support_type": k[0], "support_method": k[1], "support_unit": k[2],
                     "cohort": k[3], "n": int(len(g)), "width_x": round(x, 1),
                     "coverage": round(cov, 3), "MAE_log10": round(mae, 4), "tier": tier})
    return pd.DataFrame(rows).sort_values("n", ascending=False)


# ------------------------------------------------------------ 출력
def won(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def demo_case(ref, d, tiers):
    """비교군이 두꺼운 실제 사업 하나로 출력 형태를 재현한다."""
    thick = ref[(ref["level"] == LADDER[0][0]) & (ref["n"] >= MIN_COHORT)]
    if not len(thick):
        return None
    t = thick.sort_values("n", ascending=False).iloc[0]
    cand = d[(d["support_type"] == t["support_type"])
             & (d["support_method"] == t["support_method"])
             & (d["support_unit"] == t["support_unit"])
             & (d["cohort"] == t["cohort"])]
    r = cand.sort_values("per_recipient").iloc[int(len(cand) * 0.7)]
    out = compare(ref, r["per_recipient"], r["support_type"], r["support_method"],
                  r["support_unit"], r["cohort"], tiers)
    out["title"] = r["title"]
    out["support_type"] = r["support_type"]
    out["support_method"] = r["support_method"]
    out["cohort"] = r["cohort"]
    return out


def main():
    d0 = pd.read_parquet(SRC)
    d, drop = prepare(d0)
    print("== Phase 0 — 타깃 정제")
    for k, v in drop.items():
        print("   %-24s %d" % (k, v))

    print("\n== Phase A — 비교군 참고분포 (출처를 필수 축으로)")
    ref = build_reference(d)
    ref.to_parquet(OUT_REF, index=False)
    print("   [data] %s  %d행" % (OUT_REF, len(ref)))
    for level, _ in LADDER:
        a = ref[ref["level"] == level]
        print("   %-22s 칸 %3d개 / 30건이상 %2d개"
              % (level, len(a), int((a["n"] >= MIN_COHORT).sum())))

    top = ref[(ref["level"] == LADDER[0][0]) & (ref["n"] >= MIN_COHORT)] \
        .sort_values("n", ascending=False)
    print("\n   1순위 비교군 (성격x방식x단위x출처, 30건 이상)")
    for _, r in top.iterrows():
        print("     %-8s %-6s %-8s %-9s n=%4d  P10 %-9s P50 %-9s P90 %-9s  폭 %.0f배"
              % (r["support_type"], r["support_method"], r["support_unit"], r["cohort"],
                 r["n"], won(r["p10"]), won(r["p50"]), won(r["p90"]), r["spread_x"]))

    print("\n== Phase B/C — 회귀와 예측구간")
    res = {}
    oofs = {}
    for wc in (False, True):
        tag = "출처_feature_포함" if wc else "출처_feature_제외"
        res[tag], oofs[tag] = run(d, wc)
        b = res[tag]
        print("\n   [%s] n=%d" % (tag, b["n"]))
        for k, v in sorted(b["point"].items(), key=lambda kv: kv[1]["MAE_log10"]):
            print("     %-24s MAE %.4f  MedAE %.4f  배수오차 %.2fx  2배이내 %.1f%%  3배이내 %.1f%%"
                  % (k, v["MAE_log10"], v["MedAE_log10"], v["geo_mean_error_x"],
                     v["within_2x"] * 100, v["within_3x"] * 100))
        print("     baseline %.4f -> %s %.4f = 개선 %.1f%% (기준 %.0f%%) => %s"
              % (b["baseline_MAE"], b["best_ml"], b["best_ml_MAE"],
                 b["improvement"] * 100, MIN_IMPROVEMENT * 100,
                 "채택" if b["adopt"] else "미채택"))
        ir, ic = b["interval_raw"], b["interval_conformal"]
        print("     구간  보정전 커버리지 %.1f%% 폭 %.1f배  ->  보정후 %.1f%% 폭 %.1f배 (delta %.3f)"
              % (ir["coverage"] * 100, ir["median_width_x"],
                 ic["coverage"] * 100, ic["median_width_x"], b["conformal_delta_mean"]))

    main_tag = "출처_feature_포함"
    print("\n== 개선율 안정성 (fold 재구성 10회, %s)" % main_tag)
    stab = stability(d, True)
    print("   개선율 %.1f%% ± %.1f%%  (범위 %.1f%% ~ %.1f%%)  기준 통과 %.0f/10"
          % (stab["mean"] * 100, stab["std"] * 100, stab["min"] * 100,
             stab["max"] * 100, stab["pass_rate"] * 10))
    res[main_tag]["stability"] = stab

    oof = oofs[main_tag]
    oof.to_parquet(OUT_OOF, index=False)
    tiers = tier_table(oof)
    tmap = {(r.support_type, r.support_method, r.support_unit, r.cohort): r.tier
            for r in tiers.itertuples()}

    print("\n== 비교군별 예측구간 실용성 등급 (%s 기준)" % main_tag)
    for _, r in tiers.iterrows():
        print("     %-8s %-6s %-8s %-9s n=%4d  폭 %6.1f배  커버 %3.0f%%  MAE %.3f  -> %s"
              % (r["support_type"], r["support_method"], r["support_unit"], r["cohort"],
                 r["n"], r["width_x"], r["coverage"] * 100, r["MAE_log10"], r["tier"]))
    print("     등급 분포: " + " / ".join("%s %d" % (t, int((tiers["tier"] == t).sum()))
                                        for t in TIERS))

    demo = demo_case(ref, d, tmap)
    if demo:
        print("\n== 조회 예시")
        print("   대상: %s" % demo["title"])
        print("   비교군: %s x %s x %s (%d건, %s)"
              % (demo["support_type"], demo["support_method"], demo["cohort"],
                 demo["n"], demo["level"]))
        dd = demo["distribution"]
        print("   P10 %s / P50 %s / P90 %s"
              % (won(dd["p10"]), won(dd["p50"]), won(dd["p90"])))
        print("   신규사업 %s -> 비교군 내 상위 %.0f%%  (구간등급: %s)"
              % (won(demo["value"]), 100 - demo["percentile_rank"], demo["interval_tier"]))

    verdict = "Go" if (len(top) >= 5 and res[main_tag]["adopt"]) else "Conditional"
    print("\n== 판정: %s" % verdict)

    C.save_report("m45_m2_amount.json", {
        "target": "log10(기업당 지원액) — stated_cap 만",
        "target_cleaning": drop,
        "ladder": [l for l, _ in LADDER], "fixed_axes": FIXED,
        "min_cohort": MIN_COHORT, "reference_rows": int(len(ref)),
        "cohorts_ge30": {l: int(((ref["level"] == l) & (ref["n"] >= MIN_COHORT)).sum())
                         for l, _ in LADDER},
        "results": res, "main": main_tag,
        "interval_tiers": tiers.to_dict("records"),
        "tier_counts": {t: int((tiers["tier"] == t).sum()) for t in TIERS},
        "demo": demo, "verdict": verdict,
    })
    write_md(ref, top, res, tiers, demo, drop, verdict, main_tag)


def write_md(ref, top, res, tiers, demo, drop, verdict, main_tag):
    b = res[main_tag]
    L = ["# 모델 2 — 유사 비교군 내 기업당 지원규모", "",
         "> 하지 않는 말: 적정 / 과다 / 과소 / 삭감 필요",
         "> 하는 말: 비교군, 표본 수, P10·P50·P90, percentile rank, 예측값, 구간 등급", "",
         "## 0. 2-A(추이 분류)를 하지 않는 이유", "",
         "이미 이 데이터에서 재고 기각했다 (`a02_ts_stl_diagnosis` / `a03_ts_trend_test`,",
         "커밋 `dcbbf90`).", "",
         "| 검정 | 결과 |", "|---|---|",
         "| STL (per_company) | trend 0.146 / seasonal 0.000 / ACF12 0.013 → 시간구조 조건 미충족 |",
         "| 추세검정 지원성격 6종 | 전부 '추세없음' (BH q 0.79~0.80) |",
         "| 전·후반 2기간 재검정 | 전부 '변화없음' (p 0.18~0.93) |",
         "| 예측 벤치마크 | 최고 모델이 Last Value (MAE_log10 0.322) |", "",
         "표본도 없다. 2019~2025 per_company 관측이 487건이라 연도×지원성격 칸이",
         "대부분 한 자릿수다. 2026(728건)은 층화표본이 아니라 전량 수집이라",
         "이어 붙이면 '표본→전수' 전환이 상승으로 둔갑한다.", "",
         "## 1. 타깃 정제", "",
         "`per_recipient` 는 두 가지 다른 값을 섞고 있었다.", "",
         "```text", "stated_cap        원문에 적힌 기업당 '한도'",
         "budget_div_count  총예산 / 지원건수 = '평균'", "```", "",
         "전체로 재면 1.20배라 안 갈리는 것처럼 보인다(p=0.79). 그러나 실제로 쓰는",
         "비교군 안에서 재면 갈린다 — 사업화×grant×company 에서 cap 5,000만원 vs",
         "div 501만원 = **9.97배** (p=0.001). 그래서 `stated_cap` 만 쓴다.", "",
         "| 단계 | 건수 |", "|---|---:|"]
    for k, v in drop.items():
        L.append("| %s | %d |" % (k, v))

    L += ["", "## 2. 출처를 필수 축으로 올린 이유", "",
          "M12 는 출처를 사다리 안쪽에 두고 30건에 못 미치면 섞었다. 섞으면 안 되는",
          "정도가 측정된 것보다 크다 — 10칸 중 8칸이 유의하게 갈리고 최대 40배다.", "",
          "```text",
          "연구개발 x grant x company   taxonomy 121건 vs bizinfo  34건   40.00배  p<1e-4",
          "사업화   x grant x project   taxonomy  16건 vs bizinfo  64건   20.00배  p<1e-4",
          "사업화   x grant x company   taxonomy 316건 vs bizinfo 286건   10.00배  p<1e-4",
          "```", "",
          "더 중요한 것은 이 차이를 관측 가능한 특성으로 대리할 수 없다는 점이다.",
          "`agency_type` 을 축에 넣어 통제해도 사업화×grant×company×central 에서",
          "taxonomy 315건 vs bizinfo 18건이 여전히 **10배**(p<1e-4)다. 수행기관 차이가",
          "아니라 기재 관행 차이라는 뜻이다.", "",
          "그래서 비교군 사다리는 이렇게 된다 — `지원단위`와 `출처`는 후퇴로 없앨 수 없다.", "",
          "```text"] + ["%d순위  %s" % (i + 1, l) for i, (l, _) in enumerate(LADDER)] + [
          "```", "",
          "| 단계 | 칸 수 | 30건 이상 |", "|---|---:|---:|"]
    for level, _ in LADDER:
        a = ref[ref["level"] == level]
        L.append("| %s | %d | %d |" % (level, len(a), int((a["n"] >= MIN_COHORT).sum())))

    L += ["", "### 1순위 비교군 참고분포 (30건 이상)", "",
          "| 지원성격 | 지원방식 | 단위 | 출처 | n | P10 | P50 | P90 | P90/P10 |",
          "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for _, r in top.iterrows():
        L.append("| %s | %s | %s | %s | %d | %s | %s | %s | %.0f배 |"
                 % (r["support_type"], r["support_method"], r["support_unit"],
                    r["cohort"], r["n"], won(r["p10"]), won(r["p50"]), won(r["p90"]),
                    r["spread_x"]))

    L += ["", "## 3. 회귀 성능", "",
          "타깃 log10(기업당 지원액) / 검증 GroupKFold(5) by program_stem", "",
          "출처를 feature 로 넣는 것은 '사용자가 비교 모집단을 골랐다'는 가정이다.",
          "그 가정이 점수를 얼마나 부풀리는지 알아야 하므로 두 조건 다 쟀다.", ""]
    for tag, r in res.items():
        L += ["### %s" % tag, "",
              "| 모델 | MAE(log10) | MedAE | 배수 오차 | 2배 이내 | 3배 이내 |",
              "|---|---:|---:|---:|---:|---:|"]
        for k, v in sorted(r["point"].items(), key=lambda kv: kv[1]["MAE_log10"]):
            L.append("| %s | %.4f | %.4f | %.2fx | %.1f%% | %.1f%% |"
                     % (k, v["MAE_log10"], v["MedAE_log10"], v["geo_mean_error_x"],
                        v["within_2x"] * 100, v["within_3x"] * 100))
        L += ["", "baseline %.4f → %s %.4f = **개선 %.1f%%** (기준 %.0f%%) → **%s**"
              % (r["baseline_MAE"], r["best_ml"], r["best_ml_MAE"],
                 r["improvement"] * 100, r["min_required"] * 100,
                 "채택" if r["adopt"] else "미채택"), ""]
        s = r.get("stability")
        if s:
            L += ["fold 재구성 %d회: 개선율 **%.1f%% ± %.1f%%** (범위 %.1f%% ~ %.1f%%), "
                  "기준 통과 %.0f/%d." % (s["n_repeat"], s["mean"] * 100, s["std"] * 100,
                                       s["min"] * 100, s["max"] * 100,
                                       s["pass_rate"] * s["n_repeat"], s["n_repeat"]),
                  "",
                  "> 기준선 바로 위다. 여유가 없다는 사실을 함께 읽어야 한다 — "
                  "최저 시드가 %.1f%%로 기준과 같다." % (s["min"] * 100), ""]

    L += ["## 4. 예측구간", "",
          "| 조건 | 커버리지(명목 80%) | 구간폭 중앙값 | Interval Score |",
          "|---|---:|---:|---:|"]
    for tag, r in res.items():
        for kind, key in (("보정 전", "interval_raw"), ("보정 후(CQR)", "interval_conformal")):
            v = r[key]
            L.append("| %s / %s | %.1f%% | %.1f배 | %.4f |"
                     % (tag, kind, v["coverage"] * 100, v["median_width_x"],
                        v["interval_score_median"]))

    L += ["", "### 비교군별 실용성 등급", "",
          "```text",
          "참고 가능              구간폭 <= %.0f배    구간을 그대로 낸다" % TIER_OK,
          "범위 넓음              <= %.0f배          '폭이 넓다'를 함께 표기한다" % TIER_WIDE,
          "참고 범위 제시 어려움    그 위             숫자 대신 '편차가 커 제시하기 어렵다'",
          "```", "",
          "| 지원성격 | 지원방식 | 단위 | 출처 | n | 구간폭 | 커버리지 | MAE | 등급 |",
          "|---|---|---|---|---:|---:|---:|---:|---|"]
    for _, r in tiers.iterrows():
        L.append("| %s | %s | %s | %s | %d | %.1f배 | %.0f%% | %.3f | %s |"
                 % (r["support_type"], r["support_method"], r["support_unit"],
                    r["cohort"], r["n"], r["width_x"], r["coverage"] * 100,
                    r["MAE_log10"], r["tier"]))

    if demo:
        dd = demo["distribution"]
        L += ["", "## 5. 조회 예시 — 출력 형태", "",
              "```text", "대상: %s" % demo["title"], "",
              "비교군: %s x %s x %s (%s)" % (demo["support_type"], demo["support_method"],
                                             demo["cohort"], demo["level"]),
              "비교사업: %d건" % demo["n"], "",
              "P10       %s" % won(dd["p10"]), "중앙값    %s" % won(dd["p50"]),
              "P90       %s" % won(dd["p90"]), "",
              "신규사업  %s" % won(demo["value"]),
              "-> 비교군 내 상위 %.0f%%" % (100 - demo["percentile_rank"]),
              "구간 등급: %s" % demo["interval_tier"], "```", "",
              "금지되는 문장: `지원금이 과도하다`", ""]

    L += ["## 6. 판정", "", "**%s**" % verdict, "",
          "- Phase A (비교군 percentile) 채택 — 1순위 비교군 %d칸이 30건 이상" % len(top),
          "- Phase B (회귀) %s — %s 대비 개선 %.1f%%"
          % ("채택" if b["adopt"] else "미채택", b["best_ml"], b["improvement"] * 100),
          "- Phase C (예측구간) CQR 보정으로 커버리지 %.1f%% → %.1f%%"
          % (b["interval_raw"]["coverage"] * 100, b["interval_conformal"]["coverage"] * 100),
          "- 2-A (추이 분류) 미채택 — 데이터에 시간 구조 없음(0장)", ""]

    p = os.path.join(C.REPORTS, "m45_m2_amount.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
