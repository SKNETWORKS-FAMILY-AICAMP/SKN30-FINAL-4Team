"""M06 — 지원 성격 분류 (중분류를 대분류로 묶어서 재학습).

배경: "융자를 지원해줄지 연구비를 지원해줄지" 같은 지원 방식 분류가 목적이다.
이건 업종(식품/ICT/로봇 등 산업분야)이 아니라 중분류(연구개발/융자/보증/
사업화/컨설팅 등)의 역할이다. 업종 분류는 이 목적에 직접 관련이 없어 제외한다.

중분류 원본 61종은 "사업화(일반)/사업화(콘텐츠)/사업화(기술)/사업화(SW·서비스)/
사업화(수출)"처럼 괄호 안 세부 유형까지 쪼개져 있어 지나치게 세분화됐다.
괄호 앞 대분류만 남기면 지원 성격 단위로 자연스럽게 묶인다.

원천은 2023 중앙부처 엑셀 909건뿐이다(기업마당 두 원천에는 이 라벨이 없음).
"""
import re
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.decomposition import TruncatedSVD
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score)
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import LinearSVC

from common import PROC, save_report

warnings.filterwarnings("ignore")
TAX = PROC + "/business_taxonomy.parquet"
MIN_SUPPORT = 3   # 대분류로 묶었으니 임계값을 조금 올린다


def coarsen(v):
    """'사업화(일반)' -> '사업화'. 복수라벨은 첫 번째를 대표값으로 채택.

    '설비(스마트, 저감)'처럼 괄호 안에 콤마가 있는 값이 있어, 먼저 괄호를
    제거한 뒤 콤마로 나눠야 한다(순서를 바꾸면 괄호 안 콤마에서 잘못 잘린다).
    """
    if not isinstance(v, str) or not v.strip():
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = stripped.split(",")[0].strip()
    return first if first else None


def tfidf():
    return TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                           min_df=2, sublinear_tf=True, max_features=60000)


def models(seed):
    from lightgbm import LGBMClassifier
    from xgboost import XGBClassifier
    from catboost import CatBoostClassifier
    svd = lambda: TruncatedSVD(n_components=150, random_state=seed)
    return {
        "Majority(하한선)": Pipeline([("t", tfidf()),
                                     ("m", DummyClassifier(strategy="most_frequent"))]),
        "TFIDF+LogisticRegression": Pipeline([("t", tfidf()), ("m", LogisticRegression(
            max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))]),
        "TFIDF+LinearSVM": Pipeline([("t", tfidf()), ("m", LinearSVC(
            C=1.0, class_weight="balanced", random_state=seed))]),
        "SVD+RandomForest": Pipeline([("t", tfidf()), ("s", svd()), ("m", RandomForestClassifier(
            n_estimators=500, class_weight="balanced_subsample", n_jobs=-1, random_state=seed))]),
        "SVD+XGBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.1, subsample=0.9,
            colsample_bytree=0.9, tree_method="hist", n_jobs=-1,
            random_state=seed, verbosity=0))]),
        "SVD+LightGBM": Pipeline([("t", tfidf()), ("s", svd()), ("m", LGBMClassifier(
            n_estimators=300, learning_rate=0.1, num_leaves=15,
            min_child_samples=1, min_split_gain=0.0, n_jobs=-1,
            random_state=seed, verbose=-1))]),
        "SVD+CatBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", CatBoostClassifier(
            iterations=300, depth=5, learning_rate=0.1, verbose=0,
            random_seed=seed, allow_writing_files=False))]),
    }


