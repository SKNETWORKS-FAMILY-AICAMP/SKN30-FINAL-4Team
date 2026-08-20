"""A05 — 지원규모 관측 기반 추이·범위 산출물 (설계서 v3 19장 / 21.3장).

설계서 v3 18장에서 조건5(baseline 개선) 미충족으로 예측을 제공하지 않기로 했다.
19장이 정한 대안은 '예측 대신 관측 기반 추이와 범위'다.

    예상 지원금 = 53,821,400원          ← 이렇게 하지 않는다
    최근 3년 기술 분야 기업당 지원금
    중앙값 약 5,000만원, P25~P75 2,000만~1억원   ← 이렇게 한다

핵심 원칙
  1) 금액 의미를 섞지 않는다(설계서 v3 24장 규칙8).
     per_company / total_budget / periodic / per_project 를 분리해 산출한다.
  2) 이상치가 강하므로 평균만 쓰지 않고 median·p25·p75 를 함께 낸다.
  3) 관측 건수를 반드시 함께 표기한다. n=3 인 중앙값과 n=200 인 중앙값은
     신뢰도가 다르다. 최소 관측 미만은 '표본 부족'으로 표시한다.
  4) sum_total_budget 은 정부 전체 예산이 아니라 '관측된 공고문에서 추출된
     총사업비의 합계'로만 해석한다(설계서 v3 4.6 주석).

산출물
  reference_support_amount.parquet  : 기계 판독용 전체 조합
  19_지원규모_참고범위.md            : 사람이 읽는 리포트
"""
import argparse
import os

import numpy as np
import pandas as pd

from common import PROC, ROOT, save_report, SANE_RANGE

OBS = PROC + "/support_amount_observations.parquet"
OUT_PARQUET = PROC + "/reference_support_amount.parquet"
OUT_MD = os.path.join(ROOT, "reports", "19_지원규모_참고범위.md")

TYPES = ["per_company", "total_budget", "periodic", "per_project"]
TYPE_LABEL = {
    "per_company": "기업당 지원금",
    "total_budget": "총사업비",
    "periodic": "정기 지원금(월/연)",
    "per_project": "과제당 지원금",
}
MIN_N = 5          # 이 미만이면 참고범위를 제시하지 않는다
RECENT_YEARS = 3

# 금액 의미별 상식 범위. 벗어나면 파싱 오류로 보고 제외한다.
#
# 실제로 확인된 오류: "예산규모는 1조 4,517억원"(정부 전체 창업지원 예산)에서
# 파서가 '1조'를 놓치고 '4,517억원'만 뽑은 뒤 per_company 로 분류했다.
# 기업 한 곳에 4,517억을 준다는 값이 통계에 섞이면 평균이 완전히 왜곡된다.
# (중앙값은 견디지만 평균·최대값이 무의미해진다)
# SANE_RANGE 는 common 에 있다 — a02(플래그 부착)·a03·a04 와 같은 기준을 쓴다.


