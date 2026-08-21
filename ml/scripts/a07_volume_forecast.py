"""A07 — 공고량 예측 모델 비교 + 향후 전망.

A06 에서 공고량은 시계열 구조가 나왔다(전체 추세 0.83 / 계절 0.78, 9개 중 8개
조건 충족). 지원규모(A03, 0/4 충족)와 반대다. 그래서 여기서는 실제로 예측을
시도하고, A04 와 같은 규율로 채택 여부를 정한다.

채택 규율 (A04 조건5 와 동일)
    baseline 을 이기지 못하면 예측을 제공하지 않는다. 공고량의 baseline 은
    Seasonal Naive(12개월 전 같은 달)다. 공고는 예산 주기를 타므로 "작년 이맘때"가
    강한 기준선이고, 이걸 못 이기면 모델을 쓸 이유가 없다.

검증
    walk-forward(expanding window). 랜덤 분할은 쓰지 않는다.
      fold k: train ~(Y-1)-12  ->  validate Y
      Y = 2020..2024  (2025 는 3/27 까지 부분 연도라 A06 에서 이미 제외)

누수 통제
    - lag/rolling 은 전부 shift(1) 이후 계산한다. 시점 t 의 피처는 t-1 까지의
      정보만 담는다.
    - 스케일러는 fold 의 train 에만 fit 한다.
    - 시리즈 전체에 적합한 뒤 과거를 평가하지 않는다.

지표
    MAE / RMSE / sMAPE 와 함께 방향 정확도를 본다. 운영에서는 "다음 달 공고가
    늘까 줄까"가 절대값보다 쓸모 있는 경우가 많다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from common import PROC, save_report

warnings.filterwarnings("ignore")

TOTAL = PROC + "/volume_monthly_total.parquet"
CAT = PROC + "/volume_monthly_category.parquet"
OUT_FORECAST = PROC + "/volume_forecast.parquet"

VALIDATE_YEARS = [2020, 2021, 2022, 2023, 2024]
LAGS = [1, 2, 3, 6, 12]
FEATS = ([f"lag_{l}" for l in LAGS]
         + ["roll_mean_3", "roll_mean_6", "roll_mean_12", "roll_std_12", "yoy_diff"]
         + ["month_sin", "month_cos", "quarter"])


def make_features(df):
    """시점 t 의 피처가 t-1 이전만 보게 만든다. shift(1) 을 먼저 건다."""
    d = df.copy().reset_index(drop=True)
    d["dt"] = pd.PeriodIndex(d["ym"], freq="M").to_timestamp()
    y = d["count"].astype(float)
    past = y.shift(1)                      # 여기서부터 미래 정보가 끊긴다
    for l in LAGS:
        d[f"lag_{l}"] = y.shift(l)
    d["roll_mean_3"] = past.rolling(3).mean()
    d["roll_mean_6"] = past.rolling(6).mean()
    d["roll_mean_12"] = past.rolling(12).mean()
    d["roll_std_12"] = past.rolling(12).std()
    d["yoy_diff"] = y.shift(1) - y.shift(13)
    mo = d["dt"].dt.month
    d["month_sin"] = np.sin(2 * np.pi * mo / 12)
    d["month_cos"] = np.cos(2 * np.pi * mo / 12)
    d["quarter"] = d["dt"].dt.quarter
    d["year"] = d["dt"].dt.year
    return d


def models(seed):
    from lightgbm import LGBMRegressor
    from xgboost import XGBRegressor
    return {
        "Ridge": Pipeline([("s", StandardScaler()), ("m", Ridge(alpha=1.0))]),
        "RandomForest": RandomForestRegressor(n_estimators=400, min_samples_leaf=2,
                                              n_jobs=-1, random_state=seed),
        "XGBoost": XGBRegressor(n_estimators=400, max_depth=4, learning_rate=0.05,
                                subsample=0.9, colsample_bytree=0.9, n_jobs=-1,
                                random_state=seed, verbosity=0),
        "LightGBM": LGBMRegressor(n_estimators=400, learning_rate=0.05, num_leaves=15,
                                  min_child_samples=5, n_jobs=-1,
                                  random_state=seed, verbose=-1),
    }


def smape(y, p):
    d = (np.abs(y) + np.abs(p)) / 2
    m = d != 0
    return float(np.mean(np.abs(y[m] - p[m]) / d[m]) * 100) if m.any() else np.nan


def direction_acc(y, p, prev):
    """실제 증감 방향과 예측 증감 방향이 같은 비율."""
    m = ~np.isnan(prev)
    if not m.any():
        return np.nan
    return float(np.mean(np.sign(y[m] - prev[m]) == np.sign(p[m] - prev[m])))


def evaluate(d, seed):
    """walk-forward. 각 fold 의 예측을 모아 한 번에 지표를 낸다."""
    usable = d.dropna(subset=FEATS + ["count"]).reset_index(drop=True)
    preds = {k: [] for k in ["Seasonal Naive", "Last Value", "MA(3)"]}
    preds.update({k: [] for k in models(seed)})
    truth, prevs, keys = [], [], []

    for Y in VALIDATE_YEARS:
        tr = usable[usable["year"] < Y]
        te = usable[usable["year"] == Y]
        if len(tr) < 24 or te.empty:
            continue
        Xtr, ytr = tr[FEATS].values, tr["count"].values
        Xte, yte = te[FEATS].values, te["count"].values
        truth.append(yte)
        prevs.append(te["lag_1"].values)
        keys.append(te["ym"].values)
        preds["Seasonal Naive"].append(te["lag_12"].values)
        preds["Last Value"].append(te["lag_1"].values)
        preds["MA(3)"].append(te["roll_mean_3"].values)
        for name, mk in models(seed).items():
            m = clone(mk)
            m.fit(Xtr, ytr)
            preds[name].append(m.predict(Xte))

    y = np.concatenate(truth)
    prev = np.concatenate(prevs)
    out = {}
    stacked = {k: np.concatenate(v) for k, v in preds.items() if v}
    stacked["Ensemble(단순평균)"] = np.mean(
        [stacked[k] for k in ("RandomForest", "XGBoost", "LightGBM", "Ridge")], axis=0)
    for name, p in stacked.items():
        out[name] = {
            "MAE": round(float(mean_absolute_error(y, p)), 3),
            "RMSE": round(float(np.sqrt(mean_squared_error(y, p))), 3),
            "sMAPE": round(smape(y, p), 3),
            "direction_acc": round(direction_acc(y, p, prev), 4),
        }
    return out, len(y), np.concatenate(keys)


def recursive_forecast(d, name, horizon, seed):
    """채택 모델로 향후 horizon 개월을 순차 예측한다.

    한 달을 예측하면 그 값을 lag 로 넣어 다음 달을 예측한다. 오차가 누적되므로
    앞쪽 몇 달만 신뢰할 수 있다. 리포트에 그 한계를 함께 적는다.
    """
    usable = d.dropna(subset=FEATS + ["count"]).reset_index(drop=True)
    pool = models(seed)
    if name.startswith("Ensemble"):
        # 검증에서 이긴 것이 앙상블이면 전망도 같은 구성으로 낸다.
        fitted = [clone(v).fit(usable[FEATS].values, usable["count"].values)
                  for v in pool.values()]
        predict = lambda x: float(np.mean([f.predict(x)[0] for f in fitted]))
    elif name in pool:
        m = clone(pool[name]).fit(usable[FEATS].values, usable["count"].values)
        predict = lambda x: float(m.predict(x)[0])
    elif name == "Seasonal Naive":
        predict = None          # 12개월 전 값을 그대로 쓴다. 아래에서 처리.
    else:
        return None

    hist = d[["ym", "count"]].dropna().copy()
    series = list(hist["count"].astype(float).values)
    last_p = pd.Period(hist["ym"].iloc[-1], freq="M")
    rows = []
    for h in range(1, horizon + 1):
        p = last_p + h
        past = pd.Series(series, dtype=float)
        feat = {}
        for l in LAGS:
            feat[f"lag_{l}"] = past.iloc[-l] if len(past) >= l else np.nan
        sh = past
        feat["roll_mean_3"] = sh.iloc[-3:].mean()
        feat["roll_mean_6"] = sh.iloc[-6:].mean()
        feat["roll_mean_12"] = sh.iloc[-12:].mean()
        feat["roll_std_12"] = sh.iloc[-12:].std()
        feat["yoy_diff"] = (past.iloc[-1] - past.iloc[-13]) if len(past) >= 13 else np.nan
        mo = p.month
        feat["month_sin"] = np.sin(2 * np.pi * mo / 12)
        feat["month_cos"] = np.cos(2 * np.pi * mo / 12)
        feat["quarter"] = (mo - 1) // 3 + 1
        if predict is None:                      # Seasonal Naive
            if len(past) < 12:
                break
            yhat = float(past.iloc[-12])
        else:
            x = np.array([[feat[k] for k in FEATS]], dtype=float)
            if np.isnan(x).any():
                break
            yhat = predict(x)
        rows.append({"ym": str(p), "horizon": h, "forecast": round(yhat, 1)})
        series.append(yhat)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--horizon", type=int, default=12)
    args = ap.parse_args()

    total = pd.read_parquet(TOTAL)
    d = make_features(total)
    res, n_eval, _ = evaluate(d, args.seed)

    print("=== 전체 공고량 — walk-forward 검증 (%s년, %d개월 평가) ==="
          % ("·".join(map(str, VALIDATE_YEARS)), n_eval))
    print("%-22s%10s%10s%10s%12s" % ("모델", "MAE", "RMSE", "sMAPE", "방향정확도"))
    print("-" * 64)
    for k, v in sorted(res.items(), key=lambda kv: kv[1]["MAE"]):
        print("%-22s%10.2f%10.2f%9.2f%%%12.3f"
              % (k, v["MAE"], v["RMSE"], v["sMAPE"], v["direction_acc"]))

    baseline = res["Seasonal Naive"]["MAE"]
    best = min(res.items(), key=lambda kv: kv[1]["MAE"])
    gain = (baseline - best[1]["MAE"]) / baseline
    passed = best[0] not in ("Seasonal Naive", "Last Value", "MA(3)") and gain > 0
    print()
    print("baseline(Seasonal Naive) MAE %.2f -> 최고 %s MAE %.2f (%+.1f%%)"
          % (baseline, best[0], best[1]["MAE"], -gain * 100))
    print("조건5(baseline 개선): %s" % ("충족" if passed else "미충족"))

    # ---- 분야별 ----
    cat = pd.read_parquet(CAT)
    per_cat = {}
    print()
    print("=== 분야별 (최고 모델 MAE / baseline 대비) ===")
    print("%-8s%10s%14s%12s%12s" % ("분야", "월평균", "최고모델", "MAE", "개선율"))
    print("-" * 58)
    for c in sorted(cat["category_large"].unique()):
        g = cat[cat["category_large"] == c].sort_values("ym")
        dc = make_features(g[["ym", "count"]])
        try:
            rc, nc, _ = evaluate(dc, args.seed)
        except Exception:
            continue
        b = rc["Seasonal Naive"]["MAE"]
        bm = min(rc.items(), key=lambda kv: kv[1]["MAE"])
        imp = (b - bm[1]["MAE"]) / b if b else 0
        per_cat[c] = {"mean": round(float(g["count"].mean()), 1),
                      "best_model": bm[0], "best_MAE": bm[1]["MAE"],
                      "baseline_MAE": b, "improvement": round(imp, 4),
                      "direction_acc": bm[1]["direction_acc"],
                      "all": rc}
        print("%-8s%10.1f%14s%12.2f%11.1f%%"
              % (c, g["count"].mean(), bm[0], bm[1]["MAE"], imp * 100))

    # ---- 전망 ----
    fc = None
    if passed:
        fc = recursive_forecast(d, best[0], args.horizon, args.seed)
        if fc is not None and not fc.empty:
            fc["scope"] = "전체"
            parts = [fc]
            for c, info in per_cat.items():
                if info["improvement"] <= 0:
                    continue
                g = cat[cat["category_large"] == c].sort_values("ym")
                f2 = recursive_forecast(make_features(g[["ym", "count"]]),
                                        info["best_model"], args.horizon, args.seed)
                if f2 is not None and not f2.empty:
                    f2["scope"] = c
                    parts.append(f2)
            fc = pd.concat(parts, ignore_index=True)
            fc.to_parquet(OUT_FORECAST, index=False)
            print()
            print("=== 향후 %d개월 전망 (전체) ===" % args.horizon)
            for _, r in fc[fc["scope"] == "전체"].iterrows():
                print("  %s  %7.0f건" % (r["ym"], r["forecast"]))
            print("→ %s (분야별 포함 %d행)" % (OUT_FORECAST, len(fc)))

    save_report("a07_volume_forecast.json", {
        "target": "월별 공고량 (1개월 ahead)",
        "validation": "walk-forward expanding window",
        "validate_years": VALIDATE_YEARS,
        "n_eval_months": int(n_eval),
        "features": FEATS,
        "leak_control": ["lag/rolling 은 shift(1) 이후 계산",
                         "스케일러는 fold train 에만 fit",
                         "walk-forward, 랜덤 분할 금지"],
        "results_total": res,
        "baseline": "Seasonal Naive (12개월 전 같은 달)",
        "baseline_MAE": baseline,
        "best_model": best[0], "best_MAE": best[1]["MAE"],
        "improvement_vs_baseline": round(gain, 4),
        "condition5_passed": bool(passed),
        "by_category": per_cat,
        "forecast_horizon": args.horizon if passed else 0,
        "forecast_caveat": ("순차(recursive) 예측이라 예측값을 다시 입력으로 넣는다. "
                            "오차가 누적되므로 뒤로 갈수록 신뢰도가 떨어진다. "
                            "1~3개월을 주로 보고 그 이후는 방향 참고용으로 쓴다."),
        "output": OUT_FORECAST if fc is not None else None,
    })


if __name__ == "__main__":
    main()
