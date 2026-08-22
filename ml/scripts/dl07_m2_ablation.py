"""DL07 — 공고량 예측: 소형 시계열 신경망(LSTM) + Ablation (모델 2).

기준선 (timeseries-analysis A07, walk-forward 2020~2024)
    Seasonal Naive (baseline)      MAE 118.32
    Ensemble(RF+XGB+LGBM+Ridge)    MAE 104.16  (-12.0% vs baseline)  <- ML 채택 모델

왜 딥러닝이 불리한 조건인지 먼저 밝힌다
    전체 공고량 시계열은 146개월뿐이다. RoBERTa 같은 사전학습 전이가 없는
    "처음부터 학습하는" 신경망에게 146개는 극히 적은 표본이다(참고: A04에서
    이미 지원규모 예측은 트리 계열 ML조차 baseline 을 못 이겼다 — 데이터가
    적을 때 유연한 모델이 불리하다는 같은 패턴이 우려된다).
    그래도 같은 잣대(walk-forward, baseline 대비 개선)로 정직하게 재본다.
    못 이기면 그 결과를 그대로 보고한다 — A04/A07 에서 지켜온 규율과 같다.

구성
    아키텍처: 단순 LSTM(단변량, lag 시퀀스 입력) — Transformer 는 146개 표본에
    과적합이 더 클 것으로 예상돼 배제하고, 검증 가능한 최소 구조로 시작한다.
    입력: 과거 lookback개월의 log1p(count) 시퀀스.
    출력: 다음 달 count (역변환).

    walk-forward 검증은 A07 과 동일 — 2020~2024, expanding window.
    lag/rolling 은 전부 t-1 까지만 쓴다(A07 과 같은 누수 통제).

Ablation 축
    hidden_size    16 / 32 / 64
    num_layers     1 / 2
    lookback       6 / 12 / 24
    dropout        0.0 / 0.2 / 0.4
    lr             1e-3 / 3e-3 / 1e-2
    epochs         50 / 100 / 200
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import mean_absolute_error, mean_squared_error

DATA = os.environ.get("DL2_DATA", "/workspace/dl2/volume_monthly_total.parquet")
OUTDIR = os.environ.get("DL2_OUT", "/workspace/dl2/reports")

VALIDATE_YEARS = [2020, 2021, 2022, 2023, 2024]
BASELINE_SEASONAL_NAIVE = 118.32
BASELINE_ENSEMBLE = 104.16

BASE = {"hidden": 32, "layers": 1, "lookback": 12, "dropout": 0.2,
        "lr": 3e-3, "epochs": 100}
AXES = {
    "hidden": [16, 32, 64],
    "layers": [1, 2],
    "lookback": [6, 12, 24],
    "dropout": [0.0, 0.2, 0.4],
    "lr": [1e-3, 3e-3, 1e-2],
    "epochs": [50, 100, 200],
}


class LSTMForecaster(nn.Module):
    def __init__(self, hidden, layers, dropout):
        super().__init__()
        self.lstm = nn.LSTM(1, hidden, num_layers=layers, batch_first=True,
                            dropout=dropout if layers > 1 else 0.0)
        self.drop = nn.Dropout(dropout)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x):
        out, _ = self.lstm(x)
        last = out[:, -1, :]
        return self.head(self.drop(last)).squeeze(-1)


def make_windows(series, lookback):
    """series: log1p(count) 1차원 배열. 각 t 에 대해 [t-lookback..t-1] -> t."""
    X, y, idx = [], [], []
    for t in range(lookback, len(series)):
        X.append(series[t - lookback:t])
        y.append(series[t])
        idx.append(t)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32), np.array(idx)


def train_predict(series_log, years, lookback, cfg, seed=42):
    """walk-forward. 매 검증연도마다 그 이전 데이터로만 학습한다."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    X, y, idx = make_windows(series_log, lookback)
    preds = np.full(len(series_log), np.nan, dtype=np.float32)

    for Y in years:
        cutoff = pd.Period("%d-01" % Y, freq="M").ordinal - pd.Period(YM0, freq="M").ordinal
        tr_mask = idx < cutoff
        te_mask = (idx >= cutoff) & (idx < cutoff + 12)
        if tr_mask.sum() < lookback * 2 or te_mask.sum() == 0:
            continue

        # 학습 구간 통계로만 표준화한다(검증 구간 정보가 새면 안 된다).
        # 정규화 없이 넣으면 72개뿐인 표본으로 절대 스케일을 못 배우고
        # LSTM 이 평균값으로 붕괴한다 — 실측: 입력과 무관하게 상수만 출력.
        mu, sd = float(y[tr_mask].mean()), float(y[tr_mask].std() + 1e-6)
        Xtr = torch.tensor((X[tr_mask] - mu) / sd).unsqueeze(-1)
        ytr = torch.tensor((y[tr_mask] - mu) / sd)
        Xte = torch.tensor((X[te_mask] - mu) / sd).unsqueeze(-1)

        model = LSTMForecaster(cfg["hidden"], cfg["layers"], cfg["dropout"])
        opt = torch.optim.Adam(model.parameters(), lr=cfg["lr"])
        lossf = nn.MSELoss()
        model.train()
        for _ in range(cfg["epochs"]):
            opt.zero_grad()
            out = model(Xtr)
            loss = lossf(out, ytr)
            loss.backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            p = model(Xte).numpy() * sd + mu       # 정규화 역변환
        preds[idx[te_mask]] = p
    return preds


