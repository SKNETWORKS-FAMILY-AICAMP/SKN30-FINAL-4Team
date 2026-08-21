"""A09 — 지원성격별 지원규모 기준선 + 분야별 공고량 전망.

두 축을 왜 다르게 잡는가
    ① 지원규모  지원성격(융자/판로/설비…)으로 나눈다.  <- 모델 1 의 산출
    ② 공고량    분야(경영/기술/수출…)로 나눈다.        <- 원천 제공 필드

    처음에는 둘 다 분야로 나눴다. 그런데 분야는 기업마당이 원천에 이미 담아 주는
    값이라, 그것만 쓰면 모델 1 이 모델 2 에 아무 기여도 하지 않는다. 모델 1 을
    만든 이유가 원천에 없는 '지원성격' 축을 만드는 것인데 정작 쓰지 않고 있었다.
    기업이 하는 질문도 "금융 분야는 얼마쯤?"이 아니라 "융자를 받으면 얼마쯤?"이다.

    금액 쪽은 실제로 지원성격이 더 잘 설명한다(같은 표본에서 55.2% vs 48.4%).
    무엇보다 값의 폭이 다르다 — 융자 2.5억원 / 판로 300만원으로 80배 차이가
    나는데, 분야로 묶으면 이 구분이 평균에 묻힌다.

공고량은 왜 지원성격으로 못 나누는가 (실측 근거)
    지원성격 라벨은 모델 1 이 원문을 읽어야 나온다. 그런데 장기 공고량 시계열의
    원천인 목록 97,794건은 대부분 원문이 없고 제목만 있다. 제목만으로 분류하면
    두 가지가 깨진다.

      ㄱ. 연도별 분류율이 30.0~41.3% 로 11.3%p 흔들린다.
          → 라벨 붙은 건수를 세면 커버리지 변동이 가짜 추세로 나타난다.
      ㄴ. 구성비 자체가 편향된다. 같은 공고를 원문으로 분류한 결과와 비교하면
          사업화 30.7%->20.7%(-10.0%p), 융자 11.6%->20.3%(+8.7%p), TVD 0.146.
          융자는 제목만으로 티가 나고 사업화는 본문을 봐야 알 수 있어서다.
          → 총량을 구성비로 배분하는 방법도 쓸 수 없다.

    (분류기 자체가 불안정한 것은 아니다. 두 방식이 모두 확신한 753건의 라벨
     일치율은 92.3% 다. 문제는 '어느 건을 확신하느냐'가 편향된다는 것이다.)

    그래서 공고량은 결측 0% 인 분야 축을 유지한다. 지원성격별 공고량 전망은
    목록 원문 확보율이 오르기 전에는 제공하지 않는다.

곱하지 않는다
    공고량 전망과 기업당 금액을 곱해 총액을 내지 않는다. 공고 1건당 지원 기업
    수를 모르기 때문에 곱하면 근거 없는 숫자가 된다. 두 표를 나란히 낸다.
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
OUT_AMOUNT = PROC + "/support_outlook_by_type.parquet"
OUT_VOLUME = PROC + "/support_outlook_volume.parquet"
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


def amount_stats(g, min_n):
    a = g["amount_max"].dropna()
    if len(a) < min_n:
        return None
    return {"n": int(len(a)), "median": float(a.median()),
            "p25": float(a.quantile(.25)), "p75": float(a.quantile(.75))}


def summarize(g, keys, min_n):
    rows = []
    for kv, gg in g.groupby(keys, observed=True):
        s = amount_stats(gg, min_n)
        if not s:
            continue
        kv = kv if isinstance(kv, tuple) else (kv,)
        rows.append({**dict(zip(keys, kv)), **s})
    return pd.DataFrame(rows)


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
    n_all = len(g)
    gt = g[g["support_type"].notna()].copy()
    print("지원규모 관측(%s, 이상치 제외) %d건 / 지원성격 보유 %d건 (%.1f%%)"
          % (AMOUNT_TYPE, n_all, len(gt), len(gt) / n_all * 100))
    print("기간 %s ~ %s" % (g["date"].min().date(), g["date"].max().date()))

    # ---- ① 지원성격별 기준선 (주축) ----
    by_type = summarize(gt, ["support_type"], args.min_n)
    by_type = by_type.sort_values("median", ascending=False)

    print()
    print("=== ① 지원성격별 기업당 지원금 (모델 1 산출 축) ===")
    print("%-12s%8s%14s%26s" % ("지원성격", "관측수", "중앙값", "P25 ~ P75"))
    print("-" * 62)
    for _, r in by_type.iterrows():
        print("%-12s%8d%14s%26s"
              % (r["support_type"], r["n"], won(r["median"]),
                 "%s ~ %s" % (won(r["p25"]), won(r["p75"]))))

    # ---- ② 지원성격 × 분기 (시기별) ----
    seasonal = summarize(gt, ["support_type", "quarter"], args.min_n)
    if not seasonal.empty:
        base = by_type.set_index("support_type")
        seasonal["amount_basis"] = "지원성격×분기"
        print()
        print("=== ② 지원성격 × 분기 (표본 %d건 이상인 칸만) ===" % args.min_n)
        print("%-12s%8s%8s%14s%26s" % ("지원성격", "분기", "관측수", "중앙값", "P25 ~ P75"))
        print("-" * 70)
        for _, r in seasonal.sort_values(["support_type", "quarter"]).iterrows():
            print("%-12s%8s%8d%14s%26s"
                  % (r["support_type"], "Q%d" % r["quarter"], r["n"], won(r["median"]),
                     "%s ~ %s" % (won(r["p25"]), won(r["p75"]))))
        seasonal.to_parquet(OUT_AMOUNT, index=False)
    else:
        by_type.to_parquet(OUT_AMOUNT, index=False)

    # ---- ③ 분야별 공고량 전망 (보조축, 완전 데이터) ----
    vol = pd.DataFrame()
    if os.path.exists(FORECAST):
        fc = pd.read_parquet(FORECAST)
        fc["dt"] = pd.PeriodIndex(fc["ym"], freq="M").to_timestamp()
        fc["quarter"] = fc["dt"].dt.quarter
        fc["year"] = fc["dt"].dt.year
        vol = (fc[fc["scope"] != "전체"]
               .groupby(["scope", "year", "quarter"], observed=True)["forecast"]
               .sum().reset_index()
               .rename(columns={"scope": "large_category",
                                "forecast": "volume_forecast"}))
        vol = vol.sort_values(["year", "quarter", "volume_forecast"],
                              ascending=[True, True, False])
        vol.to_parquet(OUT_VOLUME, index=False)
        print()
        print("=== ③ 분야별 공고량 전망 (원천 제공 축 — 결측 0%) ===")
        print("%-9s%-8s%12s" % ("시기", "분야", "공고량 전망"))
        print("-" * 32)
        for _, r in vol.head(16).iterrows():
            print("%-9s%-8s%11.0f건"
                  % ("%dQ%d" % (r["year"], r["quarter"]), r["large_category"],
                     r["volume_forecast"]))
    else:
        print("\n[경고] 공고량 전망 파일이 없다 — A07 을 먼저 실행해야 한다")

    # ---- 문서 ----
    with io.open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# 지원성격별 지원규모 + 분야별 공고량 전망\n\n")
        f.write("두 표는 축이 다르다. 금액은 모델 1 이 만든 **지원성격** 축, 공고량은\n")
        f.write("원천이 제공하는 **분야** 축이다. 공고량을 지원성격으로 못 나누는\n")
        f.write("이유는 아래 '한계'에 적었다.\n\n")
        f.write("> 두 값을 곱해 총액을 내지 않는다. 공고 1건당 지원 기업 수를 모르기\n")
        f.write("> 때문에 곱하면 근거 없는 숫자가 된다.\n\n")

        f.write("## ① 지원성격별 기업당 지원금 (관측 기반)\n\n")
        f.write("| 지원성격 | 관측수 | 중앙값 | P25 ~ P75 |\n|---|---:|---:|---|\n")
        for _, r in by_type.iterrows():
            f.write("| %s | %d | %s | %s ~ %s |\n"
                    % (r["support_type"], r["n"], won(r["median"]),
                       won(r["p25"]), won(r["p75"])))

        if not seasonal.empty:
            f.write("\n## ② 지원성격 × 분기\n\n")
            f.write("| 지원성격 | 분기 | 관측수 | 중앙값 | P25 ~ P75 |\n|---|---|---:|---:|---|\n")
            for _, r in seasonal.sort_values(["support_type", "quarter"]).iterrows():
                f.write("| %s | Q%d | %d | %s | %s ~ %s |\n"
                        % (r["support_type"], r["quarter"], r["n"], won(r["median"]),
                           won(r["p25"]), won(r["p75"])))

        if not vol.empty:
            f.write("\n## ③ 분야별 공고량 전망\n\n")
            f.write("| 시기 | 분야 | 공고량 전망 |\n|---|---|---:|\n")
            for _, r in vol.iterrows():
                f.write("| %dQ%d | %s | %.0f건 |\n"
                        % (r["year"], r["quarter"], r["large_category"],
                           r["volume_forecast"]))

        f.write("\n## 한계\n\n")
        f.write("- **금액은 예측이 아니라 관측 요약이다.** 지원규모는 시계열 구조가 없어\n")
        f.write("  (STL 강도 4개 유형 전부 0.3 미만, A03) 어떤 모델도 baseline 을 못\n")
        f.write("  이겼다(A04). 그래서 분포만 보여준다.\n")
        f.write("- **공고량은 지원성격으로 나누지 않는다.** 목록 97,794건은 대부분 원문이\n")
        f.write("  없어 제목만으로 분류해야 하는데, 연도별 분류율이 11.3%p 흔들리고\n")
        f.write("  구성비도 편향된다(사업화 -10.0%p / 융자 +8.7%p, TVD 0.146).\n")
        f.write("- **표본이 얇은 칸이 있다.** 관측수(n)를 함께 보고, %d건 미만은 아예 내지\n" % args.min_n)
        f.write("  않았다. 지원성격 라벨 커버리지 자체도 %.1f%% 다.\n"
                % (len(gt) / n_all * 100))

    save_report("a09_support_outlook.json", {
        "design": ("금액은 지원성격 축(모델 1 산출), 공고량은 분야 축(원천 제공). "
                   "축이 다른 이유와 근거를 아래에 남긴다."),
        "amount_axis": "support_type",
        "volume_axis": "large_category",
        "why_amount_by_support_type": {
            "reason": ("분야는 원천이 이미 주는 값이라 그것만 쓰면 모델 1 이 모델 2 에 "
                       "기여하지 못한다. 기업의 질문도 '융자를 받으면 얼마쯤'이다."),
            "variance_explained_same_sample": {"support_type": 55.2,
                                               "large_category": 48.4},
            "spread_example": "융자 중앙값 2.5억원 vs 판로 300만원 (80배)",
        },
        "why_volume_not_by_support_type": {
            "coverage_drift_pct_point": 11.3,
            "yearly_usable_rate_range": [30.0, 41.3],
            "composition_bias_tvd": 0.146,
            "composition_bias_examples": {"사업화": -10.0, "융자": 8.7},
            "classifier_agreement_when_both_confident": 92.3,
            "conclusion": ("커버리지가 흔들려 건수를 세면 가짜 추세가 나고, 구성비도 "
                           "편향돼 총량 배분도 못 쓴다. 목록 원문 확보율이 오르기 전에는 "
                           "제공하지 않는다."),
        },
        "amount_type": AMOUNT_TYPE,
        "min_n": args.min_n,
        "observations_total": int(n_all),
        "observations_with_support_type": int(len(gt)),
        "support_type_coverage": round(len(gt) / n_all, 4),
        "by_support_type": by_type.to_dict("records"),
        "by_support_type_quarter": seasonal.to_dict("records") if not seasonal.empty else [],
        "volume_rows": int(len(vol)),
        "no_multiplication": ("공고량과 기업당 금액을 곱해 총액을 내지 않는다. "
                              "공고 1건당 지원 기업 수를 모른다."),
        "outputs": [OUT_AMOUNT, OUT_VOLUME, OUT_MD],
    })
    print()
    print("→ %s" % OUT_AMOUNT)
    print("→ %s" % OUT_VOLUME)
    print("→ %s" % OUT_MD)


if __name__ == "__main__":
    main()
