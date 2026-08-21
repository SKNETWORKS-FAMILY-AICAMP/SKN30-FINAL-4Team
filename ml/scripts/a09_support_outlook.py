"""A09 — 분야별·시기별 지원 전망: 공고량 전망 × 지원규모 참고범위.

두 축을 하나의 표로 합친다.
    ① 얼마나 나오는가  A07 공고량 전망 (예측 가능 — baseline 대비 12% 개선)
    ② 얼마를 주는가    A02 관측 기반 지원규모 (예측 불가 — 관측 범위만 제공)

왜 한쪽만 예측하는가
    같은 잣대(STL 추세·계절성 강도 0.3)로 재보면 두 축의 성질이 정반대다.
        공고량    전체 추세 0.83 / 계절 0.78 — 9개 중 8개 충족
        지원규모  4개 유형 전부 0.3 미만 — 0개 충족
    공고량은 예산 주기를 타는 행정 리듬이라 반복되지만, 지원금액은 사업마다
    예산·정책으로 정해지는 값이라 시간의 함수가 아니다. 실측으로도 지원규모는
    어떤 모델도 "직전 값 그대로"를 못 이겼다(A04). 그래서 금액은 예측하지 않고
    관측된 분포(중앙값·사분위)를 그대로 보여준다.

계절 프로파일
    시기별 금액 추이는 월별 중앙값을 그대로 쓰면 표본이 얇아 튄다. 분기로 묶고
    최소 표본을 넘는 칸만 낸다. 그래도 신뢰구간이 넓다는 점은 리포트에 남긴다.

읽는 법
    "2026년 2분기 수출 분야는 공고가 약 N건 나올 전망이고, 그 분야 기업당
     지원금은 관측상 중앙값 M원(P25~P75 구간)이다."
    금액 쪽은 전망이 아니라 관측 요약이다. 둘을 곱해 총액을 추정하지 않는다
    (공고 1건당 지원 기업 수를 모르므로 곱하면 근거 없는 숫자가 된다).
"""
import argparse
import io
import os
import warnings

import numpy as np
import pandas as pd

from common import PROC, REPORTS, save_report, mark_outliers

warnings.filterwarnings("ignore")

OBS = PROC + "/support_amount_observations.parquet"
FORECAST = PROC + "/volume_forecast.parquet"
VOL_CAT = PROC + "/volume_monthly_category.parquet"
OUT = PROC + "/support_outlook.parquet"
OUT_MD = REPORTS + "/a09_support_outlook.md"

AMOUNT_TYPE = "per_company"     # 기업 입장에서 가장 직접적인 축
MIN_N = 5                       # 이 미만이면 범위를 내지 않는다


