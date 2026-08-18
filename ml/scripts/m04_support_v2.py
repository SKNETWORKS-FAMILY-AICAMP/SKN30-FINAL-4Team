"""M04 — 지원규모 모델 재설계: 등급 분류(A안) vs 텍스트 피처 추가 회귀(B안).

M03의 회귀는 평균 2.96배 빗나간다(5배 이내 78%, 10배 초과 12%).
"이 중에서는 CatBoost가 낫다"는 상대적 결론일 뿐 실용 정밀도는 아니다.
원인 두 가지를 각각 검증한다.

A안 — 점 추정 대신 등급 분류
  Pre-Review 용도는 "억 단위 규모인가"를 아는 것이므로 등급이면 충분할 수 있다.
  경계는 (1) 실무 기준 고정값, (2) 분위수 기반 균등 두 가지를 모두 본다.
  순서형이므로 '인접 등급 포함 정확도'를 함께 측정한다.

B안 — 텍스트 피처 추가
  현재 피처는 분야/기관/업종/지역 메타데이터뿐이고, 계획서 9.2가 요구한
  title/description 임베딩이 빠져 있다. 피처 집합만 바꿔가며 같은 모델(CatBoost)로
  측정해 텍스트의 기여를 분리한다.

분할은 M03과 동일한 GroupKFold(그룹=연도 제거 사업명) — 연도별 재공고 누수 차단.
TF-IDF/SVD는 fold train에만 fit한다. 사전학습 임베딩은 라벨을 보지 않으므로
전체에 대해 한 번 계산해도 누수가 아니다.
"""
import argparse
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
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             mean_absolute_error, r2_score)
from sklearn.model_selection import GroupKFold

from common import PROC, save_report
from m03_support_amount import (CAT_FEATS, MAX_WON, MIN_WON, NUM_FEATS,
                                group_key)

warnings.filterwarnings("ignore")

# 실무 기준 경계 (원). Pre-Review에서 의미가 갈리는 지점으로 잡았다.
FIXED_BINS = [(0, 10_000_000, "1천만 미만"),
              (10_000_000, 100_000_000, "1천만~1억"),
              (100_000_000, float("inf"), "1억 이상")]
FIXED_BINS5 = [(0, 5_000_000, "500만 미만"),
               (5_000_000, 20_000_000, "500만~2천만"),
               (20_000_000, 50_000_000, "2천만~5천만"),
               (50_000_000, 200_000_000, "5천만~2억"),
               (200_000_000, float("inf"), "2억 이상")]

EMB_MODEL = "jhgan/ko-sroberta-multitask"


def load_with_text():
    d = pd.read_parquet(PROC + "/announcement_detail_enriched.parquet")
    d["text"] = (d["title"].fillna("") + "\n" + d["summary_text"].fillna("") + "\n"
                 + d["target_text"].fillna("") + "\n"
                 + d["doc_text"].fillna("").str.slice(0, 3000))
    d["source"] = "openapi"
    for c in ("middle_category", "industry"):
        d[c] = np.nan

    t = pd.read_parquet(PROC + "/business_taxonomy.parquet")
    t = t.rename(columns={"large_category": "category_large"})
    t["text"] = (t["title"].fillna("") + "\n" + t["purpose"].fillna("") + "\n"
                 + t["content"].fillna("") + "\n" + t["target_text"].fillna(""))
    t["source"] = "excel2023"
    t["region"] = np.nan
    t["doc_chars"] = t["text"].str.len()

    cols = ["title", "text", "category_large", "agency", "executor", "region",
            "middle_category", "industry", "support_amount_max",
            "support_amount_type", "support_count", "support_ratio",
            "self_payment_ratio", "support_period_year", "n_amount_candidates",
            "doc_chars", "source"]
    both = pd.concat([d.reindex(columns=cols), t.reindex(columns=cols)],
                     ignore_index=True)
    both = both[both["support_amount_type"] == "per_company"]
    both = both[both["support_amount_max"].between(MIN_WON, MAX_WON)]
    return both.reset_index(drop=True)


def meta_matrix(df):
    X = df[CAT_FEATS + NUM_FEATS].copy()
    for c in CAT_FEATS:
        X[c] = X[c].astype("object").where(X[c].notna(), "__NA__").astype(str)
    for c in NUM_FEATS:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    Xd = pd.get_dummies(X, columns=CAT_FEATS)
    Xd.columns = [re.sub(r"[^0-9A-Za-z_가-힣]", "_", str(c)) for c in Xd.columns]
    return Xd.loc[:, ~Xd.columns.duplicated()]


def embed(texts):
    """사전학습 문장 임베딩. 라벨을 보지 않으므로 전체 계산이 누수가 아니다."""
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(EMB_MODEL)
    v = m.encode([t[:2000] for t in texts], batch_size=32,
                 show_progress_bar=False, normalize_embeddings=True)
    return np.asarray(v, dtype=np.float32)


