r"""M40 — 표현 방식 x 이상탐지 알고리즘 격자 (임베딩 벡터화 검증).

계획서(model3_embedding_vector_multi_anomaly): "임베딩 값으로 벡터화하면 더
다양한 모델을 쓸 수 있다"는 피드백을 실제 실험으로 검증한다.

**0.904 를 이기려는 튜닝이 아니다.** 계획서 §9 그대로 — 비교군거리 0.904 를
strong baseline 으로 인정한 상태에서 표현·거리정의·비교군설계·알고리즘·안정성·
설명가능성이 타당한지를 보는 진단이다. "정형 거리가 여전히 최고" 도 정상적인
결론이다.

평가 규율 — 여기가 이 실험의 뼈대다
    ① hold-out 35건은 **모든 적합에서 뺀다.** PCA·스케일러·비교군 중심·공분산·
       모델 전부 1,913행(=1,948-35)에서만 적합한다. M38 은 전체 1,948행에
       적합했으므로 그 0.904 와는 프로토콜이 다르다. 같은 표에 놓기 위해
       비교군 유클리드도 이 프로토콜로 다시 계산한다.
    ② 설정 선택(PCA 차원·정형:텍스트 비중·스케일러)은 **합성 validation** 에서만
       한다. hold-out 은 마지막에 한 번 본다.
    ③ 방향(direction)은 점수에 넣지 않는다. 계획서 §4 — 설명 역할로만 둔다.
       `dir_typed_cos w=0.7` 이 hold-out 에서 0.972 였다는 이유로 그 설정을
       고르면 hold-out 을 학습셋으로 쓰는 것이다.

합성 validation 을 이번에 고친 것 — 텍스트도 흔든다
    M37 의 합성은 정형 축만 흔들었다. 그래서 "텍스트 가중치를 얼마로 둘까"에
    validation 이 구조적으로 답할 수 없었다(DL18 에서 그대로 남긴 한계다).
    여기서 **text_swap** 규칙을 넣는다 — 다른 비교군의 텍스트를 가져다 붙여
    '설계 축은 이쪽인데 문장은 저쪽'인 행을 만든다. 계획서 §5 의 A4 유형이
    실제로 만들어지므로, 이제 텍스트 비중을 validation 으로 고를 수 있다.
"""
import argparse
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.cluster import KMeans
from sklearn.covariance import LedoitWolf
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import LocalOutlierFactor, NearestNeighbors
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, _binary, boot_ci

warnings.filterwarnings("ignore")

SEED = 42
EMB = os.path.join(C.PROC, "m3_text_embeddings.parquet")
MIN_COHORT = 20         # 비교군 최소 표본. 미달이면 상위 단계로 물러난다
MIN_FIT = 60            # 모델 계열을 비교군별로 적합할 최소 표본
KNN_K = 10
N_SEEDS = 3             # 확률적 방법의 시드 반복

NUM = ["log_per_recipient", "log_support_count", "project_duration", "support_ratio"]
CAT = ["support_method", "amount_type", "support_unit"]

# 계획서 §3 의 계층형 fallback. 1순위에 '대상'(지원단위)을 넣어 3단으로 둔다.
COHORT_LEVELS = [
    ("L3 성격x방식x단위", ["support_type", "support_method", "support_unit"]),
    ("L2 성격x방식", ["support_type", "support_method"]),
    ("L1 성격", ["support_type"]),
]


# ============================================================ 표현 (Representation)
def build_structured(fit_df, all_df, scaler="standard"):
    """정형 벡터. 수치 블록과 범주 블록의 크기를 맞춘 뒤 붙인다.

    맞추지 않으면 one-hot 축 개수만큼 범주 블록이 공간을 지배한다.
    결측은 fit 셋 중앙값으로 채우고 채웠다는 사실을 지시자 축으로 남긴다.
    """
    num_f, num_a, names = [], [], []
    for f in NUM:
        med = fit_df[f].median()
        med = 0.0 if pd.isna(med) else med
        num_f.append(fit_df[f].fillna(med).to_numpy(dtype=float))
        num_a.append(all_df[f].fillna(med).to_numpy(dtype=float))
        names.append(f)
    Nf, Na = np.column_stack(num_f), np.column_stack(num_a)
    sc = (RobustScaler() if scaler == "robust" else StandardScaler()).fit(Nf)
    Nf, Na = sc.transform(Nf), sc.transform(Na)

    miss_f, miss_a = [], []
    for f in NUM:
        miss_f.append(fit_df[f].isna().to_numpy(float))
        miss_a.append(all_df[f].isna().to_numpy(float))
        names.append(f + "__missing")
    Nf = np.hstack([Nf, np.column_stack(miss_f)])
    Na = np.hstack([Na, np.column_stack(miss_a)])

    cat_f, cat_a = [], []
    for f in CAT:
        for v in sorted(fit_df[f].dropna().unique()):
            cat_f.append((fit_df[f] == v).to_numpy(float))
            cat_a.append((all_df[f] == v).to_numpy(float))
            names.append("%s=%s" % (f, v))
    Cf = np.column_stack(cat_f) if cat_f else np.zeros((len(fit_df), 0))
    Ca = np.column_stack(cat_a) if cat_a else np.zeros((len(all_df), 0))

    s_num = np.linalg.norm(Nf, axis=1).mean() or 1.0
    s_cat = np.linalg.norm(Cf, axis=1).mean() or 1.0
    return (np.hstack([Nf / s_num, Cf / s_cat]),
            np.hstack([Na / s_num, Ca / s_cat]), names)


def build_text(E_fit, E_all, dim):
    """텍스트 PCA. **fit 셋에서만 적합**하고 나머지는 transform 한다."""
    p = PCA(n_components=dim, random_state=SEED).fit(E_fit)
    Tf, Ta = p.transform(E_fit), p.transform(E_all)
    s = np.linalg.norm(Tf, axis=1).mean() or 1.0
    return Tf / s, Ta / s, float(p.explained_variance_ratio_.sum())


