"""A10 — 지원규모의 연도별 증감 추이 (핵심 결과).

무엇을 묻는가
    "분야별·지원성격별로 예상 지원규모가 해마다 어떻게 변했는가."
    A09 는 지원성격별 '수준'(중앙값·사분위)을 냈고, A03/A04 는 월 단위 시계열
    구조를 봤다. 여기서는 연 단위 '변화'를 본다.

축을 둘로 낸다
    지원성격  융자/판로/사업화… — 모델 1 이 만든 축. 원천에 없는 정보다.
              단 라벨 커버리지가 48.5% 라 셀이 얇다.
    대분류    경영/기술/수출…   — 원천이 주는 축. 결측 0% 라 셀이 두껍다.
    같은 질문을 두 축으로 재고, 결론이 갈리면 그것도 결과로 적는다.

출처를 섞지 않는다 — 이게 이 분석의 핵심 통제다
    관측의 출처가 연도로 완전히 갈린다.
        2019~2025  list_sample  (층화표본, 모집단의 8%)
        2026       openapi      (전량)
    두 구간을 이어 붙여 추세를 그리면 '표본 → 전수' 전환이 증감으로 둔갑한다.
    그래서 추세 검정은 list_sample 2019~2025 로만 하고, 2026 은 다른 기준의
    참고치로 따로 낸다.

이상치·금액유형
    A02 의 is_outlier(SANE_RANGE 밖 = 파싱 오류) 를 제외한다.
    주 지표는 per_company(기업당). 기업이 받는 금액이라 가장 직접적이다.
    나머지 유형도 같이 재서, 결론이 유형에 따라 달라지는지 확인한다.

다중비교
    분야 8개를 각각 검정하면 우연히 유의해지는 게 나온다(α=0.05 에서 기대 0.4개).
    Benjamini-Hochberg 로 보정한 q 값을 함께 낸다. 보정 전후로 판정이 바뀌는
    항목은 '약함'으로 표시한다.

검정력
    이 표본이 몇 %의 변화를 잡을 수 있는지 함께 계산한다. "추세 없음"이
    "변화가 없다"인지 "너무 적어서 못 본다"인지 구분해야 한다.
"""
import argparse
import io
import warnings

import numpy as np
import pandas as pd
from scipy import stats

from common import PROC, REPORTS, save_report, mark_outliers

warnings.filterwarnings("ignore")

OBS = PROC + "/support_amount_observations.parquet"
OUT = PROC + "/amount_trend_by_year.parquet"
OUT_MD = REPORTS + "/a10_amount_trend.md"

TREND_SOURCE = "list_sample"       # 동일 표본설계 구간
TREND_YEARS = (2019, 2025)
REF_SOURCE = "openapi"             # 다른 기준의 참고치
PRIMARY_TYPE = "per_company"
MIN_N_TREND = 12                   # 이보다 적으면 검정하지 않는다
MIN_N_CELL = 3                     # 연도 칸 표시 최소 관측수
ALPHA = 0.05


