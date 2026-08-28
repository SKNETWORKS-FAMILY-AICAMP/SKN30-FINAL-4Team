r"""M59 — Model 3 실험 B: multi-prototype 대표벡터 (지시서 Part B 5절, 2순위).

가설
    같은 비교군 안에도 정상적인 설계 패턴이 여럿 있을 수 있다.

        소액 · 다수 지원
        고액 · 소수 지원

    평균 하나만 대표로 쓰면 두 정상 패턴 **모두** 중앙에서 멀어져,
    "흔한 설계인데 이례적"으로 잡힌다.

    현행   C = 비교군 평균          점수 = distance(X, C)
    후보   비교군을 2~3 prototype 으로 나누고 **가장 가까운 prototype 까지의
           거리**를 쓴다.

조건 (지시서 5절)
    - 충분히 큰 비교군에만 적용한다. 20건을 3덩이로 쪼개면 덩이당 7건이라
      중심 자체가 표본 잡음이 된다.
    - cluster 수를 사람 라벨 ROC 에 맞춰 최적화하지 않는다. k 는 2와 3만
      보고, 고르는 근거는 ROC 가 아니라 **군집 구조가 실제로 있는가**와
      안정성 지표다.

먼저 물어야 할 것 — 나눌 구조가 있기는 한가
    가설이 맞으려면 비교군 안에 실제로 덩이가 있어야 한다. 없으면 KMeans 는
    연속적인 구름을 임의로 자르고, 그 자른 선은 표본이 바뀔 때마다 움직인다
    (Deep SVDD 가 무너진 방식이다 — DL17).

    그래서 실루엣을 **귀무 대조와 함께** 잰다. 각 축을 따로 섞어 축 간
    결합구조만 없앤 데이터에 같은 KMeans 를 돌린다. 진짜 덩이가 있으면
    실제 실루엣이 귀무보다 뚜렷하게 높아야 한다. 이 대조가 없으면 아무
    구름에서나 나오는 0.3~0.4 를 '구조'로 오독한다.
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m3_lab as L

PROTO_MIN = 100          # 이보다 작은 비교군에는 prototype 을 나누지 않는다
K_GRID = [1, 2, 3]
N_NULL = 5               # 귀무 대조 반복 수


def cluster_structure(train, proto_min=PROTO_MIN, seed=L.SEED):
    """비교군 안에 나눌 덩이가 실제로 있는가 — 실루엣 vs 축 셔플 귀무."""
    res = L.score_pool(train, train)
    Xtr, _, n_num = L.build_vectors_v(train, train)
    key = list(zip(res["level"], res["cohort_key"]))
    rows = []
    rng = np.random.default_rng(seed)
    for gk in sorted(set(key)):
        mask = np.array([k == gk for k in key])
        M = Xtr[mask]
        if len(M) < proto_min:
            continue
        rec = {"cohort": "%s (%s)" % (gk[1], gk[0]), "n": int(len(M))}
        for k in (2, 3):
            lab = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(M)
            real = float(silhouette_score(M, lab)) if len(set(lab)) > 1 else 0.0
            nulls = []
            for _ in range(N_NULL):
                S = np.column_stack([rng.permutation(M[:, j]) for j in range(M.shape[1])])
                lb = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(S)
                nulls.append(float(silhouette_score(S, lb)) if len(set(lb)) > 1 else 0.0)
            rec["sil_k%d" % k] = round(real, 4)
            rec["null_k%d" % k] = round(float(np.mean(nulls)), 4)
            rec["gap_k%d" % k] = round(real - float(np.mean(nulls)), 4)
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("n", ascending=False)


def run_variant(train, k, ids, y, holdout_ids):
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    kw = {"n_proto": k, "proto_min": PROTO_MIN}
    res = L.score_pool(train, train, **kw)
    res_ho = L.score_pool(fit, train, **kw)
    return {
        "k": k, "proto_min": PROTO_MIN,
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

    print("M59 — 실험 B: multi-prototype 대표벡터 (지시서 Part B 5절)")
    print("  pool %d행 · 비교군 정의는 현행(A0) 고정 · prototype 은 n>=%d 에만"
          % (len(train), PROTO_MIN))

    # ------------------------------------------------- 0. 나눌 구조가 있는가
    print("\n== 0. 비교군 안에 실제로 덩이가 있는가 (실루엣 vs 축셔플 귀무)")
    cs = cluster_structure(train)
    print("  %-22s %6s %8s %8s %8s %8s %8s %8s"
          % ("비교군", "n", "sil k2", "귀무 k2", "격차", "sil k3", "귀무 k3", "격차"))
    for _, r in cs.iterrows():
        print("  %-22s %6d %8.3f %8.3f %+8.3f %8.3f %8.3f %+8.3f"
              % (r["cohort"][:22], r["n"], r["sil_k2"], r["null_k2"], r["gap_k2"],
                 r["sil_k3"], r["null_k3"], r["gap_k3"]))
    n_struct = int(((cs["gap_k2"] > 0.05) | (cs["gap_k3"] > 0.05)).sum())
    print("  -> 귀무 대비 실루엣 격차 > 0.05 인 비교군: %d / %d" % (n_struct, len(cs)))

    # ---------------------------------------------------------- 1~5. 변형 비교
    variants = {}
    for k in K_GRID:
        print("\n  [k=%d] 채점 중..." % k)
        variants[k] = run_variant(train, k, ids, y, holdout_ids)
    base = variants[1]

    print("\n== 1. 비교군 구성 · 현행 대비 순위 변화")
    print("  %-8s %10s %8s %8s %10s %10s"
          % ("k", "Spearman", "Top10", "Top30", "얇은비교군", "수치퍼짐"))
    for k, v in variants.items():
        c = L.compare(base["_score"], v["_score"])
        v["vs_base"] = c
        print("  k=%-6d %10.4f %8.3f %8.3f %10d %10.3f"
              % (k, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                 v["cohort"]["n_thin"], v["cohort"]["within_cohort_spread_num"]))

    print("\n== 2. 재표집 안정성")
    for metric, lbl in (("spearman_mean", "Spearman"), ("top30_mean", "Top30 겹침")):
        print("  -- %s   %s" % (lbl, "  ".join("%5d%%" % int(f * 100) for f in L.FRACS)))
        for k, v in variants.items():
            print("     k=%-4d       %s"
                  % (k, "  ".join("%6.4f" % v["stability"]["frac_%.1f" % f][metric]
                                  for f in L.FRACS)))

    print("\n== 3. Synthetic · 의존도 · attribution")
    print("  %-8s %10s %10s %12s %12s"
          % ("k", "최저단조성", "축귀속평균", "최대축점유", "설명축유지"))
    for k, v in variants.items():
        s, d, a = v["synthetic"], v["dependency"], v["attribution"]
        print("  k=%-6d %10.3f %10.3f %12.3f %12.3f"
              % (k, s["min_positive_rate"], s["mean_axis_attribution"],
                 d["max_axis_share"], a["top1_axis_agreement_mean"]))

    print("\n== 4. Exploratory ROC-AUC / PR-AUC (보조지표)")
    for k, v in variants.items():
        b = v["labeled"]
        print("  k=%-6d ROC %.4f  PR %.4f" % (k, b["roc_auc"], b["pr_auc"]))

    print("\n== 5. 판정")
    for k, v in variants.items():
        if k == 1:
            v.update({"fails": [], "verdict": "기준선 (현행 = 평균 하나)"})
            print("  k=%-6d 기준선" % k)
            continue
        fails = L.verdict(v["vs_base"], v["stability"], v["synthetic"],
                          v["dependency"], v["attribution"],
                          base["stability"], base["synthetic"],
                          base["dependency"], base["attribution"])
        if n_struct == 0:
            fails.insert(0, "나눌 군집 구조 자체가 없음 (실루엣이 귀무와 구별 안 됨)")
        v["fails"] = fails
        v["verdict"] = "REJECT" if fails else "채택 후보"
        print("  k=%-6d %-10s %s" % (k, v["verdict"], " / ".join(fails) or "필수조건 통과"))

    rep = {
        "실험": "B. multi-prototype 대표벡터 (지시서 Part B 5절)",
        "비교군정의": "현행 A0 고정 (실험 A 결론)",
        "proto_min": PROTO_MIN,
        "n_pool": int(len(train)),
        "cluster_structure": cs.to_dict("records"),
        "n_cohorts_with_structure": n_struct,
        "n_cohorts_tested": int(len(cs)),
        "variants": {str(k): {a: b for a, b in v.items() if not a.startswith("_")}
                     for k, v in variants.items()},
    }
    C.save_report("m59_m3_prototype.json", rep)
    write_md(rep, variants, cs, n_struct)


def write_md(r, variants, cs, n_struct):
    base = variants[1]
    L_ = ["# M59 — 실험 B: multi-prototype 대표벡터", "",
          "> 지시서 Part B 5절, 실험 우선순위 **2순위**.", "",
          "## 0. 가설", "",
          "같은 비교군 안에도 정상적인 설계 패턴이 여럿 있을 수 있습니다.", "",
          "```text",
          "소액 · 다수 지원",
          "고액 · 소수 지원",
          "```", "",
          "평균 하나만 대표로 쓰면 **두 정상 패턴 모두** 중앙에서 멀어져 \"흔한",
          "설계인데 이례적\"으로 잡힙니다. 후보는 비교군을 2~3 prototype 으로",
          "나누고 가장 가까운 prototype 까지의 거리를 쓰는 것입니다.", "",
          "```text",
          "현행  C = 비교군 평균 하나",
          "후보  KMeans k=2 / k=3 (n >= %d 인 비교군에만) -> 최근접 prototype 거리"
          % r["proto_min"],
          "```", "",
          "## 1. 먼저 물어야 할 것 — 나눌 구조가 있기는 한가", "",
          "가설이 맞으려면 비교군 안에 **실제로 덩이가** 있어야 합니다. 없으면",
          "KMeans 는 연속적인 구름을 임의로 자르고, 그 자른 선은 표본이 바뀔",
          "때마다 움직입니다 — Deep SVDD 가 무너진 방식입니다(DL17, 시드만 바꿔도",
          "상위30 의 62% 교체).", "",
          "그래서 실루엣을 **귀무 대조와 함께** 쟀습니다. 각 축을 따로 섞어 축",
          "간 결합구조만 없앤 데이터에 같은 KMeans 를 돌린 값이 `귀무` 입니다.",
          "이 대조가 없으면 아무 구름에서나 나오는 0.3~0.4 를 '구조'로 읽게",
          "됩니다.", "",
          "| 비교군 | n | 실루엣 k=2 | 귀무 k=2 | 격차 | 실루엣 k=3 | 귀무 k=3 | 격차 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for _, x in cs.iterrows():
        L_.append("| `%s` | %d | %.3f | %.3f | **%+.3f** | %.3f | %.3f | **%+.3f** |"
                  % (x["cohort"], x["n"], x["sil_k2"], x["null_k2"], x["gap_k2"],
                     x["sil_k3"], x["null_k3"], x["gap_k3"]))
    L_ += ["",
           "귀무 대비 실루엣 격차가 0.05 를 넘는 비교군: **%d / %d**"
           % (n_struct, len(cs)), "",
           "## 2. 현행 대비 순위 변화 · 비교군 구성", "",
           "| k | Spearman | Top10 | Top30 | 얇은 비교군 | 수치축 퍼짐 |",
           "|---|---:|---:|---:|---:|---:|"]
    for k, v in variants.items():
        c = v["vs_base"]
        L_.append("| %d | %.4f | %.3f | %.3f | %d | %.3f |"
                  % (k, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                     v["cohort"]["n_thin"], v["cohort"]["within_cohort_spread_num"]))

    L_ += ["", "## 3. 재표집 안정성", ""]
    for metric, lbl in (("spearman_mean", "Spearman 순위상관"), ("top30_mean", "Top30 겹침")):
        L_ += ["**%s**" % lbl, "",
               "| k | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
               "|---|" + "---:|" * len(L.FRACS)]
        for k, v in variants.items():
            L_.append("| %d | %s |"
                      % (k, " | ".join("%.4f" % v["stability"]["frac_%.1f" % f][metric]
                                       for f in L.FRACS)))
        L_.append("")

    L_ += ["## 4. Synthetic · 의존도 · attribution · 참고 ROC", "",
           "| k | 최저 단조성 | 축 귀속 평균 | 최대 축 점유율 | 설명축 유지율 | ROC-AUC | PR-AUC |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for k, v in variants.items():
        s, d, a, b = v["synthetic"], v["dependency"], v["attribution"], v["labeled"]
        L_.append("| %d | %.3f | %.3f | %.3f | %.3f | %.4f | %.4f |"
                  % (k, s["min_positive_rate"], s["mean_axis_attribution"],
                     d["max_axis_share"], a["top1_axis_agreement_mean"],
                     b["roc_auc"], b["pr_auc"]))

    L_ += ["", "## 5. 판정", "", "| k | 판정 | 이유 |", "|---|---|---|"]
    for k, v in variants.items():
        L_.append("| %d | **%s** | %s |" % (k, v["verdict"], " / ".join(v["fails"]) or "—"))

    k2, k3 = variants[2], variants[3]
    best = cs.loc[cs["gap_k3"].idxmax()]
    L_ += ["", "## 6. 읽은 것", "",
           "**(1) 나눌 구조는 있긴 하지만 얇습니다.** 큰 비교군 %d개 중 %d개에서만"
           % (len(cs), n_struct),
           "귀무 대비 실루엣 격차가 0.05 를 넘었고, 그것도 전부 k=3 쪽입니다",
           "(가장 큰 곳이 `%s` %+.3f). k=2 는 여섯 곳 모두 귀무와 사실상"
           % (best["cohort"], best["gap_k3"]),
           "구별되지 않습니다(격차 %+.3f ~ %+.3f). k=2 의 실루엣 절대값이 0.8~0.9"
           % (cs["gap_k2"].min(), cs["gap_k2"].max()),
           "로 높게 나오는 것은 범주 one-hot 축이 깔끔하게 갈리기 때문이고, 같은",
           "값이 귀무에서도 나옵니다 — **귀무 대조를 안 붙였으면 이걸 구조로**",
           "**읽었을 것**입니다.", "",
           "**(2) 단조성은 깨지지 않습니다.** k=2 %.3f / k=3 %.3f 로 현행 %.3f 와"
           % (k2["synthetic"]["min_positive_rate"], k3["synthetic"]["min_positive_rate"],
              base["synthetic"]["min_positive_rate"]),
           "큰 차이가 없습니다. \"멀어질수록 점수가 오른다\"는 성질 자체는 유지되고,",
           "feature dependency 는 오히려 개선됩니다(최대 축 점유율 %.3f → %.3f / %.3f)."
           % (base["dependency"]["max_axis_share"], k2["dependency"]["max_axis_share"],
              k3["dependency"]["max_axis_share"]),
           "즉 가설이 터무니없는 것은 아닙니다.", "",
           "**(3) 무너지는 곳은 재현성입니다.** 재표집 Top30 겹침이 %.4f(현행)에서"
           % base["stability"]["frac_0.8"]["top30_mean"],
           "k=2 %.4f / k=3 %.4f 로, 순위상관도 %.4f → %.4f / %.4f 로 떨어집니다."
           % (k2["stability"]["frac_0.8"]["top30_mean"],
              k3["stability"]["frac_0.8"]["top30_mean"],
              base["stability"]["frac_0.8"]["spearman_mean"],
              k2["stability"]["frac_0.8"]["spearman_mean"],
              k3["stability"]["frac_0.8"]["spearman_mean"]),
           "표집비율 50%%에서는 Top30 이 %.4f 까지 내려갑니다(현행 %.4f)."
           % (k2["stability"]["frac_0.5"]["top30_mean"],
              base["stability"]["frac_0.5"]["top30_mean"]),
           "구조가 얇은 곳에 KMeans 를 돌리면 자르는 선이 표본마다 움직이고, 그",
           "움직임이 그대로 상위 목록의 교체로 나타납니다.", "",
           "**결론: 현행 평균 대표벡터(k=1) 유지.** 이유는 \"가설이 틀렸다\"가",
           "아니라 **\"이 표본으로는 그 가설을 안정적으로 쓸 수 없다\"** 입니다.",
           "M44 가 Deep SVDD 를 뺀 이유와 같은 자리입니다 — 표현력이 아니라",
           "**재현성**이 이 모델의 요구사항입니다. 비교군별 표본이 지금의 몇 배가",
           "되면 k=3 을 (1)에서 구조가 확인된 비교군에 한해 다시 볼 수 있습니다.", ""]

    p = os.path.join(C.REPORTS, "m59_m3_prototype.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L_))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
