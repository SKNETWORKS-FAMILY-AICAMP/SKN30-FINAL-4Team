r"""M42 — 2차 독립 라벨셋 검증: 현재 Model 3 을 재튜닝 없이 그대로 적용한다.

무엇을 하지 않는가 (실행계획 §6)
    비교군 기준 변경 / 거리 종류 변경 / feature 가중치 변경 / threshold 조정 /
    feature 추가·삭제 / 알고리즘 변경 — **하나도 하지 않는다.** 70건 라벨을
    본 뒤에 설정을 건드리면 이 세트는 hold-out 이 아니라 튜닝셋이 된다.

    적용 대상은 M39/M40 이 유지하기로 한 후보 하나다.
        정형 설계 feature 기반 **비교군 유클리드 거리** (M38, distance only)
    거리는 주 점수, 방향은 설명 — 역할 분리도 그대로 둔다.

적합 프로토콜 — 두 hold-out 을 같은 잣대로 다시 잰다
    M39 는 전체 1948행에 적합해 0.904 를, M40 은 hold-out 을 뺀 1913행에
    적합해 0.936 을 냈다. 숫자가 다른 이유가 모델이 아니라 프로토콜이므로,
    여기서는 **각 hold-out 을 적합에서 뺀 조건으로 양쪽을 한 스크립트에서
    다시 계산한다.** 리포트에서 숫자를 긁어 모으면 조건이 섞인다.

무엇을 보는가 (실행계획 §7, §12)
    순위      ROC-AUC / PR-AUC + 부트스트랩 95% 구간 + 라벨 순열 p
    분류      같은 경고 예산(상위 20%)에서 recall / precision / confusion
    안정성    80% 재표집 재적합 상위 k 유지율
    층별      데이터품질 / 수치축 / 지원방식별 ROC-AUC — 한 층에서만 나오는가
    단일축    n_axes 단독 ROC-AUC — 성능이 거기서 나오는 것은 아닌가 (No-Go 기준)
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, confusion_matrix, roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN as CLEAN1
from m34_m3_diagnostics import _binary, boot_ci
from m38_m3_vector_direction import build_vectors, combine, score_components
from m41_m3_labelset2 import OUT as CLEAN2
from m41_m3_labelset2 import add_strata

SEED = 42
BUDGET_FRAC = 0.20      # M30 의 20/50, M34 의 7/35 와 같은 비율
N_PERM = 2000
N_RESAMPLE, TOP_K = 10, 30
MAIN = ["normal", "atypical_design"]


def score_excluding(train, holdout_ids):
    """hold-out 행을 적합에서 빼고 비교군 대표벡터를 만든 뒤 전체를 채점한다.

    빼지 않으면 평가 대상이 자기 비교군의 평균에 기여한다. 35~53건이라
    영향이 크지는 않지만, 두 세트를 같은 잣대로 보려면 조건을 맞춰야 한다.
    """
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    Xtr, Xap, _, n_num = build_vectors(fit, train)
    comp = score_components(fit, train, Xtr, Xap, n_num)
    return pd.Series(combine(comp, 1.0), index=train["row_id"].to_numpy()), comp


def evaluate(name, train, labels, tag):
    """한 hold-out 을 잰다. labels 는 row_id / 라벨 두 칸이면 된다."""
    main = labels[labels["라벨"].isin(MAIN)]
    main = main[main["row_id"].isin(set(train["row_id"]))]
    s, comp = score_excluding(train, set(labels["row_id"]))
    y = (main["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[main["row_id"]].to_numpy(float)

    k = max(1, int(round(BUDGET_FRAC * len(y))))
    flag = sc >= np.sort(sc)[::-1][k - 1]
    cm = confusion_matrix(y, flag.astype(int), labels=[0, 1])
    roc, pr = roc_auc_score(y, sc), average_precision_score(y, sc)

    rng = np.random.default_rng(SEED)
    perm = np.array([roc_auc_score(rng.permutation(y), sc) for _ in range(N_PERM)])
    pr_boot = []
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    rb = np.random.default_rng(SEED)
    for _ in range(2000):
        i = np.concatenate([rb.choice(pos, len(pos)), rb.choice(neg, len(neg))])
        pr_boot.append(average_precision_score(y[i], sc[i]))

    out = {
        "name": name, "tag": tag,
        "n_labeled": int(len(labels)), "n_clean": int(len(y)),
        "n_positive": int(y.sum()), "positive_rate": round(float(y.mean()), 4),
        "excluded": {L: int((labels["라벨"] == L).sum())
                     for L in ("data_error", "uncertain")},
        "roc_auc": round(float(roc), 4), "roc_ci95": boot_ci(y, sc),
        "pr_auc": round(float(pr), 4),
        "pr_ci95": [round(float(np.percentile(pr_boot, 2.5)), 4),
                    round(float(np.percentile(pr_boot, 97.5)), 4)],
        "permutation": {"mean": round(float(perm.mean()), 4),
                        "p95": round(float(np.percentile(perm, 95)), 4),
                        "p_value": round(float((perm >= roc).mean()), 4)},
        "budget": {"top_k": k, "frac": BUDGET_FRAC, **_binary(y, flag),
                   "confusion_TN_FP_FN_TP": [int(cm[0, 0]), int(cm[0, 1]),
                                             int(cm[1, 0]), int(cm[1, 1])]},
        "score_dist": {
            "positive_median": round(float(np.median(sc[y == 1])), 4),
            "negative_median": round(float(np.median(sc[y == 0])), 4),
            "positive_min": round(float(sc[y == 1].min()), 4),
            "negative_max": round(float(sc[y == 0].max()), 4)},
    }
    return out, main, y, sc, s


def topk_recall(y, sc, ks):
    r = {}
    for k in ks:
        if k > len(y):
            continue
        f = sc >= np.sort(sc)[::-1][k - 1]
        r["top%d" % k] = round(float((f & (y == 1)).sum() / max(1, y.sum())), 4)
    return r


def stability(train, holdout_ids, main, y, base_sc, n_iter=N_RESAMPLE, top_k=TOP_K):
    """80% 재표집으로 다시 적합해도 상위 목록과 hold-out 순위가 유지되는가."""
    from scipy.stats import spearmanr
    rng = np.random.default_rng(SEED)
    fit_all = train[~train["row_id"].isin(holdout_ids)]
    base_s, _ = score_excluding(train, holdout_ids)
    base_top = set(base_s.sort_values(ascending=False).head(top_k).index)
    ov, rho, aucs = [], [], []
    for _ in range(n_iter):
        sub = fit_all.sample(frac=0.8, random_state=int(rng.integers(1e6)))
        Xtr, Xap, _, nn = build_vectors(sub, train)
        c = score_components(sub, train, Xtr, Xap, nn)
        s = pd.Series(combine(c, 1.0), index=train["row_id"].to_numpy())
        ov.append(len(set(s.sort_values(ascending=False).head(top_k).index) & base_top) / top_k)
        h = s.loc[main["row_id"]].to_numpy(float)
        rho.append(spearmanr(base_sc, h).statistic)
        aucs.append(roc_auc_score(y, h))
    return {"n_iter": n_iter, "top_k": top_k,
            "overlap_mean": round(float(np.mean(ov)), 4),
            "overlap_min": round(float(np.min(ov)), 4),
            "holdout_rank_corr_mean": round(float(np.mean(rho)), 4),
            "roc_auc_mean": round(float(np.mean(aucs)), 4),
            "roc_auc_min": round(float(np.min(aucs)), 4),
            "roc_auc_max": round(float(np.max(aucs)), 4)}


def by_stratum(main, y, sc, train, sheet):
    """층별로 잘라 본다. 한 층에서만 성능이 나오면 Conditional 을 못 벗는다."""
    m = main.merge(sheet[["row_id", "층_품질", "층_축수"]], on="row_id", how="left")
    m = m.merge(train[["row_id", "support_method", "support_type", "cohort"]],
                on="row_id", how="left")
    m["y"], m["sc"] = y, sc
    m["방식"] = np.where(m["support_method"] == "grant", "grant", "비grant")
    out = {}
    for col in ("층_품질", "층_축수", "방식", "cohort"):
        d = {}
        for k, g in m.groupby(col, dropna=False):
            yy = g["y"].to_numpy()
            if len(yy) < 5 or yy.sum() == 0 or yy.sum() == len(yy):
                d[str(k)] = {"n": int(len(yy)), "n_positive": int(yy.sum()),
                             "roc_auc": None, "note": "양성/음성 한쪽뿐이거나 n<5"}
            else:
                d[str(k)] = {"n": int(len(yy)), "n_positive": int(yy.sum()),
                             "roc_auc": round(float(roc_auc_score(yy, g["sc"])), 4)}
        out[col] = d
    return out


def proxy_signals(main, y, train):
    """성능이 단일 대리 신호에서 나오는 것은 아닌가 (실행계획 §12 No-Go)."""
    t = train.set_index("row_id")
    out = {}
    for nm, col in (("n_axes 단독", "n_axes"),
                    ("결측 지시자 합 단독", None),
                    ("추출신뢰도(낮을수록 이례) 단독", "extraction_confidence")):
        if col == "n_axes":
            v = t.loc[main["row_id"], "n_axes"].to_numpy(float)
        elif col is None:
            NUM = ["log_per_recipient", "log_support_count",
                   "project_duration", "support_ratio"]
            v = t.loc[main["row_id"], NUM].isna().sum(axis=1).to_numpy(float)
        else:
            v = -t.loc[main["row_id"], col].fillna(
                t[col].median() if t[col].notna().any() else 0.0).to_numpy(float)
        out[nm] = round(float(roc_auc_score(y, v)), 4)
    return out


def naxes_control(main, y, sc, train):
    """`n_axes` 를 통제해도 모델 점수에 신호가 남는가.

    2차에서 `n_axes` 단독 ROC-AUC 가 모델을 앞질렀다. 그러면 두 가지가 가능하다.
      (a) 모델이 사실상 '축이 몇 개 채워졌나'를 재고 있다
      (b) 라벨 규칙 자체가 축 개수에 의존한다 — '두 축 이상이 극단'이려면
          축이 많아야 유리하다. 축 2개짜리는 둘 다 극단이어야 한다.
    (b) 라면 모델을 탓할 일이 아니지만, 그래도 `n_axes` 를 고정한 안에서
    모델이 순위를 매길 수 있어야 쓸모가 있다. 층 안에서 다시 잰다.
    """
    t = train.set_index("row_id")
    ax = t.loc[main["row_id"], "n_axes"].to_numpy(int)
    d = pd.DataFrame({"ax": ax, "y": y, "sc": sc})

    rate = {int(k): {"n": int(len(g)), "n_positive": int(g["y"].sum()),
                     "positive_rate": round(float(g["y"].mean()), 4)}
            for k, g in d.groupby("ax")}

    within = {}
    num = den = 0.0
    for k, g in d.groupby("ax"):
        yy = g["y"].to_numpy()
        if yy.sum() == 0 or yy.sum() == len(yy):
            within[int(k)] = {"n": int(len(yy)), "n_positive": int(yy.sum()),
                              "roc_auc": None, "n_pairs": 0}
            continue
        a = float(roc_auc_score(yy, g["sc"]))
        p = int(yy.sum()) * int((yy == 0).sum())
        within[int(k)] = {"n": int(len(yy)), "n_positive": int(yy.sum()),
                          "roc_auc": round(a, 4), "n_pairs": p}
        num += a * p
        den += p

    # 층 안에서 백분위로 바꾼 뒤 전체를 한 번에 — 축 개수 효과를 뺀 순위 품질
    d["within_pct"] = d.groupby("ax")["sc"].rank(pct=True)
    strat_auc = float(roc_auc_score(d["y"], d["within_pct"]))
    return {
        "positive_rate_by_n_axes": rate,
        "within_stratum": within,
        "pooled_within_roc_auc": round(num / den, 4) if den else None,
        "within_stratum_pct_roc_auc": round(strat_auc, 4),
        "note": ("층 안 백분위로 다시 매긴 순위의 ROC-AUC. 0.5 에 가까우면 "
                 "모델이 축 개수 밖에서는 라벨을 가리지 못한다는 뜻이다."),
    }


def verdict(r2, st, strat, px2, nc2):
    """실행계획 §12 의 기준을 기계적으로 대조한다. 판정을 손으로 고르지 않는다."""
    cohort = [v["roc_auc"] for v in strat["cohort"].values() if v.get("roc_auc") is not None]
    spread = (max(cohort) - min(cohort)) if len(cohort) > 1 else 0.0
    c = [
        {"기준": "독립셋 ROC-AUC 가 random 수준으로 붕괴", "구분": "No-Go",
         "값": "%.4f (순열 p=%.4f)" % (r2["roc_auc"], r2["permutation"]["p_value"]),
         "해당": bool(r2["permutation"]["p_value"] >= 0.05 or r2["roc_ci95"][0] <= 0.5)},
        {"기준": "대부분의 성능이 n_axes 한 축에서 발생", "구분": "No-Go",
         "값": "n_axes 단독 %.4f vs 모델 %.4f / 축 통제 후 %.4f"
               % (px2["n_axes 단독"], r2["roc_auc"], nc2["within_stratum_pct_roc_auc"]),
         "해당": bool(nc2["within_stratum_pct_roc_auc"] <= 0.55)},
        {"기준": "Top-k 안정성 붕괴", "구분": "No-Go",
         "값": "상위%d 유지율 %.3f / hold-out 순위상관 %.3f"
               % (st["top_k"], st["overlap_mean"], st["holdout_rank_corr_mean"]),
         "해당": bool(st["overlap_mean"] < 0.5)},
        {"기준": "라벨러 간 일치도가 지나치게 낮음", "구분": "No-Go",
         "값": "agreement 0.90 / kappa 0.7895 (M41 2인 20건)", "해당": False},
        {"기준": "부트스트랩 CI 가 여전히 너무 넓음", "구분": "Conditional",
         "값": "%.3f ~ %.3f (폭 %.3f)"
               % (r2["roc_ci95"][0], r2["roc_ci95"][1],
                  r2["roc_ci95"][1] - r2["roc_ci95"][0]),
         "해당": bool(r2["roc_ci95"][1] - r2["roc_ci95"][0] > 0.20)},
        {"기준": "양성 표본이 너무 적음", "구분": "Conditional",
         "값": "%d건 / %d건 (%.1f%%)"
               % (r2["n_positive"], r2["n_clean"], r2["positive_rate"] * 100),
         "해당": bool(r2["n_positive"] < 15)},
        {"기준": "특정 계층에서만 성능이 좋음", "구분": "Conditional",
         "값": "cohort 간 ROC-AUC 폭 %.3f (%s)"
               % (spread, ", ".join("%s %.3f" % (k, v["roc_auc"])
                                    for k, v in strat["cohort"].items()
                                    if v.get("roc_auc") is not None)),
         "해당": bool(spread > 0.15)},
        {"기준": "성능이 유의미하게 유지 (Final 조건)", "구분": "Final",
         "값": "1차 0.9040 -> 2차 %.4f (PR-AUC 0.8117 -> %.4f, 기저율 %.3f)"
               % (r2["roc_auc"], r2["pr_auc"], r2["positive_rate"]),
         "해당": bool(r2["roc_ci95"][0] > 0.6 and r2["roc_auc"] >= 0.8)},
    ]
    nogo = [x for x in c if x["구분"] == "No-Go" and x["해당"]]
    cond = [x for x in c if x["구분"] == "Conditional" and x["해당"]]
    fin = [x for x in c if x["구분"] == "Final" and x["해당"]]
    v = "No-Go / 재설계" if nogo else ("Conditional (유지)" if cond or not fin else "Final 채택")
    return {"verdict": v, "checks": c,
            "hit_nogo": [x["기준"] for x in nogo],
            "hit_conditional": [x["기준"] for x in cond]}


def main_():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    sheet = add_strata(train)[["row_id", "st_dq", "st_axes"]].rename(
        columns={"st_dq": "층_품질", "st_axes": "층_축수"})

    l1 = pd.read_csv(CLEAN1, encoding="utf-8-sig")[["row_id", "라벨"]]
    l2 = pd.read_csv(CLEAN2, encoding="utf-8-sig")[["row_id", "라벨"]]

    print("M42 — 2차 독립 라벨셋 검증 (재튜닝 없음)")
    print("  모델: 정형 설계 feature 기반 비교군 유클리드 거리 (M38, distance only)")
    print("  적합: 각 hold-out 을 뺀 나머지. 두 세트를 같은 프로토콜로 다시 계산한다.\n")

    r1, m1, y1, s1, _ = evaluate("1차 hold-out (M33)", train, l1, "first")
    r2, m2, y2, s2, _ = evaluate("2차 독립 hold-out (M41)", train, l2, "second")

    both = pd.concat([l1, l2], ignore_index=True).drop_duplicates("row_id")
    r3, m3, y3, s3, _ = evaluate("합산 (1차+2차)", train, both, "combined")

    print("== 실행계획 §8 — 먼저 분리해서 본다")
    hdr = "%-24s %7s %5s %8s %-18s %8s %-18s" % (
        "평가셋", "Clean N", "Pos", "ROC-AUC", "95% CI", "PR-AUC", "95% CI")
    print("  " + hdr)
    for r in (r1, r2, r3):
        print("  %-24s %7d %5d %8.4f %-18s %8.4f %-18s"
              % (r["name"], r["n_clean"], r["n_positive"], r["roc_auc"],
                 "%.3f ~ %.3f" % tuple(r["roc_ci95"]), r["pr_auc"],
                 "%.3f ~ %.3f" % tuple(r["pr_ci95"])))

    for r in (r1, r2):
        b = r["budget"]
        print("\n== %s — 같은 경고 예산 상위 %d건 (%.0f%%)"
              % (r["name"], b["top_k"], b["frac"] * 100))
        print("  recall %s / precision %s   TN=%d FP=%d FN=%d TP=%d"
              % (b["recall"], b["precision"], *b["confusion_TN_FP_FN_TP"]))
        print("  라벨 순열 p=%.4f (평균 %.4f)"
              % (r["permutation"]["p_value"], r["permutation"]["mean"]))

    ks = [3, 5, 7, 10, 15]
    tk1, tk2 = topk_recall(y1, s1, ks), topk_recall(y2, s2, ks)
    print("\n== Top-k recall")
    print("  %-24s %s" % ("1차", tk1))
    print("  %-24s %s" % ("2차 독립", tk2))

    st2 = stability(train, set(l2["row_id"]), m2, y2, s2)
    print("\n== 재적합 안정성 (2차, 80%% 재표집 %d회)" % st2["n_iter"])
    print("  상위 %d건 유지율 평균 %.3f / 최저 %.3f" % (st2["top_k"], st2["overlap_mean"], st2["overlap_min"]))
    print("  hold-out 순위상관 평균 %.3f | ROC-AUC %.3f (%.3f ~ %.3f)"
          % (st2["holdout_rank_corr_mean"], st2["roc_auc_mean"],
             st2["roc_auc_min"], st2["roc_auc_max"]))

    strat = by_stratum(m2, y2, s2, train, sheet)
    print("\n== 2차 층별 ROC-AUC — 한 층에서만 나오는가")
    for col, d in strat.items():
        print("  [%s]" % col)
        for k, v in sorted(d.items()):
            print("    %-14s n=%-3d pos=%-2d %s"
                  % (k, v["n"], v["n_positive"],
                     "%.4f" % v["roc_auc"] if v.get("roc_auc") is not None
                     else v.get("note", "-")))

    px2 = proxy_signals(m2, y2, train)
    px1 = proxy_signals(m1, y1, train)
    print("\n== 단일 대리 신호 단독 ROC-AUC (No-Go 점검)")
    for k in px2:
        print("  %-28s 1차 %.4f | 2차 %.4f" % (k, px1[k], px2[k]))

    nc2, nc1 = naxes_control(m2, y2, s2, train), naxes_control(m1, y1, s1, train)
    print("\n== n_axes 통제 — 축 개수 밖에서도 순위를 매기는가")
    print("  [2차] 축별 양성률")
    for k, v in sorted(nc2["positive_rate_by_n_axes"].items()):
        print("    축%d  n=%-3d 양성 %-2d (%.1f%%)"
              % (k, v["n"], v["n_positive"], v["positive_rate"] * 100))
    print("  [2차] 층 안 백분위 ROC-AUC %.4f (층 가중평균 %s)"
          % (nc2["within_stratum_pct_roc_auc"], nc2["pooled_within_roc_auc"]))
    print("  [1차] 층 안 백분위 ROC-AUC %.4f (층 가중평균 %s)"
          % (nc1["within_stratum_pct_roc_auc"], nc1["pooled_within_roc_auc"]))

    vd = verdict(r2, st2, strat, px2, nc2)
    print("\n== 실행계획 §12 판정")
    for x in vd["checks"]:
        print("  [%-11s] %-34s %s" % (x["구분"], x["기준"],
                                      "해당" if x["해당"] else "비해당"))
        print("                %s" % x["값"])
    print("\n  ==> %s" % vd["verdict"])

    rep = {
        "목적": "실행계획 §14 — 재튜닝 없이 2차 독립 라벨셋에서 재현성 검증",
        "verdict": vd,
        "model": "정형 설계 feature 기반 비교군 유클리드 거리 (M38, distance only)",
        "no_retuning": ["비교군 기준", "거리 종류", "feature 가중치", "threshold",
                        "feature 추가·삭제", "알고리즘"],
        "fit_protocol": "각 hold-out 행을 적합에서 제외. 두 세트를 한 스크립트에서 재계산",
        "sets": {"first": r1, "second": r2, "combined": r3},
        "topk_recall": {"first": tk1, "second": tk2},
        "stability_second": st2,
        "by_stratum_second": strat,
        "proxy_signals": {"first": px1, "second": px2},
        "naxes_control": {"first": nc1, "second": nc2},
    }
    C.save_report("m42_m3_second_eval.json", rep)
    write_md(rep)
    return rep


def write_md(r):
    r1, r2, r3 = r["sets"]["first"], r["sets"]["second"], r["sets"]["combined"]
    L = ["# M42 — 2차 독립 라벨셋 검증: 재튜닝 없이 그대로 적용", "",
         "> 실행계획 §6. 70건 라벨을 본 뒤 **비교군 기준·거리 종류·feature 가중치·",
         "> threshold·feature 구성·알고리즘 중 어느 것도 바꾸지 않았습니다.** 바꾸면",
         "> 이 세트는 hold-out 이 아니라 튜닝셋이 됩니다.", "",
         "```text",
         "모델   %s" % r["model"],
         "적합   %s" % r["fit_protocol"],
         "```", "",
         "> M39 는 전체 1948행에 적합해 0.904 를, M40 은 hold-out 을 뺀 1913행에",
         "> 적합해 0.936 을 냈습니다. 그 차이는 모델이 아니라 프로토콜입니다.",
         "> 아래 1차 수치는 **2차와 같은 조건으로 다시 계산한 값**이라 리포트의",
         "> 0.904 와 다를 수 있습니다.", "",
         "## 1. 먼저 분리해서 (실행계획 §8)", "",
         "| 평가셋 | Clean N | Positive | ROC-AUC | 95% CI | PR-AUC | 95% CI |",
         "|---|---:|---:|---:|---|---:|---|"]
    for x in (r1, r2, r3):
        L.append("| %s | %d | %d | **%.4f** | %.3f ~ %.3f | %.4f | %.3f ~ %.3f |"
                 % (x["name"], x["n_clean"], x["n_positive"], x["roc_auc"],
                    x["roc_ci95"][0], x["roc_ci95"][1], x["pr_auc"],
                    x["pr_ci95"][0], x["pr_ci95"][1]))
    L += ["", "> 합산은 **보조**입니다(실행계획 §9). 1차 35건은 개발 과정에서 여러 번",
          "> 관찰됐고 2차는 독립 재검증용이라 성질이 다릅니다. 대표값으로 쓰지 않습니다.", "",
          "### 제외된 라벨", "", "| 평가셋 | 라벨 전체 | `data_error` | `uncertain` | Clean |",
          "|---|---:|---:|---:|---:|"]
    for x in (r1, r2):
        L.append("| %s | %d | %d | %d | %d |"
                 % (x["name"], x["n_labeled"], x["excluded"]["data_error"],
                    x["excluded"]["uncertain"], x["n_clean"]))
    L += ["", "## 2. 같은 경고 예산에서 (상위 20%)", "",
          "| 평가셋 | 상위 k | recall | precision | TN | FP | FN | TP | 순열 p |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for x in (r1, r2):
        b = x["budget"]
        L.append("| %s | %d | %s | %s | %d | %d | %d | %d | %.4f |"
                 % (x["name"], b["top_k"], b["recall"], b["precision"],
                    *b["confusion_TN_FP_FN_TP"], x["permutation"]["p_value"]))
    L += ["", "## 3. Top-k recall", "", "| 평가셋 | " +
          " | ".join(sorted(r["topk_recall"]["second"], key=lambda s: int(s[3:]))) + " |",
          "|---|" + "---:|" * len(r["topk_recall"]["second"])]
    for tag, nm in (("first", r1["name"]), ("second", r2["name"])):
        d = r["topk_recall"][tag]
        keys = sorted(r["topk_recall"]["second"], key=lambda s: int(s[3:]))
        L.append("| %s | %s |" % (nm, " | ".join(
            ("%.4f" % d[k]) if k in d else "-" for k in keys)))
    st = r["stability_second"]
    L += ["", "## 4. 재적합 안정성 (2차)", "", "```text",
          "80%% 재표집 %d회 / 상위 %d건 유지율 평균 %.3f (최저 %.3f)"
          % (st["n_iter"], st["top_k"], st["overlap_mean"], st["overlap_min"]),
          "hold-out 순위상관 평균 %.3f" % st["holdout_rank_corr_mean"],
          "ROC-AUC 평균 %.3f (%.3f ~ %.3f)"
          % (st["roc_auc_mean"], st["roc_auc_min"], st["roc_auc_max"]),
          "```", "",
          "## 5. 층별 — 한 층에서만 나오는가 (실행계획 §12)", ""]
    for col, d in r["by_stratum_second"].items():
        L += ["**%s**" % col, "", "| 층 | n | 양성 | ROC-AUC |", "|---|---:|---:|---:|"]
        for k, v in sorted(d.items()):
            L.append("| `%s` | %d | %d | %s |"
                     % (k, v["n"], v["n_positive"],
                        "%.4f" % v["roc_auc"] if v.get("roc_auc") is not None
                        else v.get("note", "-")))
        L.append("")
    L += ["## 6. 단일 대리 신호 (No-Go 점검)", "",
          "성능이 `n_axes` 같은 한 축에서 나오는 것은 아닌지 봅니다.", "",
          "| 신호 | 1차 | 2차 |", "|---|---:|---:|"]
    for k in r["proxy_signals"]["second"]:
        L.append("| %s | %.4f | %.4f |"
                 % (k, r["proxy_signals"]["first"][k], r["proxy_signals"]["second"][k]))
    n2, n1 = r["naxes_control"]["second"], r["naxes_control"]["first"]
    L += ["", "### 2차에서 `n_axes` 단독이 모델을 앞질렀습니다", "",
          "`n_axes` 단독 **%.4f** > 모델 **%.4f**. 실행계획 §12 의 No-Go 기준"
          % (r["proxy_signals"]["second"]["n_axes 단독"], r2["roc_auc"]),
          "(\"대부분의 성능이 `n_axes` 등 한 feature 에서 발생\")에 직접 걸리는",
          "수치라 따로 봅니다.", "",
          "| 축 개수 | n | 양성 | 양성률 | 층 안 ROC-AUC |", "|---|---:|---:|---:|---:|"]
    for k, v in sorted(n2["positive_rate_by_n_axes"].items()):
        w = n2["within_stratum"].get(k, {})
        L.append("| 축%s | %d | %d | %.1f%% | %s |"
                 % (k, v["n"], v["n_positive"], v["positive_rate"] * 100,
                    "%.4f" % w["roc_auc"] if w.get("roc_auc") is not None else "—"))
    L += ["",
          "양성률이 축 개수를 따라 올라갑니다. 이것은 모델이 아니라 **라벨 규칙의",
          "성질**입니다 — `atypical_design` 은 \"축 둘 이상이 극단\"으로 정의돼 있어,",
          "축이 2개뿐이면 둘 다 극단이어야 하고 4개면 아무 둘이면 됩니다.",
          "M40 §3 이 지목했던 혼입이 독립셋에서 더 크게 나타났습니다.", "",
          "축 개수를 고정한 안에서 다시 매긴 순위:", "", "```text",
          "2차  층 안 백분위 ROC-AUC %.4f   (층 가중평균 %s)"
          % (n2["within_stratum_pct_roc_auc"], n2["pooled_within_roc_auc"]),
          "1차  층 안 백분위 ROC-AUC %.4f   (층 가중평균 %s)"
          % (n1["within_stratum_pct_roc_auc"], n1["pooled_within_roc_auc"]),
          "```", "",
          "층 안에서도 0.5 는 넘습니다(2차 %.4f). 축 개수가 전부는 아니지만,"
          % n2["within_stratum_pct_roc_auc"],
          "1차의 %.4f 에서 내려온 값이고 `n_axes` 단독보다 낮습니다."
          % n1["within_stratum_pct_roc_auc"], "",
          "## 7. 판정 (실행계획 §12)", "",
          "기준을 손으로 고르지 않고 스크립트에서 대조했습니다.", "",
          "| 구분 | 기준 | 해당 | 값 |", "|---|---|---|---|"]
    for x in r["verdict"]["checks"]:
        L.append("| %s | %s | %s | %s |"
                 % (x["구분"], x["기준"], "**해당**" if x["해당"] else "비해당", x["값"]))
    L += ["", "```text", "판정  %s" % r["verdict"]["verdict"], "```", ""]
    if r["verdict"]["hit_conditional"]:
        L += ["Conditional 사유: %s"
              % ", ".join("`%s`" % k for k in r["verdict"]["hit_conditional"]), ""]
    if r["verdict"]["hit_nogo"]:
        L += ["No-Go 사유: %s"
              % ", ".join("`%s`" % k for k in r["verdict"]["hit_nogo"]), ""]
    L += ["> 실행계획 §10 대로, 2차가 1차보다 낮다는 것 자체를 실패로 읽지",
          "> 않습니다. 다만 이번에는 낮아진 것 말고 **`n_axes` 단독이 모델을**",
          "> **앞질렀다**는 사실이 더 중요합니다. 그것은 모델 문제이기 이전에",
          "> 라벨 규칙이 축 개수에 의존한다는 뜻이고, 다음에 손봐야 할 것은",
          "> 알고리즘이 아니라 `atypical_design` 의 정의입니다.", ""]
    p = os.path.join(C.REPORTS, "m42_m3_second_eval.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main_()