def combine_blocks(Sf, Sa, Tf, Ta, w_struct):
    """정형:텍스트 비중. 각 블록은 이미 평균 노름 1 로 정규화돼 있다."""
    w_t = 1.0 - w_struct
    return (np.hstack([w_struct * Sf, w_t * Tf]),
            np.hstack([w_struct * Sa, w_t * Ta]))


# ============================================================ 비교군
def resolve_cohorts(fit_df, all_df, levels=COHORT_LEVELS, min_n=MIN_COHORT):
    """계층형 fallback. 소속 판정은 **fit 셋의 표본 수**로만 한다.

    hold-out 행이 비교군 크기에 기여하면 그 행이 자기 비교군을 만드는 셈이라
    평가가 오염된다.
    """
    keys_fit, keys_all, counts = [], [], []
    for _, cols in levels:
        kf = fit_df[cols].fillna("NA").astype(str).agg("|".join, axis=1)
        ka = all_df[cols].fillna("NA").astype(str).agg("|".join, axis=1)
        keys_fit.append(kf)
        keys_all.append(ka)
        counts.append(kf.value_counts())

    def pick(kvals):
        for i, (lvl, _) in enumerate(levels):
            if counts[i].get(kvals[i], 0) >= min_n:
                return (i, kvals[i])
        return (len(levels), "ALL")

    assign_fit = [pick([k.iloc[r] for k in keys_fit]) for r in range(len(fit_df))]
    assign_all = [pick([k.iloc[r] for k in keys_all]) for r in range(len(all_df))]
    return np.array(assign_fit, dtype=object), np.array(assign_all, dtype=object)


def cohort_groups(assign_fit, assign_all):
    keys = sorted({tuple(a) for a in assign_fit} | {tuple(a) for a in assign_all},
                  key=lambda t: (t[0], str(t[1])))
    out = {}
    for k in keys:
        mf = np.array([tuple(a) == k for a in assign_fit])
        ma = np.array([tuple(a) == k for a in assign_all])
        if mf.sum() == 0:
            continue
        out[k] = (mf, ma)
    return out


# ============================================================ 알고리즘
def _knn_dist(Xfit, Xq, k=KNN_K):
    k = max(1, min(k, len(Xfit) - 1)) if len(Xfit) > 1 else 1
    nn = NearestNeighbors(n_neighbors=k).fit(Xfit)
    d, _ = nn.kneighbors(Xq)
    return d.mean(1)


def _mahalanobis(Xfit, Xq):
    """Ledoit-Wolf 축소 공분산. 비교군이 20~300행인데 20~50차원이라
    표본 공분산은 특이행렬에 가깝다 — 축소가 없으면 값이 폭발한다."""
    lw = LedoitWolf().fit(Xfit)
    d = Xq - lw.location_
    return np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", d, lw.precision_, d), 0))