def evaluate(total, lookback, cfg, years):
    global YM0
    YM0 = total["ym"].iloc[0]
    series_log = np.log1p(total["count"].astype(float).values)
    dt = pd.PeriodIndex(total["ym"], freq="M")
    year_arr = dt.year.values

    preds_log = train_predict(series_log, years, lookback, cfg)
    mask = ~np.isnan(preds_log) & np.isin(year_arr, years)
    y_true = np.expm1(series_log[mask])
    y_pred = np.expm1(preds_log[mask])
    y_pred = np.clip(y_pred, 0, None)

    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
    d = (np.abs(y_true) + np.abs(y_pred)) / 2
    m = d != 0
    smape = float(np.mean(np.abs(y_true[m] - y_pred[m]) / d[m]) * 100) if m.any() else np.nan

    prev = np.expm1(series_log[np.where(mask)[0] - 1])
    dir_acc = float(np.mean(np.sign(y_true - prev) == np.sign(y_pred - prev)))

    return {"MAE": round(mae, 2), "RMSE": round(rmse, 2),
            "sMAPE": round(smape, 2), "direction_acc": round(dir_acc, 4),
            "n_eval": int(mask.sum())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--axes", nargs="+", default=list(AXES.keys()))
    args = ap.parse_args()

    total = pd.read_parquet(DATA).sort_values("ym").reset_index(drop=True)
    print("공고량 %d개월 (%s ~ %s)" % (len(total), total["ym"].iloc[0], total["ym"].iloc[-1]))
    print("기준선 — Seasonal Naive MAE %.2f / ML Ensemble(채택) MAE %.2f"
          % (BASELINE_SEASONAL_NAIVE, BASELINE_ENSEMBLE))
    print("기준 설정:", BASE)
    print(flush=True)

    t0 = time.time()
    results = {}
    base_res = evaluate(total, BASE["lookback"], BASE, VALIDATE_YEARS)
    results["__base__"] = {"config": dict(BASE), **base_res}
    print("[기준] MAE %.2f sMAPE %.2f%% 방향정확도 %.3f (%.0fs)"
          % (base_res["MAE"], base_res["sMAPE"], base_res["direction_acc"],
             time.time() - t0), flush=True)
    print()

    for axis in args.axes:
        if axis not in AXES:
            continue
        print("[축: %s]" % axis, flush=True)
        results[axis] = {}
        for val in AXES[axis]:
            if val == BASE[axis]:
                results[axis][str(val)] = {**base_res, "is_base": True, "delta": 0.0}
                print("  %-10s MAE %.2f (기준)" % (str(val), base_res["MAE"]), flush=True)
                continue
            cfg = dict(BASE)
            cfg[axis] = val
            lb = cfg["lookback"]
            r = evaluate(total, lb, cfg, VALIDATE_YEARS)
            d = r["MAE"] - base_res["MAE"]     # 음수가 개선(MAE 는 낮을수록 좋음)
            results[axis][str(val)] = {**r, "is_base": False, "delta": round(d, 2)}
            print("  %-10s MAE %.2f (%+.2f)  [%.0fs 누적]"
                  % (str(val), r["MAE"], d, time.time() - t0), flush=True)
        print(flush=True)

    print("=" * 60)
    print("%-12s%14s%12s%12s" % ("축", "최적값", "MAE", "영향폭"))
    print("-" * 60)
    best_cfg = dict(BASE)
    for axis in args.axes:
        if axis not in results or axis == "__base__":
            continue
        vals = results[axis]
        best_v = min(vals, key=lambda k: vals[k]["MAE"])   # MAE 최소가 최적
        maes = [v["MAE"] for v in vals.values()]
        print("%-12s%14s%12.2f%12.2f" % (axis, best_v, vals[best_v]["MAE"], max(maes) - min(maes)))
        for orig in AXES[axis]:
            if str(orig) == best_v:
                best_cfg[axis] = orig
                break

    print()
    print("축별 최적값 조합:", best_cfg)
    final_res = evaluate(total, best_cfg["lookback"], best_cfg, VALIDATE_YEARS)
    print("[최종] macroF1 대신 MAE 기준 — %.2f (base 대비 %+.2f, baseline 대비 %+.1f%%)"
          % (final_res["MAE"], final_res["MAE"] - base_res["MAE"],
             (final_res["MAE"] - BASELINE_SEASONAL_NAIVE) / BASELINE_SEASONAL_NAIVE * 100))
    beats_seasonal = final_res["MAE"] < BASELINE_SEASONAL_NAIVE
    beats_ensemble = final_res["MAE"] < BASELINE_ENSEMBLE
    print("Seasonal Naive(%.2f) 대비: %s" % (BASELINE_SEASONAL_NAIVE, "개선" if beats_seasonal else "미달"))
    print("ML Ensemble(%.2f, 현재 채택) 대비: %s" % (BASELINE_ENSEMBLE, "개선" if beats_ensemble else "미달"))

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "dl_m2_ablation_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "target": "월별 공고량(전체), 1개월 ahead",
            "architecture": "단변량 LSTM (lag 시퀀스 -> 다음 달)",
            "validation": "walk-forward, %s" % VALIDATE_YEARS,
            "method": "one-factor-at-a-time",
            "base_config": BASE, "base_result": base_res,
            "baselines": {"seasonal_naive": BASELINE_SEASONAL_NAIVE,
                         "ml_ensemble_adopted": BASELINE_ENSEMBLE},
            "axes": {k: v for k, v in results.items() if k != "__base__"},
            "best_per_axis": {k: str(v) for k, v in best_cfg.items()},
            "best_config_combined": best_cfg,
            "final_combined_result": final_res,
            "beats_seasonal_naive": bool(beats_seasonal),
            "beats_ml_ensemble": bool(beats_ensemble),
            "total_seconds": round(time.time() - t0, 1),
            "caveat": ("146개월은 처음부터 학습하는 신경망에 매우 적은 표본이다. "
                       "axes 수치는 선택 편향이 있다. ML Ensemble 을 못 이기면 "
                       "A04/A07 규율에 따라 이 모델은 채택하지 않는다."),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s (%.1f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