# ------------------------------------------------------------------ A안
def bracketize(vals, bins):
    lab = np.full(len(vals), -1)
    names = []
    for i, (lo, hi, nm) in enumerate(bins):
        lab[(vals >= lo) & (vals < hi)] = i
        names.append(nm)
    return lab, names


def quantile_bins(vals, k):
    edges = np.unique(np.quantile(vals, np.linspace(0, 1, k + 1)))
    lab = np.clip(np.digitize(vals, edges[1:-1]), 0, len(edges) - 2)
    names = ["Q%d (%.0f만~%.0f만)" % (i + 1, edges[i] / 1e4, edges[i + 1] / 1e4)
             for i in range(len(edges) - 1)]
    return lab, names


def clf_models(seed, n_cls):
    from lightgbm import LGBMClassifier
    from xgboost import XGBClassifier
    from catboost import CatBoostClassifier
    return {
        "Majority(하한선)": DummyClassifier(strategy="most_frequent"),
        "LogisticRegression": LogisticRegression(max_iter=2000, C=1.0,
                                                 class_weight="balanced",
                                                 random_state=seed),
        "RandomForest": RandomForestClassifier(n_estimators=400, min_samples_leaf=2,
                                               class_weight="balanced_subsample",
                                               n_jobs=-1, random_state=seed),
        "XGBoost": XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.1,
                                 subsample=0.9, colsample_bytree=0.9, n_jobs=-1,
                                 random_state=seed, verbosity=0),
        "LightGBM": LGBMClassifier(n_estimators=300, learning_rate=0.1, num_leaves=15,
                                   min_child_samples=1, min_split_gain=0.0,
                                   n_jobs=-1, random_state=seed, verbose=-1),
        "CatBoost": CatBoostClassifier(iterations=300, depth=4, learning_rate=0.1,
                                       verbose=0, random_seed=seed,
                                       allow_writing_files=False),
    }


def run_brackets(df, X, groups, seed, folds):
    vals = df["support_amount_max"].values.astype(float)
    schemes = {
        "고정 3등급": bracketize(vals, FIXED_BINS),
        "고정 5등급": bracketize(vals, FIXED_BINS5),
        "분위 3등급": quantile_bins(vals, 3),
        "분위 5등급": quantile_bins(vals, 5),
    }
    out = {}
    for sname, (y, names) in schemes.items():
        gkf = GroupKFold(n_splits=folds)
        res = {}
        for mname, mk in clf_models(seed, len(names)).items():
            pred = np.zeros(len(y), dtype=int)
            for tr, te in gkf.split(X, y, groups):
                m = clone(mk)
                xtr, xte = X.iloc[tr].copy(), X.iloc[te].copy()
                med = xtr.median(numeric_only=True)
                xtr = xtr.fillna(med)
                xte = xte.fillna(med)
                m.fit(xtr, y[tr])
                pred[te] = np.asarray(m.predict(xte)).ravel().astype(int)
            adj = float(np.mean(np.abs(pred - y) <= 1))     # 인접 등급 허용
            res[mname] = {
                "accuracy": round(float(accuracy_score(y, pred)), 4),
                "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
                "adjacent_accuracy": round(adj, 4),
            }
        best = max(res.items(), key=lambda kv: kv[1]["macro_f1"])
        # 승자 혼동행렬
        pred = np.zeros(len(y), dtype=int)
        for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
            m = clone(clf_models(seed, len(names))[best[0]])
            xtr, xte = X.iloc[tr].copy(), X.iloc[te].copy()
            med = xtr.median(numeric_only=True)
            m.fit(xtr.fillna(med), y[tr])
            pred[te] = np.asarray(m.predict(xte.fillna(med))).ravel().astype(int)
        out[sname] = {
            "class_names": names,
            "class_dist": {names[i]: int((y == i).sum()) for i in range(len(names))},
            "results": res, "best_model": best[0],
            "best_macro_f1": best[1]["macro_f1"],
            "confusion_matrix": confusion_matrix(y, pred, labels=list(range(len(names)))).tolist(),
        }
        print("  [%s] 최고 %s  acc %.4f  macroF1 %.4f  인접포함 %.4f"
              % (sname, best[0], res[best[0]]["accuracy"], best[1]["macro_f1"],
                 res[best[0]]["adjacent_accuracy"]), flush=True)
    return out