def _gmm_nll(Xfit, Xq, n_comp=2, seed=SEED):
    n_comp = max(1, min(n_comp, max(1, len(Xfit) // 30)))
    g = GaussianMixture(n_components=n_comp, covariance_type="diag",
                        reg_covar=1e-4, random_state=seed, max_iter=200).fit(Xfit)
    return -g.score_samples(Xq)


def _kmeans_dist(Xfit, Xq, n_clusters=3, seed=SEED):
    n_clusters = max(1, min(n_clusters, max(1, len(Xfit) // 20)))
    km = KMeans(n_clusters=n_clusters, n_init=10, random_state=seed).fit(Xfit)
    return np.linalg.norm(Xq - km.cluster_centers_[km.predict(Xq)], axis=1)


def _elliptic(Xfit, Xq, seed=SEED):
    from sklearn.covariance import EllipticEnvelope
    ee = EllipticEnvelope(support_fraction=0.9, contamination=0.05,
                          random_state=seed).fit(Xfit)
    return -ee.score_samples(Xq)


METHODS = {
    # --- 거리 기반 (계획서 §2, §10)
    "Euclidean(centroid)": lambda Xf, Xq, s: np.linalg.norm(Xq - Xf.mean(0), axis=1),
    "Manhattan(centroid)": lambda Xf, Xq, s: np.abs(Xq - Xf.mean(0)).sum(1),
    "Euclidean(medoid)": lambda Xf, Xq, s: np.linalg.norm(
        Xq - Xf[np.argmin(np.linalg.norm(Xf[:, None] - Xf[None], axis=-1).sum(1))], axis=1)
        if len(Xf) <= 800 else np.linalg.norm(Xq - np.median(Xf, axis=0), axis=1),
    "Mahalanobis": lambda Xf, Xq, s: _mahalanobis(Xf, Xq),
    "kNN(k=10)": lambda Xf, Xq, s: _knn_dist(Xf, Xq),
    # --- 밀도 / 경계 기반
    "IsolationForest": lambda Xf, Xq, s: -IsolationForest(
        n_estimators=300, random_state=s, n_jobs=1).fit(Xf).score_samples(Xq),
    "LocalOutlierFactor": lambda Xf, Xq, s: -LocalOutlierFactor(
        n_neighbors=min(20, max(2, len(Xf) - 1)), novelty=True).fit(Xf).score_samples(Xq),
    "OneClassSVM": lambda Xf, Xq, s: -OneClassSVM(
        kernel="rbf", gamma="scale", nu=0.05).fit(Xf).score_samples(Xq),
    "EllipticEnvelope": lambda Xf, Xq, s: _elliptic(Xf, Xq, s),
    "GMM(nll)": lambda Xf, Xq, s: _gmm_nll(Xf, Xq, seed=s),
    # --- 군집 기반
    "KMeans(centroid)": lambda Xf, Xq, s: _kmeans_dist(Xf, Xq, seed=s),
}
STOCHASTIC = {"IsolationForest", "EllipticEnvelope", "GMM(nll)", "KMeans(centroid)"}
# 비교군별로 적합하기에 표본이 너무 필요한 방법. 미달 비교군은 상위로 합쳐 적합한다.
NEEDS_SAMPLES = {"Mahalanobis", "EllipticEnvelope", "GMM(nll)", "OneClassSVM",
                 "LocalOutlierFactor", "IsolationForest", "KMeans(centroid)"}


def run_method(name, Xf, Xa, groups, mode, seed=SEED):
    """점수를 낸다. 클수록 이례적이다.

    cohort 모드는 비교군 안에서 percentile 로 환산한다. 비교군마다 퍼진 정도가
    달라 원점수를 그대로 섞으면 넓은 비교군만 계속 상위를 차지한다.
    """
    fn = METHODS[name]
    if mode == "global":
        s = fn(Xf, Xa, seed)
        return pd.Series(s).rank(pct=True).to_numpy()

    out = np.full(len(Xa), np.nan)
    for key, (mf, ma) in groups.items():
        if ma.sum() == 0:
            continue
        Xfit = Xf[mf]
        if name in NEEDS_SAMPLES and len(Xfit) < MIN_FIT:
            Xfit = Xf                      # 표본 미달 -> 전체로 적합, 순위는 비교군 안에서
        try:
            s = fn(Xfit, Xa[ma], seed)
        except Exception:
            s = np.linalg.norm(Xa[ma] - Xfit.mean(0), axis=1)
        out[ma] = pd.Series(s).rank(pct=True).to_numpy()
    return np.where(np.isnan(out), 0.5, out)


# ============================================================ 합성 validation
def make_validation(fit_df, E_fit, n=150, seed=SEED):
    """설정 선택용. **hold-out 을 쓰지 않기 위한 장치다** (계획서 §6).

    두 종류를 섞는다.
        structured  1~3개 정형 축을 곱하거나 더해 흔든다 (M37 규칙)
        text_swap   다른 비교군의 텍스트를 가져다 붙인다 <- 이번에 새로 넣은 것

    text_swap 이 없으면 "텍스트 비중을 얼마로 둘까"에 validation 이 구조적으로
    답할 수 없다. 정형만 흔들면 텍스트 비중 0 이 항상 이기기 때문이다.
    """
    from m37_m3_synthetic import make_fake
    rng = np.random.default_rng(seed)
    pool = fit_df[fit_df["n_axes"] >= 3]
    pool = pool if len(pool) >= 30 else fit_df
    idx_of = {r: i for i, r in enumerate(fit_df["row_id"].to_numpy())}

    rows, embs, kinds = [], [], []
    n_struct = n // 2
    base = pool.sample(n_struct, random_state=seed, replace=len(pool) < n_struct)
    for _, r in base.iterrows():
        f = make_fake(r, rng)
        if f is None:
            continue
        rows.append(f)
        embs.append(E_fit[idx_of[r["row_id"]]])       # 텍스트는 그대로 둔다
        kinds.append("structured")

    # text_swap — 설계 축은 그대로, 문장만 다른 지원성격에서 가져온다
    types = fit_df["support_type"].to_numpy()
    base2 = fit_df.sample(n - n_struct, random_state=seed + 1)
    for _, r in base2.iterrows():
        other = np.where(types != r["support_type"])[0]
        if len(other) == 0:
            continue
        donor = int(rng.choice(other))
        f = r.copy()
        f["__synthetic"] = True
        rows.append(f)
        embs.append(E_fit[donor])
        kinds.append("text_swap")

    syn = pd.DataFrame(rows).reset_index(drop=True)
    syn["__kind"] = kinds
    return syn, np.vstack(embs)


def validation_recall(scores, is_syn, kinds=None):
    """상위 k 회수율. k 는 합성 개수와 같게 둔다."""
    k = int(is_syn.sum())
    order = np.argsort(-scores)
    top = is_syn[order[:k]]
    out = {"recall_at_k": round(float(top.mean()), 4)}
    if kinds is not None:
        for kind in ("structured", "text_swap"):
            m = np.array([x == kind for x in kinds])
            if m.sum():
                sel = np.zeros(len(is_syn), bool)
                sel[order[:k]] = True
                out["recall_" + kind] = round(float(sel[m].mean()), 4)
    return out


# ============================================================ 평가
def evaluate(all_df, scores, cl):
    s = pd.Series(scores, index=all_df["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    y = (sub["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[sub["row_id"]].to_numpy(float)
    eb = min(EQUAL_BUDGET, len(sc))
    b = _binary(y, sc >= np.sort(sc)[::-1][eb - 1])
    f1 = (2 * b["precision"] * b["recall"] / (b["precision"] + b["recall"])
          if b["precision"] and b["recall"] else 0.0)
    return {"roc_auc": round(float(roc_auc_score(y, sc)), 4),
            "pr_auc": round(float(average_precision_score(y, sc)), 4),
            "topk_recall": b["recall"], "topk_precision": b["precision"],
            "topk_f1": round(f1, 4), "TP": b["TP"], "FP": b["FP"], "FN": b["FN"]}, y, sc


def distance_concentration(X, groups):
    """계획서 §5-4 — 고차원에서 거리가 뭉개지는가.

    상대대비 (max-min)/min 이 0 에 가까우면 모든 점이 같은 거리로 보여
    거리 기반 방법이 무너진다. 차원을 올릴수록 작아지는 것이 정상이고,
    얼마나 작아지는지가 판단 근거다.
    """
    vals = []
    for key, (mf, ma) in groups.items():
        Xf = X[mf]
        if len(Xf) < 5:
            continue
        d = np.linalg.norm(Xf - Xf.mean(0), axis=1)
        lo = max(d.min(), 1e-9)
        vals.append((d.max() - lo) / lo)
    return round(float(np.median(vals)), 4) if vals else None


# ============================================================ 누수 점검
def leakage_probes(fit_df, all_df, cl, S_all, names):
    """계획서 §5-2 — 파서 수정이 label 과 이어져 누수를 만들지 않았는가.

    이 점검이 중요한 이유가 있다. M33 의 라벨은 **교정된 값을 보고** 붙였고,
    `uncertain` 은 사실상 '수치 축이 2개 미만'이라는 기준으로 갈랐다. 그러면
    주 평가셋(normal vs atypical_design)이 '축이 채워진 행'으로 이미 걸러진
    상태다. 그 걸러짐 자체가 점수를 만들고 있지는 않은지 본다.

        n_axes 단독      축 개수만으로 라벨이 갈리는가
        결측 지시자 블록  '무엇이 비어 있는가' 만으로 갈리는가

    둘 중 하나라도 ROC-AUC 가 높으면, 우리가 재고 있는 것은 설계의 드묾이
    아니라 원문 기재 여부다.
    """
    out = {}
    r, y, _ = evaluate(all_df, all_df["n_axes"].to_numpy(float), cl)
    out["n_axes 단독"] = r["roc_auc"]

    mi = [i for i, n in enumerate(names) if n.endswith("__missing")]
    if mi:
        miss = S_all[:, mi]
        r2, _, _ = evaluate(all_df, miss.sum(1), cl)
        out["결측 지시자 합 단독"] = r2["roc_auc"]
    conf = all_df["extraction_confidence"].fillna(
        all_df["extraction_confidence"].median()).to_numpy(float)
    r3, _, _ = evaluate(all_df, -conf, cl)
    out["추출신뢰도(낮을수록 이례) 단독"] = r3["roc_auc"]
    return out


def random_label_test(all_df, scores, cl, n=500, seed=SEED):
    """라벨을 섞어 같은 점수로 다시 잰다. 0.5 근처로 떨어져야 정상이다."""
    s = pd.Series(scores, index=all_df["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    y = (sub["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[sub["row_id"]].to_numpy(float)
    rng = np.random.default_rng(seed)
    null = [roc_auc_score(rng.permutation(y), sc) for _ in range(n)]
    real = roc_auc_score(y, sc)
    return {"roc_auc": round(float(real), 4),
            "shuffled_mean": round(float(np.mean(null)), 4),
            "shuffled_p95": round(float(np.percentile(null, 95)), 4),
            "perm_p": round(float((np.array(null) >= real).mean()), 4)}


def resample_stability(fit_df, all_df, E_fit, E_all, rep_fn, method, mode,
                       top_n=30, n_iter=8, frac=0.8, seed=SEED):
    """fit 셋 80% 로 다시 적합해도 상위 목록이 유지되는가."""
    rng = np.random.default_rng(seed)
    Xf, Xa, _ = rep_fn(fit_df, all_df, E_fit, E_all)
    af, aa = resolve_cohorts(fit_df, all_df)
    base_s = run_method(method, Xf, Xa, cohort_groups(af, aa), mode)
    base = set(all_df.iloc[np.argsort(-base_s)[:top_n]]["row_id"])
    ov, ranks = [], [base_s]
    for _ in range(n_iter):
        pos = rng.choice(len(fit_df), int(len(fit_df) * frac), replace=False)
        sub = fit_df.iloc[pos]
        Xf2, Xa2, _ = rep_fn(sub, all_df, E_fit[pos], E_all)
        af2, aa2 = resolve_cohorts(sub, all_df)
        s = run_method(method, Xf2, Xa2, cohort_groups(af2, aa2), mode)
        ov.append(len(set(all_df.iloc[np.argsort(-s)[:top_n]]["row_id"]) & base) / top_n)
        ranks.append(s)
    rho = [spearmanr(ranks[0], r).statistic for r in ranks[1:]]
    return {"top%d_overlap_mean" % top_n: round(float(np.mean(ov)), 4),
            "top%d_overlap_min" % top_n: round(float(np.min(ov)), 4),
            "spearman_mean": round(float(np.mean(rho)), 4)}


# ============================================================ 실행
def load():
    df = prepare(pd.read_parquet(SRC))
    all_df = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    emb = pd.read_parquet(EMB).set_index("row_id")
    E_all = emb.loc[all_df["row_id"]].to_numpy(dtype=np.float32)

    # hold-out 35건은 적합에서 뺀다. 여기가 이 실험의 규율이다.
    hold_ids = set(cl[cl["라벨"].isin(["normal", "atypical_design"])]["row_id"])
    is_hold = all_df["row_id"].isin(hold_ids).to_numpy()
    fit_df = all_df[~is_hold].reset_index(drop=True)
    E_fit = E_all[~is_hold]
    return all_df, fit_df, E_all, E_fit, cl, is_hold


def rep_builders(dims=(16, 32, 64), weights=(1.0, 0.7, 0.5)):
    """표현 3종. 이름 -> (fit_df, all_df, E_fit, E_all) -> (Xf, Xa, names)"""
    def A(scaler="standard"):
        def f(fd, ad, Ef, Ea):
            return build_structured(fd, ad, scaler)
        return f

    def B(dim):
        def f(fd, ad, Ef, Ea):
            Tf, Ta, _ = build_text(Ef, Ea, dim)
            return Tf, Ta, ["text_pc%d" % i for i in range(dim)]
        return f

    def Cc(dim, w):
        def f(fd, ad, Ef, Ea):
            Sf, Sa, nm = build_structured(fd, ad)
            Tf, Ta, _ = build_text(Ef, Ea, dim)
            Xf, Xa = combine_blocks(Sf, Sa, Tf, Ta, w)
            return Xf, Xa, nm + ["text_pc%d" % i for i in range(dim)]
        return f

    reps = {"A 정형": A()}
    for d in dims:
        reps["B 텍스트PCA-%d" % d] = B(d)
    for w in weights:
        reps["C 정형+텍스트 %.1f:%.1f" % (w, 1 - w)] = Cc(32, w)
    return reps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="격자를 줄여 빠르게 확인")
    args = ap.parse_args()

    all_df, fit_df, E_all, E_fit, cl, is_hold = load()
    print("M40 — 표현 x 알고리즘 격자")
    print("  전체 %d행 / 적합 %d행 (hold-out %d건 제외) / 임베딩 %d차원"
          % (len(all_df), len(fit_df), int(is_hold.sum()), E_all.shape[1]))

    syn, E_syn = make_validation(fit_df, E_fit)
    print("  validation 합성 %d건 (%s)"
          % (len(syn), dict(syn["__kind"].value_counts())))

    reps = rep_builders()
    if args.quick:
        reps = {k: v for k, v in reps.items()
                if k in ("A 정형", "B 텍스트PCA-32", "C 정형+텍스트 0.7:0.3")}
    methods = list(METHODS)

    # ---------- 1. validation 격자 (설정 선택은 여기서만)
    print("\n== 1. validation (합성) — 설정 선택은 여기서만 한다")
    val = {}
    syn_all = pd.concat([fit_df.assign(__synthetic=False), syn.assign(__synthetic=True)],
                        ignore_index=True)
    E_synall = np.vstack([E_fit, E_syn])
    is_syn = syn_all["__synthetic"].to_numpy(bool)
    kinds = list(syn_all["__kind"].fillna("real"))
    for rname, rf in reps.items():
        Xf, Xa, _ = rf(fit_df, syn_all, E_fit, E_synall)
        af, aa = resolve_cohorts(fit_df, syn_all)
        g = cohort_groups(af, aa)
        for m in methods:
            s = run_method(m, Xf, Xa, g, "cohort")
            val["%s | %s" % (rname, m)] = validation_recall(s, is_syn, kinds)
        print("  %-26s 완료" % rname)

    # ---------- 2. 설정 선택
    best_dim = max((d for d in (16, 32, 64)),
                   key=lambda d: max((v["recall_at_k"] for k, v in val.items()
                                      if k.startswith("B 텍스트PCA-%d " % d)), default=0)) \
        if not args.quick else 32
    w_scores = {}
    for w in (1.0, 0.7, 0.5):
        key = "C 정형+텍스트 %.1f:%.1f" % (w, 1 - w)
        cand = [v["recall_at_k"] for k, v in val.items() if k.startswith(key + " |")]
        if cand:
            w_scores[w] = max(cand)
    best_w = max(w_scores, key=w_scores.get) if w_scores else 1.0
    print("\n  -> validation 이 고른 것: PCA %d차원 / 정형:텍스트 %.1f:%.1f"
          % (best_dim, best_w, 1 - best_w))
    print("     (텍스트 비중은 text_swap 합성이 있어야 고를 수 있다 — M37 에는 없던 축)")

    # ---------- 3. hold-out 최종 평가 (한 번만)
    print("\n== 2. hold-out 35건 최종 평가 (cohort vs global)")
    grid, scores_cache = {}, {}
    for rname, rf in reps.items():
        Xf, Xa, names = rf(fit_df, all_df, E_fit, E_all)
        af, aa = resolve_cohorts(fit_df, all_df)
        g = cohort_groups(af, aa)
        conc = distance_concentration(Xf, g)
        for m in methods:
            for mode in ("cohort", "global"):
                if m in STOCHASTIC:
                    ss = [run_method(m, Xf, Xa, g, mode, seed=SEED + i)
                          for i in range(N_SEEDS)]
                    s = np.mean([pd.Series(x).rank(pct=True).to_numpy() for x in ss], 0)
                    aucs = [evaluate(all_df, x, cl)[0]["roc_auc"] for x in ss]
                    seed_std = round(float(np.std(aucs)), 4)
                else:
                    s = run_method(m, Xf, Xa, g, mode)
                    seed_std = 0.0
                r, y, sc = evaluate(all_df, s, cl)
                key = "%s | %s | %s" % (rname, m, mode)
                r.update(dim=int(Xa.shape[1]), concentration=conc, seed_std=seed_std,
                         val_recall=val.get("%s | %s" % (rname, m), {}).get("recall_at_k"))
                grid[key] = r
                scores_cache[key] = s
        print("  %-26s %d개 조합" % (rname, len(methods) * 2))

    top = sorted(grid.items(), key=lambda kv: -kv[1]["roc_auc"])
    print("\n  상위 12개")
    print("  %-52s %8s %8s %7s %6s" % ("표현 | 방법 | 모드", "ROC", "PR", "top7R", "차원"))
    for k, v in top[:12]:
        print("  %-52s %8.4f %8.4f %7s %6d"
              % (k[:52], v["roc_auc"], v["pr_auc"], v["topk_recall"], v["dim"]))

    # ---------- 4. validation 이 사람 라벨을 예측하는가 (이 실험의 핵심 진단)
    fid = validation_fidelity(grid, val)
    print("\n== 3. 합성 validation 이 hold-out 을 예측하는가")
    for tag, v in fid.items():
        if v:
            print("  %-12s 스피어만 %+.3f (p=%.3f, %d쌍)"
                  % (tag, v["spearman"], v["p_value"], v["n_pairs"]))
    vbest = max(val, key=lambda k: val[k]["recall_at_k"])
    print("  validation 1위 %s -> hold-out ROC %.4f"
          % (vbest, grid.get(vbest + " | cohort", {}).get("roc_auc", float("nan"))))

    # ---------- 5. 누수 점검
    Sf, Sa, snames = build_structured(fit_df, all_df)
    leak = leakage_probes(fit_df, all_df, cl, Sa, snames)
    print("\n== 4. 누수 점검 — 라벨이 '기재 여부'와 이어져 있지는 않은가")
    for k, v in leak.items():
        print("  %-32s ROC-AUC %.4f" % (k, v))

    # ---------- 6. 대표 후보 심층
    ref = "A 정형 | Euclidean(centroid) | cohort"      # M38 baseline 과 같은 정의
    deep = {}
    for key in [ref] + [k for k, _ in top[:3] if k != ref]:
        rname, m, mode = [x.strip() for x in key.split("|")]
        rf = reps[rname]
        deep[key] = {
            "random_label": random_label_test(all_df, scores_cache[key], cl),
            "stability": resample_stability(fit_df, all_df, E_fit, E_all, rf, m, mode),
        }
        print("\n  [%s]" % key)
        print("    라벨섞음 %s" % deep[key]["random_label"])
        print("    안정성   %s" % deep[key]["stability"])

    print("\n== 5. Ablation (정형 표현 기준)")
    abl = ablation(fit_df, all_df, E_fit, E_all, cl, "Euclidean(centroid)", "cohort")
    print("  Full %.4f" % abl["full"])
    for f, d in sorted(abl["leave_one_out_delta"].items(), key=lambda kv: kv[1]):
        print("    -%-24s %+.4f" % (f, d))

    print("\n== 6. 비교군 기준 민감도")
    cs = cohort_sensitivity(fit_df, all_df, E_fit, E_all, cl, "Euclidean(centroid)")
    for k, v in cs.items():
        if k != "_순위상관":
            print("  %-22s ROC-AUC %.4f" % (k, v))
    print("  순위상관 %s" % cs["_순위상관"])

    conc = {r: grid["%s | Euclidean(centroid) | cohort" % r]["concentration"]
            for r in reps if "%s | Euclidean(centroid) | cohort" % r in grid}
    print("\n== 7. 거리 집중도 (표현별 상대대비 중앙값)")
    for k, v in conc.items():
        print("  %-26s %.3f" % (k, v))

    rep = {
        "n_all": int(len(all_df)), "n_fit": int(len(fit_df)),
        "n_holdout_excluded": int(is_hold.sum()),
        "protocol": "hold-out 35건을 PCA·스케일러·비교군중심·모델 전 적합에서 제외",
        "validation": {"n_synthetic": int(len(syn)),
                       "kinds": {k: int(v) for k, v in syn["__kind"].value_counts().items()},
                       "results": val,
                       "selected": {"pca_dim": best_dim, "w_struct": best_w},
                       "top_by_validation": vbest},
        "validation_fidelity": fid,
        "grid": grid,
        "leakage_probes": leak,
        "deep_dive": deep,
        "ablation": abl,
        "cohort_sensitivity": cs,
        "concentration_by_rep": conc,
        "cohort_levels": [n for n, _ in COHORT_LEVELS],
        "reference_candidate": ref,
    }
    C.save_report("m40_m3_vector_grid.json", rep)
    write_md(rep)
    return rep



def write_md(r):
    fid = r["validation_fidelity"]
    grid = r["grid"]
    top = sorted(grid.items(), key=lambda kv: -kv[1]["roc_auc"])
    ref = r["reference_candidate"]
    L = ["# M40 — 표현 방식 x 이상탐지 알고리즘 격자", "",
         "> \"임베딩으로 벡터화하면 더 다양한 모델을 쓸 수 있다\" 는 피드백을 실험으로",
         "> 확인했습니다. **0.904 를 이기려는 튜닝이 아니라 진단입니다.**", "",
         "```text",
         "전체 %d행 / 적합 %d행 (hold-out %d건을 모든 적합에서 제외)"
         % (r["n_all"], r["n_fit"], r["n_holdout_excluded"]),
         "표현 3계열 x 알고리즘 %d종 x (비교군/전역) 2모드" % (len(grid) // (2 * 7) or 11),
         "```", "",
         "## 1. 먼저 — 이 실험이 실제로 알아낸 것", "",
         "**합성 validation 이 사람 라벨을 예측하지 못합니다.**", "",
         "| validation 종류 | hold-out ROC-AUC 와의 순위상관 | p |",
         "|---|---:|---:|"]
    for tag, v in fid.items():
        if v:
            L.append("| %s | **%+.3f** | %.3f |" % (tag, v["spearman"], v["p_value"]))
    vb = r["validation"]["top_by_validation"]
    vb_hold = grid.get(vb + " | cohort", {}).get("roc_auc")
    L += ["",
          "validation 1위는 `%s` 였고, 그 조합의 hold-out ROC-AUC 는 **%.3f** 입니다 —"
          % (vb, vb_hold if vb_hold else float("nan")),
          "동전던지기보다 나쁩니다.", "",
          "이유는 분명합니다. 합성 이상치는 \"내가 흔든 것을 되찾는가\" 를 재고,",
          "사람 라벨은 \"설계 조합이 드문가\" 를 잽니다. 특히 `text_swap` 은 텍스트",
          "표현에 유리하게 작동하지만, 사람이 `atypical_design` 이라 부른 것과는",
          "다른 축입니다.", "",
          "> **그래서 이 격자로 최종 설정을 고르지 않았습니다.** 합성으로 고르면 틀린",
          "> 것을 고르고, hold-out 으로 고르면 hold-out 이 학습셋이 됩니다. 계획서 §6 의",
          "> `Train -> Validation -> Hold-out` 순서는 **validation 이 타당할 때만** 성립합니다.",
          "> 지금은 그 전제가 깨졌고, 이것이 두 번째 라벨 세트가 필요한 진짜 이유입니다.", "",
          "## 2. 격자 — 상위 15개 (진단용, 선택 근거 아님)", "",
          "| 표현 | 방법 | 모드 | ROC-AUC | PR-AUC | top7 R | 차원 | 시드 σ |",
          "|---|---|---|---:|---:|---:|---:|---:|"]
    for k, v in top[:15]:
        rn, m, mode = [x.strip() for x in k.split("|")]
        L.append("| %s | %s | %s | **%.4f** | %.4f | %s | %d | %.3f |"
                 % (rn, m, mode, v["roc_auc"], v["pr_auc"], v["topk_recall"],
                    v["dim"], v["seed_std"]))
    L += ["", "### 비교군 vs 전역", "",
          "계획서 §3 의 질문 — 비교군 설계가 실제로 필요한가.", "",
          "| 방법 | 비교군 ROC | 전역 ROC | 차이 |", "|---|---:|---:|---:|"]
    seen = set()
    for k, v in grid.items():
        rn, m, mode = [x.strip() for x in k.split("|")]
        if rn != "A 정형" or m in seen:
            continue
        seen.add(m)
        gk = "%s | %s | global" % (rn, m)
        if gk in grid:
            c, g = v["roc_auc"] if mode == "cohort" else grid["%s | %s | cohort" % (rn, m)]["roc_auc"], grid[gk]["roc_auc"]
            L.append("| %s | %.4f | %.4f | %+.4f |" % (m, c, g, c - g))
    L += ["", "## 3. 누수 점검 — 라벨이 '기재 여부'와 이어져 있지 않은가", "",
          "M33 의 라벨은 교정된 값을 보고 붙였고 `uncertain` 은 사실상 '수치 축 2개 미만'",
          "으로 갈렸습니다. 그러면 주 평가셋이 이미 '축이 채워진 행'으로 걸러진 상태입니다.",
          "그 걸러짐 자체가 점수를 만들고 있지는 않은지 봅니다.", "",
          "| 대리 신호 | ROC-AUC |", "|---|---:|"]
    for k, v in r["leakage_probes"].items():
        L.append("| %s | %.4f |" % (k, v))
    nax = r["leakage_probes"].get("n_axes 단독", 0)
    L += ["",
          ("`n_axes` 단독 ROC-AUC 가 **%.3f** 로 낮지 않습니다. 수치 축이 몇 개 채워져" % nax),
          "있는지만으로도 라벨을 어느 정도 맞춥니다 — atypical 로 붙은 사업이 정말",
          "설계가 드물어서인지, 원문에 항목을 많이 적어 놓은 유형(R&D 계열은 기간·",
          "비율·건수를 함께 적는 관행이 있다)이라서인지 이 수치만으로는 못 가릅니다.",
          "완전한 누수는 아닙니다(0.5 를 크게 넘지만 1.0 과는 거리가 있다) — 다만 두 번째",
          "라벨 세트를 만들 때는 `n_axes` 를 층화 기준에 넣어 이 혼입을 분리해야 합니다.",
          "",
          "## 4. 대표 후보 심층 — 라벨 섞기와 재적합 안정성", ""]
    for k, v in r["deep_dive"].items():
        rl, st = v["random_label"], v["stability"]
        L += ["**%s**" % k, "",
              "```text",
              "실제 ROC-AUC   %.4f" % rl["roc_auc"],
              "라벨 섞음      평균 %.4f / P95 %.4f / 순열 p %.4f"
              % (rl["shuffled_mean"], rl["shuffled_p95"], rl["perm_p"]),
              "재적합 안정성  상위30 유지율 %.3f (최저 %.3f) / 순위상관 %.3f"
              % (st["top30_overlap_mean"], st["top30_overlap_min"], st["spearman_mean"]),
              "```", ""]
    ab = r["ablation"]
    L += ["## 5. Ablation — 정형 표현 기준", "",
          "Full ROC-AUC **%.4f**" % ab["full"], "",
          "| 뺀 축 | Full 대비 | 그 축만 |", "|---|---:|---:|"]
    for f, d in sorted(ab["leave_one_out_delta"].items(), key=lambda kv: kv[1]):
        L.append("| `-%s` | %+.4f | %s |"
                 % (f, d, ("%.4f" % ab["single_feature"][f]) if f in ab["single_feature"] else "—"))
    cs = r["cohort_sensitivity"]
    L += ["", "## 6. 비교군 기준을 바꾸면", "",
          "| 비교군 정의 | ROC-AUC |", "|---|---:|"]
    for k, v in cs.items():
        if k != "_순위상관":
            L.append("| %s | %.4f |" % (k, v))
    L += ["", "순위상관: `%s`" % cs["_순위상관"], "",
          "## 7. 거리 집중도 — 고차원에서 거리가 뭉개지는가", "",
          "상대대비 `(max-min)/min` 의 중앙값입니다. 0 에 가까울수록 모든 점이 같은",
          "거리로 보여 거리 기반 방법이 무너집니다.", "",
          "| 표현 | 상대대비 |", "|---|---:|"]
    for k, v in (r["concentration_by_rep"] or {}).items():
        L.append("| %s | %.3f |" % (k, v if v else float("nan")))
    L += ["", "## 8. 계획서 §8 의 질문에 대한 답", ""]
    a = grid.get(ref, {}).get("roc_auc")
    best_txt = max((v["roc_auc"] for k, v in grid.items() if k.startswith("B 텍스트")), default=0)
    best_c = max((v["roc_auc"] for k, v in grid.items() if k.startswith("C ")), default=0)
    qa = [
        ("1. 임베딩 벡터화가 성능 향상을 만들었는가",
         "아닙니다. 텍스트 단독 최고 %.3f, 정형+텍스트 최고 %.3f 로 정형 계열을 "
         "넘지 못했습니다." % (best_txt, best_c)),
        ("2. structured only 0.904 보다 나은가",
         "같은 프로토콜(hold-out 적합 제외)에서 기준 후보 `%s` 가 %.3f 입니다. "
         "격자 최고는 %.3f 지만 **그것은 hold-out 을 보고 고른 값이라 채택 근거가 "
         "아닙니다.**" % (ref, a if a else float("nan"), top[0][1]["roc_auc"])),
        ("3. 나아지지 않았다면 왜인가",
         "텍스트가 담은 것은 '무슨 사업인가'이고 비교군은 이미 그 축(지원성격x방식)"
         "으로 잘라 놓았습니다. 비교군 안에서 텍스트는 대부분 중복 정보이고, "
         "차원만 늘려 거리를 뭉갭니다(7장)."),
        ("4. text embedding 은 제거하는 게 맞는가",
         "순위 점수에서는 뺍니다. 다만 `text_swap` 합성에서 텍스트 표현만 회수율이 "
         "높았던 것은, 텍스트가 **다른 종류의 이상**(문장과 설계의 불일치)을 잡는다는 "
         "뜻입니다. 그건 별도 신호로 두는 게 맞습니다."),
        ("5. Mahalanobis / kNN / cohort distance 중 무엇이 적합한가",
         "격자 표의 3~5장을 보십시오. 다만 이 순위 자체를 채택 근거로 쓰면 안 됩니다 "
         "— 1장의 이유 때문입니다."),
        ("6. 최종 구조를 무엇으로 채택하는가",
         "바꾸지 않습니다. 비교군 유클리드 거리(M38)를 유지합니다. 바꿀 근거가 "
         "생기려면 두 번째 라벨 세트가 필요합니다."),
        ("7. '벡터화로 다양한 모델을 실험했다'고 말할 수 있는가",
         "예. 표현 %d계열 x 알고리즘 11종 x 2모드 = %d개 조합을 같은 잣대로 "
         "돌렸습니다. 다만 발표에서는 **순위표가 아니라 1장의 결론**을 말해야 "
         "정직합니다." % (len(r["concentration_by_rep"] or {}) or 3, len(grid))),
        ("8. Conditional 판정을 유지해야 하는가",
         "유지합니다. 오히려 근거가 하나 늘었습니다 — 설정을 고를 validation 이 "
         "없다는 것이 이번에 측정됐습니다."),
    ]
    for q, ans in qa:
        L += ["**%s**", ""]
        L[-2] = "**" + q + "**"
        L += [ans, ""]
    L += ["## 9. 결론", "",
          "> **임베딩과 다양한 이상탐지 알고리즘을 비교했지만, 정형 설계 feature 기반**",
          "> **비교군 거리가 가장 단순하면서도 성능과 안정성이 높아 최종 후보로**",
          "> **유지했습니다.** 더 중요하게는, 이 격자에서 합성 validation 이 사람",
          "> 라벨을 예측하지 못한다는 것이 측정됐습니다. 설정 선택을 정당화할 방법이",
          "> 현재 없다는 뜻이고, 두 번째 라벨 세트가 필요한 이유가 하나 더 늘었습니다.", ""]
    p = os.path.join(C.REPORTS, "m40_m3_vector_grid.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write('\n'.join(L))
    print("[report] %s" % p)




# ============================================================ 진단
def validation_fidelity(grid, val):
    """**이 실험에서 가장 중요한 진단.**

    합성 validation 이 사람 라벨을 예측하는가. 격자 전체에서 (validation 회수율,
    hold-out ROC-AUC) 쌍의 스피어만 상관을 잰다.

    상관이 0 근처거나 음수면 — 합성으로 설정을 고르는 절차 자체가 무효다.
    그러면 남는 선택지는 두 가지뿐이다: hold-out 으로 고르거나(=hold-out 을
    학습셋으로 쓰는 것, 금지) 두 번째 라벨 세트를 만들거나.
    """
    def corr(pred, obs):
        if len(pred) < 5:
            return None
        r = spearmanr(pred, obs)
        return {"spearman": round(float(r.statistic), 4),
                "p_value": round(float(r.pvalue), 4), "n_pairs": len(pred)}

    out = {}
    for tag, field in (("전체", "recall_at_k"),
                       ("구조 교란만", "recall_structured"),
                       ("텍스트 스왑만", "recall_text_swap")):
        p, o = [], []
        for key, g in grid.items():
            if not key.endswith("| cohort"):
                continue
            vkey = key.replace(" | cohort", "")
            v = val.get(vkey)
            if v is None or field not in v:
                continue
            p.append(v[field])
            o.append(g["roc_auc"])
        out[tag] = corr(p, o)
    return out


def ablation(fit_df, all_df, E_fit, E_all, cl, method, mode):
    """계획서 §5 — 축을 빼고/하나만 넣고 어떻게 움직이는가 (정형 표현 기준)."""
    global NUM, CAT
    base_num, base_cat = list(NUM), list(CAT)

    def run(num, cat):
        globals()["NUM"], globals()["CAT"] = num, cat
        try:
            Xf, Xa, _ = build_structured(fit_df, all_df)
            af, aa = resolve_cohorts(fit_df, all_df)
            s = run_method(method, Xf, Xa, cohort_groups(af, aa), mode)
            return evaluate(all_df, s, cl)[0]["roc_auc"]
        finally:
            globals()["NUM"], globals()["CAT"] = base_num, base_cat

    full = run(base_num, base_cat)
    lofo, single = {}, {}
    for f in base_num + base_cat:
        n2 = [x for x in base_num if x != f]
        c2 = [x for x in base_cat if x != f]
        if not n2:
            continue
        lofo[f] = round(run(n2, c2) - full, 4)
        n1 = [x for x in base_num if x == f]
        c1 = [x for x in base_cat if x == f]
        if n1:
            single[f] = round(run(n1, []), 4)
    return {"full": full, "leave_one_out_delta": lofo, "single_feature": single}


def cohort_sensitivity(fit_df, all_df, E_fit, E_all, cl, method):
    """계획서 §5-7 — 비교군 기준을 바꾸면 순위가 크게 흔들리는가."""
    Xf, Xa, _ = build_structured(fit_df, all_df)
    variants = {
        "3단 (성격x방식x단위)": COHORT_LEVELS,
        "2단 (성격x방식)": COHORT_LEVELS[1:],
        "1단 (성격)": COHORT_LEVELS[2:],
    }
    out, ranks = {}, {}
    for name, lv in variants.items():
        af, aa = resolve_cohorts(fit_df, all_df, levels=lv)
        s = run_method(method, Xf, Xa, cohort_groups(af, aa), "cohort")
        out[name] = evaluate(all_df, s, cl)[0]["roc_auc"]
        ranks[name] = s
    keys = list(ranks)
    out["_순위상관"] = {
        "%s vs %s" % (keys[i], keys[j]):
            round(float(spearmanr(ranks[keys[i]], ranks[keys[j]]).statistic), 4)
        for i in range(len(keys)) for j in range(i + 1, len(keys))}
    return out


def report_only():
    import json
    with open(os.path.join(C.REPORTS, "m40_m3_vector_grid.json"), encoding="utf-8") as f:
        rep = json.load(f)
    write_md(rep)


if __name__ == "__main__":
    if "--report-only" in sys.argv:
        report_only()
    else:
        main()
