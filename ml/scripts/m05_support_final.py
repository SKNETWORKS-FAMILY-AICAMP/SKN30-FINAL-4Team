"""M05 — 지원규모 최종 모델: 등급 분류 + 텍스트 피처.

M03/M04에서 확인한 두 결론을 합친다.
  A안(M04): 점 추정 회귀보다 3등급 분류가 실용적이다.
            회귀는 10배 초과 오차가 12.2%인데 3등급은 두 칸 이상 오차가 5%다.
  B안(M04): 메타데이터만으로는 부족하고 텍스트를 넣으면 개선된다.
            단 SBERT 임베딩(768차원)은 361행에 과하여 오히려 악화됐다.
            차원이 낮은 TF-IDF·SVD100이 낫다.

따라서 최종안은 '3등급 분류 + 메타 + TF-IDF·SVD100'이다.

분할은 GroupKFold(그룹=연도 제거 사업명). TF-IDF/SVD는 fold train에만 fit한다.
"""
import argparse
import warnings

import numpy as np
from sklearn.base import clone
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score)
from sklearn.model_selection import GroupKFold

from common import save_report
from m03_support_amount import group_key
from m04_support_v2 import FIXED_BINS, bracketize, clf_models, load_with_text, meta_matrix

warnings.filterwarnings("ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--svd", type=int, default=100)
    args = ap.parse_args()

    df = load_with_text()
    groups = df["title"].map(group_key).values
    Xm = meta_matrix(df)
    texts = df["text"].fillna("").astype(str).values
    y, names = bracketize(df["support_amount_max"].values.astype(float), FIXED_BINS)
    folds = list(GroupKFold(n_splits=args.folds).split(Xm, y, groups))

    print("대상 %d행 / 그룹 %d개" % (len(df), len(set(groups))))
    print("클래스 분포:", {names[i]: int((y == i).sum()) for i in range(len(names))})
    print()

    def run(mk, use_text):
        pred = np.zeros(len(y), dtype=int)
        for tr, te in folds:
            med = Xm.iloc[tr].median(numeric_only=True)
            a = Xm.iloc[tr].fillna(med).values
            b = Xm.iloc[te].fillna(med).values
            if use_text:
                # TF-IDF/SVD는 우리 데이터에 fit하므로 반드시 fold train에만
                tv = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                     min_df=3, sublinear_tf=True, max_features=40000)
                sv = TruncatedSVD(n_components=args.svd, random_state=args.seed)
                a = np.hstack([a, sv.fit_transform(tv.fit_transform(texts[tr]))])
                b = np.hstack([b, sv.transform(tv.transform(texts[te]))])
            m = clone(mk)
            m.fit(a, y[tr])
            pred[te] = np.asarray(m.predict(b)).ravel().astype(int)
        return pred

    results, preds = {}, {}
    for use_text, tag in [(False, "메타만"), (True, "메타+텍스트")]:
        print("=== 3등급 분류 · %s ===" % tag, flush=True)
        for nm, mk in clf_models(args.seed, len(names)).items():
            p = run(mk, use_text)
            key = "%s / %s" % (nm, tag)
            results[key] = {
                "accuracy": round(float(accuracy_score(y, p)), 4),
                "macro_f1": round(float(f1_score(y, p, average="macro", zero_division=0)), 4),
                "weighted_f1": round(float(f1_score(y, p, average="weighted", zero_division=0)), 4),
                "adjacent_accuracy": round(float(np.mean(np.abs(p - y) <= 1)), 4),
                "uses_text": use_text,
            }
            preds[key] = p
            print("  %-20s acc %.4f  macroF1 %.4f  인접포함 %.4f"
                  % (nm, results[key]["accuracy"], results[key]["macro_f1"],
                     results[key]["adjacent_accuracy"]), flush=True)
        print()

    best = max(results.items(), key=lambda kv: kv[1]["macro_f1"])
    bp = preds[best[0]]
    cm = confusion_matrix(y, bp, labels=list(range(len(names))))

    print("=" * 66)
    print("최종 채택: %s  (macroF1 %.4f)" % (best[0], best[1]["macro_f1"]))
    print()
    print("혼동행렬 (행=실제, 열=예측)")
    print("%14s" % "" + "".join("%14s" % n for n in names))
    for i, n in enumerate(names):
        print("%14s" % n + "".join("%14d" % v for v in cm[i]))
    print()
    print(classification_report(y, bp, target_names=names, digits=3, zero_division=0))

    rep = classification_report(y, bp, target_names=names, output_dict=True,
                                zero_division=0)
    save_report("m05_support_final.json", {
        "rows": len(df), "n_groups": len(set(groups)), "folds": args.folds,
        "split": "GroupKFold(그룹=연도 제거한 사업명)",
        "task": "지원규모 3등급 분류 (기업당 최대지원금)",
        "class_names": names,
        "class_dist": {names[i]: int((y == i).sum()) for i in range(len(names))},
        "svd_components": args.svd,
        "results": results,
        "best": best[0], "best_macro_f1": best[1]["macro_f1"],
        "confusion_matrix": cm.tolist(),
        "per_class": {k: v for k, v in rep.items() if k in names},
        "macro_avg": rep["macro avg"], "weighted_avg": rep["weighted avg"],
        "note": ("회귀(M03) 대비: 10배 초과 오차 12.2%% -> 두 칸 이상 오차 "
                 "%.1f%%. 텍스트 피처는 TF-IDF·SVD100 사용 — SBERT 768차원은 "
                 "361행에 과하여 M04에서 오히려 악화됐다."
                 % ((1 - best[1]["adjacent_accuracy"]) * 100)),
    })


if __name__ == "__main__":
    main()
