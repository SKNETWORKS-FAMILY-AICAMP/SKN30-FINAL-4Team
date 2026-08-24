"""M10 — 모델 2~4 착수 전 feature coverage / 실현가능성 게이트.

설계서 Step 2 에 해당한다. 모델을 짜기 전에 "그 모델이 요구하는 feature 가
실제로 몇 %나 있는가"를 재고, 없으면 없다고 적는다.

여기서 답하는 질문 네 가지
    1. 설계서가 나열한 feature 중 실제로 존재하는 것은 무엇인가
    2. '지원성격 + 지원방식' 2단 비교군이 실제로 2단인가
       (지원방식이 지원성격에 흡수되면 비교군은 1단으로 붕괴한다 -> Cramer's V)
    3. 비교군을 잘랐을 때 30건 이상 남는 칸이 몇 개인가 (모델 3·4 의 실질 제약)
    4. 금액 축이 서로 다른 의미(총예산/기업당/기간당)로 섞여 있지 않은가

결론은 모델별 Go / Conditional / No-Go 로 적는다. 지표가 나쁘면 나쁘다고 적는 것이
이 스크립트의 목적이다 — 통과시키는 것이 목적이 아니다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SRC = os.path.join(C.PROC, "design_features.parquet")

# 설계서가 모델별로 요구한 feature. 이름이 다르면 우리 컬럼명으로 옮겨 적었다.
REQUIRED = {
    "model2": ["support_type", "support_method", "support_target", "industry",
               "policy_purpose", "amount_max", "support_count", "per_recipient",
               "support_ratio", "support_unit", "project_duration", "agency_type"],
    "model3": ["support_type", "support_method", "amount_max", "amount_type",
               "support_count", "per_recipient", "support_ratio",
               "self_burden_ratio", "support_unit"],
    "model4": ["support_type", "support_method", "support_target", "support_count",
               "per_recipient", "amount_max", "support_ratio", "project_duration",
               "support_unit", "industry", "policy_purpose", "agency_type"],
}
MIN_COHORT = 30      # 설계서: 독립 비교군 후보
MIN_REFERENCE = 15   # 설계서: 소규모 참고 패턴


def cramers_v(a, b):
    """두 범주형 축의 연관 강도. 1에 가까우면 한 축이 다른 축을 결정한다."""
    from scipy.stats import chi2_contingency
    tab = pd.crosstab(a, b)
    if tab.shape[0] < 2 or tab.shape[1] < 2:
        return None, None
    chi2 = chi2_contingency(tab)[0]
    n = tab.values.sum()
    phi2 = chi2 / n
    r, k = tab.shape
    # bias 보정 (Bergsma) — 칸이 많고 표본이 적을 때 V 가 부풀려지는 것을 막는다
    phi2c = max(0.0, phi2 - (k - 1) * (r - 1) / (n - 1))
    rc = r - (r - 1) ** 2 / (n - 1)
    kc = k - (k - 1) ** 2 / (n - 1)
    denom = min(kc - 1, rc - 1)
    return (float(np.sqrt(phi2c / denom)) if denom > 0 else None), int(n)


def coverage_table(df):
    feats = sorted({f for fs in REQUIRED.values() for f in fs})
    rows = []
    for f in feats:
        row = {"feature": f}
        for c, g in df.groupby("cohort"):
            row[c] = round(float(g[f].notna().mean()) * 100, 1)
        rows.append(row)
    return pd.DataFrame(rows).set_index("feature")


def cohort_sizes(df, keys):
    g = df.dropna(subset=keys).groupby(keys).size().sort_values(ascending=False)
    return g


def amount_profile(df):
    out = {}
    for (co, at), g in df.groupby(["cohort", "amount_type"]):
        v = g.loc[g["amount_max"].notna() & ~g["amount_outlier"], "amount_max"]
        if len(v) < 5:
            continue
        out["%s/%s" % (co, at)] = {
            "n": int(len(v)),
            "p10": float(v.quantile(0.10)), "p50": float(v.median()),
            "p90": float(v.quantile(0.90)),
            "log10_std": round(float(np.log10(v.clip(lower=1)).std()), 3),
        }
    return out


def won(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    v = float(v)
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            q = v / mult
            return ("%.1f%s" % (q, unit)) if q < 100 else ("%,.0f%s" % (q, unit)).replace(",", ",")
    return "%.0f원" % v


def main():
    df = pd.read_parquet(SRC)
    cov = coverage_table(df)

    # --- 축 독립성 -------------------------------------------------------
    indep = {}
    for c, g in df.groupby("cohort"):
        sub = g.dropna(subset=["support_type", "support_method"])
        v, n = cramers_v(sub["support_type"], sub["support_method"])
        indep[c] = {"cramers_v": round(v, 3) if v is not None else None, "n": n}

    # --- 비교군 크기 -----------------------------------------------------
    sizes = {}
    for c, g in df.groupby("cohort"):
        s2 = cohort_sizes(g, ["support_type", "support_method"])
        s1 = cohort_sizes(g, ["support_type"])
        sizes[c] = {
            "2단(성격x방식)": {"ge30": int((s2 >= MIN_COHORT).sum()),
                            "15_29": int(((s2 >= MIN_REFERENCE) & (s2 < MIN_COHORT)).sum()),
                            "lt15": int((s2 < MIN_REFERENCE).sum()),
                            "covered_rows": int(s2[s2 >= MIN_COHORT].sum()),
                            "total_rows": int(s2.sum())},
            "1단(성격)": {"ge30": int((s1 >= MIN_COHORT).sum()),
                        "covered_rows": int(s1[s1 >= MIN_COHORT].sum()),
                        "total_rows": int(s1.sum())},
        }

    # 모델 3 은 '금액이 있는 행'만 비교 대상이다. 그 기준으로 다시 잰다.
    m3 = df[df["per_recipient"].notna() & ~df["amount_outlier"]]
    m3_sizes = {}
    for c, g in m3.groupby("cohort"):
        s2 = cohort_sizes(g, ["support_type", "support_method"])
        m3_sizes[c] = {"n_rows": int(len(g)), "ge30_cells": int((s2 >= MIN_COHORT).sum()),
                       "covered_rows": int(s2[s2 >= MIN_COHORT].sum()),
                       "top": {"%s/%s" % k: int(v) for k, v in s2.head(12).items()}}
    s2_pool = cohort_sizes(m3, ["support_type", "support_method"])
    m3_sizes["pooled"] = {"n_rows": int(len(m3)),
                          "ge30_cells": int((s2_pool >= MIN_COHORT).sum()),
                          "covered_rows": int(s2_pool[s2_pool >= MIN_COHORT].sum())}

    # --- 모델 4 는 완결 행(결측 없는 다변량)이 있어야 한다 ----------------
    m4_feats = ["per_recipient", "support_count", "support_ratio", "project_duration"]
    complete = {}
    for c, g in df.groupby("cohort"):
        nn = g[m4_feats].notna().sum(axis=1)
        complete[c] = {"4개_전부": int((nn == 4).sum()), "3개_이상": int((nn >= 3).sum()),
                       "2개_이상": int((nn >= 2).sum()), "n": int(len(g))}

    verdict = build_verdict(cov, indep, sizes, m3_sizes, complete)

    # --- 출력 ------------------------------------------------------------
    print("== feature coverage (%, 코호트별)")
    print(cov.to_string())
    print("\n== 지원성격 vs 지원방식 독립성 (Cramer's V)")
    for c, v in indep.items():
        print("  %-10s V=%s (n=%s)" % (c, v["cramers_v"], v["n"]))
    print("\n== 비교군 크기")
    for c, v in sizes.items():
        print("  %-10s 2단 >=30: %d칸 (%d/%d행 포함) | 1단 >=30: %d칸 (%d/%d행)"
              % (c, v["2단(성격x방식)"]["ge30"], v["2단(성격x방식)"]["covered_rows"],
                 v["2단(성격x방식)"]["total_rows"], v["1단(성격)"]["ge30"],
                 v["1단(성격)"]["covered_rows"], v["1단(성격)"]["total_rows"]))
    print("\n== 모델 3 대상(금액 확보 행) 비교군")
    for c, v in m3_sizes.items():
        print("  %-10s %d행, >=30 칸 %d개, 포함 %d행"
              % (c, v["n_rows"], v["ge30_cells"], v.get("covered_rows", 0)))
    print("\n== 모델 4 완결 행 (수치 4축 중 몇 개가 채워졌는가)")
    for c, v in complete.items():
        print("  %-10s 4개 %d / 3개+ %d / 2개+ %d  (전체 %d)"
              % (c, v["4개_전부"], v["3개_이상"], v["2개_이상"], v["n"]))
    print("\n== 판정")
    for m, v in verdict.items():
        print("  %-8s %-12s %s" % (m, v["verdict"], v["reason"]))

    C.save_report("m10_design_coverage.json", {
        "rows": int(len(df)),
        "coverage_pct": cov.to_dict(),
        "axis_independence": indep,
        "cohort_sizes": sizes,
        "model3_cohorts": m3_sizes,
        "model4_complete_rows": complete,
        "amount_profile": amount_profile(df),
        "gates": {"min_cohort": MIN_COHORT, "min_reference": MIN_REFERENCE},
        "verdict": verdict,
    })
    write_md(cov, indep, sizes, m3_sizes, complete, verdict, df)


def build_verdict(cov, indep, sizes, m3_sizes, complete):
    """게이트 통과 여부를 규칙으로 판정한다. 판단을 사람 손에 맡기지 않는다."""
    out = {}

    # 모델 2 — 군집. 요구 feature 의 평균 커버리지와 축 독립성이 관건이다.
    tax_cov = cov["taxonomy"]
    m2_cov = float(tax_cov[REQUIRED["model2"]].mean())
    v = indep.get("taxonomy", {}).get("cramers_v") or 0
    if m2_cov >= 60 and v < 0.7:
        m2 = ("Go", "taxonomy 평균 커버리지 %.0f%%, 지원방식이 지원성격과 독립적(V=%.2f)" % (m2_cov, v))
    elif m2_cov >= 50:
        m2 = ("Conditional",
              "taxonomy 평균 커버리지 %.0f%%. 축 독립성 V=%.2f — 군집이 지원성격 복제로 "
              "귀결되는지 ARI 로 반드시 확인해야 한다" % (m2_cov, v))
    else:
        m2 = ("No-Go", "taxonomy 평균 커버리지 %.0f%% — 군집을 만들 feature 가 부족하다" % m2_cov)
    out["model2"] = {"verdict": m2[0], "reason": m2[1]}

    # 모델 3 — 비교군 percentile. >=30 칸이 덮는 행 비율이 관건이다.
    pooled = m3_sizes.get("pooled", {})
    ratio = (pooled.get("covered_rows", 0) / max(pooled.get("n_rows", 1), 1)) * 100
    if pooled.get("ge30_cells", 0) >= 8 and ratio >= 70:
        m3 = ("Go", "금액 확보 %d행 중 %.0f%%가 30건 이상 비교군에 속한다(%d칸)"
              % (pooled.get("n_rows", 0), ratio, pooled.get("ge30_cells", 0)))
    elif pooled.get("ge30_cells", 0) >= 4:
        m3 = ("Conditional",
              "30건 이상 비교군 %d칸이 %.0f%%만 덮는다 — 나머지는 1단(지원성격)으로 "
              "후퇴하거나 '비교군 부족'을 출력해야 한다"
              % (pooled.get("ge30_cells", 0), ratio))
    else:
        m3 = ("No-Go", "30건 이상 비교군이 %d칸뿐이다" % pooled.get("ge30_cells", 0))
    out["model3"] = {"verdict": m3[0], "reason": m3[1]}

    # 모델 4 — 다변량 이상탐지. 결측이 많으면 '희귀'가 아니라 '미기재'를 탐지한다.
    tot3 = sum(v["3개_이상"] for v in complete.values())
    tot = sum(v["n"] for v in complete.values())
    r = tot3 / max(tot, 1) * 100
    if r >= 50:
        m4 = ("Go", "수치 3축 이상 채워진 행 %d건(%.0f%%)" % (tot3, r))
    elif r >= 20:
        m4 = ("Conditional",
              "수치 3축 이상 채워진 행이 %d건(%.0f%%)뿐이다 — 결측 자체가 이상치로 "
              "잡히지 않도록 결측 지시자를 분리하고 완결 행만 학습해야 한다" % (tot3, r))
    else:
        m4 = ("No-Go", "수치 3축 이상 채워진 행이 %.0f%%뿐이다" % r)
    out["model4"] = {"verdict": m4[0], "reason": m4[1]}
    return out


def write_md(cov, indep, sizes, m3_sizes, complete, verdict, df):
    L = ["# 모델 2~4 feature coverage / 실현가능성", "",
         "> 모델을 짜기 전에 그 모델이 요구하는 feature 가 실제로 몇 %나 있는지 잰다.",
         "> 없으면 없다고 적는다. 통과시키는 것이 목적이 아니다.", "",
         "## 1. feature 커버리지 (%)", ""]
    L.append("| feature | " + " | ".join(cov.columns) + " | 요구 모델 |")
    L.append("|---|" + "---:|" * len(cov.columns) + "---|")
    for f, row in cov.iterrows():
        who = ",".join(m[-1] for m, fs in REQUIRED.items() if f in fs)
        L.append("| %s | %s | %s |" % (f, " | ".join("%.1f" % x for x in row), who))

    L += ["", "## 2. 비교군 축이 정말 2단인가", "",
          "지원방식을 지원성격에서 유도하면 비교군은 1단으로 붕괴한다.",
          "두 축을 독립적으로 뽑은 뒤 연관 강도를 쟀다.", "",
          "| 코호트 | Cramer's V | n | 해석 |", "|---|---:|---:|---|"]
    for c, v in indep.items():
        vv = v["cramers_v"]
        interp = ("독립 축으로 성립" if vv is not None and vv < 0.5 else
                  "부분 중복" if vv is not None and vv < 0.7 else "사실상 같은 축")
        L.append("| %s | %s | %s | %s |" % (c, vv, v["n"], interp))

    L += ["", "## 3. 비교군 크기 (지원성격 × 지원방식)", "",
          "| 코호트 | ≥30 칸 | 15~29 칸 | <15 칸 | ≥30 칸이 덮는 행 |",
          "|---|---:|---:|---:|---|"]
    for c, v in sizes.items():
        a = v["2단(성격x방식)"]
        L.append("| %s | %d | %d | %d | %d / %d (%.0f%%) |"
                 % (c, a["ge30"], a["15_29"], a["lt15"], a["covered_rows"],
                    a["total_rows"], a["covered_rows"] / max(a["total_rows"], 1) * 100))

    L += ["", "## 4. 모델 3 대상 — 금액이 확보된 행만", "",
          "| 코호트 | 금액 확보 행 | ≥30 비교군 칸 | 덮는 행 |", "|---|---:|---:|---|"]
    for c, v in m3_sizes.items():
        L.append("| %s | %d | %d | %d (%.0f%%) |"
                 % (c, v["n_rows"], v["ge30_cells"], v.get("covered_rows", 0),
                    v.get("covered_rows", 0) / max(v["n_rows"], 1) * 100))

    L += ["", "## 5. 모델 4 대상 — 수치 4축(기업당지원액·지원건수·지원비율·사업기간) 완결도", "",
          "결측이 많으면 이상탐지는 '희귀한 설계'가 아니라 '미기재'를 탐지한다.", "",
          "| 코호트 | 4축 전부 | 3축 이상 | 2축 이상 | 전체 |", "|---|---:|---:|---:|---:|"]
    for c, v in complete.items():
        L.append("| %s | %d | %d | %d | %d |"
                 % (c, v["4개_전부"], v["3개_이상"], v["2개_이상"], v["n"]))

    L += ["", "## 6. 판정", "", "| 모델 | 판정 | 근거 |", "|---|---|---|"]
    names = {"model2": "모델 2 설계유형 군집", "model3": "모델 3 지원규모 상대비교",
             "model4": "모델 4 설계 이상탐지"}
    for m, v in verdict.items():
        L.append("| %s | **%s** | %s |" % (names[m], v["verdict"], v["reason"]))

    L += ["", "## 7. 설계서 대비 없는 것", "",
          "설계서가 나열했으나 현 데이터에 존재하지 않아 이번 구현에서 뺀 feature:", "",
          "```text",
          "support_cap        기업당 한도와 총예산이 amount_type 하나로만 갈린다",
          "support_rate       taxonomy 49% / bizinfo 9% — 비교군 축으로 못 쓴다",
          "ministry           bizinfo 목록 표본에는 소관기관이 없다",
          "implementing_agency_type  taxonomy 는 99% 가 central 이라 변별력 0",
          "```", ""]
    p = os.path.join(C.REPORTS, "m10_design_coverage.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