# ------------------------------------------------------------------ B안
def run_feature_sets(df, Xmeta, groups, seed, folds, use_emb):
    from catboost import CatBoostRegressor
    y = np.log10(df["support_amount_max"].values.astype(float))
    texts = df["text"].fillna("").astype(str).values
    gkf = GroupKFold(n_splits=folds)
    folds_idx = list(gkf.split(Xmeta, y, groups))

    emb = None
    if use_emb:
        print("  임베딩 계산 중 (%s, %d건)..." % (EMB_MODEL, len(texts)), flush=True)
        emb = embed(texts)
        print("  임베딩 shape:", emb.shape, flush=True)

    def evaluate(build):
        pred = np.zeros(len(y))
        for tr, te in folds_idx:
            xtr, xte = build(tr, te)
            m = CatBoostRegressor(iterations=400, depth=5, learning_rate=0.05,
                                  verbose=0, random_seed=seed,
                                  allow_writing_files=False)
            m.fit(xtr, y[tr])
            pred[te] = np.asarray(m.predict(xte)).ravel()
        ratio = 10 ** np.abs(y - pred)
        return {
            "MAE_log10": round(float(mean_absolute_error(y, pred)), 4),
            "R2_log10": round(float(r2_score(y, pred)), 4),
            "geo_mean_error_x": round(float(10 ** np.mean(np.abs(y - pred))), 3),
            "within_2x": round(float(np.mean(ratio <= 2)), 4),
            "within_5x": round(float(np.mean(ratio <= 5)), 4),
            "over_10x": round(float(np.mean(ratio > 10)), 4),
        }

    Xm = Xmeta.copy()

    def f_meta(tr, te):
        med = Xm.iloc[tr].median(numeric_only=True)
        return Xm.iloc[tr].fillna(med).values, Xm.iloc[te].fillna(med).values

    def f_tfidf(tr, te):
        # TF-IDF/SVD는 반드시 fold train에만 fit
        tv = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3,
                             sublinear_tf=True, max_features=40000)
        sv = TruncatedSVD(n_components=100, random_state=seed)
        a = sv.fit_transform(tv.fit_transform(texts[tr]))
        b = sv.transform(tv.transform(texts[te]))
        med = Xm.iloc[tr].median(numeric_only=True)
        return (np.hstack([Xm.iloc[tr].fillna(med).values, a]),
                np.hstack([Xm.iloc[te].fillna(med).values, b]))

    def f_text_only(tr, te):
        tv = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=3,
                             sublinear_tf=True, max_features=40000)
        sv = TruncatedSVD(n_components=100, random_state=seed)
        return (sv.fit_transform(tv.fit_transform(texts[tr])),
                sv.transform(tv.transform(texts[te])))

    def f_emb(tr, te):
        med = Xm.iloc[tr].median(numeric_only=True)
        return (np.hstack([Xm.iloc[tr].fillna(med).values, emb[tr]]),
                np.hstack([Xm.iloc[te].fillna(med).values, emb[te]]))

    sets = {"F1 메타만 (M03 현재)": f_meta,
            "F2 메타 + TFIDF·SVD100": f_tfidf,
            "F3 텍스트만 (TFIDF·SVD100)": f_text_only}
    if use_emb:
        sets["F4 메타 + SBERT임베딩"] = f_emb

    out = {}
    for nm, fn in sets.items():
        out[nm] = evaluate(fn)
        print("  %-26s MAE_log10 %.4f  R2 %.4f  평균 %.2f배  2배이내 %.1f%%"
              % (nm, out[nm]["MAE_log10"], out[nm]["R2_log10"],
                 out[nm]["geo_mean_error_x"], out[nm]["within_2x"] * 100), flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-emb", action="store_true", help="SBERT 임베딩 생략")
    args = ap.parse_args()

    df = load_with_text()
    groups = df["title"].map(group_key).values
    Xmeta = meta_matrix(df)
    print("대상 %d행 / 그룹 %d개 / 메타피처 %d개\n"
          % (len(df), len(set(groups)), Xmeta.shape[1]))

    print("=== A안: 등급 분류 ===", flush=True)
    a = run_brackets(df, Xmeta, groups, args.seed, args.folds)
    print("\n=== B안: 피처 집합별 회귀 (모델 고정 CatBoost) ===", flush=True)
    b = run_feature_sets(df, Xmeta, groups, args.seed, args.folds, not args.no_emb)

    save_report("m04_support_v2.json", {
        "rows": len(df), "n_groups": len(set(groups)),
        "split": "GroupKFold(그룹=연도 제거한 사업명)", "folds": args.folds,
        "A_bracket_classification": a,
        "B_feature_sets_regression": b,
        "embedding_model": None if args.no_emb else EMB_MODEL,
        "note": ("A안은 순서형이라 인접 등급 허용 정확도를 함께 본다. "
                 "B안은 모델을 CatBoost로 고정해 피처 기여만 분리한다."),
    })
    print("\n저장 완료")


if __name__ == "__main__":
    main()
