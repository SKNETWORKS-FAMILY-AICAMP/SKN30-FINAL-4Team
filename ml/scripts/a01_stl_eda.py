"""A01 — 분야별 월별 공고량 STL 시계열 EDA (설계서 v3 10장).

STL은 예측 모델이 아니라 '시계열의 구조를 진단하고 이후 모델 선택의 근거를
만드는 EDA 단계'다. 시각화만으로 좋다/나쁘다를 말하지 않고 정량 지표를 낸다.

    Y(t) = Trend(t) + Seasonal(t) + Residual(t)

지표 (설계서 10.3)
    Trend Strength     F_T = max(0, 1 - Var(R) / Var(T+R))
    Seasonal Strength  F_S = max(0, 1 - Var(R) / Var(S+R))
    Residual ACF       STL 이후에도 시간 의존성이 남는지 (Ljung-Box)

누수 주의 (설계서 10.5)
    이 스크립트는 EDA 용도이므로 전체 기간 STL을 사용해도 된다.
    단, STL 성분을 예측 모델의 Feature로 쓸 때는 반드시 fold 내부에서만 fit해야
    한다. 그 경우는 별도 스크립트에서 처리한다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.seasonal import STL
from statsmodels.tsa.stattools import acf, adfuller

from common import FIGURES, PROC, save_report

warnings.filterwarnings("ignore")
TS = PROC + "/timeseries_aggregate.parquet"


def strength(resid, comp):
    """설계서 10.3의 성분 강도. 1에 가까울수록 그 성분이 강하다."""
    v_r = np.var(resid, ddof=1)
    v_cr = np.var(comp + resid, ddof=1)
    if v_cr <= 0:
        return 0.0
    return float(max(0.0, 1.0 - v_r / v_cr))


def diagnose(series, period=12, robust=True):
    """단일 시계열 STL 분해 + 정량 진단."""
    res = STL(series, period=period, robust=robust).fit()
    T, S, R = res.trend, res.seasonal, res.resid

    ft = strength(R, T)
    fs = strength(R, S)

    # 잔차에 시간 의존성이 남았는지 — Ljung-Box (lag 12)
    lb = acorr_ljungbox(R, lags=[12], return_df=True)
    lb_p = float(lb["lb_pvalue"].iloc[0])

    # 원계열 ACF (lag12가 크면 연간 계절성)
    a = acf(series.values, nlags=min(24, len(series) // 2 - 1), fft=True)

    # 정상성
    try:
        adf_p = float(adfuller(series.values)[1])
    except Exception:
        adf_p = float("nan")

    return {
        "n_months": int(len(series)),
        "mean": round(float(series.mean()), 2),
        "std": round(float(series.std()), 2),
        "trend_strength": round(ft, 4),
        "seasonal_strength": round(fs, 4),
        "residual_var": round(float(np.var(R, ddof=1)), 2),
        "residual_var_ratio": round(float(np.var(R, ddof=1) / np.var(series, ddof=1)), 4),
        "acf_lag1": round(float(a[1]), 4) if len(a) > 1 else None,
        "acf_lag12": round(float(a[12]), 4) if len(a) > 12 else None,
        "ljungbox_p_lag12": round(lb_p, 4),
        "residual_autocorr_remains": bool(lb_p < 0.05),
        "adf_p": round(adf_p, 4),
        "is_stationary": bool(adf_p < 0.05),
        "trend_direction": ("증가" if T.iloc[-12:].mean() > T.iloc[:12].mean() else "감소"),
    }, res


def verdict(d):
    """설계서 10.4의 판단 원칙에 따른 요약."""
    parts = []
    parts.append("추세 강함" if d["trend_strength"] >= 0.6
                 else ("추세 보통" if d["trend_strength"] >= 0.3 else "추세 약함"))
    parts.append("계절성 강함" if d["seasonal_strength"] >= 0.6
                 else ("계절성 보통" if d["seasonal_strength"] >= 0.3 else "계절성 약함"))
    if d["residual_autocorr_remains"]:
        parts.append("잔차 자기상관 남음")
    return " / ".join(parts)


def plot_all(decomps, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    try:
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

    n = len(decomps)
    fig, axes = plt.subplots(n, 3, figsize=(15, 2.2 * n), sharex=True)
    if n == 1:
        axes = axes.reshape(1, -1)
    for i, (name, res) in enumerate(decomps.items()):
        axes[i, 0].plot(res.trend.index.to_timestamp(), res.trend, color="#1f77b4")
        axes[i, 0].set_ylabel(name, fontsize=9)
        axes[i, 1].plot(res.seasonal.index.to_timestamp(), res.seasonal, color="#2ca02c")
        axes[i, 2].plot(res.resid.index.to_timestamp(), res.resid, color="#d62728", lw=0.7)
        if i == 0:
            axes[i, 0].set_title("Trend")
            axes[i, 1].set_title("Seasonal")
            axes[i, 2].set_title("Residual")
    fig.suptitle("분야별 월별 공고량 STL 분해", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print("[figure] %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=int, default=12)
    ap.add_argument("--no-robust", action="store_true")
    args = ap.parse_args()

    ts = pd.read_parquet(TS)
    ts = ts[~ts["is_partial_month"]].copy()   # 2025-03 부분월 제외

    results, decomps = {}, {}

    # 전체 합산
    total = ts.groupby("ym", observed=True)["announcement_count"].sum().sort_index()
    total.index = pd.PeriodIndex(total.index, freq="M")
    d, res = diagnose(total, args.period, not args.no_robust)
    d["verdict"] = verdict(d)
    results["전체"] = d
    decomps["전체"] = res

    # 분야별
    for cat in sorted(ts["category"].unique()):
        s = (ts[ts["category"] == cat]
             .set_index("ym")["announcement_count"].sort_index())
        s.index = pd.PeriodIndex(s.index, freq="M")
        if len(s) < args.period * 2 + 1:
            print("건너뜀(길이부족): %s" % cat)
            continue
        d, res = diagnose(s, args.period, not args.no_robust)
        d["verdict"] = verdict(d)
        results[cat] = d
        decomps[cat] = res

    # 출력 — 설계서 10.4 표 형식
    print()
    print("=" * 104)
    print("%-6s%8s%10s%10s%12s%12s%10s  %s"
          % ("분야", "월수", "평균", "추세강도", "계절성강도", "잔차분산비", "ACF12", "판단"))
    print("-" * 104)
    for name, d in results.items():
        print("%-6s%8d%10.1f%10.4f%12.4f%12.4f%10s  %s"
              % (name, d["n_months"], d["mean"], d["trend_strength"],
                 d["seasonal_strength"], d["residual_var_ratio"],
                 ("%.3f" % d["acf_lag12"]) if d["acf_lag12"] is not None else "-",
                 d["verdict"]))
    print("=" * 104)

    # Seasonal Naive 정당화 근거 (설계서 10.1 목적 4)
    strong_seasonal = [k for k, v in results.items()
                       if k != "전체" and v["seasonal_strength"] >= 0.5]
    print()
    print("계절성 강도 >= 0.5 인 분야: %d/%d — %s"
          % (len(strong_seasonal), len(results) - 1, ", ".join(strong_seasonal) or "없음"))
    print("→ Seasonal Naive 를 핵심 baseline 으로 두는 근거 %s"
          % ("성립" if len(strong_seasonal) >= (len(results) - 1) / 2 else "약함"))

    plot_all(decomps, FIGURES + "/stl_decomposition.png")

    save_report("a01_stl_eda.json", {
        "period": args.period, "robust": not args.no_robust,
        "excluded": "2025-03 부분월",
        "note": "EDA 용도이므로 전체기간 STL 사용. 모델 Feature 로 쓸 때는 fold 내부 fit 필요(설계서 10.5)",
        "metrics_definition": {
            "trend_strength": "max(0, 1 - Var(R)/Var(T+R))",
            "seasonal_strength": "max(0, 1 - Var(R)/Var(S+R))",
            "ljungbox_p_lag12": "p<0.05 면 STL 이후에도 잔차에 자기상관 남음",
        },
        "results": results,
        "strong_seasonal_categories": strong_seasonal,
        "seasonal_naive_justified": len(strong_seasonal) >= (len(results) - 1) / 2,
        "figure": FIGURES + "/stl_decomposition.png",
    })


if __name__ == "__main__":
    main()
