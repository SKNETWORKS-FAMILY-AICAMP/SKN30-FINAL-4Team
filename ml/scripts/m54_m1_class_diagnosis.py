"""M54 — 모델 1 클래스별 진단. 새 모델을 찾지 않는다.

방향서 §11 의 4순위: "새 모델 탐색보다 class-level confusion / recall /
low confidence / 실제 서비스 input consistency 를 먼저 보라."

그래서 이 스크립트는 **아무것도 학습하지 않는다.** 이미 저장된 산출물만 읽는다.

    reports/dl12_m1_candidates_dl.json   외부 131건 정답(gold)과 세 DL 후보의 예측
    reports/dl16_m1_abstention.json      판단보류 커버리지 곡선(KLUE-BERT)
    reports/m03_input_alignment.json     학습 입력 vs 서비스 입력 정합 실험
    data/processed/m1_dl_bundle/*.parquet 학습 1,404건 / 외부 131건(라벨 확신도 포함)

M29 는 같은 131건에 대해 ML(LinearSVM) 의 클래스별 표를 이미 남겼다. 여기서는
**채택 모델인 KLUE-BERT** 쪽을 같은 형식으로 만들어 나란히 둔다. 채택 모델의
클래스별 약점을 모르면 "정확도 0.8422"가 어느 클래스에서 나는 오차인지 알 수
없고, 그 클래스가 곧 모델 2·3 의 비교군을 잘못 잡는 지점이 된다.

한계 두 가지를 먼저 적는다.

    1 dl12 의 external_pred 는 시드 하나의 예측이다. 정확도는 3시드 평균
      0.8422 ± 0.0072 로 보고돼 있고, 아래 클래스별 표는 그 중 한 시드다.
      클래스별 수치는 ±1건에 크게 흔들린다(정답 1~2건짜리 클래스가 있다).
    2 외부 131건에 아예 없는 클래스가 4종(교육훈련·수출통관·해외수주·실증·
      해외인증) 이라 그 클래스의 외부 성능은 측정되지 않았다.
"""
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

BUNDLE = os.path.join(C.PROC, "m1_dl_bundle")
ADOPTED = "KLUE-BERT"


def load():
    with open(os.path.join(C.REPORTS, "dl12_m1_candidates_dl.json"), encoding="utf-8") as f:
        dl12 = json.load(f)
    with open(os.path.join(C.REPORTS, "dl16_m1_abstention.json"), encoding="utf-8") as f:
        dl16 = json.load(f)
    with open(os.path.join(C.REPORTS, "m03_input_alignment.json"), encoding="utf-8") as f:
        m03 = json.load(f)
    tr = pd.read_parquet(os.path.join(BUNDLE, "train.parquet"))
    ex = pd.read_parquet(os.path.join(BUNDLE, "external.parquet"))
    return dl12, dl16, m03, tr, ex


def per_class(gold, pred, train_counts):
    """클래스별 재현율·정밀도. 학습 표본 수를 같은 줄에 붙인다 —
    낮은 재현율이 모델 문제인지 표본 문제인지 한 줄에서 보이게."""
    rows = []
    for c in sorted(set(gold), key=lambda k: -Counter(gold)[k]):
        n_gold = sum(1 for g in gold if g == c)
        n_pred = sum(1 for p in pred if p == c)
        tp = sum(1 for gd, p in zip(gold, pred) if gd == c and p == c)
        rows.append({"클래스": c, "학습표본": int(train_counts.get(c, 0)),
                     "외부정답수": n_gold, "재현율": round(tp / n_gold, 2) if n_gold else None,
                     "예측수": n_pred,
                     "정밀도": round(tp / n_pred, 2) if n_pred else None})
    return pd.DataFrame(rows)


def flows(gold, pred, k=10):
    c = Counter((g, p) for g, p in zip(gold, pred) if g != p)
    return [{"정답": g, "예측": p, "건수": n} for (g, p), n in c.most_common(k)]


