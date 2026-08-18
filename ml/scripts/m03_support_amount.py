"""M03 — 지원규모 회귀 모델 비교 (설계서 9장).

타깃은 계획서 9.3의 MVP 선택인 '기업당 최대지원금'(per_company)이다.
의미가 unknown인 금액은 기업당인지 총사업비인지 모르므로 타깃에서 제외한다.
섞어서 하나의 회귀 타깃으로 쓰면 계획서 6.3이 경고한 바로 그 오류가 된다.

금액이 50만원~수천억으로 자릿수를 넘나들어 log10 스케일로 모델링한다.

분할: GroupKFold(그룹 = 연도를 제거한 사업명 핵심어)
      같은 사업이 해마다 재공고되므로 단순 KFold는 train/test에 같은 사업을
      나눠 담아 성능을 부풀린다.
"""
import argparse
import re
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold

from common import PROC, save_report

warnings.filterwarnings("ignore")
ENRICHED = PROC + "/announcement_detail_enriched.parquet"
TAX = PROC + "/business_taxonomy.parquet"

# 기업당 지원금의 상식적 범위. 벗어나면 파싱 오류로 보고 제외한다.
MIN_WON, MAX_WON = 1_000_000, 10_000_000_000

CAT_FEATS = ["category_large", "agency", "executor", "region",
             "middle_category", "industry"]
NUM_FEATS = ["support_count", "support_ratio", "self_payment_ratio",
             "support_period_year", "n_amount_candidates", "doc_chars"]

YEAR_RE = re.compile(r"(20\d{2})\s*년?")
BRACKET = re.compile(r"^\s*[\[\(][^\]\)]{1,12}[\]\)]")


def group_key(title):
    """연도·지역 태그를 지운 사업명 → 같은 사업의 연도별 재공고를 한 그룹으로."""
    t = BRACKET.sub("", str(title))
    t = YEAR_RE.sub("", t)
    t = re.sub(r"(모집|공고|안내|참가기업|신청|접수|재공고|추가)", "", t)
    return re.sub(r"\s+", "", t)[:40] or "UNK"


def load():
    d = pd.read_parquet(ENRICHED)
    d["source"] = "openapi"
    for c in ("middle_category", "industry"):
        d[c] = np.nan

    t = pd.read_parquet(TAX)
    t = t.rename(columns={"large_category": "category_large"})
    t["source"] = "excel2023"
    t["region"] = np.nan
    t["doc_chars"] = t["text_for_model"].str.len()

    cols = (["title", "category_large", "agency", "executor", "region",
             "middle_category", "industry", "support_amount_max",
             "support_amount_type", "support_count", "support_ratio",
             "self_payment_ratio", "support_period_year",
             "n_amount_candidates", "doc_chars", "source"])
    both = pd.concat([d.reindex(columns=cols), t.reindex(columns=cols)],
                     ignore_index=True)
    return both


def models(seed):
    from lightgbm import LGBMRegressor
    from xgboost import XGBRegressor
    from catboost import CatBoostRegressor
    return {
        "Ridge": Ridge(alpha=1.0, random_state=seed),
        "RandomForest": RandomForestRegressor(n_estimators=400, min_samples_leaf=2,
                                              n_jobs=-1, random_state=seed),
        "XGBoost": XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.05,
                                subsample=0.9, colsample_bytree=0.9, n_jobs=-1,
                                random_state=seed, verbosity=0),
        "LightGBM": LGBMRegressor(n_estimators=400, learning_rate=0.05, num_leaves=15,
                                  min_child_samples=5, n_jobs=-1,
                                  random_state=seed, verbose=-1),
        "CatBoost": CatBoostRegressor(iterations=400, depth=5, learning_rate=0.05,
                                      verbose=0, random_seed=seed,
                                      allow_writing_files=False),
    }


