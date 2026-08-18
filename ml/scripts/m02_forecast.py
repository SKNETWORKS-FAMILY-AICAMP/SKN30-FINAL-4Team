"""M02 — 공고량 시계열 예측 모델 비교 (설계서 8장).

검증은 walk-forward(expanding window). 랜덤 분할은 쓰지 않는다.
  fold1  train ~2021-12  → val 2022
  fold2  train ~2022-12  → val 2023
  fold3  train ~2023-12  → val 2024
2025년은 3/27까지의 부분 연도라 평가에서 제외한다.

타깃: 시점 t의 announcement_count
피처: lag_1..12, rolling_*(전부 shift(1) 이후 계산), 캘린더, 분야
      → 시점 t의 피처는 t-1 이전 정보만 담고 있다.

누수 통제
  - 스케일러는 fold의 train에만 fit
  - SARIMA/Prophet은 fold마다 재적합 (전체 시리즈 적합 후 과거 평가 금지)
"""
import argparse
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from common import PROC, save_report

warnings.filterwarnings("ignore")
TS = PROC + "/timeseries_aggregate.parquet"
FOLDS = [("2021-12", "2022"), ("2022-12", "2023"), ("2023-12", "2024")]

FEATS = ["lag_1", "lag_2", "lag_3", "lag_6", "lag_12",
         "rolling_mean_3", "rolling_mean_6", "rolling_mean_12", "rolling_std_12",
         "yoy_diff", "month", "quarter", "month_sin", "month_cos"]


def smape(y, p):
    d = (np.abs(y) + np.abs(p)) / 2
    m = d != 0
    return float(np.mean(np.abs(y[m] - p[m]) / d[m]) * 100) if m.any() else np.nan


def direction_acc(y, p, base):
    """전월 대비 증감 방향을 맞춘 비율. 회귀지만 읽기 쉬운 보조 지표."""
    m = ~np.isnan(base)
    if not m.any():
        return np.nan
    return float(np.mean(np.sign(y[m] - base[m]) == np.sign(p[m] - base[m])))


def score(y, p, base):
    y, p = np.asarray(y, float), np.asarray(p, float)
    return {"MAE": round(float(mean_absolute_error(y, p)), 3),
            "RMSE": round(float(np.sqrt(mean_squared_error(y, p))), 3),
            "sMAPE": round(smape(y, p), 2),
            "direction_acc": round(direction_acc(y, p, np.asarray(base, float)), 4)}


def ml_models(seed):
    from lightgbm import LGBMRegressor
    from xgboost import XGBRegressor
    from catboost import CatBoostRegressor
    return {
        "LinearRegression": Pipeline([("s", StandardScaler()), ("m", LinearRegression())]),
        "RandomForest": RandomForestRegressor(n_estimators=500, min_samples_leaf=2,
                                              n_jobs=-1, random_state=seed),
        "XGBoost": XGBRegressor(n_estimators=600, max_depth=5, learning_rate=0.05,
                                subsample=0.9, colsample_bytree=0.9, n_jobs=-1,
                                random_state=seed, verbosity=0),
        "LightGBM": LGBMRegressor(n_estimators=600, learning_rate=0.05, num_leaves=31,
                                  n_jobs=-1, random_state=seed, verbose=-1),
        "CatBoost": CatBoostRegressor(iterations=600, depth=6, learning_rate=0.05,
                                      verbose=0, random_seed=seed,
                                      allow_writing_files=False),
    }


def fit_sarima(train_values, n_ahead):
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    m = SARIMAX(train_values, order=(1, 0, 1), seasonal_order=(1, 1, 1, 12),
                enforce_stationarity=False, enforce_invertibility=False)
    return np.asarray(m.fit(disp=False).forecast(n_ahead), float)


def _ensure_tbb_on_path():
    """한국어 Windows에서 Prophet 백엔드가 죽는 문제 우회.

    cmdstanpy 1.3.0이 `where.exe tbb.dll` 출력을 UTF-8로 디코딩하는데,
    DLL을 못 찾으면 한글(CP949) 오류 메시지가 나와 UnicodeDecodeError가 난다.
    tbb.dll 위치를 PATH에 넣으면 오류 출력 자체가 사라져 정상 동작한다.
    """
    import glob
    import os
    import prophet
    base = os.path.join(os.path.dirname(prophet.__file__), "stan_model")
    for dll in glob.glob(os.path.join(base, "**", "tbb*.dll"), recursive=True):
        d = os.path.dirname(dll)
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        break


