r"""M58 — Model 3 실험 A: 비교군(cohort) 정교화 (지시서 Part B 4절, 1순위).

Model 3 에서 가장 중요한 것은 **무엇과 비교하느냐**다. 점수는 "비교군
대표설계에서 얼마나 떨어져 있는가"이므로, 비교군이 이질적이면 그 이질성이
그대로 이례성 점수로 둔갑한다.

후보 (지시서 4절. A2 는 현행과 같아 중복이라 만들지 않는다)

    A0  지원성격 x 지원방식 -> 지원성격 -> 전체        (현행 = 기준선)
    A1  지원성격 -> 전체                              (더 굵게)
    A3  + 지원단위                                    (더 잘게)
    A4  + 기관계열                                    (가장 잘게)

결측 필드는 그 단계를 **건너뛴다**
    지원단위는 pool 의 15%, 기관계열은 37%가 비어 있다. 결측을 '미상'이라는
    범주로 묶으면 실체 없는 비교군이 생긴다. 그래서 그 행은 해당 단계를
    쓰지 못하고 상위 단계로 물러난다 — 필드 가용성의 대가가 fallback 비율에
    그대로 나타난다. 이것이 지시서 4절의 "실제 서비스에서 안정적으로 확보
    가능한 필드만" 을 숫자로 확인하는 방법이다.

판정 (지시서 4절 채택 기준 · 9절 필수조건)

    1순위 안정적인 비교군      재표집 Spearman / Top-K 겹침
    2순위 설명 가능한 score    attribution top1 축 유지율
    3순위 synthetic 일관성     4축 단조성
    4순위 exploratory ROC      **보조지표.** 올라도 위가 깨지면 reject

    문턱은 결과를 보기 전에 m3_lab 에 못박혀 있다(KEEP_TOP30 0.918 /
    KEEP_SPEARMAN 0.969 — M44·M48 이 이미 잰 재표집 변동폭).
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m3_lab as L


def run_variant(train, name, ladder, ids, y, holdout_ids):
    """하나의 비교군 정의를 전 항목으로 잰다."""
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    kw = {"ladder": ladder}
    res = L.score_pool(train, train, **kw)
    # 라벨 평가만 hold-out 을 적합에서 뺀다 (M44 와 같은 규약)
    res_ho = L.score_pool(fit, train, **kw)
    return {
        "name": name,
        "ladder": [list(c) for c in ladder],
        "cohort": L.cohort_profile(res),
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

    print("M58 — 실험 A: 비교군 정교화 (지시서 Part B 4절)")
    print("  pool %d행 · 판정은 라벨 없이, ROC 는 참고값(clean %d건 · 양성 %d)"
          % (len(train), len(y), int(y.sum())))
    print("  사전 고정 문턱: Top30 겹침 >= %.3f / Spearman >= %.3f"
          % (L.KEEP_TOP30, L.KEEP_SPEARMAN))

    variants = {}
    for name, ladder in L.LADDERS.items():
        print("\n  [%s] 채점 중..." % name)
        variants[name] = run_variant(train, name, ladder, ids, y, holdout_ids)
    base = variants[L.BASE_LADDER]

    # ---------------------------------------------------------- 1. 비교군 구성
    print("\n== 1. 비교군 구성 — 잘게 쪼갠 대가가 어디에 나타나는가")
    print("  %-20s %7s %7s %7s %9s %8s %9s %8s %8s"
          % ("후보", "비교군수", "중앙n", "하위10%", "얇은비교군", "fallback",
             "범주점유", "퍼짐", "수치퍼짐"))
    for nm, v in variants.items():
        c = v["cohort"]
        print("  %-20s %7d %7d %7d %9d %8d %9.4f %8.3f %8.3f"
              % (nm, c["n_distinct_cohorts"], c["cohort_size_median"],
                 c["cohort_size_p10"], c["n_thin"], c["n_global_fallback"],
                 c["cat_block_share_of_D"], c["within_cohort_spread"],
                 c["within_cohort_spread_num"]))
    print("  * 범주점유 = 차이벡터 중 범주 one-hot 이 차지하는 비율. 높을수록")
    print("    '드문 설계'가 아니라 '비교군이 이질적'인 것을 재고 있다. 다만 범주축으로")
    print("    비교군을 나누면 그 축 기여가 0 이 되므로 일부는 기계적이다 — 그래서")
    print("    범주 분할에 영향받지 않는 수치퍼짐을 같이 낸다.")
    print("\n  단계별 배분")
    for nm, v in variants.items():
        print("  %-20s %s" % (nm, v["cohort"]["level_dist"]))

    # ---------------------------------------------------------- 2. 기준선 대비
    print("\n== 2. 현행 대비 순위가 얼마나 바뀌는가 (라벨 없음)")
    print("  %-20s %10s %8s %8s %8s" % ("후보", "Spearman", "Top10", "Top30", "Top39"))
    for nm, v in variants.items():
        c = L.compare(base["_score"], v["_score"])
        v["vs_base"] = c
        print("  %-20s %10.4f %8.3f %8.3f %8.3f"
              % (nm, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                 c["top39_overlap"]))

    # ------------------------------------------------------ 3. 재표집 안정성
    print("\n== 3. 재표집 안정성 — 이 실험의 1순위 판정 근거")
    print("  %-20s %s" % ("후보", "  ".join("%5s%%" % int(f * 100) for f in L.FRACS)))
    for metric, lbl in (("spearman_mean", "Spearman"), ("top30_mean", "Top30 겹침")):
        print("  -- %s" % lbl)
        for nm, v in variants.items():
            print("  %-20s %s"
                  % (nm, "  ".join("%6.4f" % v["stability"]["frac_%.1f" % f][metric]
                                   for f in L.FRACS)))

    # --------------------------------------------------- 4. synthetic / 의존도
    print("\n== 4. Synthetic 단조성 · feature 의존도 · attribution")
    print("  %-20s %10s %10s %12s %10s %12s"
          % ("후보", "최저단조성", "축귀속평균", "최대축점유", "제거최저ρ", "설명축유지"))
    for nm, v in variants.items():
        s, d, a = v["synthetic"], v["dependency"], v["attribution"]
        print("  %-20s %10.3f %10.3f %12.3f %10.4f %12.3f"
              % (nm, s["min_positive_rate"], s["mean_axis_attribution"],
                 d["max_axis_share"], d["min_ablation_spearman"],
                 a["top1_axis_agreement_mean"]))
    print("\n  축별 단조성 (양의 상관 비율)")
    for nm, v in variants.items():
        print("  %-20s %s"
              % (nm, "  ".join("%s %.2f" % (L.AXIS_KR[k], x["positive_rate"])
                               for k, x in v["synthetic"]["monotonicity"].items())))

    # ------------------------------------------------------- 5. 참고 ROC
    print("\n== 5. Exploratory ROC-AUC / PR-AUC (보조지표 — 단독 채택 근거 아님)")
    print("  %-20s %10s %10s %10s %10s"
          % ("후보", "ROC-AUC", "PR-AUC", "Recall", "Precision"))
    for nm, v in variants.items():
        b = v["labeled"]
        print("  %-20s %10.4f %10.4f %10s %10s"
              % (nm, b["roc_auc"], b["pr_auc"],
                 "-" if b["recall"] is None else "%.4f" % b["recall"],
                 "-" if b["precision"] is None else "%.4f" % b["precision"]))

    # ------------------------------------------------------------ 6. 판정
    #
    # 두 관문을 따로 통과해야 채택이다.
    #   (1) 필수조건 (지시서 9절) — 무엇도 악화되지 않았는가
    #   (2) 실험 목적 (지시서 4절) — 실제로 **더 동질적인 비교군**이 되었는가
    #
    # (1)만 보면 "아무것도 안 나빠졌다"가 곧 "채택"이 된다. 실험 A 는 안정성을
    # 올리는 실험이 아니라 동질성을 올리는 실험이므로, 목적을 못 이룬 변형은
    # 필수조건을 통과해도 채택하지 않는다.
    print("\n== 6. 판정 — 두 관문")
    print("  %-20s %-26s %-26s %s"
          % ("후보", "(1) 필수조건 (9절)", "(2) 실험 목적: 동질성", "최종"))
    bc = base["cohort"]
    for nm, v in variants.items():
        if nm == L.BASE_LADDER:
            v.update({"fails": [], "purpose": [], "verdict": "기준선"})
            print("  %-20s %-26s %-26s %s" % (nm, "-", "-", "기준선"))
            continue
        fails = L.verdict(v["vs_base"], v["stability"], v["synthetic"],
                          v["dependency"], v["attribution"],
                          base["stability"], base["synthetic"],
                          base["dependency"], base["attribution"])
        # 5번 조건 — fallback 비율 비정상 증가
        if v["cohort"]["n_thin"] > bc["n_thin"] * 1.2:
            fails.append("얇은 비교군 %d->%d" % (bc["n_thin"], v["cohort"]["n_thin"]))
        if v["cohort"]["n_global_fallback"] > bc["n_global_fallback"] * 1.2:
            fails.append("전체 fallback %d->%d"
                         % (bc["n_global_fallback"], v["cohort"]["n_global_fallback"]))

        c = v["cohort"]
        gain = []
        if c["cat_block_share_of_D"] < bc["cat_block_share_of_D"] - 0.01:
            gain.append("범주점유 %.3f->%.3f"
                        % (bc["cat_block_share_of_D"], c["cat_block_share_of_D"]))
        if c["within_cohort_spread_num"] < bc["within_cohort_spread_num"] - 0.005:
            gain.append("수치퍼짐 %.3f->%.3f"
                        % (bc["within_cohort_spread_num"], c["within_cohort_spread_num"]))
        v["fails"], v["purpose"] = fails, gain

        if fails:
            v["verdict"] = "REJECT"
        elif not gain:
            v["verdict"] = "미채택 (목적 미달)"
        elif v["cohort"]["n_thin"] > bc["n_thin"]:
            # 필수조건을 통과하고 동질성도 올랐다. 남은 대가가 **얇은 비교군**
            # 하나뿐이라면 여기서 단정하지 않는다 — 그 대가를 회수하는 것이
            # 정확히 실험 C(MIN_COHORT·fallback ladder)의 일이기 때문이다.
            # M48·M50 이 순위 흔들림의 주범으로 지목한 것도 얇은 비교군이다.
            v["verdict"] = "조건부 — 실험 C 로 이월"
            v["fails"].append("얇은 비교군 %d->%d (실험 C 에서 회수 가능한지 확인)"
                              % (bc["n_thin"], v["cohort"]["n_thin"]))
        else:
            v["verdict"] = "채택 후보"
        print("  %-20s %-26s %-26s %s"
              % (nm, (" / ".join(v["fails"]) or "통과")[:26],
                 (" / ".join(gain) or "개선 없음")[:26], v["verdict"]))

    rep = {
        "실험": "A. 비교군 정교화 (지시서 Part B 4절)",
        "기준선": L.BASE_LADDER,
        "결측처리": "키 컬럼이 하나라도 비면 그 단계를 건너뛰고 상위 단계로 물러난다",
        "n_pool": int(len(train)), "n_label_clean": int(len(y)),
        "n_label_positive": int(y.sum()),
        "문턱": {"top30": L.KEEP_TOP30, "spearman": L.KEEP_SPEARMAN,
               "note": "결과를 보기 전에 M44·M48 재표집 변동폭에서 가져와 고정"},
        "variants": {nm: {k: x for k, x in v.items() if not k.startswith("_")}
                     for nm, v in variants.items()},
    }
    C.save_report("m58_m3_cohort_refine.json", rep)
    write_md(rep, variants)


def write_md(r, variants):
    L_ = ["# M58 — 실험 A: 비교군(cohort) 정교화", "",
          "> 지시서 Part B 4절, 실험 우선순위 **1순위**. Model 3 에서 가장 중요한",
          "> 것은 **무엇과 비교하느냐**입니다 — 비교군이 이질적이면 그 이질성이",
          "> 그대로 이례성 점수로 둔갑합니다.", "",
          "```text",
          "pool %d행 · 판정은 사람 라벨 0건" % r["n_pool"],
          "ROC 는 참고값 (clean %d건 · 양성 %d)" % (r["n_label_clean"], r["n_label_positive"]),
          "문턱  Top30 겹침 >= %.3f / Spearman >= %.3f (결과 보기 전 고정)"
          % (r["문턱"]["top30"], r["문턱"]["spearman"]),
          "```", "",
          "## 0. 후보와 결측 처리", "",
          "| 후보 | 비교군 사다리 |", "|---|---|"]
    for nm, v in variants.items():
        L_.append("| %s | %s |"
                  % (nm, " → ".join("×".join(c) for c in v["ladder"]) + " → 전체"))
    L_ += ["",
           "**결측 필드는 그 단계를 건너뜁니다.** `지원단위` 는 pool 의 15%,",
           "`기관계열` 은 37%가 비어 있습니다. 결측을 '미상'이라는 하나의 범주로",
           "묶으면 *단위를 모르는 사업끼리* 라는 실체 없는 비교군이 생깁니다.",
           "그래서 그 행은 해당 단계를 쓰지 못하고 상위 단계로 물러납니다 —",
           "필드 가용성의 대가가 fallback 비율에 그대로 나타나게 하려는 것입니다.", "",
           "## 1. 잘게 쪼갠 대가가 어디에 나타나는가", "",
           "| 후보 | 비교군 수 | 크기 중앙값 | 하위 10% 크기 | 얇은 비교군(n≤30) | 전체 fallback | 범주블록 점유 | 퍼짐 | 수치축 퍼짐 |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        c = v["cohort"]
        L_.append("| %s | %d | %d | %d | %d | %d | %.4f | %.3f | %.3f |"
                  % (nm, c["n_distinct_cohorts"], c["cohort_size_median"],
                     c["cohort_size_p10"], c["n_thin"], c["n_global_fallback"],
                     c["cat_block_share_of_D"], c["within_cohort_spread"],
                     c["within_cohort_spread_num"]))
    L_ += ["",
           "> **범주블록 점유**를 같이 싣는 이유. 비교군을 굵게 잡으면 표본이 커져",
           "> 안정성 지표는 저절로 좋아집니다. 그런데 실험 A 의 목적은 안정성이",
           "> 아니라 **더 동질적인 비교군**입니다. 이 값은 차이벡터 `D` 중 범주",
           "> one-hot(지원방식·금액형태·지원단위)이 차지하는 비율로, 크다는 것은",
           "> \"이 사업은 드문 설계다\"가 아니라 \"이 사업은 융자인데 비교군은",
           "> 보조금이다\" 를 재고 있다는 뜻입니다. 두 축을 같이 보지 않으면",
           "> *전부 한 덩이로 묶으면 제일 안정적* 이라는 결론에 도달합니다.", "",
           "> 다만 이 값의 감소는 **일부 기계적**입니다 — 범주축으로 비교군을",
           "> 나누면 비교군 안에서 그 축이 상수가 되어 기여가 0 이 됩니다.",
           "> 그래서 범주 분할에 영향받지 않는 **수치축 퍼짐**(비교군 내부",
           "> 설계값 4축의 평균 거리)을 같이 싣습니다. 이쪽이 줄어야 진짜로",
           "> 설계가 비슷한 사업끼리 묶인 것입니다."]
    L_ += ["", "단계별 배분", "", "| 후보 | 배분 |", "|---|---|"]
    for nm, v in variants.items():
        L_.append("| %s | `%s` |" % (nm, v["cohort"]["level_dist"]))

    L_ += ["", "## 2. 현행 대비 순위 변화 (라벨 없음)", "",
           "| 후보 | Spearman | Top10 | Top30 | Top39 |", "|---|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        c = v["vs_base"]
        L_.append("| %s | %.4f | %.3f | %.3f | %.3f |"
                  % (nm, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                     c["top39_overlap"]))

    L_ += ["", "## 3. 재표집 안정성 — 1순위 판정 근거", "",
           "대표벡터를 만드는 표본을 줄여 다시 만듭니다. 이 방식에는 난수 초기값이",
           "없어 흔들림의 원인은 표본뿐입니다.", "", "**Spearman 순위상관**", "",
           "| 후보 | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
           "|---|" + "---:|" * len(L.FRACS)]
    for nm, v in variants.items():
        L_.append("| %s | %s |"
                  % (nm, " | ".join("%.4f" % v["stability"]["frac_%.1f" % f]["spearman_mean"]
                                    for f in L.FRACS)))
    L_ += ["", "**Top30 겹침**", "",
           "| 후보 | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
           "|---|" + "---:|" * len(L.FRACS)]
    for nm, v in variants.items():
        L_.append("| %s | %s |"
                  % (nm, " | ".join("%.4f" % v["stability"]["frac_%.1f" % f]["top30_mean"]
                                    for f in L.FRACS)))

    L_ += ["", "## 4. Synthetic 단조성 · feature 의존도 · attribution", "",
           "| 후보 | 최저 단조성 | 축 귀속 평균 | 최대 축 점유율 | 축 제거 최저 ρ | 설명축 유지율 |",
           "|---|---:|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        s, d, a = v["synthetic"], v["dependency"], v["attribution"]
        L_.append("| %s | %.3f | %.3f | %.3f | %.4f | %.3f |"
                  % (nm, s["min_positive_rate"], s["mean_axis_attribution"],
                     d["max_axis_share"], d["min_ablation_spearman"],
                     a["top1_axis_agreement_mean"]))
    L_ += ["",
           "- **최저 단조성** — 4개 설계축 중 가장 나쁜 축의 '멀어질수록 점수가",
           "  오른다' 비율. 1.000 이면 4축 전부 100%입니다.",
           "- **최대 축 점유율** — 상위 39건에서 한 축이 차지하는 평균 기여도.",
           "  높을수록 그 축 하나짜리 모델에 가깝습니다(지시서 8절 dependency).",
           "- **설명축 유지율** — 80% 재표집에서 상위 30건의 최대 기여축이",
           "  그대로인 비율. 점수가 안정적이어도 설명이 매번 바뀌면 담당자에게",
           "  나가는 문장이 흔들립니다.", "",
           "축별 단조성", "", "| 후보 | " + " | ".join(L.AXIS_KR[a] for a in L.NUM) + " |",
           "|---|" + "---:|" * len(L.NUM)]
    for nm, v in variants.items():
        m = v["synthetic"]["monotonicity"]
        L_.append("| %s | %s |"
                  % (nm, " | ".join("%.2f" % m[a]["positive_rate"] for a in L.NUM)))

    L_ += ["", "## 5. Exploratory ROC-AUC / PR-AUC (보조지표)", "",
           "> 양성 5건입니다. 변형 간 우열을 가릴 힘이 없습니다(M44 의 CI 0.575~0.908).",
           "> **이 표만 보고 채택하지 않습니다.**", "",
           "| 후보 | ROC-AUC | PR-AUC | Recall | Precision |",
           "|---|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        b = v["labeled"]
        L_.append("| %s | %.4f | %.4f | %s | %s |"
                  % (nm, b["roc_auc"], b["pr_auc"],
                     "—" if b["recall"] is None else "%.4f" % b["recall"],
                     "—" if b["precision"] is None else "%.4f" % b["precision"]))

    b, a1, a3, a4 = (variants[L.BASE_LADDER], variants["A1 성격"],
                     variants["A3 +지원단위"], variants["A4 +기관계열"])
    L_ += ["", "## 6. 판정 — 두 관문", "",
           "채택하려면 **둘 다** 통과해야 합니다.", "",
           "1. **필수조건(지시서 9절)** — ranking stability 악화 없음 · synthetic",
           "   단조성 유지 · attribution 유지 · feature dependency 악화 없음 ·",
           "   fallback 비정상 증가 없음. ROC-AUC 가 올라도 하나가 깨지면 reject.",
           "2. **실험 목적(지시서 4절)** — 실제로 *더 동질적인 비교군*이 되었는가.",
           "   (1)만 보면 \"아무것도 안 나빠졌다\"가 곧 채택이 됩니다. 실험 A 는",
           "   안정성을 올리는 실험이 아니라 동질성을 올리는 실험입니다.", "",
           "| 후보 | (1) 필수조건 | (2) 동질성 | 최종 판정 |", "|---|---|---|---|"]
    for nm, v in variants.items():
        L_.append("| %s | %s | %s | **%s** |"
                  % (nm, " / ".join(v["fails"]) or "통과",
                     " / ".join(v["purpose"]) or "개선 없음", v["verdict"]))
    L_ += ["", "## 7. 읽은 것", "",
           "**어느 후보도 현행을 대체하지 못합니다 — 서로 반대쪽 대가를 치릅니다.**", "",
           "**A1(굵게)** 은 재표집 안정성이 전 구간에서 가장 좋습니다. 표본이",
           "커졌으니 당연합니다. 그러나 범주블록 점유가 %.3f → %.3f 로 **오르고**"
           % (b["cohort"]["cat_block_share_of_D"], a1["cohort"]["cat_block_share_of_D"]),
           "수치축 퍼짐도 %.3f → %.3f 로 커집니다. 즉 점수가 \"드문 설계\"보다"
           % (b["cohort"]["within_cohort_spread_num"],
              a1["cohort"]["within_cohort_spread_num"]),
           "\"융자인데 보조금 무리에 섞여 있음\" 쪽을 더 재게 됩니다. 실험의 목적과",
           "반대 방향이라 채택하지 않습니다.", "",
           "**A3(+지원단위)** 은 반대입니다. 범주블록 점유가 %.3f → %.3f 로 거의"
           % (b["cohort"]["cat_block_share_of_D"], a3["cohort"]["cat_block_share_of_D"]),
           "절반이 되고 수치축 퍼짐도 %.3f → %.3f 로 줄어 **진짜로 더 동질적**입니다."
           % (b["cohort"]["within_cohort_spread_num"],
              a3["cohort"]["within_cohort_spread_num"]),
           "필수조건도 전부 통과하고 exploratory ROC 도 %.4f → %.4f 로 내려가지"
           % (b["labeled"]["roc_auc"], a3["labeled"]["roc_auc"]),
           "않습니다. **대가는 딱 하나 — 얇은 비교군이 %d → %d 로 늘고**"
           % (b["cohort"]["n_thin"], a3["cohort"]["n_thin"]),
           "**Top30 겹침이 표집비율 80% 이하에서 기준선보다 낮아집니다**",
           "(%s vs 기준선 %s)."
           % (" / ".join("%.3f" % a3["stability"]["frac_%.1f" % f]["top30_mean"]
                         for f in L.FRACS),
              " / ".join("%.3f" % b["stability"]["frac_%.1f" % f]["top30_mean"]
                         for f in L.FRACS)),
           "여기서 단정하지 않고 **실험 C 로 넘깁니다.** 남은 대가가 얇은 비교군",
           "하나뿐인데, 그 대가를 회수하는 것이 정확히 실험 C(`MIN_COHORT`·fallback",
           "사다리)가 하는 일이기 때문입니다. M48·M50 이 순위 흔들림의 주범으로",
           "지목한 것도 같은 얇은 비교군입니다. A3 의 최종 채택 여부는 M60 에서",
           "정합니다.", "",
           "**A4(+기관계열)** 은 exploratory ROC 가 %.4f 로 넷 중 가장 높습니다."
           % a4["labeled"]["roc_auc"],
           "그런데도 reject 입니다 — 재표집 순위상관·Top30 이 모두 악화되고 얇은",
           "비교군이 %d건으로 늡니다. **지시서가 \"ROC 가 올라가도 ranking**"
           % a4["cohort"]["n_thin"],
           "**stability 가 크게 악화되면 reject\" 라고 못박은 자리가 정확히**",
           "**여기입니다.** 기관계열은 pool 의 37%가 결측이라, 정보가 늘어난 것이",
           "아니라 *기관계열을 아는 사업* 과 *모르는 사업* 을 갈라놓은 것에",
           "가깝습니다.", "",
           "**이 실험의 결론**", "", "```text",
           "A1 미채택   안정성은 최고지만 동질성이 나빠진다 — 실험 목적과 반대",
           "A3 조건부   동질성이 실제로 개선된 유일한 후보. 대가는 얇은 비교군뿐이라",
           "            실험 C(MIN_COHORT) 에서 회수 가능한지 보고 M60 에서 확정",
           "A4 REJECT   ROC 는 가장 높으나 안정성·얇은 비교군이 함께 악화",
           "```", "",
           "그리고 어느 경우든 `지원단위` 결측 15%·`기관계열` 결측 37%가 데이터",
           "수집 단계에서 줄어야 이 방향이 온전히 열립니다. 이것은 모델링이 아니라",
           "**데이터 문제**이고, M50 이 얇은 비교군에 대해 내린 결론(\"계산법으로",
           "풀 수 없다, 표본 확대가 해법\")과 같은 자리입니다.", ""]

    p = os.path.join(C.REPORTS, "m58_m3_cohort_refine.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L_))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