def won(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    x = float(x)
    if x >= 1e8:
        return "{:.1f}억원".format(x / 1e8)
    if x >= 1e4:
        return "{:,.0f}만원".format(x / 1e4)
    return "{:,.0f}원".format(x)


def amount_stats(g):
    a = g["amount_max"].dropna()
    if len(a) < MIN_N:
        return None
    return {"n": int(len(a)),
            "median": float(a.median()),
            "p25": float(a.quantile(.25)),
            "p75": float(a.quantile(.75))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    obs = pd.read_parquet(OBS)
    if "is_outlier" not in obs.columns:
        obs["is_outlier"] = mark_outliers(obs)
    g = obs[(obs["amount_type"] == AMOUNT_TYPE) & (~obs["is_outlier"])].copy()
    g["date"] = pd.to_datetime(g["date"], errors="coerce")
    g = g[g["date"].notna()]
    g["quarter"] = g["date"].dt.quarter
    print("지원규모 관측(%s, 이상치 제외) %d건 / %s ~ %s"
          % (AMOUNT_TYPE, len(g), g["date"].min().date(), g["date"].max().date()))

    # ---- ① 분야별 금액 기준선 ----
    by_cat = {}
    for c, gg in g.groupby("large_category", observed=True):
        s = amount_stats(gg)
        if s:
            by_cat[c] = s

    # ---- ② 분야 × 분기 (시기별 추이) ----
    rows = []
    for (c, q), gg in g.groupby(["large_category", "quarter"], observed=True):
        s = amount_stats(gg)
        if s:
            rows.append({"large_category": c, "quarter": int(q), **s})
    seasonal = pd.DataFrame(rows)

    # ---- ③ 공고량 전망 ----
    fc = pd.read_parquet(FORECAST) if os.path.exists(FORECAST) else pd.DataFrame()
    if fc.empty:
        print("공고량 전망 파일이 없다 — A07 을 먼저 실행해야 한다")
        return
    fc["dt"] = pd.PeriodIndex(fc["ym"], freq="M").to_timestamp()
    fc["quarter"] = fc["dt"].dt.quarter
    fc["year"] = fc["dt"].dt.year

    vol_q = (fc[fc["scope"] != "전체"]
             .groupby(["scope", "year", "quarter"], observed=True)["forecast"]
             .sum().reset_index()
             .rename(columns={"scope": "large_category", "forecast": "volume_forecast"}))

    out = vol_q.merge(seasonal, on=["large_category", "quarter"], how="left")
    # 분기 표본이 얇으면 분야 전체 기준선으로 채운다(출처를 표시한다).
    out["amount_basis"] = np.where(out["median"].notna(), "분야×분기", "분야 전체")
    for k in ("median", "p25", "p75", "n"):
        out[k] = out[k].fillna(out["large_category"].map(
            lambda c: by_cat.get(c, {}).get(k)))
    out = out.dropna(subset=["median"]).sort_values(
        ["year", "quarter", "volume_forecast"], ascending=[True, True, False])
    out.to_parquet(OUT, index=False)

    print()
    print("=== 분야별 지원규모 기준선 (기업당, 관측) ===")
    print("%-8s%8s%14s%26s" % ("분야", "관측수", "중앙값", "P25 ~ P75"))
    print("-" * 58)
    for c, s in sorted(by_cat.items(), key=lambda kv: -kv[1]["median"]):
        print("%-8s%8d%14s%26s"
              % (c, s["n"], won(s["median"]), "%s ~ %s" % (won(s["p25"]), won(s["p75"]))))

    print()
    print("=== 분야 × 분기 전망 (공고량은 예측, 금액은 관측) ===")
    print("%-9s%-7s%-6s%11s%14s%22s%10s"
          % ("시기", "분야", "관측수", "공고량전망", "금액중앙값", "P25 ~ P75", "금액출처"))
    print("-" * 84)
    for _, r in out.head(24).iterrows():
        print("%-9s%-7s%6d%11.0f건%14s%22s%10s"
              % ("%dQ%d" % (r["year"], r["quarter"]), r["large_category"], r["n"],
                 r["volume_forecast"], won(r["median"]),
                 "%s ~ %s" % (won(r["p25"]), won(r["p75"])), r["amount_basis"]))

    with io.open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# 분야별·시기별 지원 전망\n\n")
        f.write("공고량은 A07 예측값(baseline 대비 12% 개선, 방향정확도 0.82)이고,\n")
        f.write("지원금액은 A02 관측 기반 요약이다. 금액은 예측이 아니다 — 시계열\n")
        f.write("구조가 없어(STL 강도 4개 유형 전부 0.3 미만) 예측하지 않는다.\n\n")
        f.write("> 공고량과 금액을 곱해 총액을 내지 않는다. 공고 1건당 지원 기업 수를\n")
        f.write("> 모르기 때문에 곱하면 근거 없는 숫자가 된다.\n\n")
        f.write("| 시기 | 분야 | 공고량 전망 | 기업당 금액(중앙값) | P25 ~ P75 | 관측수 | 금액 출처 |\n")
        f.write("|---|---|---:|---:|---|---:|---|\n")
        for _, r in out.iterrows():
            f.write("| %dQ%d | %s | %.0f건 | %s | %s ~ %s | %d | %s |\n"
                    % (r["year"], r["quarter"], r["large_category"], r["volume_forecast"],
                       won(r["median"]), won(r["p25"]), won(r["p75"]),
                       r["n"], r["amount_basis"]))
    print()
    print("→ %s" % OUT)
    print("→ %s" % OUT_MD)

    save_report("a09_support_outlook.json", {
        "design": "공고량은 예측(A07), 지원금액은 관측 요약(A02). 성질이 달라 따로 다룬다.",
        "why_amount_not_forecast": (
            "STL 추세·계절성 강도가 4개 금액유형 전부 0.3 미만이고(A03), "
            "예측에서도 어떤 모델도 Last Value baseline 을 못 이겼다(A04). "
            "지원금액은 사업별 예산·정책이 정하는 값이라 시간의 함수가 아니다."),
        "amount_type": AMOUNT_TYPE,
        "min_n": args.min_n,
        "observations_used": int(len(g)),
        "baseline_by_category": by_cat,
        "rows": int(len(out)),
        "amount_basis_dist": out["amount_basis"].value_counts().to_dict(),
        "caveat": ("분기별 금액은 표본이 얇아 신뢰구간이 넓다. 관측수(n)를 함께 보고, "
                   "n 이 작은 칸은 분야 전체 기준선으로 대체했다(금액 출처 열 참고). "
                   "공고량 전망은 순차 예측이라 뒤로 갈수록 오차가 누적된다."),
        "outputs": [OUT, OUT_MD],
    })


if __name__ == "__main__":
    main()