def fit_prophet(train_df, future_dates):
    _ensure_tbb_on_path()
    from prophet import Prophet
    p = Prophet(yearly_seasonality=True, weekly_seasonality=False,
                daily_seasonality=False)
    p.fit(train_df)
    fc = p.predict(pd.DataFrame({"ds": future_dates}))
    return fc["yhat"].values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-slow", action="store_true", help="SARIMA/Prophet 생략")
    args = ap.parse_args()

    ts = pd.read_parquet(TS)
    ts = ts[~ts["is_partial_month"]].copy()
    ts["date"] = pd.PeriodIndex(ts["ym"], freq="M").to_timestamp()
    ts = ts.dropna(subset=FEATS).sort_values(["category", "ym"]).reset_index(drop=True)
    cats = sorted(ts["category"].unique())
    print("학습가능 %d행 / %d분야 / %s~%s\n" % (len(ts), len(cats),
                                              ts["ym"].min(), ts["ym"].max()))

    results = {}
    preds_store = {}

    for cut, valyr in FOLDS:
        tr = ts[ts["ym"] <= cut]
        va = ts[ts["ym"].str.startswith(valyr)]
        if va.empty:
            continue
        y, base = va["announcement_count"].values, va["lag_1"].values
        print("[fold %s] train %d / val %d" % (valyr, len(tr), len(va)), flush=True)

        # 베이스라인은 피처에서 바로 나온다
        for name, col in [("Last Value", "lag_1"), ("Seasonal Naive", "lag_12"),
                          ("Moving Average(3)", "rolling_mean_3")]:
            p = va[col].values
            results.setdefault(name, {})[valyr] = score(y, p, base)
            preds_store.setdefault(name, []).append((valyr, y, p, base))

        # ML: 분야를 원핫으로 넣고 전 분야 한 번에 학습
        Xtr = pd.get_dummies(tr[FEATS + ["category"]], columns=["category"])
        Xva = pd.get_dummies(va[FEATS + ["category"]], columns=["category"])
        Xva = Xva.reindex(columns=Xtr.columns, fill_value=0)
        ytr = tr["announcement_count"].values
        for name, m in ml_models(args.seed).items():
            m.fit(Xtr, ytr)
            p = np.asarray(m.predict(Xva), float)
            results.setdefault(name, {})[valyr] = score(y, p, base)
            preds_store.setdefault(name, []).append((valyr, y, p, base))

        # 통계 모델: 분야별 개별 적합, fold마다 재적합
        if not args.skip_slow:
            for name in ("SARIMA", "Prophet"):
                parts = []
                for c in cats:
                    trc = tr[tr["category"] == c].sort_values("ym")
                    vac = va[va["category"] == c].sort_values("ym")
                    if vac.empty:
                        continue
                    if len(trc) < 30:
                        parts.append(pd.Series(
                            np.repeat(trc["announcement_count"].mean(), len(vac)),
                            index=vac.index))
                        continue
                    try:
                        if name == "SARIMA":
                            f = fit_sarima(trc["announcement_count"].values, len(vac))
                        else:
                            f = fit_prophet(
                                pd.DataFrame({"ds": trc["date"].values,
                                              "y": trc["announcement_count"].values}),
                                vac["date"].values)
                    except Exception as e:
                        print("    %s/%s 실패 → 평균 대체 (%s)" % (name, c, type(e).__name__))
                        f = np.repeat(trc["announcement_count"].mean(), len(vac))
                    parts.append(pd.Series(np.asarray(f, float), index=vac.index))
                p = pd.concat(parts).reindex(va.index).values
                results.setdefault(name, {})[valyr] = score(y, p, base)
                preds_store.setdefault(name, []).append((valyr, y, p, base))
                print("    %s 완료" % name, flush=True)

    # 앙상블은 단일 모델 비교가 끝난 뒤 추가 후보로 투입한다
    members = [m for m in ("XGBoost", "LightGBM", "CatBoost", "SARIMA", "Seasonal Naive")
               if m in preds_store]
    if len(members) >= 2:
        for _, valyr in FOLDS:
            rows = {m: next((t for t in preds_store[m] if t[0] == valyr), None)
                    for m in members}
            if any(v is None for v in rows.values()):
                continue
            y = rows[members[0]][1]
            base = rows[members[0]][3]
            p = np.mean([rows[m][2] for m in members], axis=0)
            results.setdefault("Ensemble(단순평균)", {})[valyr] = score(y, p, base)

    summary = {}
    for name, folds in results.items():
        summary[name] = {k: round(float(np.mean([f[k] for f in folds.values()])), 3)
                         for k in ("MAE", "RMSE", "sMAPE", "direction_acc")}
        summary[name]["per_fold_MAE"] = {k: v["MAE"] for k, v in folds.items()}

    order = sorted(summary.items(), key=lambda kv: kv[1]["MAE"])
    print("\n" + "=" * 74)
    print("%-22s%9s%9s%9s%10s" % ("모델", "MAE", "RMSE", "sMAPE", "방향적중"))
    print("-" * 74)
    for name, s in order:
        print("%-22s%9.2f%9.2f%9.2f%10.3f" % (name, s["MAE"], s["RMSE"],
                                              s["sMAPE"], s["direction_acc"]))

    best = order[0]
    sn = summary.get("Seasonal Naive", {}).get("MAE")
    save_report("m02_forecast.json", {
        "rows_trainable": len(ts), "categories": cats,
        "folds": [{"train_until": c, "validate": v} for c, v in FOLDS],
        "excluded": "2025년 부분 연도(2025-03-27까지)",
        "target": "announcement_count (1개월 ahead)",
        "features": FEATS,
        "leak_control": ["lag/rolling은 shift(1) 이후 계산",
                         "스케일러는 fold train에만 fit",
                         "SARIMA/Prophet은 fold마다 재적합"],
        "results": summary,
        "best_model": best[0], "best_MAE": best[1]["MAE"],
        "baseline_seasonal_naive_MAE": sn,
        "improvement_vs_seasonal_naive": (round((sn - best[1]["MAE"]) / sn, 4)
                                          if sn else None),
    })
    if sn:
        print("\n최고: %s MAE %.2f (Seasonal Naive %.2f 대비 %+.1f%%)"
              % (best[0], best[1]["MAE"], sn, (sn - best[1]["MAE"]) / sn * 100))


if __name__ == "__main__":
    main()
