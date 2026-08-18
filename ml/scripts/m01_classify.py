"""M01 — 중분류/업종 분류 모델 비교 (CPU 티어).

대분류(8분야)는 기업마당이 이미 부여하므로 제외한다.
중분류(61종)·업종(65종)은 2023 중앙부처 엑셀에만 있는 라벨이라 예측 대상이다.

설계:
  - 표본 909건에 클래스가 많아 단일 분할은 분산이 커서 Stratified 5-Fold CV
  - 단일표본 클래스는 층화가 불가능하므로 지원>=2 클래스만 평가 대상
  - TF-IDF는 반드시 fold 안에서 fit (Pipeline) — 전체에 fit하면 누수
  - 누수 대조군: 메타 줄이 포함된 원문으로 학습한 결과를 함께 측정

GPU 티어(Sentence-BERT / KLUE-RoBERTa)는 m02에서 이어붙인다.
"""
import argparse
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.decomposition import TruncatedSVD
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from common import PROC, save_report

warnings.filterwarnings("ignore")
TAX = f"{PROC}/business_taxonomy.parquet"
MIN_SUPPORT = 2


def tfidf(**kw):
    """한국어는 형태소 분석기 없이도 char n-gram이 강하다."""
    return TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                           min_df=2, sublinear_tf=True, max_features=60000, **kw)


def build_models(seed):
    from lightgbm import LGBMClassifier
    from xgboost import XGBClassifier
    from catboost import CatBoostClassifier

    svd = lambda: TruncatedSVD(n_components=150, random_state=seed)
    return {
        "Majority(하한선)": Pipeline([("t", tfidf()), ("m", DummyClassifier(strategy="most_frequent"))]),
        "TFIDF+LogisticRegression": Pipeline([("t", tfidf()), ("m", LogisticRegression(
            max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))]),
        "TFIDF+LinearSVM": Pipeline([("t", tfidf()), ("m", LinearSVC(
            C=1.0, class_weight="balanced", random_state=seed))]),
        "SVD+RandomForest": Pipeline([("t", tfidf()), ("s", svd()), ("m", RandomForestClassifier(
            n_estimators=500, class_weight="balanced_subsample", n_jobs=-1, random_state=seed))]),
        # GBDT는 다중분류에서 반복 1회당 클래스 수만큼 트리를 만든다.
        # 49~56클래스 × 400 iterations는 fold당 2만 그루가 넘어 비현실적이라
        # 세 모델 모두 동일하게 150으로 낮췄다(공정 비교 유지).
        # 승자 확정 후 ablation에서 다시 올려 탐색한다.
        "SVD+XGBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", XGBClassifier(
            n_estimators=150, max_depth=4, learning_rate=0.15, subsample=0.9,
            colsample_bytree=0.9, tree_method="hist", n_jobs=-1,
            random_state=seed, verbosity=0))]),
        # min_child_samples 기본값(20)이나 5는 이 데이터에서 LightGBM을 무력화한다.
        # 클래스당 표본이 약 14개뿐이라 분할 조건을 못 넘겨 Majority 수준으로 붕괴한다
        # (macroF1 0.008). 1로 완화하면 0.234로 회복 — 모델 성질이 아니라 설정 문제였다.
        "SVD+LightGBM": Pipeline([("t", tfidf()), ("s", svd()), ("m", LGBMClassifier(
            n_estimators=300, learning_rate=0.1, num_leaves=15,
            min_child_samples=1, min_split_gain=0.0, min_child_weight=1e-5,
            colsample_bytree=0.7, n_jobs=-1, random_state=seed, verbose=-1))]),
        "SVD+CatBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", CatBoostClassifier(
            iterations=150, depth=4, learning_rate=0.15, verbose=0,
            thread_count=-1, random_seed=seed, allow_writing_files=False))]),
    }