def won(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    x = float(x)
    if x >= 1e8:
        return "{:.1f}억원".format(x / 1e8)
    if x >= 1e4:
        return "{:,.0f}만원".format(x / 1e4)
    return "{:,.0f}원".format(x)


def bh(pvals):
    """Benjamini-Hochberg q 값. 순서를 지켜 되돌린다."""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    q = np.empty(n)
    prev = 1.0
    for rank, i in enumerate(order[::-1]):
        k = n - rank
        prev = min(prev, p[i] * n / k)
        q[i] = prev
    return q


def spearman_trend(df):
    """연도 vs log10 금액. 금액은 자릿수 범위가 넓어 로그로 본다."""
    if len(df) < MIN_N_TREND:
        return None
    rho, p = stats.spearmanr(df["year"], np.log10(df["amount_max"]))
    if np.isnan(rho):
        return None
    first = df[df["year"] == df["year"].min()]["amount_max"].median()
    last = df[df["year"] == df["year"].max()]["amount_max"].median()
    return {"n": int(len(df)), "rho": round(float(rho), 4), "p": float(p),
            "first_year_median": float(first), "last_year_median": float(last),
            "change_ratio": round(float(last / first), 3) if first else None}


def yearly_table(df, key, years):
    """축 × 연도 중앙값·관측수."""
    rows = []
    for k, g in df.groupby(key, observed=True):
        row = {key: k}
        for y in years:
            s = g[g["year"] == y]["amount_max"]
            row["y%d_median" % y] = float(s.median()) if len(s) >= MIN_N_CELL else None
            row["y%d_n" % y] = int(len(s))
        row["n_total"] = int(len(g))
        rows.append(row)
    return pd.DataFrame(rows)


def power_note(df):
    """연도별 중앙값이 잡음 대비 얼마나 흔들리는지 → 최소 탐지 가능 변화율."""
    lg = np.log10(df["amount_max"])
    sd = float(lg.std())
    per_year = float(df.groupby("year").size().mean())
    se = sd / np.sqrt(per_year)
    mde = (10 ** (1.96 * se) - 1) * 100
    return {"log10_sd": round(sd, 3), "obs_per_year": round(per_year, 1),
            "se_log10": round(se, 3),
            "min_detectable_change_pct": round(mde, 1)}


def run_axis(df, key, years, label, lines):
    tab = yearly_table(df, key, years)
    tab = tab.sort_values("n_total", ascending=False)

    print()
    print("=== %s × 연도 — 중앙값(관측수) ===" % label)
    hdr = "%-12s" % label + "".join("%15d" % y for y in years)
    print(hdr)
    print("-" * len(hdr))
    for _, r in tab.iterrows():
        row = "%-12s" % r[key]
        for y in years:
            m, n = r["y%d_median" % y], r["y%d_n" % y]
            row += "%15s" % (("%s(%d)" % (won(m), n)) if m is not None else
                             ("—(%d)" % n if n else "—"))
        print(row)

    # 추세 검정
    res, keys = [], []
    for k, g in df.groupby(key, observed=True):
        t = spearman_trend(g)
        if t:
            res.append(t)
            keys.append(k)
    if not res:
        return tab, []
    qs = bh([r["p"] for r in res])
    out = []
    for k, r, q in zip(keys, res, qs):
        sig_raw = r["p"] < ALPHA
        sig_adj = q < ALPHA
        if not sig_raw:
            verdict = "추세없음"
        elif sig_adj:
            verdict = "증가" if r["rho"] > 0 else "감소"
        else:
            verdict = ("증가(약함)" if r["rho"] > 0 else "감소(약함)")
        out.append({key: k, **r, "q": round(float(q), 4),
                    "verdict": verdict})
    out = sorted(out, key=lambda d: d["p"])

    print()
    print("추세 검정 (연도 vs log10 금액, Spearman / BH 보정)")
    print("%-12s%7s%10s%10s%10s%12s" % (label, "n", "rho", "p", "q(BH)", "판정"))
    print("-" * 62)
    for r in out:
        print("%-12s%7d%10.3f%10.4f%10.4f%12s"
              % (r[key], r["n"], r["rho"], r["p"], r["q"], r["verdict"]))

    lines.append("\n## %s × 연도\n" % label)
    lines.append("| %s | " % label + " | ".join(str(y) for y in years) + " | 전체 n |")
    lines.append("|---" * (len(years) + 2) + "|")
    for _, r in tab.iterrows():
        cells = []
        for y in years:
            m, n = r["y%d_median" % y], r["y%d_n" % y]
            cells.append(("%s (%d)" % (won(m), n)) if m is not None else
                         ("— (%d)" % n if n else "—"))
        lines.append("| %s | %s | %d |" % (r[key], " | ".join(cells), r["n_total"]))
    lines.append("\n| %s | n | rho | p | q(BH) | 판정 |" % label)
    lines.append("|---|---:|---:|---:|---:|---|")
    for r in out:
        lines.append("| %s | %d | %.3f | %.4f | %.4f | %s |"
                     % (r[key], r["n"], r["rho"], r["p"], r["q"], r["verdict"]))
    return tab, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount-type", default=PRIMARY_TYPE)
    args = ap.parse_args()

    obs = pd.read_parquet(OBS)
    if "is_outlier" not in obs.columns:
        obs["is_outlier"] = mark_outliers(obs)
    obs = obs[~obs["is_outlier"] & obs["amount_max"].notna()].copy()
    obs["year"] = pd.to_datetime(obs["date"], errors="coerce").dt.year

    y0, y1 = TREND_YEARS
    years = list(range(y0, y1 + 1))
    base = obs[(obs["source"] == TREND_SOURCE) & obs["year"].between(y0, y1)]
    g = base[base["amount_type"] == args.amount_type]

    print("추세 구간: %s %d~%d / %s %d건 (이상치 제외)"
          % (TREND_SOURCE, y0, y1, args.amount_type, len(g)))
    print("2026 은 출처가 %s(전량)로 달라 추세 검정에서 제외하고 참고치로만 낸다." % REF_SOURCE)

    lines = ["# 지원규모의 연도별 증감 추이", "",
             "지원성격 축과 대분류 축으로 같은 질문을 두 번 재고, 결론이 갈리면 그것도 적는다.",
             "",
             "> **출처 통제** — 관측 출처가 연도로 갈린다(2019~2025 층화표본 / 2026 전량).",
             "> 이어 붙이면 '표본→전수' 전환이 증감으로 둔갑하므로, 추세는 2019~2025 로만 재고",
             "> 2026 은 다른 기준의 참고치로 따로 낸다.", ""]

    # ---- 축 1: 지원성격 (모델 1 산출) ----
    gt = g[g["support_type"].notna()]
    print("\n지원성격 라벨 보유 %d건 (%.1f%%)" % (len(gt), len(gt) / len(g) * 100))
    tab_t, trend_t = run_axis(gt, "support_type", years, "지원성격", lines)

    # ---- 축 2: 대분류 (원천 제공) ----
    tab_c, trend_c = run_axis(g, "large_category", years, "대분류", lines)

    # ---- 전체 pooled ----
    pooled = spearman_trend(g)
    med = {int(y): float(v) for y, v in g.groupby("year")["amount_max"].median().items()}
    cnt = {int(y): int(v) for y, v in g.groupby("year").size().items()}
    print()
    print("=== 전체 pooled ===")
    print("n=%d  rho=%.3f  p=%.4f  → %s"
          % (pooled["n"], pooled["rho"], pooled["p"],
             "추세없음" if pooled["p"] >= ALPHA else "추세있음"))
    print("연도별 중앙값:", {y: won(v) for y, v in med.items()})

    # ---- 금액유형별 교차확인 ----
    by_type = {}
    print()
    print("=== 금액유형별 교차확인 (결론이 유형에 따라 달라지는가) ===")
    print("%-14s%8s%10s%10s%12s" % ("금액유형", "n", "rho", "p", "판정"))
    print("-" * 56)
    for t in ["per_company", "per_project", "total_budget", "periodic"]:
        s = base[base["amount_type"] == t]
        r = spearman_trend(s)
        if r:
            v = "추세없음" if r["p"] >= ALPHA else ("증가" if r["rho"] > 0 else "감소")
            by_type[t] = {**r, "verdict": v}
            print("%-14s%8d%10.3f%10.4f%12s" % (t, r["n"], r["rho"], r["p"], v))

    # ---- 검정력 ----
    pw = power_note(g)
    print()
    print("=== 검정력 ===")
    print("log10 표준편차 %.3f / 연평균 관측 %.0f건 → 연도 중앙값 표준오차 %.3f"
          % (pw["log10_sd"], pw["obs_per_year"], pw["se_log10"]))
    print("→ 연간 %.0f%% 미만의 변화는 잡음과 구별하기 어렵다."
          % pw["min_detectable_change_pct"])
    print("   '추세 없음'은 '변화가 없다'가 아니라 '이 표본으로는 못 본다'에 가깝다.")

    # ---- 2026 참고치 ----
    ref = obs[(obs["source"] == REF_SOURCE) & (obs["amount_type"] == args.amount_type)]
    ref_t = {}
    if len(ref):
        print()
        print("=== 2026 참고치 (%s 전량 — 위 추세와 기준이 다름) ===" % REF_SOURCE)
        rt = ref[ref["support_type"].notna()].groupby("support_type")["amount_max"].agg(["count", "median"])
        for k, r in rt[rt["count"] >= 5].sort_values("median", ascending=False).iterrows():
            ref_t[k] = {"n": int(r["count"]), "median": float(r["median"])}
            print("  %-12s %s (n=%d)" % (k, won(r["median"]), int(r["count"])))

    # ---- 저장 ----
    tab_t.assign(axis="support_type").rename(columns={"support_type": "key"}).to_parquet(
        OUT, index=False)

    lines += ["", "## 전체 pooled", "",
              "| 연도 | 중앙값 | 관측수 |", "|---|---:|---:|"]
    for y in years:
        lines.append("| %d | %s | %d |" % (y, won(med.get(y, np.nan)), cnt.get(y, 0)))
    lines += ["", "rho %.3f / p %.4f → **%s**"
              % (pooled["rho"], pooled["p"],
                 "추세없음" if pooled["p"] >= ALPHA else "추세있음"), ""]

    lines += ["## 검정력", "",
              "- log10 금액 표준편차 %.3f, 연평균 관측 %.0f건" % (pw["log10_sd"], pw["obs_per_year"]),
              "- 연도별 중앙값의 표준오차 %.3f (log10) → **연간 %.0f%% 미만의 변화는 탐지 불가**"
              % (pw["se_log10"], pw["min_detectable_change_pct"]),
              "- 따라서 '추세 없음'은 '변화가 없다'가 아니라 '이 표본으로는 못 본다'로 읽어야 한다.",
              ""]
    if ref_t:
        lines += ["## 2026 참고치 (Open API 전량 — 기준이 다름)", "",
                  "| 지원성격 | 중앙값 | 관측수 |", "|---|---:|---:|"]
        for k, v in sorted(ref_t.items(), key=lambda kv: -kv[1]["median"]):
            lines.append("| %s | %s | %d |" % (k, won(v["median"]), v["n"]))

    io.open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))

    save_report("a10_amount_trend.json", {
        "question": "분야별·지원성격별로 예상 지원규모가 해마다 어떻게 변했는가",
        "trend_source": TREND_SOURCE, "trend_years": list(TREND_YEARS),
        "source_control": ("관측 출처가 연도로 갈린다(2019~2025 층화표본 / 2026 전량). "
                           "이어 붙이면 표본→전수 전환이 증감으로 둔갑하므로 분리했다."),
        "amount_type": args.amount_type,
        "n_rows": int(len(g)),
        "n_with_support_type": int(len(gt)),
        "support_type_coverage": round(len(gt) / len(g), 4),
        "by_support_type": trend_t,
        "by_large_category": trend_c,
        "pooled": pooled,
        "pooled_median_by_year": med,
        "pooled_count_by_year": cnt,
        "by_amount_type": by_type,
        "multiple_comparison": "Benjamini-Hochberg q. 보정 후 유의하지 않으면 '약함' 표기.",
        "power": pw,
        "reference_2026": ref_t,
        "caveat": ("셀이 얇다. 지원성격 축은 라벨 커버리지가 %.1f%% 라 더 얇다. "
                   "연도 칸은 관측 %d건 이상일 때만 값을 낸다."
                   % (len(gt) / len(g) * 100, MIN_N_CELL)),
        "outputs": [OUT, OUT_MD],
    })
    print()
    print("→ %s" % OUT)
    print("→ %s" % OUT_MD)


if __name__ == "__main__":
    main()
