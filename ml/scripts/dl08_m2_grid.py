"""DL08 — DL07 보강: OFAT 조합 실패 후 소규모 그리드 탐색.

DL-M2 ablation 에서 축별 최적값을 그대로 합쳤더니 base(131.60)보다 나빠졌다
(176.70). One-factor-at-a-time 은 축 간 상호작용을 못 잡는다는 문서화된
한계가 실제로 나타난 사례다. 유망 구간(개별 축에서 개선을 보인 값들)만
좁혀서 그리드로 재탐색한다. 146개월 데이터라 전체 그리드도 계산비용이
작아 부담이 없다.
"""
import itertools
import json
import os

import numpy as np
import pandas as pd

from dl07_m2_ablation import BASE, BASELINE_ENSEMBLE, BASELINE_SEASONAL_NAIVE, evaluate

DATA = "/workspace/dl2/volume_monthly_total.parquet"
OUT = "/workspace/dl2/reports/dl_m2_grid.json"

GRID = {
    "hidden": [32, 64],
    "layers": [1],
    "lookback": [6, 12],
    "dropout": [0.0, 0.2],
    "lr": [0.003, 0.01],
    "epochs": [100, 200],
}

total = pd.read_parquet(DATA).sort_values("ym").reset_index(drop=True)
years = [2020, 2021, 2022, 2023, 2024]

keys = list(GRID.keys())
combos = list(itertools.product(*[GRID[k] for k in keys]))
print("그리드 %d개 조합" % len(combos))

results = []
for combo in combos:
    cfg = dict(zip(keys, combo))
    r = evaluate(total, cfg["lookback"], cfg, years)
    results.append({"config": cfg, **r})
    print("%-60s MAE %.2f" % (str(cfg), r["MAE"]))

results.sort(key=lambda r: r["MAE"])
best = results[0]
print()
print("=== 최적 조합 ===")
print(best["config"], "-> MAE %.2f" % best["MAE"])
print("base(OFAT 기준설정) MAE 131.60 대비 %+.2f" % (best["MAE"] - 131.60))
print("OFAT 조합값(176.70) 대비 %+.2f" % (best["MAE"] - 176.70))
print("Seasonal Naive(%.2f) 대비: %s" % (BASELINE_SEASONAL_NAIVE,
      "개선" if best["MAE"] < BASELINE_SEASONAL_NAIVE else "미달"))
print("ML Ensemble(%.2f, 현재 채택) 대비: %s" % (BASELINE_ENSEMBLE,
      "개선" if best["MAE"] < BASELINE_ENSEMBLE else "미달"))

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({
        "purpose": "OFAT 축별 최적값 조합이 base 보다 나빠져(176.70>131.60) 소규모 그리드로 재탐색",
        "grid": GRID, "n_combos": len(combos),
        "all_results": results,
        "best": best,
        "ofat_combined_mae": 176.70,
        "ofat_base_mae": 131.60,
        "baselines": {"seasonal_naive": BASELINE_SEASONAL_NAIVE,
                     "ml_ensemble_adopted": BASELINE_ENSEMBLE},
        "beats_seasonal_naive": bool(best["MAE"] < BASELINE_SEASONAL_NAIVE),
        "beats_ml_ensemble": bool(best["MAE"] < BASELINE_ENSEMBLE),
    }, f, ensure_ascii=False, indent=2)
print("\n[report]", OUT)