def main():
    dl12, dl16, m03, tr, ex = load()
    gold = dl12["gold"]
    train_counts = tr["label"].value_counts().to_dict()
    preds = {k: v["external_pred"] for k, v in dl12["results"].items()}
    pred = preds[ADOPTED]

    print("== 대상")
    print("   채택 모델 %s / 외부 %d건 / 학습 %d건 %d클래스"
          % (ADOPTED, len(gold), len(tr), tr["label"].nunique()))
    acc = {k: round(float(np.mean([a == b for a, b in zip(gold, v)])), 4)
           for k, v in preds.items()}
    print("   이 시드 정확도: " + " / ".join("%s %.4f" % (k, v) for k, v in acc.items()))
    print("   (공표치는 3시드 평균 — KLUE-BERT 0.8422 ± 0.0072)")

    print("\n== 1. 클래스별 재현율·정밀도 (%s)" % ADOPTED)
    pc = per_class(gold, pred, train_counts)
    for _, r in pc.iterrows():
        print("   %-10s 학습%4d  정답%3d  재현율 %s  예측%3d  정밀도 %s"
              % (r["클래스"], r["학습표본"], r["외부정답수"],
                 "  - " if pd.isna(r["재현율"]) else "%.2f" % r["재현율"],
                 r["예측수"],
                 "  - " if pd.isna(r["정밀도"]) else "%.2f" % r["정밀도"]))

    weak = pc[(pc["외부정답수"] >= 5) & (pc["재현율"] < 0.7)]
    print("\n   재현율 0.7 미만 (정답 5건 이상): "
          + (", ".join("%s %.2f" % (r["클래스"], r["재현율"]) for _, r in weak.iterrows())
             or "없음"))

    print("\n== 2. 오답 흐름 (정답 → 예측)")
    fl = flows(gold, pred)
    for r in fl:
        print("   %-10s -> %-10s %d건" % (r["정답"], r["예측"], r["건수"]))

    print("\n== 3. 라벨 확신도별 정확도 (외부셋 라벨러가 남긴 등급)")
    ok = np.array([g == p for g, p in zip(gold, pred)])
    conf = ex["confidence"].fillna("미기재").to_numpy()
    conf_tbl = []
    for c in pd.unique(conf):
        m = conf == c
        conf_tbl.append({"라벨확신도": str(c), "n": int(m.sum()),
                         "정확도": round(float(ok[m].mean()), 4)})
        print("   %-6s n=%3d  정확도 %.4f" % (c, m.sum(), ok[m].mean()))
    print("   → 확신도가 낮은 건에서 정확도가 낮다면 그 오차의 일부는 모델이 아니라"
          " 정답셋 쪽 불확실성이다.")

    print("\n== 4. 원문 확보 여부별 정확도")
    doc_tbl = []
    for v in (True, False):
        m = ex["has_doc"].to_numpy() == v
        if not m.sum():
            continue
        doc_tbl.append({"첨부원문": bool(v), "n": int(m.sum()),
                        "정확도": round(float(ok[m].mean()), 4)})
        print("   원문 %-5s n=%3d  정확도 %.4f" % ("있음" if v else "없음", m.sum(),
                                                ok[m].mean()))

    print("\n== 5. 판단보류 커버리지 곡선 (dl16, 3시드 평균)")
    cov = dl16.get("mean_by_coverage", {})
    cov_rows = list(cov.get("max_proba", [])) if isinstance(cov, dict) else []
    for r in cov_rows:
        print("   커버리지 %3.0f%%  n=%3d  정확도 %.4f ± %.4f  (임계 %.3f)"
              % (r["coverage"] * 100, r["n"], r["accuracy_mean"],
                 r["accuracy_std"], r["threshold_mean"]))
    print("   ※ dl16 caveat — 임계값을 이 외부셋에서 고르면 외부가 검증셋이 아니게 된다.")

    print("\n== 6. 학습 입력 vs 서비스 입력 정합 (M03 재인용)")
    for k, v in m03.get("conditions", {}).items():
        print("   %-38s 판단보류율 %.3f  예측분포 TVD %.3f  예측클래스 %d종"
              % (k[:38], v.get("hold_rate", 0), v.get("pred_dist_tvd_vs_train", 0),
                 v.get("n_pred_classes", 0)))

    C.save_report("m54_m1_class_diagnosis.json", {
        "note": "학습 없음. 저장된 산출물(dl12/dl16/m03/m1_dl_bundle)만 재집계",
        "adopted": ADOPTED,
        "seed_accuracy_this_pred": acc,
        "published_accuracy": "0.8422 ± 0.0072 (3시드 평균)",
        "per_class": pc.to_dict("records"),
        "weak_recall": weak.to_dict("records"),
        "confusion_flows": fl,
        "by_label_confidence": conf_tbl,
        "by_has_doc": doc_tbl,
        "coverage_curve": cov_rows,
        "input_alignment": {k: {"hold_rate": v.get("hold_rate"),
                                "tvd_vs_train": v.get("pred_dist_tvd_vs_train"),
                                "n_pred_classes": v.get("n_pred_classes")}
                            for k, v in m03.get("conditions", {}).items()},
        "limits": ["external_pred 는 시드 1개",
                   "외부 131건에 없는 클래스 4종은 미측정"],
    })
    write_md(pc, weak, fl, conf_tbl, doc_tbl, cov_rows, m03, acc, gold, tr)


