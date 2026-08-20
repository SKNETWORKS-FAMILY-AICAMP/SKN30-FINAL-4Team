"""A03 — 지원규모 시계열 STL EDA (설계서 v3 17장 / 18장 조건4).

공고량 STL(A01)과 목적은 같지만 데이터 성격이 달라 처리를 다르게 한다.

  1) 지원규모는 이상치가 강하다(설계서 17장).
     대형 사업 한 건이 평균을 왜곡하므로 median 을 주 지표로 쓰고,
     평균/분위수는 참고로 함께 본다.

  2) 결측월이 존재한다(per_project 는 92개월 중 20개월 결측).
     STL 은 결측을 받지 못하므로 시간 보간 후 분해하되,
     보간 비율을 함께 기록해 신뢰도 판단에 쓴다.

  3) 금액 의미를 절대 섞지 않는다(설계서 24장 규칙8).
     per_company / total_budget / periodic / per_project 를 각각 분해한다.

설계서 18장의 예측 조건 5개 중 이 스크립트는 조건 4(시간 구조 확인)를 판정한다.
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

OBS = PROC + "/support_amount_observations.parquet"
TYPES = ["per_company", "total_budget", "periodic", "per_project"]
MIN_MONTHS = 36          # STL(period=12) 이 의미를 가지려면 최소 3주기
MAX_IMPUTE_RATIO = 0.35  # 보간 비율이 이보다 크면 신뢰하지 않는다


def strength(resid, comp):
    v_r = np.var(resid, ddof=1)
    v_cr = np.var(comp + resid, ddof=1)
    return float(max(0.0, 1.0 - v_r / v_cr)) if v_cr > 0 else 0.0


def build_series(obs, amount_type, metric="median", category=None):
    """월별 지원규모 시계열. 결측월은 시간 보간."""
    s = obs[obs["amount_type"] == amount_type]
    if category:
        s = s[s["large_category"] == category]
    if s.empty:
        return None, None

    g = s.groupby("ym")["amount_max"]
    val = g.median() if metric == "median" else g.mean()
    val.index = pd.PeriodIndex(val.index, freq="M")
    val = val.sort_index()

    full = pd.period_range(val.index.min(), val.index.max(), freq="M")
    ser = val.reindex(full)
    n_missing = int(ser.isna().sum())
    ser = ser.interpolate(method="linear", limit_direction="both")
    return ser, n_missing


def diagnose(series, n_missing, period=12):
    n = len(series)
    impute_ratio = n_missing / n if n else 1.0

    # 금액은 자릿수 범위가 넓어 로그 스케일에서 분해한다
    log_s = np.log10(series.clip(lower=1))
    res = STL(log_s, period=period, robust=True).fit()
    T, S, R = res.trend, res.seasonal, res.resid

    ft, fs = strength(R, T), strength(R, S)
    lb_p = float(acorr_ljungbox(R, lags=[12], return_df=True)["lb_pvalue"].iloc[0])
    a = acf(log_s.values, nlags=min(24, n // 2 - 1), fft=True)
    try:
        adf_p = float(adfuller(log_s.values)[1])
    except Exception:
        adf_p = float("nan")

    # 추세 방향 — 로그 추세의 처음/마지막 12개월 비교
    first, last = T.iloc[:12].mean(), T.iloc[-12:].mean()
    change_pct = (10 ** (last - first) - 1) * 100

    usable = (n >= MIN_MONTHS and impute_ratio <= MAX_IMPUTE_RATIO
              and (ft >= 0.3 or fs >= 0.3))

    return {
        "n_months": int(n),
        "n_missing": n_missing,
        "impute_ratio": round(impute_ratio, 4),
        "median_of_series": int(np.median(series)),
        "trend_strength": round(ft, 4),
        "seasonal_strength": round(fs, 4),
        "residual_var_ratio": round(float(np.var(R, ddof=1) / np.var(log_s, ddof=1)), 4),
        "acf_lag1": round(float(a[1]), 4) if len(a) > 1 else None,
        "acf_lag12": round(float(a[12]), 4) if len(a) > 12 else None,
        "ljungbox_p_lag12": round(lb_p, 4),
        "residual_autocorr_remains": bool(lb_p < 0.05),
        "adf_p": round(adf_p, 4),
        "trend_change_pct": round(float(change_pct), 1),
        "trend_direction": "증가" if change_pct > 5 else ("감소" if change_pct < -5 else "보합"),
        "condition4_time_structure": bool(usable),
    }, res


def plot(decomps, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    try:
        plt.rcParams["font.family"] = "Malgun Gothic"
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass
    n = len(decomps)
    if n == 0:
        return
    fig, axes = plt.subplots(n, 3, figsize=(15, 2.4 * n))
    if n == 1:
        axes = axes.reshape(1, -1)
    for i, (name, res) in enumerate(decomps.items()):
        idx = res.trend.index.to_timestamp()
        axes[i, 0].plot(idx, res.trend, color="#1f77b4")
        axes[i, 0].set_ylabel(name, fontsize=9)
        axes[i, 1].plot(idx, res.seasonal, color="#2ca02c")
        axes[i, 2].plot(idx, res.resid, color="#d62728", lw=0.7)
        if i == 0:
            for j, t in enumerate(["Trend (log10)", "Seasonal", "Residual"]):
                axes[i, j].set_title(t)
    fig.suptitle("지원규모(중앙값) 시계열 STL 분해 — 금액의미별", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print("[figure] %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=int, default=12)
    ap.add_argument("--by-category", action="store_true",
                    help="대분류별로도 분해 (표본이 충분한 조합만)")
    args = ap.parse_args()

    obs = pd.read_parquet(OBS)
    # 파싱 오류(SANE_RANGE 밖)는 a02 가 붙인 플래그로 제외한다.
    # 예전에는 a05 에만 필터가 있어 STL·예측이 4,517억원 같은 값을 그대로 봤다.
    if "is_outlier" in obs.columns:
        n0 = len(obs)
        obs = obs[~obs["is_outlier"]].copy()
        print("이상치 제외: %d건 (%.1f%%)" % (n0 - len(obs), (n0 - len(obs)) / n0 * 100))
    results, decomps = {}, {}

    print("=== 금액의미별 STL (중앙값 기준, log10 스케일) ===")
    print("%-14s%7s%7s%9s%10s%12s%12s%10s  %s"
          % ("금액의미", "월수", "결측", "보간비", "중앙값(만)", "추세강도", "계절성강도", "추세변화", "조건4"))
    print("-" * 108)
    for t in TYPES:
        ser, miss = build_series(obs, t)
        if ser is None or len(ser) < MIN_MONTHS:
            print("%-14s  월수 부족 — 분해 불가" % t)
            results[t] = {"skipped": "insufficient_months"}
            continue
        d, res = diagnose(ser, miss, args.period)
        results[t] = d
        decomps[t] = res
        print("%-14s%7d%7d%9.2f%10.0f%12.4f%12.4f%9s%+.0f%%  %s"
              % (t, d["n_months"], d["n_missing"], d["impute_ratio"],
                 d["median_of_series"] / 1e4, d["trend_strength"],
                 d["seasonal_strength"], d["trend_direction"],
                 d["trend_change_pct"], "충족" if d["condition4_time_structure"] else "미충족"))

    # 대분류별 (선택)
    cat_results = {}
    if args.by_category:
        print()
        print("=== 대분류 × per_company (표본 충분한 조합만) ===")
        for cat in sorted(obs["large_category"].dropna().unique()):
            ser, miss = build_series(obs, "per_company", category=cat)
            if ser is None or len(ser) < MIN_MONTHS:
                continue
            if miss / len(ser) > MAX_IMPUTE_RATIO:
                print("%-8s 결측 과다(%d/%d) — 제외" % (cat, miss, len(ser)))
                continue
            d, _ = diagnose(ser, miss, args.period)
            cat_results[cat] = d
            print("%-8s 월수 %d 보간 %.2f  추세 %.3f 계절성 %.3f  %s %+.0f%%"
                  % (cat, d["n_months"], d["impute_ratio"], d["trend_strength"],
                     d["seasonal_strength"], d["trend_direction"], d["trend_change_pct"]))

    plot(decomps, FIGURES + "/stl_support_amount.png")

    passed = [k for k, v in results.items()
              if isinstance(v, dict) and v.get("condition4_time_structure")]
    print()
    print("설계서 18장 조건4(시간 구조 확인) 충족: %d/%d — %s"
          % (len(passed), len(TYPES), ", ".join(passed) or "없음"))

    save_report("a03_support_stl.json", {
        "period": args.period,
        "metric": "median (설계서 17장 — 지원규모는 이상치가 강해 평균만 쓰지 않는다)",
        "scale": "log10 (금액 자릿수 범위가 넓음)",
        "min_months": MIN_MONTHS, "max_impute_ratio": MAX_IMPUTE_RATIO,
        "condition4_rule": "월수>=36 AND 보간비율<=0.35 AND (추세강도>=0.3 OR 계절성강도>=0.3)",
        "by_amount_type": results,
        "by_category_per_company": cat_results,
        "condition4_passed": passed,
        "figure": FIGURES + "/stl_support_amount.png",
    })


if __name__ == "__main__":
    main()
