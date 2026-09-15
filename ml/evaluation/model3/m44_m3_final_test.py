r"""M44 — Model 3 최종 테스트. 구조를 Freeze 하고 독립 라벨셋에서 그대로 잰다.

고정된 최종 구조 (지시서 §16)

    정형 설계 feature -> 사업별 벡터 X
                      -> 유사사업 비교군 대표벡터 C
                      -> 차이벡터 D = X - C
                         ├─ 거리 ||D||  : 최종 anomaly score
                         └─ 방향 D/||D||: 왜 벗어났는지 설명 (점수에 합치지 않음)

    Text Embedding 은 최종 탐지에서 제외한다 — text only 성능이 낮았고(M40),
    structured+text 가 structured only 를 넘지 못했으며, 고차원에서 거리
    집중이 확인됐다(상대대비 4.63 -> 0.71).

하지 않는 것 (지시서 §4)
    새 모델 추가 / OneClassSVM·Deep SVDD 재튜닝 / AutoEncoder / Text 튜닝 /
    거리 metric·feature weight·cohort 기준·threshold 를 **결과를 보고** 변경.

평가 (지시서 §5)
    normal vs atypical_design 만. data_error / uncertain 은 제외.
    ROC-AUC / PR-AUC / Recall / Precision / F1 / Bootstrap 95% CI /
    Top-k Recall / Top-k Stability, 그리고 `n_axes` 단독 baseline.

    라벨은 **v2(M43, 축 개수 중립)를 주 기준**으로 삼는다. v1 은 `atypical_design`
    정의가 축 개수를 세는 규칙이라 `n_axes` 와 엮여 있었다(M42). v1 수치도
    참고로 같이 낸다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

def _find_ml_root(_start):
    """`ml/` 를 위로 거슬러 찾는다. 파일이 몇 단계 아래로 옮겨져도 동작한다."""
    _p = _os.path.abspath(_start)
    while True:
        _p = _os.path.dirname(_p)
        if (_os.path.isdir(_os.path.join(_p, "pipelines"))
                and _os.path.isdir(_os.path.join(_p, "data"))):
            return _p
        if _p == _os.path.dirname(_p):
            raise RuntimeError("ml root not found from %s" % _start)


_ML = _find_ml_root(__file__)
for _d in ("pipelines", "evaluation", "experiments"):
    _base = _os.path.join(_ML, _d)
    if not _os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in _os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in _sys.path:
            _sys.path.insert(0, _dp)
# -------------------------------------------------------------------------

import os
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m3_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN as CLEAN1
from m38_m3_vector_direction import (TYPED_TEXT, build_vectors, combine,
                                     score_components)
from m41_m3_labelset2 import OUT as HOLDOUT2_V1
from m43_m3_label_rule_v2 import OUT as HOLDOUT2_V2

SEED = 42
N_BOOT = 4000
BUDGET_FRAC = 0.20
ALERT_RATE = 0.02       # M20 이 합성 이상치로 정한 운영 경고율. 여기서 바꾸지 않는다
TOPK = [3, 5, 7, 10, 15]
N_RESAMPLE, STAB_K = 10, 30


def fit_score(train, holdout_ids):
    """hold-out 을 적합에서 빼고 비교군 대표벡터를 만든 뒤 거리 점수를 낸다.

    combine(comp, 1.0) 은 거리 100%, 방향 0% 다 — 지시서 §2 가 요구한 대로
    방향은 점수에 들어가지 않는다.
    """
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    Xtr, Xap, _, n_num = build_vectors(fit, train)
    comp = score_components(fit, train, Xtr, Xap, n_num)
    s = pd.Series(combine(comp, 1.0), index=train["row_id"].to_numpy())
    comp.index = train["row_id"].to_numpy()
    return s, comp


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


def prf(y, flag):
    tp = int((flag & (y == 1)).sum())
    fp = int((flag & (y == 0)).sum())
    fn = int((~flag & (y == 1)).sum())
    tn = int((~flag & (y == 0)).sum())
    rec = tp / (tp + fn) if tp + fn else None
    prec = tp / (tp + fp) if tp + fp else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else 0.0
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn, "n_flagged": int(flag.sum()),
            "recall": None if rec is None else round(rec, 4),
            "precision": None if prec is None else round(prec, 4),
            "f1": round(f1, 4)}


def boot(y, sc, k, n=N_BOOT, seed=SEED):
    """ROC-AUC / PR-AUC / recall / precision / F1 을 한 리샘플에서 같이 뜬다."""
    rng = np.random.default_rng(seed)
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    acc = {m: [] for m in ("roc_auc", "pr_auc", "recall", "precision", "f1")}
    for _ in range(n):
        i = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        yy, ss = y[i], sc[i]
        if yy.sum() in (0, len(yy)):
            continue
        acc["roc_auc"].append(roc_auc_score(yy, ss))
        acc["pr_auc"].append(average_precision_score(yy, ss))
        kk = max(1, min(k, len(ss)))
        m = prf(yy, ss >= np.sort(ss)[::-1][kk - 1])
        acc["recall"].append(m["recall"] or 0.0)
        acc["precision"].append(m["precision"] or 0.0)
        acc["f1"].append(m["f1"])
    return {m: [round(float(np.percentile(v, 2.5)), 4),
                round(float(np.percentile(v, 97.5)), 4)] for m, v in acc.items()}


def topk_recall(y, sc):
    out = {}
    for k in TOPK:
        if k > len(y):
            continue
        out["top%d" % k] = round(float(((sc >= np.sort(sc)[::-1][k - 1]) & (y == 1)).sum()
                                       / max(1, y.sum())), 4)
    return out


def topk_stability(train, holdout_ids, ids, base_sc, y):
    """80% 재표집으로 다시 적합해도 상위 목록이 유지되는가 (지시서 §5)."""
    from scipy.stats import spearmanr
    rng = np.random.default_rng(SEED)
    fit_all = train[~train["row_id"].isin(holdout_ids)]
    base_s, _ = fit_score(train, holdout_ids)
    base_top = set(base_s.sort_values(ascending=False).head(STAB_K).index)
    kb = max(1, int(round(BUDGET_FRAC * len(y))))
    base_hold_top = set(pd.Series(base_sc, index=ids).sort_values(ascending=False)
                        .head(kb).index)
    ov, hov, rho, aucs = [], [], [], []
    for _ in range(N_RESAMPLE):
        sub = fit_all.sample(frac=0.8, random_state=int(rng.integers(1e6)))
        Xtr, Xap, _, nn = build_vectors(sub, train)
        c = score_components(sub, train, Xtr, Xap, nn)
        s = pd.Series(combine(c, 1.0), index=train["row_id"].to_numpy())
        ov.append(len(set(s.sort_values(ascending=False).head(STAB_K).index) & base_top) / STAB_K)
        h = s.loc[ids]
        hov.append(len(set(h.sort_values(ascending=False).head(kb).index) & base_hold_top) / kb)
        rho.append(spearmanr(base_sc, h.to_numpy(float)).statistic)
        aucs.append(roc_auc_score(y, h.to_numpy(float)))
    return {"n_iter": N_RESAMPLE, "pool_top_k": STAB_K, "holdout_top_k": kb,
            "pool_top_overlap_mean": round(float(np.mean(ov)), 4),
            "pool_top_overlap_min": round(float(np.min(ov)), 4),
            "holdout_top_overlap_mean": round(float(np.mean(hov)), 4),
            "holdout_top_overlap_min": round(float(np.min(hov)), 4),
            "rank_corr_mean": round(float(np.mean(rho)), 4),
            "roc_auc_mean": round(float(np.mean(aucs)), 4),
            "roc_auc_min": round(float(np.min(aucs)), 4),
            "roc_auc_max": round(float(np.max(aucs)), 4)}


def measure(name, train, labels, col):
    main = labels[labels[col].isin(["normal", "atypical_design"])]
    main = main[main["row_id"].isin(set(train["row_id"]))]
    ids = main["row_id"].tolist()
    s, comp = fit_score(train, set(labels["row_id"]))
    y = (main[col] == "atypical_design").to_numpy(int)
    sc = s.loc[ids].to_numpy(float)

    k = max(1, int(round(BUDGET_FRAC * len(y))))
    at_budget = prf(y, sc >= np.sort(sc)[::-1][k - 1])
    cut = float(np.quantile(s.to_numpy(), 1 - ALERT_RATE))
    at_oper = prf(y, sc >= cut)

    ax = train.set_index("row_id").loc[ids, "n_axes"].to_numpy(float)
    base_k = prf(y, ax >= np.sort(ax)[::-1][k - 1])

    return {
        "name": name, "n_clean": int(len(y)), "n_positive": int(y.sum()),
        "positive_rate": round(float(y.mean()), 4),
        "excluded": {L: int((labels[col] == L).sum()) for L in ("data_error", "uncertain")},
        "roc_auc": round(float(roc_auc_score(y, sc)), 4),
        "pr_auc": round(float(average_precision_score(y, sc)), 4),
        "at_budget": {"top_k": k, "frac": BUDGET_FRAC, **at_budget},
        "at_operating": {"alert_rate": ALERT_RATE, "pool_cut": round(cut, 4), **at_oper},
        "ci95": boot(y, sc, k),
        "topk_recall": topk_recall(y, sc),
        "baseline_n_axes": {"roc_auc": round(float(roc_auc_score(y, ax)), 4),
                            "pr_auc": round(float(average_precision_score(y, ax)), 4),
                            "at_budget": base_k,
                            "gap_bootstrap": gap_ci(y, sc, ax)},
        "stability": topk_stability(train, set(labels["row_id"]), ids, sc, y),
    }, main, y, sc, comp, ids


def explain(comp, ids, sc, y, main, col, train, k):
    """방향 — 점수에 합치지 않고 '왜 벗어났는가'만 말한다 (지시서 §2)."""
    t = train.set_index("row_id")
    order = pd.Series(sc, index=ids).sort_values(ascending=False).head(k).index
    lab = main.set_index("row_id")[col]
    out = []
    for rid in order:
        c = comp.loc[rid]
        nm = c["dir_typed_name"]
        out.append({
            "row_id": rid, "title": str(t.loc[rid, "title"])[:52],
            "label": lab.loc[rid],
            "dist_pct": round(float(c["dist_pct"]), 1),
            "direction": nm or "유형 없음",
            "cosine": round(float(c["dir_typed_cos"]), 2),
            "cohort_n": int(c["cohort_n"]),
            "sentence": ("동일 비교군 대표설계에서 거리 P%.0f. %s"
                         % (c["dist_pct"],
                            TYPED_TEXT.get(nm, "비교군이 평소 변하지 않는 방향")
                            + ("으로 벗어남 (cosine %.2f)" % c["dir_typed_cos"] if nm else ""))),
        })
    return out


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)

    v2 = pd.read_csv(HOLDOUT2_V2, encoding="utf-8-sig")
    v1 = pd.read_csv(HOLDOUT2_V1, encoding="utf-8-sig")
    f1 = pd.read_csv(CLEAN1, encoding="utf-8-sig")

    print("M44 — Model 3 최종 테스트 (구조 Freeze)")
    print("  구조  정형 설계 feature -> 비교군 대표벡터 C -> D = X - C")
    print("        거리 = anomaly score / 방향 = 설명 전용 (점수 미포함)")
    print("  제외  Text Embedding\n")

    r2, m2, y2, s2, c2, ids2 = measure("2차 독립셋 · 라벨 v2 (주)", train, v2, "v2_라벨")
    r1, *_ = measure("2차 독립셋 · 라벨 v1 (참고)", train, v1, "라벨")
    r0, *_ = measure("1차 hold-out (참고)", train, f1, "라벨")

    print("== 최종 성능 — 주 기준: 2차 독립셋 · 라벨 v2")
    ci = r2["ci95"]
    b = r2["at_budget"]
    print("  Clean N %d (양성 %d, %.1f%%)  |  제외 data_error %d / uncertain %d"
          % (r2["n_clean"], r2["n_positive"], r2["positive_rate"] * 100,
             r2["excluded"]["data_error"], r2["excluded"]["uncertain"]))
    print("  %-12s %8s   %s" % ("지표", "값", "95% CI"))
    print("  %-12s %8.4f   %.3f ~ %.3f" % ("ROC-AUC", r2["roc_auc"], *ci["roc_auc"]))
    print("  %-12s %8.4f   %.3f ~ %.3f" % ("PR-AUC", r2["pr_auc"], *ci["pr_auc"]))
    print("  %-12s %8.4f   %.3f ~ %.3f" % ("Recall", b["recall"], *ci["recall"]))
    print("  %-12s %8.4f   %.3f ~ %.3f" % ("Precision", b["precision"], *ci["precision"]))
    print("  %-12s %8.4f   %.3f ~ %.3f" % ("F1", b["f1"], *ci["f1"]))
    print("  (경고 예산 상위 %d건 = %.0f%% / TP %d FP %d FN %d TN %d)"
          % (b["top_k"], b["frac"] * 100, b["TP"], b["FP"], b["FN"], b["TN"]))
    o = r2["at_operating"]
    print("  운영 경고율 %.0f%% (pool 임계 %.3f) 적용시: 경고 %d건 / TP %d"
          % (o["alert_rate"] * 100, o["pool_cut"], o["n_flagged"], o["TP"]))

    print("\n== n_axes 단독 baseline (지시서 §5)")
    for nm, r in (("2차 v2 (주)", r2), ("2차 v1 (참고)", r1), ("1차 (참고)", r0)):
        bl = r["baseline_n_axes"]
        g = bl["gap_bootstrap"]
        print("  %-14s 모델 %.4f  n_axes %.4f  격차 %+.4f  95%% [%+.4f, %+.4f]  P(>0)=%.3f"
              % (nm, r["roc_auc"], bl["roc_auc"], r["roc_auc"] - bl["roc_auc"],
                 g["ci95"][0], g["ci95"][1], g["p_gap_gt_0"]))

    print("\n== Top-k Recall")
    for nm, r in (("2차 v2 (주)", r2), ("2차 v1 (참고)", r1), ("1차 (참고)", r0)):
        print("  %-14s %s" % (nm, r["topk_recall"]))

    st = r2["stability"]
    print("\n== Top-k Stability (80%% 재표집 %d회)" % st["n_iter"])
    print("  pool 상위%d 유지율    %.3f (최저 %.3f)"
          % (st["pool_top_k"], st["pool_top_overlap_mean"], st["pool_top_overlap_min"]))
    print("  hold-out 상위%d 유지율 %.3f (최저 %.3f)"
          % (st["holdout_top_k"], st["holdout_top_overlap_mean"], st["holdout_top_overlap_min"]))
    print("  순위상관 %.3f | ROC-AUC %.3f (%.3f ~ %.3f)"
          % (st["rank_corr_mean"], st["roc_auc_mean"], st["roc_auc_min"], st["roc_auc_max"]))

    ex = explain(c2, ids2, s2, y2, m2, "v2_라벨", train, r2["at_budget"]["top_k"])
    print("\n== 방향 — 경고된 %d건의 설명 (점수에 합치지 않음)" % len(ex))
    for e in ex:
        print("  [%s] %-46s %s" % (e["label"][:4], e["title"][:44], e["sentence"][:78]))

    rep = {
        "구조": ("정형 설계 feature -> 비교군 대표벡터 C -> D = X - C; "
               "거리=anomaly score, 방향=설명 전용(점수 미포함)"),
        "text_embedding": "최종 탐지에서 제외 (M40: text only 낮음, +structured 개선 없음, 거리 집중)",
        "frozen": ["모델", "거리 metric", "feature weight", "cohort 기준", "threshold"],
        "primary": r2, "reference": {"second_v1": r1, "first": r0},
        "direction_examples": ex,
    }
    C.save_report("m44_m3_final_test.json", rep)
    write_md(rep)


def write_md(r):
    p2, rv1, r0 = r["primary"], r["reference"]["second_v1"], r["reference"]["first"]
    ci, b, o, st = p2["ci95"], p2["at_budget"], p2["at_operating"], p2["stability"]
    L = ["# M44 — Model 3 최종 테스트 (구조 Freeze)", "",
         "> 새 모델을 찾는 것이 목적이 아닙니다. **현재 선택한 단순한 구조가",
         "> 독립 데이터에서도 재현되는지** 확인하는 것입니다.", "",
         "## 1. 고정된 최종 구조", "", "```text",
         "정형 설계 feature",
         "      ↓",
         "사업별 벡터 X",
         "      ↓",
         "유사사업 비교군 구성  (지원성격 x 지원방식 -> 지원성격 -> 전체, 최소 20건)",
         "      ↓",
         "비교군 대표벡터 C     (비교군 평균)",
         "      ↓",
         "차이벡터 D = X - C",
         "      ├─ 거리 ||D||   -> 최종 anomaly score",
         "      └─ 방향 D/||D|| -> 왜 벗어났는지 설명 (점수에 합치지 않음)",
         "```", "",
         "**Text Embedding 은 최종 탐지에서 제외했습니다.** text only 최고 0.596,",
         "structured+text 가 structured only 를 넘지 못했고, 고차원에서 거리 집중이",
         "확인됐습니다(상대대비 4.63 -> 0.71). 확장 실험으로 남깁니다. (M40)", "",
         "Freeze 대상: %s" % ", ".join("`%s`" % x for x in r["frozen"]), "",
         "## 2. 최종 성능 — 2차 독립 라벨셋", "",
         "```text",
         "평가 대상  normal vs atypical_design",
         "제외       data_error %d / uncertain %d"
         % (p2["excluded"]["data_error"], p2["excluded"]["uncertain"]),
         "Clean N    %d (양성 %d, %.1f%%)"
         % (p2["n_clean"], p2["n_positive"], p2["positive_rate"] * 100),
         "```", "",
         "| 지표 | 값 | 95% CI |", "|---|---:|---|",
         "| **ROC-AUC** | **%.4f** | %.3f ~ %.3f |" % (p2["roc_auc"], *ci["roc_auc"]),
         "| **PR-AUC** | **%.4f** | %.3f ~ %.3f |" % (p2["pr_auc"], *ci["pr_auc"]),
         "| Recall | %.4f | %.3f ~ %.3f |" % (b["recall"], *ci["recall"]),
         "| Precision | %.4f | %.3f ~ %.3f |" % (b["precision"], *ci["precision"]),
         "| F1 | %.4f | %.3f ~ %.3f |" % (b["f1"], *ci["f1"]), "",
         "Recall/Precision/F1 은 경고 예산 **상위 %d건(%.0f%%)** 기준입니다 — "
         % (b["top_k"], b["frac"] * 100),
         "M30 20/50, M34 7/35 와 같은 비율입니다.", "",
         "```text",
         "TP %d   FP %d   FN %d   TN %d" % (b["TP"], b["FP"], b["FN"], b["TN"]),
         "```", "",
         "> 운영 경고율 %.0f%%(pool 임계 %.3f)를 그대로 적용하면 이 53건 중 경고는"
         % (o["alert_rate"] * 100, o["pool_cut"]),
         "> **%d건**(TP %d)입니다. 독립셋이 점수 기반으로 뽑히지 않았기 때문이고,"
         % (o["n_flagged"], o["TP"]),
         "> 운영 임계선 재산정은 별도의 Operational Calibration Set 이 필요합니다.", "",
         "## 3. `n_axes` 단독 baseline (지시서 §5)", "",
         "\"모델이 단순한 축 개수보다 실제 설계 이례성을 더 잘 설명하는가\"", "",
         "| 평가셋 | Clean N | 양성 | 모델 ROC | `n_axes` ROC | 격차 | 격차 95% CI | P(격차>0) |",
         "|---|---:|---:|---:|---:|---:|---|---:|"]
    for nm, x in (("**2차 v2 (주)**", p2), ("2차 v1 (참고)", rv1), ("1차 (참고)", r0)):
        bl = x["baseline_n_axes"]
        g = bl["gap_bootstrap"]
        L.append("| %s | %d | %d | %.4f | %.4f | **%+.4f** | %+.4f ~ %+.4f | %.3f |"
                 % (nm, x["n_clean"], x["n_positive"], x["roc_auc"], bl["roc_auc"],
                    x["roc_auc"] - bl["roc_auc"], g["ci95"][0], g["ci95"][1],
                    g["p_gap_gt_0"]))
    L += ["",
          "> v1 라벨에서는 `n_axes` 가 모델을 앞섰습니다. v1 의 `atypical_design` 이",
          "> \"극단 축이 둘 이상\"이라 **세는** 규칙이었고 축 개수와 엮여 있었기",
          "> 때문입니다(M42). v2 는 그 정의를 축 개수 중립으로 고친 라벨이고(M43),",
          "> 거기서는 모델이 앞섭니다. 다만 양성 %d건이라 **격차의 부트스트랩 구간이"
          % p2["n_positive"],
          "> 0 을 품습니다** — 부호가 뒤집힌 것은 방향으로만 읽어야 합니다.",
          "> 격차는 같은 리샘플에서 짝지어 쟀습니다(따로 재고 눈으로 빼면 표본",
          "> 변동이 상쇄되지 않습니다).", "",
          "## 4. Top-k Recall", "",
          "| 평가셋 | " + " | ".join(sorted(p2["topk_recall"], key=lambda s: int(s[3:]))) + " |",
          "|---|" + "---:|" * len(p2["topk_recall"])]
    keys = sorted(p2["topk_recall"], key=lambda s: int(s[3:]))
    for nm, x in (("**2차 v2 (주)**", p2), ("2차 v1 (참고)", rv1), ("1차 (참고)", r0)):
        L.append("| %s | %s |" % (nm, " | ".join(
            ("%.4f" % x["topk_recall"][k]) if k in x["topk_recall"] else "-" for k in keys)))
    L += ["", "## 5. Top-k Stability", "",
          "80%% 재표집으로 비교군 대표벡터 `C` 를 다시 만들어도 같은 사업을",
          "경고하는가. 이 방식에는 난수 초기값이 없으므로 흔들리는 원인은",
          "표본뿐입니다.", "", "```text",
          "재표집 %d회 / 80%%" % st["n_iter"],
          "pool 상위 %d건 유지율      %.3f  (최저 %.3f)"
          % (st["pool_top_k"], st["pool_top_overlap_mean"], st["pool_top_overlap_min"]),
          "hold-out 상위 %d건 유지율   %.3f  (최저 %.3f)"
          % (st["holdout_top_k"], st["holdout_top_overlap_mean"], st["holdout_top_overlap_min"]),
          "hold-out 순위상관          %.3f" % st["rank_corr_mean"],
          "ROC-AUC                    %.3f  (%.3f ~ %.3f)"
          % (st["roc_auc_mean"], st["roc_auc_min"], st["roc_auc_max"]),
          "```", "",
          "## 6. 방향 — 설명 전용 모듈 (지시서 §2)", "",
          "방향은 anomaly score 에 **합치지 않습니다.** 거리로 걸린 사업에 대해",
          "어느 방향으로 벗어났는지만 문장으로 말합니다.", "",
          "| 라벨 | 사업 | 거리 | 방향 유형 | 설명 |",
          "|---|---|---:|---|---|"]
    for e in r["direction_examples"]:
        L.append("| `%s` | %s | P%.0f | %s | %s |"
                 % (e["label"], e["title"][:38], e["dist_pct"], e["direction"],
                    TYPED_TEXT.get(e["direction"], "비교군이 평소 변하지 않는 방향")))
    L += ["",
          "> 출력 문구는 M13 의 허용 표현 안에서만 씁니다 — '드문 설계 조합,",
          "> 확인 필요'이지 '잘못 설계됨 / 부적절 / 과도한 지원 / 삭감 필요'가",
          "> 아닙니다.", "",
          "## 7. 최종 성능 확정", "", "```text",
          "Model 3  정형 설계 feature 기반 비교군 대표벡터 거리 탐지 + 방향 설명",
          "",
          "독립 라벨셋 %d건 (양성 %d) · 재튜닝 없음"
          % (p2["n_clean"], p2["n_positive"]),
          "  ROC-AUC   %.4f  [%.3f, %.3f]" % (p2["roc_auc"], *ci["roc_auc"]),
          "  PR-AUC    %.4f  [%.3f, %.3f]" % (p2["pr_auc"], *ci["pr_auc"]),
          "  Recall    %.4f  [%.3f, %.3f]  (상위 %d건)" % (b["recall"], *ci["recall"], b["top_k"]),
          "  Precision %.4f  [%.3f, %.3f]" % (b["precision"], *ci["precision"]),
          "  F1        %.4f  [%.3f, %.3f]" % (b["f1"], *ci["f1"]),
          "  안정성    hold-out 상위%d 유지율 %.3f / 순위상관 %.3f"
          % (st["holdout_top_k"], st["holdout_top_overlap_mean"], st["rank_corr_mean"]),
          "```", "",
          "### 같이 읽어야 하는 것", "",
          "- 양성 %d건입니다. 모든 구간이 넓습니다. 점추정을 단독으로 쓰지 마십시오."
          % p2["n_positive"],
          "- 이 수치는 **재튜닝 없이** 나온 값입니다. 독립셋을 보고 구조를 고친",
          "  적이 없으므로 낙관 편향이 없습니다.",
          "- 운영 임계선(경고율 %.0f%%)은 이 세트로 정할 수 없습니다. 별도의"
          % (o["alert_rate"] * 100),
          "  Operational Calibration Set 이 필요합니다.", ""]
    p = C.report_path("m44_m3_final_test.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
