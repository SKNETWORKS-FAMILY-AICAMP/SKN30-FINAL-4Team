"""A04 — 지원규모 시계열 예측: 설계서 v3 18장 조건5 판정.

A03에서 조건4(시간 구조 확인)를 통과한 per_company 만 대상으로 한다.
조건5는 "Walk-forward validation에서 baseline을 개선할 것"이다.
개선하지 못하면 설계서 18장대로 예측값을 제공하지 않는다.

공고량 예측(M02)과 동일한 프로토콜을 쓴다.
  - walk-forward(expanding window), 랜덤 분할 금지
  - lag/rolling 은 shift(1) 이후 계산
  - 스케일러는 fold train 에만 fit
  - baseline: Last Value / Seasonal Naive / Moving Average

다만 타깃이 다르다.
  공고량  = 건수(카운트)
  지원규모 = 월별 기업당지원금 중앙값 → 자릿수 범위가 넓어 log10 으로 모델링

관측 월수가 92개월(공고량 146개월)로 짧아 fold 를 2개만 둔다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from common import PROC, save_report

warnings.filterwarnings("ignore")

OBS = PROC + "/support_amount_observations.parquet"
TARGET_TYPE = "per_company"

FEATS = ["lag_1", "lag_2", "lag_3", "lag_6", "lag_12",
         "rolling_mean_3", "rolling_mean_6", "rolling_mean_12",
         "rolling_std_12", "yoy_diff", "month", "quarter",
         "month_sin", "month_cos", "obs_count"]


def build_frame(obs, amount_type):
    """월별 중앙값 시계열 + 인과 피처. 모든 lag/rolling 은 shift(1) 이후 계산."""
    s = obs[obs["amount_type"] == amount_type]
    g = s.groupby("ym")
    df = pd.DataFrame({
        "median_amount": g["amount_max"].median(),
        "obs_count": g["amount_max"].size(),
    })
    df.index = pd.PeriodIndex(df.index, freq="M")
    df = df.sort_index()

    full = pd.period_range(df.index.min(), df.index.max(), freq="M")
    df = df.reindex(full)
    df["obs_count"] = df["obs_count"].fillna(0)
    df["median_amount"] = df["median_amount"].interpolate(
        method="linear", limit_direction="both")

    # 로그 스케일 타깃
    y = np.log10(df["median_amount"].clip(lower=1))
    df["y"] = y

    prev = y.shift(1)                      # 당월 값이 자기 피처에 들어가지 않게
    for k in (1, 2, 3, 6, 12):
        df["lag_%d" % k] = y.shift(k)
    for w in (3, 6, 12):
        df["rolling_mean_%d" % w] = prev.rolling(w, min_periods=w).mean()
    df["rolling_std_12"] = prev.rolling(12, min_periods=12).std()
    df["yoy_diff"] = df["lag_1"] - df["lag_12"]

    df["month"] = df.index.month
    df["quarter"] = df.index.quarter
    df["month_sin"] = np.sin(2 * np.pi * df["month"] / 12)
    df["month_cos"] = np.cos(2 * np.pi * df["month"] / 12)
    df["ym"] = df.index.astype(str)
    df["year"] = df.index.year
    return df


def score(y_log, p_log, base_log):
    y, p = np.asarray(y_log, float), np.asarray(p_log, float)
    ratio = 10 ** np.abs(y - p)
    m = ~np.isnan(base_log)
    dir_acc = (float(np.mean(np.sign(y[m] - base_log[m]) == np.sign(p[m] - base_log[m])))
               if m.any() else np.nan)
    return {
        "MAE_log10": round(float(mean_absolute_error(y, p)), 4),
        "RMSE_log10": round(float(np.sqrt(mean_squared_error(y, p))), 4),
        "geo_mean_error_x": round(float(np.mean(ratio)), 3),
        "within_2x": round(float(np.mean(ratio <= 2)), 4),
        "direction_acc": round(dir_acc, 4) if not np.isnan(dir_acc) else None,
    }


def ml_models(seed):
    from lightgbm import LGBMRegressor
    from xgboost import XGBRegressor
    from catboost import CatBoostRegressor
    return {
        "Ridge": Pipeline([("s", StandardScaler()), ("m", Ridge(alpha=1.0, random_state=seed))]),
        "RandomForest": RandomForestRegressor(n_estimators=400, min_samples_leaf=2,
                                              n_jobs=-1, random_state=seed),
        "XGBoost": XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.05,
                                subsample=0.9, n_jobs=-1, random_state=seed, verbosity=0),
        "LightGBM": LGBMRegressor(n_estimators=300, learning_rate=0.05, num_leaves=7,
                                  min_child_samples=3, n_jobs=-1,
                                  random_state=seed, verbose=-1),
        "CatBoost": CatBoostRegressor(iterations=300, depth=3, learning_rate=0.05,
                                      verbose=0, random_seed=seed,
                                      allow_writing_files=False),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", default=TARGET_TYPE)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    obs = pd.read_parquet(OBS)
    # 파싱 오류(SANE_RANGE 밖)는 a02 가 붙인 플래그로 제외한다.
    # 예전에는 a05 에만 필터가 있어 STL·예측이 4,517억원 같은 값을 그대로 봤다.
    if "is_outlier" in obs.columns:
        n0 = len(obs)
        obs = obs[~obs["is_outlier"]].copy()
        print("이상치 제외: %d건 (%.1f%%)" % (n0 - len(obs), (n0 - len(obs)) / n0 * 100))
    df = build_frame(obs, args.type)
    usable = df.dropna(subset=FEATS + ["y"])
    print("대상: %s / 전체 %d개월, 워밍업 제외 후 %d개월 (%s~%s)"
          % (args.type, len(df), len(usable),
             usable["ym"].min(), usable["ym"].max()))

    years = sorted(usable["year"].unique())
    # 마지막 2개 연도를 검증으로 (관측 92개월로 짧아 fold 2개)
    folds = [(y - 1, y) for y in years[-2:]]
    print("fold: %s\n" % [("~%d" % a, str(b)) for a, b in folds])

    results, preds_store = {}, {}
    for cut_year, val_year in folds:
        tr = usable[usable["year"] <= cut_year]
        va = usable[usable["year"] == val_year]
        if len(tr) < 24 or va.empty:
            print("fold %s 건너뜀 (train %d, val %d)" % (val_year, len(tr), len(va)))
            continue
        y, base = va["y"].values, va["lag_1"].values
        print("[fold %s] train %d / val %d" % (val_year, len(tr), len(va)), flush=True)

        for name, col in [("Last Value", "lag_1"), ("Seasonal Naive", "lag_12"),
                          ("Moving Average(3)", "rolling_mean_3")]:
            p = va[col].values
            results.setdefault(name, {})[val_year] = score(y, p, base)
            preds_store.setdefault(name, []).append((val_year, y, p, base))

        Xtr, Xva = tr[FEATS].values, va[FEATS].values
        for name, m in ml_models(args.seed).items():
            m.fit(Xtr, tr["y"].values)
            p = np.asarray(m.predict(Xva), float)
            results.setdefault(name, {})[val_year] = score(y, p, base)
            preds_store.setdefault(name, []).append((val_year, y, p, base))

    if not results:
        print("검증 가능한 fold 없음")
        return

    members = [m for m in ("XGBoost", "LightGBM", "CatBoost", "Seasonal Naive")
               if m in preds_store]
    if len(members) >= 2:
        for _, val_year in folds:
            rows = {m: next((t for t in preds_store[m] if t[0] == val_year), None)
                    for m in members}
            if any(v is None for v in rows.values()):
                continue
            y = rows[members[0]][1]
            base = rows[members[0]][3]
            p = np.mean([rows[m][2] for m in members], axis=0)
            results.setdefault("Ensemble(단순평균)", {})[val_year] = score(y, p, base)

    summary = {}
    for name, f in results.items():
        summary[name] = {k: round(float(np.mean([v[k] for v in f.values()])), 4)
                         for k in ("MAE_log10", "RMSE_log10", "geo_mean_error_x", "within_2x")
                         if all(v.get(k) is not None for v in f.values())}
        das = [v["direction_acc"] for v in f.values() if v.get("direction_acc") is not None]
        summary[name]["direction_acc"] = round(float(np.mean(das)), 4) if das else None

    order = sorted(summary.items(), key=lambda kv: kv[1]["MAE_log10"])
    print("\n" + "=" * 78)
    print("%-22s%12s%10s%12s%10s" % ("모델", "MAE_log10", "배수오차", "2배이내", "방향적중"))
    print("-" * 78)
    for n, s in order:
        print("%-22s%12.4f%10.2f%11.1f%%%10s"
              % (n, s["MAE_log10"], s["geo_mean_error_x"], s["within_2x"] * 100,
                 ("%.3f" % s["direction_acc"]) if s["direction_acc"] is not None else "-"))

    best = order[0]
    baselines = {k: v["MAE_log10"] for k, v in summary.items()
                 if k in ("Last Value", "Seasonal Naive", "Moving Average(3)")}
    best_base = min(baselines.values()) if baselines else None
    improved = best_base is not None and best[1]["MAE_log10"] < best_base
    # baseline 자체가 1위면 조건5 미충족
    cond5 = improved and best[0] not in baselines

    print("=" * 78)
    print("최고: %s (MAE_log10 %.4f)" % (best[0], best[1]["MAE_log10"]))
    print("최고 baseline: %.4f (%s)"
          % (best_base, min(baselines, key=baselines.get)) if best_base else "baseline 없음")
    print()
    print("설계서 18장 조건5(baseline 개선): %s" % ("충족" if cond5 else "미충족"))
    if not cond5:
        print("→ 설계서 18장 정책에 따라 '데이터 부족/시계열 구조 불충분으로")
        print("   신뢰 가능한 예측 불가'로 표시하고 예측값을 제공하지 않는다.")

    save_report("a04_support_forecast.json", {
        "amount_type": args.type,
        "target": "log10(월별 기업당지원금 중앙값)",
        "months_total": int(len(df)), "months_usable": int(len(usable)),
        "folds": [{"train_until": a, "validate": b} for a, b in folds],
        "features": FEATS,
        "leak_control": ["lag/rolling 은 shift(1) 이후 계산",
                         "스케일러는 fold train 에만 fit",
                         "walk-forward, 랜덤 분할 금지"],
        "results": summary,
        "best_model": best[0], "best_MAE_log10": best[1]["MAE_log10"],
        "best_baseline_MAE_log10": best_base,
        "condition5_beats_baseline": bool(cond5),
        "decision": ("예측 제공 가능" if cond5 else
                     "예측 미제공 — 데이터 부족/시계열 구조 불충분 (설계서 18장)"),
    })


if __name__ == "__main__":
    main()