def won(v):
    """원 단위를 읽기 쉬운 한국어 표기로."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    v = float(v)
    if v >= 1e8:
        return "%.1f억원" % (v / 1e8)
    if v >= 1e4:
        return "%,.0f만원".replace(",", "") % (v / 1e4) if False else "{:,.0f}만원".format(v / 1e4)
    return "{:,.0f}원".format(v)


def summarize(g):
    """설계서 v3 15장 지표."""
    a = g["amount_max"].dropna()
    if a.empty:
        return None
    return {
        "n": int(len(a)),
        "median": float(a.median()),
        "mean": float(a.mean()),
        "p25": float(a.quantile(0.25)),
        "p75": float(a.quantile(0.75)),
        "min": float(a.min()),
        "max": float(a.max()),
        "sum": float(a.sum()),
        "median_support_ratio": (float(g["support_ratio"].median())
                                 if g["support_ratio"].notna().any() else None),
        "median_support_count": (float(g["support_count"].median())
                                 if g["support_count"].notna().any() else None),
    }


def build(obs, keys):
    rows = []
    for kv, g in obs.groupby(keys, observed=True, dropna=False):
        s = summarize(g)
        if s is None:
            continue
        rec = dict(zip(keys if isinstance(keys, list) else [keys],
                       kv if isinstance(kv, tuple) else (kv,)))
        rec.update(s)
        rec["sufficient"] = s["n"] >= MIN_N
        rows.append(rec)
    return pd.DataFrame(rows)


def md_table(df, dim_cols, title, note=None):
    lines = ["### %s" % title, ""]
    if note:
        lines += ["> %s" % note, ""]
    header = dim_cols + ["관측수", "중앙값", "P25~P75", "평균", "최소~최대"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(dim_cols)) + "|" + "---:|" * 5)
    for _, r in df.iterrows():
        dims = [str(r[c]) for c in dim_cols]
        if not r["sufficient"]:
            lines.append("| " + " | ".join(dims)
                         + " | %d | *표본 부족* | — | — | — |" % r["n"])
            continue
        lines.append("| " + " | ".join(dims) + " | %d | **%s** | %s ~ %s | %s | %s ~ %s |"
                     % (r["n"], won(r["median"]), won(r["p25"]), won(r["p75"]),
                        won(r["mean"]), won(r["min"]), won(r["max"])))
    lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    obs = pd.read_parquet(OBS)
    obs = obs[obs["amount_type"].isin(TYPES)].copy()
    n_before = len(obs)

    # 상식 범위를 벗어난 값은 파싱 오류. a02 가 붙인 is_outlier 플래그를 쓴다
    # (플래그가 없는 구버전 parquet 이면 여기서 직접 계산한다).
    if "is_outlier" not in obs.columns:
        from common import mark_outliers
        obs["is_outlier"] = mark_outliers(obs)
    dropped = obs[obs["is_outlier"]]["amount_type"].value_counts().to_dict()
    obs = obs[~obs["is_outlier"]].copy()
    n_dropped = n_before - len(obs)
    print("이상치 제외: %d건 (%.1f%%) — %s"
          % (n_dropped, n_dropped / n_before * 100,
             ", ".join("%s %d" % (k, v) for k, v in dropped.items() if v)))

    latest_year = int(obs["year"].max())
    recent_from = latest_year - RECENT_YEARS + 1

    frames = {}
    frames["by_type"] = build(obs, ["amount_type"])
    frames["by_type_category"] = build(obs, ["amount_type", "large_category"])
    frames["by_type_year"] = build(obs, ["amount_type", "year"])
    # 지원성격은 라벨이 있는 건만 (모델1 판단보류 제외)
    labeled = obs[obs["support_type"].notna()
                  & (obs["support_type_status"] != "판단보류")]
    frames["by_type_support"] = (build(labeled, ["amount_type", "support_type"])
                                 if not labeled.empty else pd.DataFrame())
    recent = obs[obs["year"] >= recent_from]
    frames["recent_by_type_category"] = build(recent, ["amount_type", "large_category"])

    allrows = []
    for name, df in frames.items():
        if df.empty:
            continue
        d = df.copy()
        d.insert(0, "view", name)
        allrows.append(d)
    full = pd.concat(allrows, ignore_index=True)
    full.to_parquet(OUT_PARQUET, index=False)

    # ---------- 사람이 읽는 리포트 ----------
    L = []
    L.append("# 지원규모 참고범위 (관측 기반)")
    L.append("")
    L.append("> 설계서 v3 19장 산출물. **예측값이 아니라 실제 공고문에서 추출된 관측치의 요약**이다.")
    L.append("> 지원규모 예측은 설계서 v3 18장 조건5(walk-forward에서 baseline 개선)를 충족하지 못해 제공하지 않는다.")
    L.append("")
    L.append("- 관측 기간: **%d ~ %d년**" % (int(obs["year"].min()), latest_year))
    L.append("- 총 관측: **%s건** (의미가 확정된 금액만)" % "{:,}".format(len(obs)))
    L.append("- 출처: 기업마당 공고문 원문 (pdf-inspector / rhwp 추출)")
    L.append("- 관측 %d건 미만 조합은 **표본 부족**으로 표기하고 범위를 제시하지 않는다." % args.min_n)
    L.append("- 파싱 오류로 판단되는 이상치 **%d건(%.1f%%)** 을 제외했다. "
             "예: \"예산규모는 1조 4,517억원\"(정부 전체 예산)을 기업당 지원금으로 잘못 뽑은 사례."
             % (n_dropped, n_dropped / n_before * 100))
    L.append("")
    L.append("> **금액 의미를 섞지 않았다.** 기업당 지원금과 총사업비는 성격이 달라 같은 통계로 묶으면 무의미하다.")
    L.append("")
    L.append("---")
    L.append("")

    L += md_table(frames["by_type"].assign(
        amount_type=lambda d: d["amount_type"].map(TYPE_LABEL)),
        ["amount_type"], "1. 금액 의미별 전체 요약",
        "가장 먼저 보아야 할 표. 기업당 지원금 중앙값이 실무에서 가장 자주 쓰인다.")

    for t in TYPES:
        sub = frames["by_type_category"]
        sub = sub[sub["amount_type"] == t].sort_values("n", ascending=False)
        if sub.empty:
            continue
        L += md_table(sub, ["large_category"],
                      "2-%d. %s — 분야별" % (TYPES.index(t) + 1, TYPE_LABEL[t]))

    sub = frames["by_type_year"]
    sub = sub[sub["amount_type"] == "per_company"].sort_values("year")
    if not sub.empty:
        L += md_table(sub, ["year"], "3. 기업당 지원금 — 연도별 추이",
                      "설계서 v3 18장 조건5 미충족으로 **향후 예측은 제공하지 않는다.** 아래는 과거 관측 추이다.")

    if not frames["by_type_support"].empty:
        sub = frames["by_type_support"]
        sub = sub[sub["amount_type"] == "per_company"].sort_values("n", ascending=False)
        if not sub.empty:
            L += md_table(sub, ["support_type"],
                          "4. 기업당 지원금 — 지원성격별",
                          "모델 1의 신뢰·참고용 등급 예측만 사용(판단보류 제외). "
                          "라벨 밀도가 낮아 대부분 표본 부족이다.")

    sub = frames["recent_by_type_category"]
    sub = sub[sub["amount_type"] == "per_company"].sort_values("n", ascending=False)
    if not sub.empty:
        L += md_table(sub, ["large_category"],
                      "5. 최근 %d년(%d~%d) 기업당 지원금 — 분야별"
                      % (RECENT_YEARS, recent_from, latest_year),
                      "Pre-Review 에서 가장 실용적인 표. 오래된 공고를 제외해 현재 수준에 가깝다.")

    L.append("---")
    L.append("")
    L.append("## 해석 주의사항")
    L.append("")
    L.append("1. **예측이 아니다.** 과거에 관측된 값의 요약이며, 향후 공고가 이 범위에 들어간다는 보장이 없다.")
    L.append("2. **총사업비 합계는 정부 예산이 아니다.** 원문에서 추출에 성공한 공고만의 합계다.")
    L.append("3. **평균보다 중앙값을 보라.** 대형 사업 한 건이 평균을 크게 끌어올린다.")
    L.append("4. **표본 부족 조합은 판단 근거로 쓰지 않는다.** 관측 %d건 미만은 범위를 제시하지 않았다." % args.min_n)
    L.append("5. 지원성격 라벨은 모델 추론값이며 판단보류(70.5%)를 제외한 것이다.")
    L.append("")

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % OUT_MD)

    # 콘솔 요약
    print()
    print("=== 금액 의미별 참고범위 ===")
    print("%-16s%7s%14s%26s" % ("금액의미", "관측수", "중앙값", "P25~P75"))
    print("-" * 66)
    for _, r in frames["by_type"].iterrows():
        print("%-16s%7d%14s%26s"
              % (TYPE_LABEL[r["amount_type"]], r["n"], won(r["median"]),
                 won(r["p25"]) + " ~ " + won(r["p75"])))

    print()
    print("=== 최근 %d년 기업당 지원금 — 분야별 ===" % RECENT_YEARS)
    print("%-8s%7s%14s%26s" % ("분야", "관측수", "중앙값", "P25~P75"))
    print("-" * 58)
    for _, r in sub.iterrows():
        if not r["sufficient"]:
            print("%-8s%7d%14s" % (r["large_category"], r["n"], "표본 부족"))
            continue
        print("%-8s%7d%14s%26s"
              % (r["large_category"], r["n"], won(r["median"]),
                 won(r["p25"]) + " ~ " + won(r["p75"])))

    save_report("a05_support_reference.json", {
        "observation_years": [int(obs["year"].min()), latest_year],
        "total_observations": int(len(obs)),
        "min_n_for_range": args.min_n,
        "outliers_dropped": int(n_dropped),
        "outliers_dropped_by_type": dropped,
        "sane_range": {k: list(v) for k, v in SANE_RANGE.items()},
        "recent_window_years": RECENT_YEARS,
        "views": {k: int(len(v)) for k, v in frames.items() if not v.empty},
        "by_type_summary": {
            TYPE_LABEL[r["amount_type"]]: {
                "n": int(r["n"]), "median": r["median"],
                "p25": r["p25"], "p75": r["p75"], "mean": r["mean"],
            } for _, r in frames["by_type"].iterrows()},
        "policy": ("설계서 v3 18장 조건5 미충족으로 예측 미제공. "
                   "19장에 따라 관측 기반 추이·범위만 산출."),
        "outputs": {"parquet": OUT_PARQUET, "markdown": OUT_MD},
    })


if __name__ == "__main__":
    main()