def evaluate(X, y, models, folds, seed):
    # XGBoost는 0..K-1 정수 라벨만 받는다. 지표는 라벨 표현과 무관하므로 전역 인코딩으로 통일.
    from sklearn.preprocessing import LabelEncoder
    y = LabelEncoder().fit_transform(y)
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    out = {}
    import time
    for name, mk in models.items():
        accs, mf1s, wf1s = [], [], []
        t0 = time.time()
        for tr, te in skf.split(X, y):
            from sklearn.base import clone
            m = clone(mk)
            m.fit(X[tr], y[tr])
            p = m.predict(X[te])
            accs.append(accuracy_score(y[te], p))
            mf1s.append(f1_score(y[te], p, average="macro", zero_division=0))
            wf1s.append(f1_score(y[te], p, average="weighted", zero_division=0))
        out[name] = {
            "accuracy": round(float(np.mean(accs)), 4),
            "accuracy_std": round(float(np.std(accs)), 4),
            "macro_f1": round(float(np.mean(mf1s)), 4),
            "macro_f1_std": round(float(np.std(mf1s)), 4),
            "weighted_f1": round(float(np.mean(wf1s)), 4),
            "fit_seconds": round(time.time() - t0, 1),
        }
        print(f"    {name:<26} acc {out[name]['accuracy']:.4f}  "
              f"macroF1 {out[name]['macro_f1']:.4f}  wF1 {out[name]['weighted_f1']:.4f}"
              f"  [{out[name]['fit_seconds']:.0f}s]", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    t = pd.read_parquet(TAX)
    report = {"n_rows_source": len(t), "folds": args.folds, "seed": args.seed,
              "min_support": MIN_SUPPORT, "targets": {}}

    for target in ("middle_category", "industry"):
        sub = t.dropna(subset=[target]).copy()
        vc = sub[target].value_counts()
        keep = vc[vc >= MIN_SUPPORT].index
        sub = sub[sub[target].isin(keep)]
        y = sub[target].astype(str).values
        X_clean = sub["text_for_model"].fillna("").astype(str).values
        # 누수 대조군: 메타 줄(【사업개요】 대분류/중분류/업종...)을 그대로 포함
        X_leak = (sub["meta_line_leak"].fillna("") + "\n" + sub["text_for_model"].fillna("")).astype(str).values

        print(f"\n=== {target}: {len(keep)}클래스 / {len(sub)}건 "
              f"(원본 {t[target].nunique()}종 중 지원>={MIN_SUPPORT}) ===", flush=True)
        print("  [누수 제거본]", flush=True)
        clean = evaluate(X_clean, y, build_models(args.seed), args.folds, args.seed)
        print("  [누수 포함본 — 대조군]", flush=True)
        leak_models = {k: v for k, v in build_models(args.seed).items()
                       if k in ("TFIDF+LogisticRegression", "TFIDF+LinearSVM")}
        leaky = evaluate(X_leak, y, leak_models, args.folds, args.seed)

        best = max(clean.items(), key=lambda kv: kv[1]["macro_f1"])
        report["targets"][target] = {
            "classes_total": int(t[target].nunique()),
            "classes_evaluated": int(len(keep)),
            "rows_evaluated": int(len(sub)),
            "coverage": round(len(sub) / len(t.dropna(subset=[target])), 4),
            "singleton_classes_excluded": int((vc < MIN_SUPPORT).sum()),
            "majority_share": round(float(vc.iloc[0] / vc.sum()), 4),
            "clean": clean,
            "leaky_control": leaky,
            "best_model": best[0], "best_macro_f1": best[1]["macro_f1"],
        }

    save_report("m01_classify_cpu.json", report)
    print("\n" + "=" * 68)
    for tg, r in report["targets"].items():
        print(f"{tg}: 최고 {r['best_model']} (macroF1 {r['best_macro_f1']:.4f})")
        lk = r["leaky_control"].get("TFIDF+LogisticRegression", {})
        cl = r["clean"]["TFIDF+LogisticRegression"]
        if lk:
            print(f"  누수대조(LR): macroF1 {lk['macro_f1']:.4f} → 제거 후 {cl['macro_f1']:.4f} "
                  f"({cl['macro_f1']-lk['macro_f1']:+.4f})")


if __name__ == "__main__":
    main()