def evaluate(X, y_raw, folds, seed):
    y = LabelEncoder().fit_transform(y_raw)
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    out, best_pred = {}, None
    for name, mk in models(seed).items():
        pred = np.zeros(len(y), dtype=int)
        for tr, te in skf.split(X, y):
            m = clone(mk)
            m.fit(X[tr], y[tr])
            # CatBoostClassifier.predict는 (n,1) 형태를 반환해 평탄화가 필요하다
            pred[te] = np.asarray(m.predict(X[te])).ravel().astype(int)
        out[name] = {
            "accuracy": round(float(accuracy_score(y, pred)), 4),
            "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
            "weighted_f1": round(float(f1_score(y, pred, average="weighted", zero_division=0)), 4),
        }
        print("  %-26s acc %.4f  macroF1 %.4f  wF1 %.4f"
              % (name, out[name]["accuracy"], out[name]["macro_f1"], out[name]["weighted_f1"]),
              flush=True)
        if name == max(out, key=lambda k: out[k]["macro_f1"]):
            best_pred = pred
    return out, y, best_pred


def main():
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)

    vc_before = t["middle_category"].value_counts(dropna=False)
    vc_after = t["support_type"].value_counts(dropna=False)
    print("중분류 원본 %d종 -> 지원성격 %d종으로 축소" % (t["middle_category"].nunique(), t["support_type"].nunique()))
    print()
    print("지원성격 분포:")
    for k, v in vc_after.items():
        print("  %-16s%4d건 (%.1f%%)" % (k, v, v / len(t) * 100))
    print()

    sub = t.dropna(subset=["support_type"]).copy()
    keep_vc = sub["support_type"].value_counts()
    keep = keep_vc[keep_vc >= MIN_SUPPORT].index
    n_excluded = int((~sub["support_type"].isin(keep)).sum())
    sub = sub[sub["support_type"].isin(keep)]

    X = sub["text_for_model"].fillna("").astype(str).values
    y_raw = sub["support_type"].values
    print("학습 대상: %d클래스 / %d건 (지원>=%d, 제외 %d건)"
          % (len(keep), len(sub), MIN_SUPPORT, n_excluded))
    print()

    results, y_enc, best_pred = evaluate(X, y_raw, folds=5, seed=42)
    best_name = max(results, key=lambda k: results[k]["macro_f1"])
    names = sorted(pd.Series(y_raw).unique())  # LabelEncoder alphabetical order matches sorted unique
    le_classes = LabelEncoder().fit(y_raw).classes_

    cm = confusion_matrix(y_enc, best_pred, labels=list(range(len(le_classes))))
    report = classification_report(y_enc, best_pred, target_names=le_classes,
                                   output_dict=True, zero_division=0)

    print("=" * 66)
    print("최종 채택: %s (macroF1 %.4f)" % (best_name, results[best_name]["macro_f1"]))
    print()
    print("혼동행렬 (행=실제, 열=예측)")
    header = "".join("%10s" % c[:8] for c in le_classes)
    print("%14s" % "" + header)
    for i, c in enumerate(le_classes):
        print("%14s" % c[:12] + "".join("%10d" % v for v in cm[i]))
    print()
    print(classification_report(y_enc, best_pred, target_names=le_classes,
                                digits=3, zero_division=0))

    save_report("m06_support_type.json", {
        "source": "2023 중앙부처 엑셀 909건 (기업마당 두 원천에는 이 라벨 없음)",
        "regroup_rule": "괄호 앞 대분류만 유지, 복수라벨은 첫 값 채택",
        "classes_before": int(t["middle_category"].nunique()),
        "classes_after": int(t["support_type"].nunique()),
        "class_dist_before": vc_before.to_dict(),
        "class_dist_after": vc_after.to_dict(),
        "min_support": MIN_SUPPORT,
        "classes_evaluated": int(len(keep)),
        "rows_evaluated": int(len(sub)),
        "excluded_rows": n_excluded,
        "folds": 5, "seed": 42,
        "split": "Stratified 5-Fold CV, TFIDF는 Pipeline으로 fold train에만 fit",
        "results": results,
        "best_model": best_name, "best_macro_f1": results[best_name]["macro_f1"],
        "confusion_matrix": cm.tolist(), "class_order": le_classes.tolist(),
        "per_class": {k: v for k, v in report.items() if k in le_classes},
        "macro_avg": report["macro avg"], "weighted_avg": report["weighted avg"],
    })


if __name__ == "__main__":
    main()