def metrics(y_log, p_log):
    y_won, p_won = 10 ** y_log, 10 ** p_log
    return {
        "MAE_log10": round(float(mean_absolute_error(y_log, p_log)), 4),
        "RMSE_log10": round(float(np.sqrt(mean_squared_error(y_log, p_log))), 4),
        "R2_log10": round(float(r2_score(y_log, p_log)), 4),
        "MedAE_won": int(np.median(np.abs(y_won - p_won))),
        "within_2x": round(float(np.mean(np.abs(y_log - p_log) <= np.log10(2))), 4),
        "within_5x": round(float(np.mean(np.abs(y_log - p_log) <= np.log10(5))), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-type", default="per_company")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    df = load()
    n_all = len(df)
    df = df[df["support_amount_type"] == args.target_type]
    n_typed = len(df)
    df = df[df["support_amount_max"].notna()]
    df = df[(df["support_amount_max"] >= MIN_WON) & (df["support_amount_max"] <= MAX_WON)]
    df = df.reset_index(drop=True)
    print("전체 %d → %s %d → 범위필터 후 %d행" % (n_all, args.target_type, n_typed, len(df)))
    print("출처:", df["source"].value_counts().to_dict())

    y = np.log10(df["support_amount_max"].values.astype(float))
    groups = df["title"].map(group_key).values
    print("고유 그룹 %d개 (사업명 기준)" % len(set(groups)))
    print("금액 분위: p10 %.0f만 / 중앙 %.0f만 / p90 %.0f만"
          % (np.percentile(10 ** y, 10) / 1e4, np.median(10 ** y) / 1e4,
             np.percentile(10 ** y, 90) / 1e4))

    X = df[CAT_FEATS + NUM_FEATS].copy()
    for c in CAT_FEATS:
        X[c] = X[c].astype("object").where(X[c].notna(), "__NA__").astype(str)
    for c in NUM_FEATS:
        X[c] = pd.to_numeric(X[c], errors="coerce")

    n_splits = min(args.folds, len(set(groups)))
    gkf = GroupKFold(n_splits=n_splits)
    folds = list(gkf.split(X, y, groups))

    results, oof = {}, {}

    # --- 베이스라인: 분야별 중앙값/평균, 전체 중앙값
    for name in ("Global Median", "Median by category", "Mean by category"):
        preds = np.zeros(len(y))
        for tr, te in folds:
            if name == "Global Median":
                preds[te] = np.median(y[tr])
            else:
                agg = pd.Series(y[tr]).groupby(
                    X.iloc[tr]["category_large"].values).agg(
                    "median" if "Median" in name else "mean")
                fallback = np.median(y[tr]) if "Median" in name else np.mean(y[tr])
                preds[te] = X.iloc[te]["category_large"].map(agg).fillna(fallback).values
        results[name] = metrics(y, preds)
        oof[name] = preds

    # --- ML: 범주형은 원핫, 결측은 중앙값 대치(fold train 기준)
    Xd = pd.get_dummies(X, columns=CAT_FEATS, dummy_na=False)
    # 기관명 등에 LightGBM이 거부하는 특수문자가 섞여 있어 컬럼명을 안전하게 바꾼다
    Xd.columns = [re.sub(r"[^0-9A-Za-z_가-힣]", "_", str(c)) for c in Xd.columns]
    Xd = Xd.loc[:, ~Xd.columns.duplicated()]
    for name, mk in models(args.seed).items():
        preds = np.zeros(len(y))
        for tr, te in folds:
            m = clone(mk)
            xtr, xte = Xd.iloc[tr].copy(), Xd.iloc[te].copy()
            med = xtr[NUM_FEATS].median()          # 대치값은 train에서만 구한다
            xtr[NUM_FEATS] = xtr[NUM_FEATS].fillna(med)
            xte[NUM_FEATS] = xte[NUM_FEATS].fillna(med)
            m.fit(xtr, y[tr])
            preds[te] = m.predict(xte)
        results[name] = metrics(y, preds)
        oof[name] = preds
        print("  %-18s MAE_log10 %.4f  R2 %.4f  2배이내 %.1f%%"
              % (name, results[name]["MAE_log10"], results[name]["R2_log10"],
                 results[name]["within_2x"] * 100), flush=True)

    # --- 앙상블: 단일 모델 비교 후 추가 후보
    members = [m for m in ("RandomForest", "XGBoost", "LightGBM", "CatBoost") if m in oof]
    if len(members) >= 2:
        results["Ensemble(단순평균)"] = metrics(y, np.mean([oof[m] for m in members], axis=0))

    order = sorted(results.items(), key=lambda kv: kv[1]["MAE_log10"])
    print("\n" + "=" * 78)
    print("%-22s%12s%9s%10s%10s" % ("모델", "MAE_log10", "R2", "2배이내", "5배이내"))
    print("-" * 78)
    for n, s in order:
        print("%-22s%12.4f%9.4f%9.1f%%%9.1f%%"
              % (n, s["MAE_log10"], s["R2_log10"],
                 s["within_2x"] * 100, s["within_5x"] * 100))

    best = order[0]
    base = results["Median by category"]["MAE_log10"]
    save_report("m03_support_amount.json", {
        "target_type": args.target_type,
        "target": "log10(support_amount_max)",
        "rows_all": n_all, "rows_typed": n_typed, "rows_modeled": len(df),
        "range_filter_won": [MIN_WON, MAX_WON],
        "source_dist": df["source"].value_counts().to_dict(),
        "n_groups": len(set(groups)), "folds": n_splits,
        "split": "GroupKFold(그룹=연도 제거한 사업명) — 연도별 재공고 누수 차단",
        "cat_features": CAT_FEATS, "num_features": NUM_FEATS,
        "results": results,
        "best_model": best[0], "best_MAE_log10": best[1]["MAE_log10"],
        "baseline_median_by_category_MAE_log10": base,
        "improvement_vs_baseline": round((base - best[1]["MAE_log10"]) / base, 4),
    })
    print("\n최고: %s MAE_log10 %.4f (분야중앙값 %.4f 대비 %+.1f%%)"
          % (best[0], best[1]["MAE_log10"], base,
             (base - best[1]["MAE_log10"]) / base * 100))


if __name__ == "__main__":
    main()
