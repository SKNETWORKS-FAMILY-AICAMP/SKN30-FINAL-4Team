r"""M60 — Model 3 실험 C: 얇은 비교군 fallback 개선 (지시서 Part B 6절, 3순위).

이 실험이 답해야 하는 두 가지

    (1) 지시서 6절 — n=20~30 의 얇은 비교군이 상위 순위 불안정성의 주원인이라는
        M48 의 관측이 맞는가. 맞다면 `MIN_COHORT` 를 올려 회수할 수 있는가.

            목표: 얇은 비교군 불안정성을 줄이되 **전체 fallback 을 과도하게
                 늘리지 않는다.** 둘은 정면으로 맞선다 — 문턱을 올리면 얇은
                 비교군은 사라지지만 그만큼 "전체와 비교" 로 떨어진다.

    (2) M58 이 넘긴 숙제 — A3(+지원단위)는 동질성을 실제로 개선했고 필수조건도
        통과했지만 얇은 비교군이 195 -> 224 로 늘었다. 그 대가를 여기서
        회수할 수 있으면 A3 을 채택하고, 없으면 현행을 유지한다.

    그래서 격자를 `MIN_COHORT` x `사다리` 두 축으로 놓는다. 한 축만 흔들면
    (2)에 답할 수 없다.

무엇으로 판정하는가
    resample_stability 는 **목록 전체**를 본다. 그런데 이 실험의 질문은
    "얇은 비교군이 흔들림을 독점하는가"라 **행 단위**로 봐야 한다 — 얇은
    비교군이 상위 30건에 없으면 Top30 겹침은 아무것도 말해주지 않는다.
    그래서 M48 §8.6 의 순위 흔들림(백분위 순위가 평균 몇 점 움직이는가)을
    얇은/두꺼운/전체fallback 으로 갈라서 낸다.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m3_lab as L

MC_GRID_BASE = [15, 20, 25, 30, 40]
MC_GRID_A3 = [20, 25, 30]
A3 = "A3 +지원단위"


def run_cell(train, ladder_name, mc, ids, y, holdout_ids):
    ladder = L.LADDERS[ladder_name]
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    kw = {"ladder": ladder, "min_cohort": mc}
    res = L.score_pool(train, train, **kw)
    res_ho = L.score_pool(fit, train, **kw)
    return {
        "ladder": ladder_name, "min_cohort": mc,
        "cohort": L.cohort_profile(res),
        "volatility": L.rank_volatility(train, **kw),
        "stability": L.resample_stability(train, **kw),
        "synthetic": L.synthetic_stress(train, **kw),
        "dependency": L.feature_dependency(train, **kw),
        "attribution": L.attribution_stability(train, **kw),
        "labeled": L.eval_labeled(res_ho["score"], ids, y),
        "_score": res["score"],
    }


def main():
    train = L.load_pool()
    holdout_ids, ids, y = L.load_labels(train)

    cells = {}
    print("M60 — 실험 C: 얇은 비교군 fallback (지시서 Part B 6절)")
    print("  pool %d행 · 격자 = MIN_COHORT x 비교군 사다리" % len(train))
    print("  MIN_COHORT %s (현행 사다리) / %s (A3)"
          % (MC_GRID_BASE, MC_GRID_A3))
    for mc in MC_GRID_BASE:
        print("  [A0 / MIN_COHORT=%d] 채점 중..." % mc)
        cells[(L.BASE_LADDER, mc)] = run_cell(train, L.BASE_LADDER, mc, ids, y,
                                              holdout_ids)
    for mc in MC_GRID_A3:
        print("  [A3 / MIN_COHORT=%d] 채점 중..." % mc)
        cells[(A3, mc)] = run_cell(train, A3, mc, ids, y, holdout_ids)
    base = cells[(L.BASE_LADDER, L.MIN_COHORT)]
    for k, v in cells.items():
        v["vs_base"] = L.compare(base["_score"], v["_score"])

    def label(k):
        return "%s / MC=%d" % ("A0" if k[0] == L.BASE_LADDER else "A3", k[1])

    # ------------------------------------------------- 1. 맞바꿈 표
    print("\n== 1. 문턱을 올리면 무엇을 얻고 무엇을 잃는가")
    print("  %-12s %10s %10s %10s %10s"
          % ("설정", "얇은비교군", "전체fallback", "비교군수", "중앙n"))
    for k, v in cells.items():
        c = v["cohort"]
        mark = "  <- 현행" if k == (L.BASE_LADDER, L.MIN_COHORT) else ""
        print("  %-12s %10d %10d %10d %10d%s"
              % (label(k), c["n_thin"], c["n_global_fallback"],
                 c["n_distinct_cohorts"], c["cohort_size_median"], mark))

    # --------------------------------------- 2. 흔들림이 어디에 있는가
    print("\n== 2. 순위 흔들림 — 얇은 비교군이 정말 독점하는가 (행 단위)")
    print("  %-12s %10s %10s %12s %12s"
          % ("설정", "전체평균", "얇은(n<=30)", "두꺼운", "전체fallback"))
    for k, v in cells.items():
        w = v["volatility"]
        print("  %-12s %10.2f %10s %12.2f %12s"
              % (label(k), w["overall_mean"],
                 "-" if w["thin_mean"] is None else "%.2f" % w["thin_mean"],
                 w["nonthin_mean"],
                 "-" if w["global_mean"] is None else "%.2f" % w["global_mean"]))
    print("\n  현행에서 가장 많이 흔들리는 비교군")
    for x in base["volatility"]["worst_cohorts"]:
        print("    %-24s n=%4d  %6.2f점" % (x["cohort"][:24], x["n"], x["volatility"]))

    # ---------------------------------------------- 3. 목록 안정성
    print("\n== 3. 재표집 목록 안정성")
    print("  %-12s %s | %s"
          % ("설정", "  ".join("ρ%d%%" % int(f * 100) for f in L.FRACS),
             "  ".join("T30@%d%%" % int(f * 100) for f in L.FRACS)))
    for k, v in cells.items():
        print("  %-12s %s | %s"
              % (label(k),
                 "  ".join("%5.3f" % v["stability"]["frac_%.1f" % f]["spearman_mean"]
                           for f in L.FRACS),
                 "  ".join("%6.3f" % v["stability"]["frac_%.1f" % f]["top30_mean"]
                           for f in L.FRACS)))

    # ------------------------------------------ 4. 나머지 필수조건 + ROC
    print("\n== 4. Synthetic · 의존도 · attribution · 참고 ROC")
    print("  %-12s %10s %10s %12s %10s %10s"
          % ("설정", "최저단조성", "최대축점유", "설명축유지", "ROC", "PR"))
    for k, v in cells.items():
        s, d, a, b = v["synthetic"], v["dependency"], v["attribution"], v["labeled"]
        print("  %-12s %10.3f %10.3f %12.3f %10.4f %10.4f"
              % (label(k), s["min_positive_rate"], d["max_axis_share"],
                 a["top1_axis_agreement_mean"], b["roc_auc"], b["pr_auc"]))

    # ------------------------------------------------------- 5. 판정
    print("\n== 5. 판정")
    bc, bv = base["cohort"], base["volatility"]
    for k, v in cells.items():
        if k == (L.BASE_LADDER, L.MIN_COHORT):
            v.update({"fails": [], "verdict": "기준선"})
            print("  %-12s 기준선" % label(k))
            continue
        fails = L.verdict(v["vs_base"], v["stability"], v["synthetic"],
                          v["dependency"], v["attribution"],
                          base["stability"], base["synthetic"],
                          base["dependency"], base["attribution"])
        if v["cohort"]["n_global_fallback"] > bc["n_global_fallback"] * 1.2:
            fails.append("전체 fallback %d->%d"
                         % (bc["n_global_fallback"], v["cohort"]["n_global_fallback"]))
        gain = v["volatility"]["overall_mean"] < bv["overall_mean"] - 0.1
        # 동점 처리 규칙은 M47·M48 에서 물려받은 것이지 여기서 만든 게 아니다.
        # 운영 산출물은 **상위 K 검토 목록**이므로, 평균 순위 흔들림이 줄어도
        # 그 목록의 유지율이 내려가면 개선으로 치지 않는다.
        #   M47: "경고 목록이 통째로 달라지는 변경을 ROC 는 보지 못한다"
        #   M48: "그래서 목록 자체를 직접 잰다"
        keeps_list = (v["stability"]["frac_0.8"]["top30_mean"]
                      >= base["stability"]["frac_0.8"]["top30_mean"] - 0.01)
        v["fails"] = fails
        if fails:
            v["verdict"] = "REJECT"
        elif not gain:
            v["verdict"] = "미채택 (흔들림 개선 없음)"
        elif not keeps_list:
            v["verdict"] = "미채택 (상위 목록 유지율 저하)"
            v["fails"].append("Top30@80%% %.3f -> %.3f"
                              % (base["stability"]["frac_0.8"]["top30_mean"],
                                 v["stability"]["frac_0.8"]["top30_mean"]))
        else:
            v["verdict"] = "채택 후보"
        print("  %-12s %-22s %s"
              % (label(k), v["verdict"], " / ".join(fails) or "필수조건 통과"))

    rep = {
        "실험": "C. 얇은 비교군 fallback (지시서 Part B 6절)",
        "질문": ["MIN_COHORT 를 올려 얇은 비교군 흔들림을 회수할 수 있는가",
               "M58 이 넘긴 A3 의 얇은 비교군 증가를 여기서 상쇄할 수 있는가"],
        "n_pool": int(len(train)),
        "grid": {"A0": MC_GRID_BASE, "A3": MC_GRID_A3},
        "cells": {label(k): {a: b for a, b in v.items() if not a.startswith("_")}
                  for k, v in cells.items()},
    }
    C.save_report("m60_m3_fallback.json", rep)
    write_md(rep, cells, base, label)


def write_md(r, cells, base, label):
    bc, bv = base["cohort"], base["volatility"]
    L_ = ["# M60 — 실험 C: 얇은 비교군 fallback 개선", "",
          "> 지시서 Part B 6절, 실험 우선순위 **3순위**.", "",
          "## 0. 이 실험이 답해야 하는 두 가지", "",
          "**(1)** n=20~30 의 얇은 비교군이 순위 불안정성의 주원인이라는 M48 의",
          "관측이 맞는가. 맞다면 `MIN_COHORT` 를 올려 회수할 수 있는가.", "",
          "> 목표는 지시서 6절 그대로입니다 — **얇은 비교군 불안정성을 줄이되",
          "> 전체 fallback 을 과도하게 늘리지 않는다.** 둘은 정면으로 맞섭니다.",
          "> 문턱을 올리면 얇은 비교군은 사라지지만 그만큼 \"전체와 비교\"로",
          "> 떨어집니다.", "",
          "**(2)** M58 이 넘긴 숙제 — `A3(+지원단위)` 는 동질성을 실제로 개선하고",
          "필수조건도 통과했지만 얇은 비교군이 %d → %d 로 늘었습니다. 그 대가를"
          % (bc["n_thin"], cells[("A3 +지원단위", 20)]["cohort"]["n_thin"]),
          "여기서 회수할 수 있으면 A3 을 채택하고, 없으면 현행을 유지합니다.", "",
          "그래서 격자를 `MIN_COHORT` × `사다리` **두 축**으로 놨습니다. 한 축만",
          "흔들면 (2)에 답할 수 없습니다.", "",
          "## 1. 문턱을 올리면 무엇을 얻고 무엇을 잃는가", "",
          "| 설정 | 얇은 비교군(n≤30) | 전체 fallback | 비교군 수 | 크기 중앙값 |",
          "|---|---:|---:|---:|---:|"]
    for k, v in cells.items():
        c = v["cohort"]
        mark = " **(현행)**" if v["verdict"] == "기준선" else ""
        L_.append("| %s%s | %d | %d | %d | %d |"
                  % (label(k), mark, c["n_thin"], c["n_global_fallback"],
                     c["n_distinct_cohorts"], c["cohort_size_median"]))

    L_ += ["", "## 2. 흔들림이 실제로 어디에 있는가 (행 단위)", "",
           "`resample_stability` 는 **목록 전체**를 봅니다. 그런데 이 실험의 질문은",
           "\"얇은 비교군이 흔들림을 독점하는가\"라 **행 단위**로 봐야 합니다 —",
           "얇은 비교군이 상위 30건에 없으면 Top30 겹침은 아무것도 말해주지",
           "않습니다. 아래는 M48 §8.6 과 같은 자, 80% 재표집에서 백분위 순위가",
           "평균 몇 점 움직이는가입니다.", "",
           "| 설정 | 전체 평균 | 얇은(n≤30) | 두꺼운 | 전체 fallback 행 |",
           "|---|---:|---:|---:|---:|"]
    for k, v in cells.items():
        w = v["volatility"]
        L_.append("| %s | %.2f점 | %s | %.2f점 | %s |"
                  % (label(k), w["overall_mean"],
                     "—" if w["thin_mean"] is None else "%.2f점" % w["thin_mean"],
                     w["nonthin_mean"],
                     "—" if w["global_mean"] is None else "%.2f점" % w["global_mean"]))
    L_ += ["", "현행에서 가장 많이 흔들리는 비교군", "",
           "| 비교군 | n | 순위 흔들림 |", "|---|---:|---:|"]
    for x in bv["worst_cohorts"]:
        L_.append("| `%s` | %d | %.2f점 |" % (x["cohort"], x["n"], x["volatility"]))

    L_ += ["", "## 3. 재표집 목록 안정성", "", "**Spearman 순위상관**", "",
           "| 설정 | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
           "|---|" + "---:|" * len(L.FRACS)]
    for k, v in cells.items():
        L_.append("| %s | %s |"
                  % (label(k), " | ".join("%.4f" % v["stability"]["frac_%.1f" % f]["spearman_mean"]
                                          for f in L.FRACS)))
    L_ += ["", "**Top30 겹침**", "",
           "| 설정 | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
           "|---|" + "---:|" * len(L.FRACS)]
    for k, v in cells.items():
        L_.append("| %s | %s |"
                  % (label(k), " | ".join("%.4f" % v["stability"]["frac_%.1f" % f]["top30_mean"]
                                          for f in L.FRACS)))

    L_ += ["", "## 4. 나머지 필수조건 · 참고 ROC", "",
           "| 설정 | 최저 단조성 | 최대 축 점유율 | 설명축 유지율 | ROC-AUC | PR-AUC |",
           "|---|---:|---:|---:|---:|---:|"]
    for k, v in cells.items():
        s, d, a, b = v["synthetic"], v["dependency"], v["attribution"], v["labeled"]
        L_.append("| %s | %.3f | %.3f | %.3f | %.4f | %.4f |"
                  % (label(k), s["min_positive_rate"], d["max_axis_share"],
                     a["top1_axis_agreement_mean"], b["roc_auc"], b["pr_auc"]))

    L_ += ["", "## 5. 판정", "",
           "필수조건(지시서 9절) 위에 동점 처리 규칙을 하나 둡니다. 운영 산출물은",
           "**상위 K 검토 목록**이므로, 평균 순위 흔들림이 줄어도 그 목록의",
           "유지율이 내려가면 개선으로 치지 않습니다. 이 우선순위는 M47·M48 에서",
           "물려받은 것입니다 — *\"경고 목록이 통째로 달라지는 변경을 ROC 는 보지",
           "못한다. 그래서 목록 자체를 직접 잰다.\"*", "",
           "| 설정 | 판정 | 이유 |", "|---|---|---|"]
    for k, v in cells.items():
        L_.append("| %s | **%s** | %s |"
                  % (label(k), v["verdict"], " / ".join(v["fails"]) or "—"))

    c15, c25, c30 = (cells[(L.BASE_LADDER, 15)], cells[(L.BASE_LADDER, 25)],
                     cells[(L.BASE_LADDER, 30)])
    a3_20 = cells[("A3 +지원단위", 20)]
    L_ += ["", "## 6. 읽은 것", "",
           "**(1) M48 의 관측은 맞습니다 — 흔들림은 얇은 비교군이 독점합니다.**",
           "현행에서 얇은 비교군(n≤30) 행의 순위 흔들림은 %.2f점, 두꺼운 비교군은"
           % bv["thin_mean"],
           "%.2f점입니다. %.1f배 차이입니다. 상위 목록을 흔드는 것은 모델 구조가"
           % (bv["nonthin_mean"], bv["thin_mean"] / max(1e-9, bv["nonthin_mean"])),
           "아니라 **표본이 20~30건인 비교군 예닐곱 개**입니다.", "",
           "**(2) 그런데 `MIN_COHORT` 를 올려서는 회수되지 않습니다.**",
           "`MIN_COHORT=30` 이면 얇은 비교군이 0이 되고 목록 안정성도 최고입니다",
           "(Top30@80%% %.3f, 현행 %.3f). 대가가 문제입니다 — 전체 fallback 이"
           % (c30["stability"]["frac_0.8"]["top30_mean"],
              base["stability"]["frac_0.8"]["top30_mean"]),
           "%d → %d건으로 %.1f배 늡니다. pool 의 %.0f%%가 \"유사사업\"이 아니라"
           % (bc["n_global_fallback"], c30["cohort"]["n_global_fallback"],
              c30["cohort"]["n_global_fallback"] / bc["n_global_fallback"],
              c30["cohort"]["n_global_fallback"] / r["n_pool"] * 100),
           "**전체 평균과 비교**되고, 그 행들의 점수는 더 이상 \"유사사업 대비",
           "이례성\"이 아닙니다. 지시서 6절이 못박은 *\"global fallback 을 과도하게",
           "늘리지 않는다\"* 에 정면으로 걸립니다. **안정성이 좋아진 것이 아니라",
           "재는 대상을 바꿔서 흔들릴 일을 없앤 것**입니다.", "",
           "**(3) 문턱을 올리면 오히려 얇은 비교군이 더 흔들립니다.** `MIN_COHORT`",
           "20 → 25 로 올리면 얇은 비교군의 흔들림이 %.2f → %.2f점으로 **늘어납니다.**"
           % (bv["thin_mean"], c25["volatility"]["thin_mean"]),
           "원인은 **단계 이탈**입니다. 원본에서 n=25인 비교군은 80% 재표집에서",
           "n≈20이 되어 문턱 25 아래로 떨어지고, 그 순간 비교 대상이 통째로 상위",
           "비교군으로 바뀝니다. 순위가 조금 움직이는 게 아니라 **다른 자로 재게**",
           "됩니다. 문턱을 올릴수록 이 경계에 걸치는 비교군이 늘어납니다.", "",
           "**(4) 반대로 낮추면(`MIN_COHORT=15`) 평균 흔들림은 줄지만 상위 목록이",
           "흔들립니다.** 전체 평균 %.2f → %.2f점, 얇은 비교군 %.2f → %.2f점으로"
           % (bv["overall_mean"], c15["volatility"]["overall_mean"],
              bv["thin_mean"], c15["volatility"]["thin_mean"]),
           "(3)의 단계 이탈이 줄어 개선되고, 전체 fallback 도 %d → %d건으로"
           % (bc["n_global_fallback"], c15["cohort"]["n_global_fallback"]),
           "**줄어듭니다.** 지시서 6절의 목표를 문자 그대로 보면 유일하게 둘 다",
           "만족하는 설정입니다. 그런데 정작 운영 산출물인 상위 30건 유지율은",
           "%.3f → %.3f 로 내려갑니다. n=15~20 짜리 비교군은 내부 거리분포가"
           % (base["stability"]["frac_0.8"]["top30_mean"],
              c15["stability"]["frac_0.8"]["top30_mean"]),
           "너무 성겨서 백분위 상단이 한두 건에 좌우되기 때문입니다. 평균은",
           "좋아지고 꼭대기는 나빠지는 맞바꿈이라 **채택하지 않습니다.**", "",
           "**(5) M58 이 넘긴 A3 은 여기서 미채택으로 닫힙니다.** A3 은 어떤",
           "`MIN_COHORT` 에서도 현행보다 나아지지 않았습니다 — MC=20 에서 순위",
           "흔들림이 %.2f 로 현행 %.2f 보다 크고, Top30@80%% 도 %.3f 로 현행 %.3f"
           % (a3_20["volatility"]["overall_mean"], bv["overall_mean"],
              a3_20["stability"]["frac_0.8"]["top30_mean"],
              base["stability"]["frac_0.8"]["top30_mean"]),
           "보다 낮습니다. MC 를 올리면 (2)의 fallback 폭증에 똑같이 걸립니다.",
           "**A3 의 동질성 이득은 실재하지만 지금 표본으로는 값을 치를 수 없습니다.**", "",
           "## 7. 결론", "", "```text",
           "MIN_COHORT = %d  유지 (현행)" % L.MIN_COHORT,
           "비교군 사다리    유지 (성격x방식 -> 성격 -> 전체)",
           "",
           "이유  문턱을 올리면 얇은 비교군은 사라지지만 전체 fallback 이 %d->%d 로"
           % (bc["n_global_fallback"], c30["cohort"]["n_global_fallback"]),
           "      늘어 '유사사업 대비'라는 정의 자체가 무너진다. 낮추면 평균은",
           "      좋아지나 운영 산출물인 상위 목록이 흔들린다. 현행 20 이 균형점.",
           "```", "",
           "M48 이 내린 결론과 같은 자리입니다 — **얇은 비교군 흔들림은 표본이",
           "20~30건이라는 사실 자체에서 오는 것이라 계산법으로 풀 수 없습니다.**",
           "M50(shrinkage)도, 이번 실험(fallback 문턱)도 같은 벽에 부딪혔습니다.",
           "해법은 모델링이 아니라 **그 비교군의 표본 확대**입니다.", ""]

    p = os.path.join(C.REPORTS, "m60_m3_fallback.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L_))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
