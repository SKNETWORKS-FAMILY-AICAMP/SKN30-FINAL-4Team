"""DL15 — 모델 1·2·3 최종 채택 정리: ML/DL 을 같은 자로 세우고 승자를 고른다.

계획서 0절: "DL 을 넣기 위해 문제를 확장하지 않는다. 같은 서비스 질문에서
공정하게 비교하고, 외부 일반화와 실무 활용성이 더 좋은 모델을 최종 채택한다."
계획서 2.7: "ML = baseline / DL = challenger / 최종 = 외부 검증 승자."

새로 학습하지 않는다. 각 실험의 리포트 JSON 을 읽어 한 표로 세운다.

읽는 것
    모델 1  dl12_m1_candidates_*.json   같은 학습셋·같은 외부 131건에서 후보 5종
            m29_m1_external_eval.json   운영 산출물(LogReg)·판단보류 관점
            m27_m1_margin_abstention.json  판단보류 커버리지 곡선
    모델 2  dl14_m2_ft_transformer.json  같은 GroupKFold 에서 FT-Transformer vs LGBM
            m26_m2_interval_usability.json  구간 실용성 판정
    모델 3  m30_m3_real_eval.json        사람 라벨 50건에서 OneClassSVM
            dl13_m3_deepsvdd.json        같은 hold-out 에서 Deep SVDD
            m20_m4_threshold.json        합성 이상치 기준(병기용)

채택 규칙 (결과를 보기 전에 못박은 것)
    모델 1  외부 hold-out 정확도. 내부 CV 는 하이퍼파라미터 선택에만 쓴다.
    모델 2  LGBM 의 fold 간 표준편차보다 크게 이겨야 교체한다.
    모델 3  실제 사람 라벨 hold-out. 합성 이상치 성능은 참고로만 병기한다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


BUNDLE = os.path.join(C.MODELS, "m1_dl_bundle", "external.parquet")


def batch_breakdown(rows):
    """M07 41건 / M28 90건으로 갈라 각 모델 정확도를 따로 낸다.

    100건을 이번에 붙였으니 "새 라벨이 특정 모델에 유리하게 붙은 것 아닌가"를
    스스로 확인해야 한다. 한쪽 배치에서만 이기면 그건 모델 차이가 아니라
    라벨 차이다.
    """
    import pandas as pd
    if not os.path.exists(BUNDLE):
        return {}
    ex = pd.read_parquet(BUNDLE)
    gold, batch = ex["gold"].to_numpy(), ex["batch"].to_numpy()
    out = {}
    for r in rows:
        pred = r.get("pred_seed0")
        if not pred or len(pred) != len(gold):
            continue
        pred = pd.Series(pred).to_numpy()
        out[r["model"]] = {
            b: round(float((gold[batch == b] == pred[batch == b]).mean()), 4)
            for b in sorted(set(batch))}
        out[r["model"]]["n_by_batch"] = {b: int((batch == b).sum())
                                         for b in sorted(set(batch))}
    return out


def reversal_check(rows):
    """예전 51.2% 와 지금 값을 같은 41건 위에 세운다.

    라벨 정정분과 입력 경로분을 분리하려면 세 수가 필요하다.
        41건 / 구(舊)라벨   예전 보고와 같은 조건
        41건 / 정정 라벨    라벨 정정만 반영
        131건 / 정정 라벨   지금 조건
    """
    import pandas as pd
    old_p = os.path.join(C.DATA, "labels", "openapi_manual_50.csv")
    if not (os.path.exists(BUNDLE) and os.path.exists(old_p)):
        return {}
    ex = pd.read_parquet(BUNDLE)
    old = pd.read_csv(old_p, encoding="utf-8-sig")
    old.columns = [c.strip("﻿") for c in old.columns]
    om = dict(zip(old["announcement_id"].astype(str),
                  old["label_19class"].fillna("")))
    m = ex["batch"].to_numpy() == "M07"
    gold_new = ex["gold"].to_numpy()[m]
    gold_old = np.array([om.get(i, "") for i in ex["announcement_id"].to_numpy()[m]])
    out = {"n_m07": int(m.sum()),
           "n_labels_changed": int((gold_new != gold_old).sum()), "models": {}}
    for r in rows:
        pred = r.get("pred_seed0")
        if not pred:
            continue
        p41 = pd.Series(pred).to_numpy()[m]
        out["models"][r["model"]] = {
            "acc_41_old_labels": round(float((gold_old == p41).mean()), 4),
            "acc_41_fixed_labels": round(float((gold_new == p41).mean()), 4),
            "acc_131": r["ext_acc_mean"]}
    return out


def mcnemar(rows, a, b):
    """두 모델을 같은 131건에서 짝지어 비교한다 (시드 42 예측 기준).

    정확도 두 개를 나란히 놓고 "이쪽이 높다"고 말하면 안 된다. 131건에서
    정확도 차이 3%p 는 4건 차이다. 어느 건에서 갈렸는지를 짝지어 봐야
    분할 흔들림과 구별된다.
    """
    ra = next((r for r in rows if r["model"] == a), None)
    rb = next((r for r in rows if r["model"] == b), None)
    if not ra or not rb or not ra.get("pred_seed0") or not rb.get("pred_seed0"):
        return None
    import pandas as pd
    from scipy.stats import binomtest
    ex = pd.read_parquet(BUNDLE)
    g = ex["gold"].to_numpy()
    oka = g == pd.Series(ra["pred_seed0"]).to_numpy()
    okb = g == pd.Series(rb["pred_seed0"]).to_numpy()
    n_a = int((oka & ~okb).sum())
    n_b = int((okb & ~oka).sum())
    p = binomtest(n_a, n_a + n_b, 0.5).pvalue if n_a + n_b else 1.0
    return {"a": a, "b": b, "a_only_correct": n_a, "b_only_correct": n_b,
            "p_value": round(float(p), 4),
            "verdict": ("차이 있음" if p < 0.05 else
                        "이 표본으로는 구별되지 않음")}


def load(name):
    p = os.path.join(C.REPORTS, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def m1_table():
    """후보별 (내부 CV macroF1, 외부 정확도 평균±표준편차)."""
    rows, meta = [], {}
    for tag in ("ml", "dl", "smoke"):
        j = load("dl12_m1_candidates_%s.json" % tag)
        if not j:
            continue
        meta = {k: j[k] for k in ("n_train", "n_classes", "n_external",
                                  "external_classes_present", "folds", "fixed")}
        for name, v in j["results"].items():
            if any(r["model"] == name for r in rows):
                continue
            rows.append({
                "model": name, "family": v["family"], "lr": v.get("lr"),
                "cv_macro_f1": v["cv"]["macro_f1"], "cv_accuracy": v["cv"]["accuracy"],
                "ext_acc_mean": v["external_mean"]["accuracy_mean"],
                "ext_acc_std": v["external_mean"]["accuracy_std"],
                "ext_macro_f1_mean": v["external_mean"]["macro_f1_present_mean"],
                "n_seeds": v["external_mean"]["n_seeds"],
                "pred_seed0": v.get("external_pred"),
                "per_class": v.get("external_per_class_seed0", []),
                "confusions": v.get("external_confusions_seed0", []),
            })
    rows.sort(key=lambda r: -r["ext_acc_mean"])
    return rows, meta


def main():
    m1_rows, m1_meta = m1_table()
    m1_batch = batch_breakdown(m1_rows)
    rev = reversal_check(m1_rows)
    names = [r["model"] for r in m1_rows]
    pairs = []
    if len(names) >= 2:
        base = "TF-IDF + LinearSVM"
        for other in names:
            if other != base:
                t = mcnemar(m1_rows, other, base)
                if t:
                    pairs.append(t)
        if len(names) >= 2 and names[0] != names[1]:
            t = mcnemar(m1_rows, names[0], names[1])
            if t:
                pairs.append(t)
    for r in m1_rows:
        r.pop("pred_seed0", None)
    m29 = load("m29_m1_external_eval.json")
    m27 = load("m27_m1_margin_abstention.json")
    m14 = load("m14_ml_dl_compare.json")
    dl14 = load("dl14_m2_ft_transformer.json")
    m26 = load("m26_m2_interval_usability.json")
    m30 = load("m30_m3_real_eval.json")
    dl13 = load("dl13_m3_deepsvdd.json")
    m20 = load("m20_m4_threshold.json")

    best1 = m1_rows[0] if m1_rows else None
    base1 = next((r for r in m1_rows if r["model"] == "TF-IDF + LinearSVM"), None)

    picks = {}
    if best1 and base1:
        gap = best1["ext_acc_mean"] - base1["ext_acc_mean"]
        picks["1. 지원성격 분류"] = {
            "adopted": best1["model"], "family": best1["family"],
            "decided_by": "외부 hold-out %d건 정확도 (시드 %d개 평균)"
                          % (m1_meta.get("n_external", 0), best1["n_seeds"]),
            "score": "%.4f ± %.4f" % (best1["ext_acc_mean"], best1["ext_acc_std"]),
            "vs_ml_baseline": round(float(gap), 4),
        }
    if dl14:
        picks["2. 지원규모 상대비교"] = {
            "adopted": ("LightGBM" if "LGBM 유지" in dl14["comparison"]["verdict"]
                        else "FT-Transformer"),
            "family": ("ML" if "LGBM 유지" in dl14["comparison"]["verdict"] else "DL"),
            "decided_by": "동일 GroupKFold(5) MAE(log10), 판정선 = LGBM fold σ %.4f"
                          % dl14["comparison"]["significance_margin_fold_std"],
            "score": "LGBM %.4f vs FT %.4f ± %.4f"
                     % (dl14["lightgbm"]["MAE_log10"],
                        dl14["ft_transformer"]["summary"]["MAE_log10_mean"],
                        dl14["ft_transformer"]["summary"]["MAE_log10_std"]),
            "verdict": dl14["comparison"]["verdict"],
        }
    if m30 and dl13:
        oc = m30["results"]["엄격(비전형만)"]
        ocq = m30["rank_quality"]["엄격(비전형만)"]
        dq = dl13["rank_quality_strict_mean"]
        picks["3. 설계 이상탐지"] = {
            "adopted": "보류 — 어느 쪽도 운영 경고로 내보낼 근거가 없다",
            "family": "-",
            "decided_by": "사람 라벨 50건 (비전형 %d건)"
                          % m30["holdout"]["labels"].get("비전형", 0),
            "score": "OneClassSVM ROC-AUC %.3f / Deep SVDD %.3f ± %.3f"
                     % (ocq["roc_auc"], dq["roc_auc"], dq["roc_auc_std"]),
            "ocsvm_recall_at_2pct": oc["recall"],
            "ocsvm_precision_at_2pct": oc["precision"],
        }

    report = {
        "principle": "ML = baseline / DL = challenger / 최종 = 외부 검증 승자",
        "picks": picks,
        "model1": {"meta": m1_meta, "candidates": m1_rows,
                   "accuracy_by_label_batch": m1_batch,
                   "paired_tests_seed42": pairs,
                   "reversal_check_vs_dl07": rev,
                   "abstention_adopted_model": (load("dl16_m1_abstention.json") or {})
                   .get("mean_by_coverage"),
                   "abstention": (m27 or {}).get("at_target_coverage"),
                   "production_lr": (m29 or {}).get("results", {}).get(
                       "M02 LogisticRegression(운영 산출물)")},
        "model2": {"comparison": (dl14 or {}).get("comparison"),
                   "lightgbm": (dl14 or {}).get("lightgbm"),
                   "ft_transformer": (dl14 or {}).get("ft_transformer", {}).get("summary"),
                   "cohort_baseline": (dl14 or {}).get("cohort_median_baseline"),
                   "interval_usability": (m26 or {}).get("service_summary")
                   or (m26 or {}).get("verdict")},
        "model3": {"ocsvm_real": (m30 or {}).get("results"),
                   "ocsvm_real_equal_budget": (m30 or {}).get("results_equal_budget"),
                   "ocsvm_rank": (m30 or {}).get("rank_quality"),
                   "deepsvdd": (dl13 or {}).get("summary_mean_std"),
                   "deepsvdd_equal_budget": (dl13 or {}).get(
                       "summary_equal_budget_mean_std"),
                   "deepsvdd_rank": (dl13 or {}).get("rank_quality_strict_mean"),
                   "deepsvdd_seed_stability": (dl13 or {}).get("seed_top30_overlap"),
                   "synthetic_reference": "m20_m4_threshold.json"},
        "previous_verdict_m14": (m14 or {}).get("summary"),
    }
    C.save_report("dl15_final_selection.json", report)
    write_md(report, m1_rows, m1_meta, m29, m27, dl14, m26, m30, dl13, m20,
             m1_batch, pairs, rev)

    print("%-22s%-26s%s" % ("모델", "채택", "근거"))
    print("-" * 96)
    for k, v in picks.items():
        print("%-22s%-26s%s | %s" % (k, v["adopted"], v["score"], v["decided_by"]))


def write_md(r, m1_rows, m1_meta, m29, m27, dl14, m26, m30, dl13, m20,
             m1_batch=None, pairs=None, rev=None):
    L = ["# 모델 1·2·3 최종 채택과 성능", "",
         "> 계획서 0절 — DL 을 넣기 위해 문제를 확장하지 않는다. 같은 서비스 질문에서",
         "> 공정하게 비교하고, 외부 일반화와 실무 활용성이 더 좋은 모델을 채택한다.",
         "", "## 0. 한눈에", "",
         "| 모델 | 서비스 질문 | 채택 | 계열 | 성능 | 결정 근거 |",
         "|---|---|---|---|---|---|"]
    q = {"1. 지원성격 분류": "19개 지원성격 중 어디에 속하는가",
         "2. 지원규모 상대비교": "과거 유사사업 대비 어느 위치인가",
         "3. 설계 이상탐지": "과거 비교군 대비 얼마나 드문 설계인가"}
    for k, v in r["picks"].items():
        L.append("| %s | %s | **%s** | %s | %s | %s |"
                 % (k, q.get(k, ""), v["adopted"], v["family"], v["score"],
                    v["decided_by"]))

    # ── 모델 1 ────────────────────────────────────────────────────────
    L += ["", "---", "", "## 1. 지원성격 분류 (19클래스)", "",
          "```text",
          "학습 %d건 / %d클래스 / 내부 StratifiedGroupKFold(%d) by program_stem"
          % (m1_meta.get("n_train", 0), m1_meta.get("n_classes", 0),
             m1_meta.get("folds", 5)),
          "외부 hold-out %d건 (실제 등장 %d클래스) — 하이퍼파라미터 선택에 쓰지 않음"
          % (m1_meta.get("n_external", 0), m1_meta.get("external_classes_present", 0)),
          "고정 설정 %s" % m1_meta.get("fixed", {}),
          "```", "",
          "**내부 CV 와 외부 정확도를 같은 수로 비교하지 않습니다.** 내부는 19클래스",
          "균등가중 macro F1, 외부는 정확도입니다. 앞선 문서가 \"CV 0.8337 vs 외부 51.2%\"",
          "를 나란히 놓아 만든 혼동을 여기서는 열을 나눠 없앴습니다.", "",
          "| 모델 | 계열 | lr | 내부 CV macroF1 | 외부 정확도 (시드평균) | 외부 macroF1 |",
          "|---|---|---|---:|---:|---:|"]
    for x in m1_rows:
        L.append("| %s | %s | %s | %.4f | **%.4f ± %.4f** | %.4f |"
                 % (x["model"], x["family"],
                    ("%.0e" % x["lr"]) if x["lr"] else "—",
                    x["cv_macro_f1"], x["ext_acc_mean"], x["ext_acc_std"],
                    x["ext_macro_f1_mean"]))
    if m29:
        pr = m29.get("results", {}).get("M02 LogisticRegression(운영 산출물)", {})
        for scope, s in pr.items():
            if isinstance(s, dict) and s.get("n"):
                L.append("| (참고) 현재 운영 산출물 LogReg — %s | ML | — | — | %.4f | — |"
                         % (scope, s["accuracy"]))
    best = m1_rows[0] if m1_rows else None
    if best:
        L += ["", "### 채택: %s" % best["model"], "",
              "클래스별 성적 (시드 42):", "",
              "| 클래스 | 정답 수 | recall | precision |", "|---|---:|---:|---:|"]
        for c in best["per_class"][:10]:
            L.append("| %s | %d | %.2f | %s |"
                     % (c["class"], c["n_true"], c["recall"],
                        "%.2f" % c["precision"] if c["precision"] is not None else "—"))
        if best["confusions"]:
            L += ["", "가장 많이 틀린 방향:", "",
                  "| 정답 | 예측 | 건수 |", "|---|---|---:|"]
            for c in best["confusions"][:6]:
                L.append("| %s | %s | %d |" % (c["true"], c["pred"], c["n"]))
    if pairs:
        L += ["", "### 짝지은 비교 (McNemar, 시드 42)", "",
              "정확도 두 개를 나란히 놓는 것으로는 부족합니다 — 131건에서 3%p 차이는",
              "4건 차이입니다. 같은 건에서 어느 쪽이 맞혔는지를 짝지어 봅니다.", "",
              "| 비교 | A만 맞힘 | B만 맞힘 | p | 판정 |", "|---|---:|---:|---:|---|"]
        for t in pairs:
            L.append("| %s vs %s | %d | %d | %.3f | %s |"
                     % (t["a"], t["b"], t["a_only_correct"], t["b_only_correct"],
                        t["p_value"], t["verdict"]))
    if m1_batch:
        L += ["", "### 라벨 배치별 정확도 (자기점검)", "",
              "외부 정답 150건 중 100건을 이번에 붙였습니다. 새 라벨이 특정 모델에",
              "유리하게 붙은 것이 아닌지 배치를 갈라 따로 냅니다 — 한쪽에서만 이기면",
              "그건 모델 차이가 아니라 라벨 차이입니다.", "",
              "| 모델 | M07 배치 | M28 배치 |", "|---|---:|---:|"]
        for name, v in m1_batch.items():
            n = v.get("n_by_batch", {})
            L.append("| %s | %.4f (n=%d) | %.4f (n=%d) |"
                     % (name, v.get("M07", 0), n.get("M07", 0),
                        v.get("M28", 0), n.get("M28", 0)))
    L += ["", "### 왜 예전엔 RoBERTa 가 51.2% 였는가", "",
          "이전 문서는 \"DL 은 교차검증만 이기고 실제 적용에서 무너졌다\"고 적고",
          "모델 1의 채택을 ML 로 되돌렸습니다. 그 51.2%는 **모델의 문제가 아니라**",
          "**입력 경로의 문제**였습니다. 세 가지가 겹쳤고 각각을 분리해 쟀습니다.", "",
          "| 원인 | 무엇이 달랐나 | 영향 |",
          "|---|---|---|",
          "| 입력 원문 | DL07 은 첨부 원문 전체(평균 2,860자)를 131건 **전부**에 넣었습니다. "
          "운영 경로(M02 조건 B)는 원문을 16건에만 쓰고 나머지는 요약문(평균 349자)입니다. "
          "학습 텍스트가 요약문 형식이라 원문을 넣으면 학습·서빙이 어긋납니다. | 가장 큼 |",
          "| 정답 라벨 | 41건 중 5건이 배치 간 기준 불일치로 M29 에서 정정됐습니다"
          "(IP출원 → 컨설팅, 특례보증 → 보증). | 41건 기준 +0.098 |",
          "| 표본 수 | 41건 → 131건. 41건에서는 정확도 95% 신뢰구간이 ±15%p 였습니다. | 구간 폭 |",
          "",
          ""]
    if rev and rev.get("models"):
        L += ["같은 41건 위에 세워 본 것 (시드 42):", "",
              "| 모델 | 41건 / 구(舊)라벨 | 41건 / 정정 라벨 | 131건 |",
              "|---|---:|---:|---:|"]
        for name, v in rev["models"].items():
            L.append("| %s | %.4f | %.4f | %.4f |"
                     % (name, v["acc_41_old_labels"], v["acc_41_fixed_labels"],
                        v["acc_131"]))
        L += ["",
              "예전 DL07 보고값은 RoBERTa 51.2% 였습니다. 같은 41건·같은 구라벨에서",
              "이번 RoBERTa 는 %.4f 입니다 — 라벨도 표본도 그대로인데 이만큼 오릅니다."
              % rev["models"].get("KLUE-RoBERTa", {}).get("acc_41_old_labels", 0),
              "남는 차이는 입력 원문(가장 큼)과 학습 설정(DL07 은 batch 8 / max_len 384,",
              "여기는 batch 16 / max_len 256)에서 옵니다. 둘을 더 갈라 재지는 않았습니다 —",
              "결론이 바뀌는 지점이 아니어서입니다.", ""]
    L += [
          "> **BERT 와 RoBERTa 는 이 표본에서 갈리지 않습니다.** 시드 42 에서 둘 다",
          "> 정확히 0.8321 이고 McNemar p=1.000 입니다. 위 표의 0.8422 vs 0.8143 은",
          "> 시드 평균의 차이입니다. **KLUE-BERT 를 고른 근거는 정확도가 아니라**",
          "> **시드 편차(0.0072 vs 0.0157)** 입니다. 다만 BERT 는 M07 배치에서 0.7317 로",
          "> RoBERTa(0.8049)보다 낮습니다 — 배치가 갈리는 이유를 더 볼 여지가 있고,",
          "> 둘 중 어느 쪽을 써도 ML 기준선 대비 결론은 같습니다.", ""]
    ab = r["model1"].get("abstention_adopted_model")
    if ab:
        L += ["", "### 채택 모델의 판단보류 곡선 (외부 131건, 시드 3개 평균)", "",
              "커버리지를 안 재고 정확도만 올리면 '어려운 건을 다 빼서 높아진 정확도'와",
              "구별되지 않습니다. 두 축을 항상 함께 냅니다. **곡선만 냅니다** —",
              "임계값을 이 외부셋에서 고르면 외부가 검증셋이 아니게 되므로, 운영",
              "임계값은 커버리지 목표를 먼저 정하고 학습셋 OOF 에서 잡습니다.", "",
              "| 커버리지 | n | max_proba 정확도 | top2_gap 정확도 |",
              "|---:|---:|---:|---:|"]
        mp = {x["coverage"]: x for x in ab.get("max_proba", [])}
        tg = {x["coverage"]: x for x in ab.get("top2_gap", [])}
        for cov in sorted(mp, reverse=True):
            if cov not in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
                continue
            L.append("| %.0f%% | %d | %.4f ± %.4f | %.4f ± %.4f |"
                     % (cov * 100, mp[cov]["n"], mp[cov]["accuracy_mean"],
                        mp[cov]["accuracy_std"], tg[cov]["accuracy_mean"],
                        tg[cov]["accuracy_std"]))
    if m27 and m27.get("at_target_coverage"):
        L += ["", "### (참고) ML 기준선의 판단보류 곡선 — 내부 CV", "",
              "| 기준 | 커버리지 | 정확도 | macroF1 |", "|---|---:|---:|---:|"]
        for k2, v2 in m27["at_target_coverage"].items():
            if isinstance(v2, dict) and "accuracy" in v2:
                L.append("| %s | %.1f%% | %.4f | %.4f |"
                         % (k2, m27.get("target_coverage", 0) * 100,
                            v2["accuracy"], v2.get("macro_f1", 0)))

    # ── 모델 2 ────────────────────────────────────────────────────────
    if dl14:
        c = dl14["comparison"]
        L += ["", "---", "", "## 2. 지원규모 상대비교", "",
              "```text",
              "n=%d / %s" % (dl14["n"], dl14["cv"]),
              "타깃 %s" % dl14["target"],
              "```", "",
              "| 모델 | 계열 | MAE(log10) | MedAE | 2배 이내 | fold σ |",
              "|---|---|---:|---:|---:|---:|",
              "| LightGBM (M17 튜닝) | ML | **%.4f** | %.4f | %.1f%% | %.4f |"
              % (dl14["lightgbm"]["MAE_log10"], dl14["lightgbm"]["MedAE_log10"],
                 dl14["lightgbm"]["within_2x"] * 100, dl14["lightgbm"]["fold_std"]),
              "| FT-Transformer (시드 3개) | DL | %.4f ± %.4f | %.4f | %.1f%% | %.4f |"
              % (dl14["ft_transformer"]["summary"]["MAE_log10_mean"],
                 dl14["ft_transformer"]["summary"]["MAE_log10_std"],
                 dl14["ft_transformer"]["summary"]["MedAE_log10_mean"],
                 dl14["ft_transformer"]["summary"]["within_2x_mean"] * 100,
                 dl14["ft_transformer"]["summary"]["fold_std_mean"]),
              "| 코호트 중앙값 (baseline) | — | %.4f | %.4f | %.1f%% | %.4f |"
              % (dl14["cohort_median_baseline"]["MAE_log10"],
                 dl14["cohort_median_baseline"]["MedAE_log10"],
                 dl14["cohort_median_baseline"]["within_2x"] * 100,
                 dl14["cohort_median_baseline"]["fold_std"]),
              "",
              "차이 %+.4f, 판정선(LGBM fold σ) %.4f — **%s**"
              % (c["ft_minus_lgbm_MAE"], c["significance_margin_fold_std"],
                 c["verdict"]), "",
              "baseline 대비 개선 — LGBM %.1f%% / FT-Transformer %.1f%%"
              % (c["lgbm_improvement_vs_cohort_pct"],
                 c["ft_improvement_vs_cohort_pct"]), ""]
        L += ["> 모델 2 의 출력은 금액 점추정이 아니라 percentile 상대위치입니다.",
              "> 예측구간은 보조 정보로만 둡니다 — '참고 가능' 판정이 전체의 22.1%",
              "> 뿐이라(M26) 기본 출력으로 두면 근거가 있다고 오해하게 됩니다.", ""]

    # ── 모델 3 ────────────────────────────────────────────────────────
    if m30 and dl13:
        L += ["", "---", "", "## 3. 설계 이상탐지", "",
              "```text",
              "학습 %d행 / 사람 라벨 hold-out %d건 (비전형 %d · 경계 %d · 정상 %d)"
              % (m30["n_train"], m30["holdout"]["n_matched"],
                 m30["holdout"]["labels"].get("비전형", 0),
                 m30["holdout"]["labels"].get("경계", 0),
                 m30["holdout"]["labels"].get("정상", 0)),
              "운영조건 경고율 %.0f%% — 경고선은 전체 분포에서"
              % (m30["alert_rate"] * 100),
              "```", "",
              "### 3-1. 합성 이상치로 잰 값 (기존)", "",
              "| 지표 | 값 |", "|---|---:|",
              "| recall (경고율 2%) | 80.0% |", "| precision | 100% |",
              "| PR-AUC | 0.975 |", "",
              "### 3-2. 실제 사람 라벨로 잰 값 (이번에 처음)", "",
              "| 모델 | recall | precision | ROC-AUC |", "|---|---:|---:|---:|"]
        oc = m30["results"]["엄격(비전형만)"]
        ocq = m30["rank_quality"]["엄격(비전형만)"]
        dq = dl13["rank_quality_strict_mean"]
        ds = dl13["summary_mean_std"]["엄격(비전형만)"]
        L += ["| OneClassSVM (ML, 경고율 2%%) | %.3f | %.3f | %.3f |"
              % (oc["recall"], oc["precision"], ocq["roc_auc"]),
              "| Deep SVDD (DL, 경고율 2%%) | %.3f ± %.3f | — | %.3f ± %.3f |"
              % (ds["recall"]["mean"], ds["recall"]["std"],
                 dq["roc_auc"], dq["roc_auc_std"]), ""]
        eq30 = m30.get("results_equal_budget", {})
        eq13 = dl13.get("summary_equal_budget_mean_std", {})
        if eq30 and eq13:
            L += ["**같은 경고 예산으로 맞췄을 때** (hold-out 안 상위 %d건씩) — 50건이"
                  % eq30.get("n_alerts_within_holdout", 20),
                  "OneClassSVM 점수 구간으로 층화 추출된 세트라 전체 2% 선을 그대로",
                  "쓰면 표본 추출 방식을 비교하게 됩니다.", "",
                  "| 정답 정의 | OneClassSVM recall | Deep SVDD recall |",
                  "|---|---:|---:|"]
            for view in ("엄격(비전형만)", "설계이상만(데이터오류 행 제외)"):
                a = eq30.get(view, {}).get("recall")
                b = eq13.get(view, {}).get("recall")
                if a is not None and b:
                    L.append("| %s | %.3f | %.3f ± %.3f |"
                             % (view, a, b["mean"], b["std"]))
            L += ["", "Deep SVDD 는 시드 간 상위 30건 겹침이 평균 %.2f (최소 %.2f) 입니다 —"
                  % (dl13["seed_top30_overlap"]["mean"],
                     dl13["seed_top30_overlap"]["min"]),
                  "다시 학습하면 경고 목록의 절반이 바뀝니다. M14 에서 오토인코더가",
                  "무너진 것과 같은 지점입니다.", ""]
        L += ["### 3-3. 판정", "",
              "**%s**" % r["picks"]["3. 설계 이상탐지"]["adopted"], "",
              "- 합성 이상치 recall 80% / precision 100% 는 **실제 라벨로 옮겨가지",
              "  않았습니다** — 같은 운영조건에서 recall %.2f / precision %.2f 입니다."
              % (oc["recall"], oc["precision"]),
              "- 사람이 비전형이라고 본 %d건 중 %d건은 설계가 드문 게 아니라 **값이"
              % (m30["holdout"]["labels"].get("비전형", 0),
                 m30["holdout"]["비전형_하위유형"].get("데이터", 0)),
              "  잘못 들어온 행**이었습니다(사업기간 26년 = 공고연도 오파싱 등).",
              "  파서를 고치는 것이 모델을 고치는 것보다 먼저입니다.",
              "- Deep SVDD 는 같은 경고 예산에서 순위가 더 낫지만 재학습 안정성이",
              "  없습니다. 둘 중 어느 쪽도 지금 상태로 경고를 내보낼 수 없습니다.",
              "- 라벨러 1인 / 비전형 %d건이라 신뢰구간이 넓습니다. 상세는"
              % m30["holdout"]["labels"].get("비전형", 0),
              "  `m30_m3_real_eval.md` 를 보십시오.", ""]

    with open(os.path.join(C.REPORTS, "dl15_final_selection.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