def write_md(pc, weak, fl, conf_tbl, doc_tbl, cov_rows, m03, acc, gold, tr):
    L = ["# M54 — 모델 1 클래스별 진단", "",
         "> 학습을 새로 하지 않는다. dl12/dl16/m03 과 학습·외부 bundle 을 다시",
         "> 집계했을 뿐이다. 방향서 §11 4순위(새 모델 탐색보다 클래스별 약점 먼저).", "",
         "채택 모델 **KLUE-BERT** / 외부 %d건 / 학습 %d건 %d클래스."
         % (len(gold), len(tr), tr["label"].nunique()), "",
         "이 문서의 클래스별 표는 **시드 하나의 예측**(이 시드 정확도 %.4f)에서 나온다."
         % acc["KLUE-BERT"],
         "공표 정확도 0.8422 ± 0.0072 는 3시드 평균이다. 정답이 1~2건인 클래스는",
         "±1건에 재현율이 통째로 바뀌므로 순위로만 읽는다.", "",
         "## 1. 클래스별 재현율·정밀도", "",
         "학습 표본 수를 같은 줄에 붙였다 — 낮은 재현율이 모델 문제인지 표본",
         "문제인지 한 줄에서 갈리게 하려는 것이다.", "",
         "| 클래스 | 학습표본 | 외부정답 | 재현율 | 예측수 | 정밀도 |",
         "|---|---:|---:|---:|---:|---:|"]
    for _, r in pc.iterrows():
        L.append("| %s | %d | %d | %s | %d | %s |"
                 % (r["클래스"], r["학습표본"], r["외부정답수"],
                    "—" if pd.isna(r["재현율"]) else "%.2f" % r["재현율"],
                    r["예측수"],
                    "—" if pd.isna(r["정밀도"]) else "%.2f" % r["정밀도"]))
    L += ["", "재현율 0.7 미만(정답 5건 이상): **"
          + (", ".join("%s %.2f" % (r["클래스"], r["재현율"]) for _, r in weak.iterrows())
             or "없음") + "**", "",
          "## 2. 오답 흐름", "",
          "| 정답 | 예측 | 건수 |", "|---|---|---:|"]
    for r in fl:
        L.append("| %s | %s | %d |" % (r["정답"], r["예측"], r["건수"]))

    L += ["", "## 3. 정답셋 쪽 불확실성", "",
          "외부 131건은 라벨러가 확신도(높음/보통/중간/낮음)를 함께 남겼다.",
          "확신도가 낮은 건에서 정확도가 낮다면 그 오차의 일부는 모델이 아니라",
          "**정답셋의 모호성**이다.", "",
          "| 라벨 확신도 | n | 정확도 |", "|---|---:|---:|"]
    for r in conf_tbl:
        L.append("| %s | %d | %.4f |" % (r["라벨확신도"], r["n"], r["정확도"]))
    L += ["", "| 첨부 원문 | n | 정확도 |", "|---|---:|---:|"]
    for r in doc_tbl:
        L.append("| %s | %d | %.4f |" % ("있음" if r["첨부원문"] else "없음",
                                        r["n"], r["정확도"]))

    if cov_rows:
        L += ["", "## 4. 판단보류 커버리지 곡선 (dl16, 3시드 평균)", "",
              "| 커버리지 | n | 정확도 | 표준편차 | 임계값 |",
              "|---:|---:|---:|---:|---:|"]
        for r in cov_rows:
            L.append("| %.0f%% | %d | %.4f | %.4f | %.3f |"
                     % (r["coverage"] * 100, r["n"], r["accuracy_mean"],
                        r["accuracy_std"], r["threshold_mean"]))
        L += ["", "> dl16 의 단서를 그대로 옮긴다 — 임계값을 이 외부셋에서 고르면",
              "> 외부셋이 더 이상 검증셋이 아니다. 운영 임계값은 커버리지 목표를",
              "> 먼저 정하고 학습셋 OOF 에서 잡는다.", ""]

    L += ["", "## 5. 학습 입력 vs 서비스 입력 (M03 재인용)", "",
          "| 조건 | 판단보류율 | 예측분포 TVD(학습 대비) | 예측 클래스 수 |",
          "|---|---:|---:|---:|"]
    for k, v in m03.get("conditions", {}).items():
        L.append("| %s | %.3f | %.3f | %d |"
                 % (k, v.get("hold_rate", 0), v.get("pred_dist_tvd_vs_train", 0),
                    v.get("n_pred_classes", 0)))

    hi = [r for r in conf_tbl if r["라벨확신도"] == "높음"]
    n_hi = hi[0]["n"] if hi else 0
    acc_hi = hi[0]["정확도"] if hi else 0.0
    err_hi = round(n_hi * (1 - acc_hi))
    n_rest = sum(r["n"] for r in conf_tbl) - n_hi
    err_rest = sum(round(r["n"] * (1 - r["정확도"])) for r in conf_tbl
                   if r["라벨확신도"] != "높음")
    biz = pc[pc["클래스"] == "사업화"]
    L += ["", "## 6. 읽은 것", "",
          "**① 오차가 다수 클래스로 빨려 들어간다.** 학습 %d건 중 사업화가 %d건"
          % (len(tr), int(tr["label"].value_counts().get("사업화", 0))),
          "(%.0f%%)으로 가장 많고, 외부셋에서도 사업화 예측이 정답 수보다 크게"
          % (tr["label"].value_counts().get("사업화", 0) / len(tr) * 100)]
    if len(biz):
        b = biz.iloc[0]
        L += ["많다(정답 %d건 · 예측 %d건 · 정밀도 %.2f). 오답 흐름 상위도 대부분"
              % (b["외부정답수"], b["예측수"], b["정밀도"]),
              "`→ 사업화`다. 재현율이 낮은 소수 클래스를 따로 고치는 것보다",
              "**다수 클래스 쏠림 자체**가 먼저다.", ""]
    L += ["**② 오차의 대부분이 정답셋이 애매했던 건에 있다.** 라벨 확신도 '높음'"
          " %d건에서는 틀린 것이 약 %d건뿐이고," % (n_hi, err_hi),
          "나머지 %d건에서 약 %d건이 틀렸다. 정확도 0.8422 는 **모델의 한계와"
          % (n_rest, err_rest),
          "정답셋의 모호성이 섞인 숫자**이며, 뒤쪽은 모델을 바꿔서 줄지 않는다.",
          "",
          "**③ 그래서 판단보류가 성능표보다 중요하다.** 커버리지를 줄이면 정확도가",
          "단조롭게 오른다(4장). 모델 1의 오류는 모델 2·3 에서 **비교군을 통째로",
          "잘못 잡는 것**으로 이어지므로, 애매한 건은 분류를 강행하는 것보다",
          "담당자에게 넘기는 쪽이 파이프라인 전체에서 싸다.",
          "",
          "**④ 서비스 입력 정합은 이미 재봤다(5장).** 정합 조건(E1)이 예측 분포를",
          "학습 분포에 더 가깝게(TVD 0.299 → 0.272) 만들고 예측 클래스 수도",
          "15종 → 17종으로 늘린다. 판단보류율은 거의 같으므로 **정확도가 아니라",
          "분포 왜곡을 줄이는 쪽**의 이득이다.", ""]

    p = os.path.join(C.REPORTS, "m54_m1_class_diagnosis.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
