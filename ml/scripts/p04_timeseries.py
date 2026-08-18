"""P04 — timeseries_aggregate (설계서 4.3 / 8장). 월 × 분야 집계 + 인과 피처.

누수 통제 핵심: 모든 lag/rolling은 shift(1) 이후에 계산한다.
즉 시점 t의 피처는 t-1 이전 정보만 쓴다. shift 없이 rolling을 걸면
당월 값이 자기 피처에 섞여 검증 성능이 부풀려진다.
"""
import numpy as np
import pandas as pd

from common import PROC, CORE8, save_report

MASTER = f"{PROC}/announcement_master.parquet"
OUT = f"{PROC}/timeseries_aggregate.parquet"
PARTIAL_FROM = "2025-03"     # 원본이 2025-03-27까지 → 3월은 불완전 월


def main():
    m = pd.read_parquet(MASTER, columns=["registered_date", "category_large"])
    m = m.dropna(subset=["registered_date", "category_large"])
    m["ym"] = m["registered_date"].dt.to_period("M")

    cats = [c for c in CORE8 if c in set(m["category_large"].astype(str))]
    full = pd.period_range(m["ym"].min(), m["ym"].max(), freq="M")
    grid = pd.MultiIndex.from_product([full, cats], names=["ym", "category"])

    cnt = (m.groupby(["ym", "category_large"], observed=True).size()
             .rename("announcement_count"))
    ts = cnt.reindex(grid, fill_value=0).reset_index()
    ts["year"] = ts["ym"].dt.year
    ts["month"] = ts["ym"].dt.month
    ts["quarter"] = ts["ym"].dt.quarter
    ts = ts.sort_values(["category", "ym"]).reset_index(drop=True)

    g = ts.groupby("category", observed=True)["announcement_count"]
    for k in (1, 2, 3, 6, 12):
        ts[f"lag_{k}"] = g.shift(k)
    # shift(1) 먼저 → 당월 값이 자기 rolling에 들어가지 않는다
    prev = g.shift(1)
    for w in (3, 6, 12):
        ts[f"rolling_mean_{w}"] = prev.groupby(ts["category"], observed=True) \
                                      .rolling(w, min_periods=w).mean().reset_index(level=0, drop=True)
    ts["rolling_std_12"] = prev.groupby(ts["category"], observed=True) \
                               .rolling(12, min_periods=12).std().reset_index(level=0, drop=True)
    ts["yoy_diff"] = ts["lag_1"] - ts["lag_12"]

    ts["month_sin"] = np.sin(2 * np.pi * ts["month"] / 12)
    ts["month_cos"] = np.cos(2 * np.pi * ts["month"] / 12)

    ts["ym_str"] = ts["ym"].astype(str)
    ts["is_partial_month"] = ts["ym_str"] >= PARTIAL_FROM
    ts["target_next"] = g.shift(-1)          # 1개월 ahead 타깃
    ts = ts.drop(columns=["ym"]).rename(columns={"ym_str": "ym"})

    ts.to_parquet(OUT, index=False)

    trainable = ts.dropna(subset=["lag_12", "rolling_mean_12", "target_next"])
    trainable = trainable[~trainable["is_partial_month"]]
    save_report("p04_timeseries.json", {
        "rows": len(ts), "categories": cats,
        "months": int(ts["ym"].nunique()),
        "period": [ts["ym"].min(), ts["ym"].max()],
        "partial_months": sorted(ts.loc[ts["is_partial_month"], "ym"].unique().tolist()),
        "zero_cells": int((ts["announcement_count"] == 0).sum()),
        "trainable_rows_after_warmup": len(trainable),
        "count_stats": {
            "mean": round(float(ts["announcement_count"].mean()), 2),
            "median": float(ts["announcement_count"].median()),
            "min": int(ts["announcement_count"].min()),
            "max": int(ts["announcement_count"].max()),
        },
        "by_category_mean": ts.groupby("category", observed=True)["announcement_count"]
                              .mean().round(1).to_dict(),
        "leak_control": "lag/rolling 전부 shift(1) 이후 계산 — 시점 t 피처는 t-1 이전만 사용",
        "output": OUT,
    })
    print(f"timeseries_aggregate {len(ts):,}행 ({len(cats)}분야 × {ts['ym'].nunique()}개월) → {OUT}")
    print(f"  워밍업(lag12) 제외 후 학습가능 {len(trainable):,}행")
    print(f"  부분월 제외: {sorted(ts.loc[ts['is_partial_month'],'ym'].unique().tolist())}")


if __name__ == "__main__":
    main()
