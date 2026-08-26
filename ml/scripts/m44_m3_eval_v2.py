r"""M44 — v2 라벨에서 같은 모델을 다시 잰다. 재튜닝 없음.

무엇을 바꿨고 무엇을 안 바꿨나
    바꾼 것    `atypical_design` 의 정의 (M43, 축 개수 중립)
    안 바꾼 것 모델, 비교군 기준, 거리 종류, feature, threshold, 적합 프로토콜,
               평가 대상 53건, `data_error`/`uncertain` 판정

    딱 하나만 바꿨으므로 두 결과의 차이는 **라벨 정의의 효과**다.

무엇을 확인하려는가
    M42 에서 `n_axes` 단독(0.8005)이 모델(0.7399)을 앞질렀다. v2 에서 라벨의
    축 개수 의존이 줄었다면(M43: 라벨 ROC-AUC 0.8005 -> 0.7000), 모델과
    `n_axes` 의 **격차**가 모델 쪽으로 움직여야 한다. 모델 절대값이 오르는
    것은 목표가 아니다 — 라벨이 바뀌었으니 절대값은 비교 대상이 아니다.

    보는 것은 두 가지다.
        1. 모델 - n_axes 격차가 v1 대비 개선됐는가
        2. 축 개수를 고정한 안(층 안 백분위)에서 모델 순위가 유지되는가
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN as CLEAN1
from m41_m3_labelset2 import OUT as HOLDOUT2_V1
from m42_m3_second_eval import evaluate, naxes_control, proxy_signals, topk_recall
from m43_m3_label_rule_v2 import OUT as HOLDOUT2_V2


def gap_ci(y, model_sc, naxes_sc, n=4000, seed=42):
    """모델 - n_axes 격차의 부트스트랩 구간. **같은 리샘플에서 짝지어** 잰다.

    양성이 5건뿐이라 두 값을 따로 재고 눈으로 빼면 안 된다. 같은 표본에서
    동시에 재야 표본 변동이 상쇄되고 격차 자체의 불확실성이 남는다.
    구간이 0 을 품으면 그 격차는 이 표본으로는 없는 것과 구분되지 않는다.
    """
    rng = np.random.default_rng(seed)
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    out = []
    for _ in range(n):
        i = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        yy = y[i]
        if yy.sum() == 0 or yy.sum() == len(yy):
            continue
        out.append(roc_auc_score(yy, model_sc[i]) - roc_auc_score(yy, naxes_sc[i]))
    out = np.array(out)
    return {"gap_mean": round(float(out.mean()), 4),
            "ci95": [round(float(np.percentile(out, 2.5)), 4),
                     round(float(np.percentile(out, 97.5)), 4)],
            "p_gap_gt_0": round(float((out > 0).mean()), 4)}


def load(path, col):
    d = pd.read_csv(path, encoding="utf-8-sig")
    return d[["row_id", col]].rename(columns={col: "라벨"})


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)

    l1 = load(CLEAN1, "라벨")
    v1 = load(HOLDOUT2_V1, "라벨")
    v2 = load(HOLDOUT2_V2, "v2_라벨")

    print("M44 — v2 라벨에서 재평가 (모델·프로토콜 그대로)")
    print("  바꾼 것: atypical_design 의 정의 하나뿐\n")

    r1, m1, y1, s1, _ = evaluate("1차 hold-out (M33)", train, l1, "first")
    rv1, mv1, yv1, sv1, _ = evaluate("2차 · 라벨 v1", train, v1, "second_v1")
    rv2, mv2, yv2, sv2, _ = evaluate("2차 · 라벨 v2", train, v2, "second_v2")

    px_v1 = proxy_signals(mv1, yv1, train)
    px_v2 = proxy_signals(mv2, yv2, train)
    nc_v1 = naxes_control(mv1, yv1, sv1, train)
    nc_v2 = naxes_control(mv2, yv2, sv2, train)

    gap_v1 = rv1["roc_auc"] - px_v1["n_axes 단독"]
    gap_v2 = rv2["roc_auc"] - px_v2["n_axes 단독"]

    print("== 1. 모델 vs n_axes 단독 — 이것이 핵심 비교")
    print("  %-14s %10s %10s %10s %8s %6s" % ("라벨", "모델", "n_axes단독", "격차", "Clean N", "양성"))
    for nm, r, px, g in (("v1 (M41)", rv1, px_v1, gap_v1), ("v2 (M43)", rv2, px_v2, gap_v2)):
        print("  %-14s %10.4f %10.4f %+10.4f %8d %6d"
              % (nm, r["roc_auc"], px["n_axes 단독"], g, r["n_clean"], r["n_positive"]))
    print("  -> 격차 변화 %+.4f (모델 쪽으로 %s)"
          % (gap_v2 - gap_v1, "개선" if gap_v2 > gap_v1 else "악화"))

    ti = train.set_index("row_id")
    ax_v1 = ti.loc[mv1["row_id"], "n_axes"].to_numpy(float)
    ax_v2 = ti.loc[mv2["row_id"], "n_axes"].to_numpy(float)
    g1 = gap_ci(yv1, sv1, ax_v1)
    g2 = gap_ci(yv2, sv2, ax_v2)
    print("\n== 1-1. 격차의 부트스트랩 구간 (같은 리샘플에서 짝지어)")
    for nm, g in (("v1 (M41)", g1), ("v2 (M43)", g2)):
        print("  %-14s 격차 %+.4f  95%% [%+.4f, %+.4f]  P(격차>0)=%.3f"
              % (nm, g["gap_mean"], g["ci95"][0], g["ci95"][1], g["p_gap_gt_0"]))

    print("\n== 2. 축 개수를 고정한 안에서의 순위 품질")
    print("  %-14s %14s %14s" % ("라벨", "층안 백분위", "층 가중평균"))
    for nm, nc in (("v1 (M41)", nc_v1), ("v2 (M43)", nc_v2)):
        print("  %-14s %14.4f %14s"
              % (nm, nc["within_stratum_pct_roc_auc"], nc["pooled_within_roc_auc"]))

    print("\n== 3. 축별 양성률 — 라벨이 실제로 평평해졌는가")
    for nm, nc in (("v1", nc_v1), ("v2", nc_v2)):
        d = nc["positive_rate_by_n_axes"]
        print("  %-4s %s" % (nm, " / ".join(
            "축%d %.1f%%(%d/%d)" % (k, v["positive_rate"] * 100, v["n_positive"], v["n"])
            for k, v in sorted(d.items()))))

    print("\n== 4. 전체 성능표 (참고 — 라벨이 다르므로 절대값 비교는 무의미)")
    print("  %-22s %8s %5s %9s %-16s %8s" % ("평가셋", "Clean N", "양성", "ROC-AUC", "95% CI", "PR-AUC"))
    for r in (r1, rv1, rv2):
        print("  %-22s %8d %5d %9.4f %-16s %8.4f"
              % (r["name"], r["n_clean"], r["n_positive"], r["roc_auc"],
                 "%.3f ~ %.3f" % tuple(r["roc_ci95"]), r["pr_auc"]))
        print("  %-22s   경고예산 상위%d: recall %s / precision %s / 순열 p %.4f"
              % ("", r["budget"]["top_k"], r["budget"]["recall"],
                 r["budget"]["precision"], r["permutation"]["p_value"]))

    tk = {"v1": topk_recall(yv1, sv1, [3, 5, 7, 10, 15]),
          "v2": topk_recall(yv2, sv2, [3, 5, 7, 10, 15])}
    print("\n== 5. Top-k recall")
    for k, v in tk.items():
        print("  %-4s %s" % (k, v))

    rep = {
        "목적": "라벨 v2(축 개수 중립)에서 같은 모델을 재평가하고 n_axes 편향 감소를 확인",
        "changed": "atypical_design 의 정의만",
        "unchanged": ["모델", "비교군 기준", "거리 종류", "feature", "threshold",
                      "적합 프로토콜", "평가 대상 53건", "data_error/uncertain"],
        "sets": {"first_v1": r1, "second_v1": rv1, "second_v2": rv2},
        "model_vs_naxes": {
            "v1": {"model": rv1["roc_auc"], "n_axes": px_v1["n_axes 단독"],
                   "gap": round(gap_v1, 4), "n_positive": rv1["n_positive"]},
            "v2": {"model": rv2["roc_auc"], "n_axes": px_v2["n_axes 단독"],
                   "gap": round(gap_v2, 4), "n_positive": rv2["n_positive"]},
            "gap_change": round(gap_v2 - gap_v1, 4),
            "gap_bootstrap": {"v1": g1, "v2": g2}},
        "proxy_signals": {"v1": px_v1, "v2": px_v2},
        "naxes_control": {"v1": nc_v1, "v2": nc_v2},
        "topk_recall": tk,
    }
    C.save_report("m44_m3_eval_v2.json", rep)
    write_md(rep)


def write_md(r):
    mv = r["model_vs_naxes"]
    r1, rv1, rv2 = r["sets"]["first_v1"], r["sets"]["second_v1"], r["sets"]["second_v2"]
    nc1, nc2 = r["naxes_control"]["v1"], r["naxes_control"]["v2"]
    rate1 = [v["positive_rate"] for v in nc1["positive_rate_by_n_axes"].values()]
    rate2 = [v["positive_rate"] for v in nc2["positive_rate_by_n_axes"].values()]
    spread1, spread2 = max(rate1) - min(rate1), max(rate2) - min(rate2)
    L = ["# M44 — v2 라벨에서 재평가: 라벨 편향이 줄었는가", "",
         "> **모델은 건드리지 않았습니다.** 비교군 기준·거리 종류·feature·threshold·",
         "> 적합 프로토콜·평가 대상 53건이 M42 와 같습니다. 바꾼 것은",
         "> `atypical_design` 의 정의 하나뿐입니다(M43).", "",
         "## 1. 핵심 — 모델 vs `n_axes` 단독", "",
         "M42 의 문제는 모델 점수가 낮다는 것이 아니라 **`n_axes` 단독이 모델을",
         "앞질렀다**는 것이었습니다. 라벨의 축 개수 의존을 줄였으면 그 격차가",
         "모델 쪽으로 움직여야 합니다.", "",
         "| 라벨 | Clean N | 양성 | 모델 | `n_axes` 단독 | 격차 |",
         "|---|---:|---:|---:|---:|---:|"]
    for nm, k in (("v1 (M41 — 세는 규칙)", "v1"), ("v2 (M43 — 축 중립)", "v2")):
        v = mv[k]
        L.append("| %s | %d | %d | %.4f | %.4f | **%+.4f** |"
                 % (nm, rv1["n_clean"], v["n_positive"], v["model"], v["n_axes"], v["gap"]))
    L += ["", "```text",
          "격차 변화  %+.4f  (%s)" % (mv["gap_change"],
                                  "모델 쪽으로 개선" if mv["gap_change"] > 0 else "악화"),
          "```", "",
          "### 격차의 불확실성 — 양성이 5건이라 반드시 같이 봅니다", "",
          "두 값을 따로 재고 눈으로 빼면 안 됩니다. **같은 리샘플에서 짝지어**",
          "재야 표본 변동이 상쇄되고 격차 자체의 불확실성이 남습니다.", "",
          "| 라벨 | 격차 평균 | 95% 구간 | P(격차>0) |", "|---|---:|---|---:|"]
    for nm, k in (("v1", "v1"), ("v2", "v2")):
        g = mv["gap_bootstrap"][k]
        L.append("| %s | %+.4f | %+.4f ~ %+.4f | %.3f |"
                 % (nm, g["gap_mean"], g["ci95"][0], g["ci95"][1], g["p_gap_gt_0"]))
    L += ["",
          "> **두 구간 모두 0 을 품습니다.** v1 에서 모델이 `n_axes` 에 졌다는 것도,",
          "> v2 에서 이겼다는 것도 이 표본(양성 9건 -> 5건)으로는 확정할 수 없습니다.",
          "> 격차의 부호가 바뀐 것은 방향으로만 읽어야 합니다.", "",
          "그러면 무엇이 움직인 것인가 — **라벨 쪽입니다.** `n_axes` 단독 ROC-AUC 는",
          "곧 \"`n_axes` 만으로 라벨을 얼마나 맞히는가\"이고, 아래 양성률 폭은 이",
          "53건에 대한 서술 통계라 추론이 끼지 않습니다.", "",
          "| | v1 | v2 |", "|---|---:|---:|",
          "| `n_axes` 만으로 라벨을 맞히는 ROC-AUC | %.4f | **%.4f** |"
          % (r["proxy_signals"]["v1"]["n_axes 단독"], r["proxy_signals"]["v2"]["n_axes 단독"]),
          "| 축별 양성률 폭 (서술 통계) | %.3f | **%.3f** |" % (spread1, spread2), "",
          "> 모델 ROC-AUC 의 **절대값**은 v1 과 v2 사이에서 비교하면 안 됩니다.",
          "> 정답이 달라졌기 때문입니다. 비교 대상은 같은 라벨 위에서 잰",
          "> **모델과 `n_axes` 의 격차**입니다.", "",
          "## 2. 축 개수를 고정한 안에서", "",
          "| 라벨 | 층 안 백분위 ROC-AUC | 층 가중평균 |", "|---|---:|---:|",
          "| v1 | %.4f | %s |" % (nc1["within_stratum_pct_roc_auc"], nc1["pooled_within_roc_auc"]),
          "| v2 | %.4f | %s |" % (nc2["within_stratum_pct_roc_auc"], nc2["pooled_within_roc_auc"]),
          "", "## 3. 축별 양성률 — 라벨이 평평해졌는가", "",
          "| 축 개수 | n | v1 양성률 | v2 양성률 |", "|---|---:|---:|---:|"]
    d1, d2 = nc1["positive_rate_by_n_axes"], nc2["positive_rate_by_n_axes"]
    for k in sorted(d1):
        L.append("| 축%s | %d | %.1f%% (%d) | %.1f%% (%d) |"
                 % (k, d1[k]["n"], d1[k]["positive_rate"] * 100, d1[k]["n_positive"],
                    d2[k]["positive_rate"] * 100, d2[k]["n_positive"]))
    L += ["", "## 4. 전체 성능표", "",
          "> 라벨 정의가 다르므로 v1 과 v2 의 ROC-AUC 를 직접 비교하지 마십시오.", "",
          "| 평가셋 | Clean N | 양성 | ROC-AUC | 95% CI | PR-AUC | 순열 p |",
          "|---|---:|---:|---:|---|---:|---:|"]
    for x in (r1, rv1, rv2):
        L.append("| %s | %d | %d | %.4f | %.3f ~ %.3f | %.4f | %.4f |"
                 % (x["name"], x["n_clean"], x["n_positive"], x["roc_auc"],
                    x["roc_ci95"][0], x["roc_ci95"][1], x["pr_auc"],
                    x["permutation"]["p_value"]))
    L += ["", "| 평가셋 | 경고 예산 | recall | precision |", "|---|---:|---:|---:|"]
    for x in (rv1, rv2):
        b = x["budget"]
        L.append("| %s | 상위 %d | %s | %s |" % (x["name"], b["top_k"], b["recall"], b["precision"]))
    L += ["", "## 5. Top-k recall", "",
          "| 라벨 | " + " | ".join(sorted(r["topk_recall"]["v2"], key=lambda s: int(s[3:]))) + " |",
          "|---|" + "---:|" * len(r["topk_recall"]["v2"])]
    keys = sorted(r["topk_recall"]["v2"], key=lambda s: int(s[3:]))
    for k in ("v1", "v2"):
        L.append("| %s | %s |" % (k, " | ".join("%.4f" % r["topk_recall"][k][x] for x in keys)))
    L += ["", "## 6. 다른 대리 신호", "", "| 신호 | v1 | v2 |", "|---|---:|---:|"]
    for k in r["proxy_signals"]["v2"]:
        L.append("| %s | %.4f | %.4f |"
                 % (k, r["proxy_signals"]["v1"][k], r["proxy_signals"]["v2"][k]))
    L += ["", "## 7. 한계 — 먼저 적습니다", "",
          "- **양성이 %d건입니다.** v1 의 %d건에서 더 줄었습니다. 부트스트랩 구간이"
          % (rv2["n_positive"], rv1["n_positive"]),
          "  그만큼 넓어지고, 순열 검정도 힘이 약해집니다. 격차 비교는 읽을 수",
          "  있지만 모델 성능의 점추정을 이 표본으로 확정할 수는 없습니다.",
          "- v2 라벨을 붙인 라벨러는 v1 라벨과 M42 결과를 이미 읽은 상태입니다.",
          "  기계 후보 판정을 코드로 먼저 확정하고 사람이 움직인 행을 전부",
          "  공개한 것이 그 대용입니다(M43 §4). 사람 개입은 **하향 4건뿐**이고",
          "  상향은 0건입니다.",
          "- 축2 층의 양성률이 여전히 낮습니다. 이 층에서 극단적인 행들이",
          "  `data_error` 로 빠졌기 때문이고(축2 30건 중 data_error 11건),",
          "  규칙이 아니라 표본의 성질입니다.", ""]
    p = os.path.join(C.REPORTS, "m44_m3_eval_v2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
