r"""M75 — 유사사업 local cohort · 중분류 내부 subgroup expert 가 M73 을 이기는가.

지시서(사용자, `model2_similarity_local_and_subgroup_clustering_experiments.md`):

    현재 최고 후보는 M73 `soft/ordinal_xgb` (OOF 0.3563 / strict 0.3756) 이다.
    금액 자체로 구간을 나누는 대신 **사업 내용이 실제로 비슷한 공고끼리 묶으면
    금액 예측이 더 좋아지는가**를 두 축으로 검증한다.

        실험 1  유사사업 similarity 기반 local cohort 예측  (1순위)
        실험 2  중분류(support_type) 내부 semantic clustering + subgroup expert

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 (구조화 + 제목 SVD64 + 원천층 + 본문 SVD64)
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    구간정의   fold train y 의 P33.3 / P66.7
    baseline   M73 `soft/ordinal_xgb` — 매 fold 에서 같이 학습해 paired 로 잰다
    masking    m2_source_features 의 마스킹 본문 그대로. 다시 마스킹하지 않는다

바뀌는 것은 **이웃 정보를 예측에 넣는 방법** 하나다.

## 실험 1 — similarity local cohort

    표현    emb    M72 semantic 임베딩(frozen, ko-sroberta/chunk_mean) 코사인
            title  fold train 에 적합한 제목 TF-IDF/SVD64 코사인
            struct 구조화 feature 표준화 + one-hot 코사인
            hyb@w  w*emb + (1-w)*struct
    K       10 / 20 / 30 / 50 / 100
    local   median · mean · q25 · q75 · std · 유사도가중평균 · sim_top1 · sim_meanK
    구조 A  local 통계를 feature 로 붙여 M73 파이프라인 전체를 다시 학습
    구조 B  local 예측을 만들어 M73 예측과 blend — alpha*M73 + (1-alpha)*local

## 실험 2 — support_type 내부 clustering + subgroup expert

    표현    임베딩 SVD16 + 구조화 표준화 + support_method/unit one-hot (전부 train 적합)
    방법    support_type 별 KMeans(K=2/3/4). n>=30 인 cluster 만 expert 를 둔다
    라우팅  hard(배정 cluster expert) / soft(역제곱거리 가중) / blend(M73 과 고정비율)
    fallback  cluster 가 작거나 없으면 M73 soft

## 선택을 어디서 하는가 (지시서 '공통 원칙')

이 실험에서 가장 위험한 자리는 K·alpha·표현을 고르는 지점이다. outer OOF 에서
고르고 같은 OOF 로 재면 반드시 좋아 보인다(M68b 의 λ, M73 의 threshold 가 겪은
함정). 그래서 M73 과 같은 두 층 구조를 쓴다.

    nested   outer train 안에서 다시 GroupKFold(3) 을 돌려 inner OOF 를 만들고
             (표현·K·local 추정량·alpha) 를 **거기서만** 고른 뒤 outer test 에
             그대로 적용한다. 승격 판정은 이 값으로만 한다.
    sweep    고정 (표현,K,alpha) 를 전체 OOF 에 그대로 적용한 표. 곡선을 보기
             위한 진단이지 후보가 아니다. 여기서 최저값을 골라 쓰지 않는다.
    fixed    구조 A 의 feature 구성과 실험 2 의 cluster 규칙은 **사전 고정**이다.
             고르는 행위가 없으므로 선택편향이 없고, 그대로 후보가 된다.

실험 2 의 blend 비중은 nested 로 고르지 않고 0.25/0.50 을 사전 고정했다. inner
에서 cluster expert 를 다시 학습하면 fold 당 3분이 더 드는데, 그 비용은 실험 1
단계에서 이웃 신호가 확인된 뒤에 내는 것이 맞다. 이 선택을 하지 않았다는 사실
자체를 보고서에 적는다.

## 누수 방지 (지시서 'Leakage 방지')

    자기 자신을 이웃으로 넣지 않는다        query 와 pool 이 항상 분리된 행 집합
    같은 program_stem 계열 이웃 제외        GroupKFold 가 이미 보장 — 실측해서 적는다
    outer test 끼리 이웃으로 쓰지 않는다    pool 은 언제나 train 쪽뿐
    local 금액 통계는 train target 만       ypool = ytr (inner 에서는 inner-train y)
    similarity 표현은 train 적합            임베딩은 frozen, SVD/scaler/KMeans 는 fold 안
    train 행의 local feature 는 inner OOF   자기 이웃 통계를 그대로 학습하는 것을 막는다

마지막 줄이 이 실험의 조용한 함정이다. 학습행의 local 통계를 outer train 전체
에서 만들면, 그 행의 이웃 안에 자기 자신과 아주 가까운 행들이 들어 있어 모델이
local 통계를 실제보다 훨씬 믿게 된다(target encoding 의 고전적 누수). 그래서
학습행 쪽 통계는 inner OOF 로만 만든다 — test 행 쪽 pool(=outer train 전체)보다
pool 이 1/3 작다는 비대칭이 남지만, 낙관 쪽으로 기울지 않는 방향의 비대칭이다.

산출
    ml/data/processed/m75_similarity_local_oof.parquet
    ml/reports/m75_m2_similarity_local.json / .md
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
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

import io
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_semantic_body as SB
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m73_m2_routing_improvement as M73

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m75_similarity_local_oof.parquet")
MD = C.report_path("m75_m2_similarity_local.md")

BUCKETS = M73.BUCKETS
CUTS = M69.CUTS
STEP = "G"
INNER_SPLITS = 3

# M73 공표치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M73_PUBLISHED = {"soft_ordinal_MAE": 0.3563, "strict_MAE": 0.3756,
                 "global_MAE": 0.3719, "global_strict_MAE": 0.3931}
BASE = "M73 soft/ordinal_xgb"

# ------------------------------------------------------------ 실험 1 격자
EMB_POOLING = "chunk_mean"
EMB_MODEL = SB.PRIMARY
SPACES = ["emb", "title", "struct", "hyb0.25", "hyb0.50", "hyb0.75"]
KS = (10, 20, 30, 50, 100)
ESTIMATORS = ("median", "wmean")          # local 예측 추정량
ALPHAS = tuple(np.round(np.arange(0.0, 1.01, 0.1), 2))   # M73 쪽 가중치
WMEAN_TAU = 0.05                          # 유사도가중평균의 온도(코사인 스케일 기준)
LOCAL_STAT_NAMES = ("median", "mean", "q25", "q75", "std", "wmean",
                    "sim_top1", "sim_meanK")

# 구조 A 는 사전 고정이다 — 고르지 않으므로 선택편향이 없다.
FEAT_KS = (10, 30, 100)
FEAT_CONFIGS = [("emb", FEAT_KS), ("hyb0.50", FEAT_KS)]
# 구조 A 의 K·표현 민감도는 global 회귀 하나로만 잰다(진단용 곡선).
FEAT_SENS = [("emb", (10,)), ("emb", (30,)), ("emb", (100,)),
             ("title", (30,)), ("struct", (30,)), ("hyb0.50", (30,))]

# 진단용 고정 sweep (승격 근거 아님)
SWEEP = [("emb", 30, "median", 0.7), ("emb", 30, "median", 0.5),
         ("emb", 100, "median", 0.7), ("hyb0.50", 30, "median", 0.7),
         ("struct", 30, "median", 0.7), ("title", 30, "median", 0.7)]

# ------------------------------------------------------------ 실험 2 격자
CLUSTER_KS = (2, 3, 4)
MIN_CLUSTER = 30                 # 지시서 '권장 최소 cluster 크기'
EMB_SVD_FOR_CLUSTER = 16
CLUSTER_BLENDS = (0.25, 0.50)    # M73 쪽 가중치 — 사전 고정

CODE_VERSION = "m75-v1"
CKPT_DIR = os.path.join(C.PROC, "m75_ckpt")


# ============================================================ 표현 · 유사도
def l2(A):
    n = np.linalg.norm(A, axis=1, keepdims=True)
    return A / np.clip(n, 1e-12, None)


def struct_space(Xs, tr, te):
    """구조화 feature 의 코사인 공간. one-hot 과 표준화 모두 train 에만 적합한다."""
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    A, B = Xs.iloc[tr], Xs.iloc[te]
    cat = [c for c in A.columns if str(A[c].dtype) == "category"]
    num = [c for c in A.columns if c not in cat]
    enc = OneHotEncoder(handle_unknown="ignore", sparse_output=False, min_frequency=5)
    Ca = enc.fit_transform(A[cat].astype(str))
    Cb = enc.transform(B[cat].astype(str))
    na = A[num].to_numpy(dtype=float)
    nb = B[num].to_numpy(dtype=float)
    med = np.nanmedian(np.where(np.isfinite(na), na, np.nan), axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    na = np.where(np.isfinite(na), na, med)
    nb = np.where(np.isfinite(nb), nb, med)
    sc = StandardScaler().fit(na)
    return (l2(np.hstack([sc.transform(na), Ca])),
            l2(np.hstack([sc.transform(nb), Cb])))


def build_spaces(Xs, Xtr, Xte, E, tr, te):
    """표현별 (train 벡터, test 벡터). 전부 outer train 에서만 적합한다.

    emb 만 예외적으로 '적합'이 없다 — frozen pretrained 인코더의 출력이라
    데이터에 맞춰지는 변환이 아니다(M72 3장 누수 점검과 같은 근거).
    """
    cols = F.title_columns()
    out = {"emb": (l2(E[tr].astype(float)), l2(E[te].astype(float))),
           "title": (l2(Xtr[cols].to_numpy(float)), l2(Xte[cols].to_numpy(float)))}
    out["struct"] = struct_space(Xs, tr, te)
    return out


def all_sims(vec_q, vec_p):
    """표현별 (n_query, n_pool) 코사인 유사도. hybrid 는 두 행렬의 가중합이다."""
    s = {k: vec_q[k] @ vec_p[k].T for k in ("emb", "title", "struct")}
    for w in (0.25, 0.50, 0.75):
        s["hyb%.2f" % w] = w * s["emb"] + (1.0 - w) * s["struct"]
    return s


# ============================================================ local 통계
def topk_stats(S, ypool, K, tau=WMEAN_TAU):
    """Top-K 이웃의 금액 통계. y 는 pool(=train) 쪽 것만 쓴다."""
    K = int(min(K, S.shape[1]))
    idx = np.argpartition(-S, K - 1, axis=1)[:, :K]
    sims = np.take_along_axis(S, idx, 1)
    order = np.argsort(-sims, axis=1)
    idx = np.take_along_axis(idx, order, 1)
    sims = np.take_along_axis(sims, order, 1)
    v = ypool[idx]
    w = np.exp((sims - sims[:, :1]) / tau)
    w = w / np.clip(w.sum(1, keepdims=True), 1e-12, None)
    return {"median": np.median(v, 1), "mean": v.mean(1),
            "q25": np.percentile(v, 25, axis=1),
            "q75": np.percentile(v, 75, axis=1),
            "std": v.std(1), "wmean": (w * v).sum(1),
            "sim_top1": sims[:, 0], "sim_meanK": sims.mean(1),
            "_idx": idx}


def local_bank(sims, ypool, ks=KS):
    """표현 x K 전체의 local 통계. numpy 연산뿐이라 격자를 넓게 둬도 싸다."""
    return {(sp, k): topk_stats(S, ypool, k) for sp, S in sims.items() for k in ks}


def local_columns(bank, space, ks, prefix="loc"):
    """구조 A 용 feature 프레임. 컬럼 이름과 순서를 고정한다."""
    return pd.DataFrame({"%s_%s_k%d" % (prefix, nm, k): bank[(space, k)][nm]
                         for k in ks for nm in LOCAL_STAT_NAMES})


# ============================================================ M73 재현 블록
def m73_block(Xtr, ytr, Xte):
    """global · 구간 expert 3 · ordinal Stage1 · soft 예측. M73 코드 경로 그대로."""
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    g = F.make_point_model().fit(Xtr, ytr).predict(Xte)
    tab = np.zeros((len(Xte), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)
    pr = M73.stage1_proba("ordinal_xgb", Xtr, ztr, Xte)
    return {"edges": edges, "global": g, "table": tab, "proba": pr,
            "soft": M73.route_soft(tab, pr)}


# ============================================================ 실험 2 clustering
def cluster_space(Xs, E, tr, te):
    """clustering 입력. 금액 target 은 쓰지 않는다(지시서 '금액 target 미사용')."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    svd = TruncatedSVD(n_components=EMB_SVD_FOR_CLUSTER,
                       random_state=F.PIPELINE_SEED)
    ea = svd.fit_transform(l2(E[tr].astype(float)))
    eb = svd.transform(l2(E[te].astype(float)))
    A, B = Xs.iloc[tr], Xs.iloc[te]
    keep_cat = [c for c in ("support_method", "support_unit") if c in A.columns]
    enc = OneHotEncoder(handle_unknown="ignore", sparse_output=False, min_frequency=5)
    Ca = enc.fit_transform(A[keep_cat].astype(str))
    Cb = enc.transform(B[keep_cat].astype(str))
    num = [c for c in ("support_count", "support_ratio", "project_duration",
                       "self_burden_ratio") if c in A.columns]
    na = A[num].to_numpy(float)
    nb = B[num].to_numpy(float)
    med = np.nanmedian(np.where(np.isfinite(na), na, np.nan), axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    na = np.where(np.isfinite(na), na, med)
    nb = np.where(np.isfinite(nb), nb, med)
    sc1 = StandardScaler().fit(ea)
    sc2 = StandardScaler().fit(na)
    return (np.hstack([sc1.transform(ea), sc2.transform(na), Ca]),
            np.hstack([sc1.transform(eb), sc2.transform(nb), Cb]))


def cluster_experts(Xtr, ytr, Xte, Rtr, Rte, st_tr, st_te, K, fallback_te):
    """support_type 안에서 KMeans(K) -> cluster expert. 작은 cluster 는 fallback.

    cluster 배정은 KMeans 거리만으로 결정되므로 test y 를 보지 않는다.
    cluster 별 금액 분산은 **배정이 끝난 뒤의 진단**으로만 계산한다(지시서
    '중요: target 분포는 clustering 이후 진단에만 사용').
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    hard = fallback_te.copy()
    soft = fallback_te.copy()
    used = np.zeros(len(Xte), dtype=bool)
    diag = []
    for s in pd.unique(st_tr):
        mtr = st_tr == s
        mte = st_te == s
        if mtr.sum() < MIN_CLUSTER * K or not mte.any():
            diag.append({"support_type": str(s), "n_train": int(mtr.sum()),
                         "n_test": int(mte.sum()), "clustered": False,
                         "reason": "n_train < %d" % (MIN_CLUSTER * K)})
            continue
        km = KMeans(n_clusters=K, n_init=10, random_state=F.PIPELINE_SEED)
        lab = km.fit_predict(Rtr[mtr])
        sizes = np.bincount(lab, minlength=K)
        valid = [c for c in range(K) if sizes[c] >= MIN_CLUSTER]
        ys = ytr[mtr]
        within = float(np.average([ys[lab == c].var() if sizes[c] else 0.0
                                   for c in range(K)],
                                  weights=np.maximum(sizes, 1)))
        try:
            sil = (float(silhouette_score(Rtr[mtr], lab))
                   if len(set(lab.tolist())) > 1 else float("nan"))
        except Exception:
            sil = float("nan")
        diag.append({
            "support_type": str(s), "n_train": int(mtr.sum()),
            "n_test": int(mte.sum()), "clustered": True, "K": K,
            "sizes": sizes.tolist(), "valid_clusters": valid,
            "silhouette": round(sil, 4),
            "var_support_type": round(float(ys.var()), 4),
            "var_within_cluster": round(within, 4),
            "var_reduction": round(float(1 - within / max(float(ys.var()), 1e-12)), 4),
            "cluster_median": [round(float(np.median(ys[lab == c])), 4)
                               if sizes[c] else None for c in range(K)],
            "cluster_iqr": [round(float(np.subtract(*np.percentile(ys[lab == c],
                                                                  [75, 25]))), 4)
                            if sizes[c] > 1 else None for c in range(K)]})
        if not valid:
            continue
        idx_tr = np.where(mtr)[0]
        idx_te = np.where(mte)[0]
        preds = {}
        for c in valid:
            rows = idx_tr[lab == c]
            preds[c] = F.make_point_model().fit(
                Xtr.iloc[rows], ytr[rows]).predict(Xte.iloc[idx_te])
        D = km.transform(Rte[mte])[:, valid]
        W = 1.0 / np.clip(D, 1e-6, None) ** 2
        W = W / W.sum(1, keepdims=True)
        P = np.column_stack([preds[c] for c in valid])
        soft[idx_te] = (W * P).sum(1)
        assign = km.predict(Rte[mte])
        for j, c in enumerate(valid):
            sel = assign == c
            if sel.any():
                hard[idx_te[sel]] = preds[c][sel]
        used[idx_te] = True
    return {"hard": hard, "soft": soft, "used": used, "diag": diag}


# ============================================================ 한 fold
def fold_compute(Xs, y, groups, titles, body, E, NB, cats, st, tr, te, i):
    """이 fold 의 모델 출력 전부. 비싼 계산은 여기 한 곳에 모여 있다."""
    from sklearn.model_selection import GroupKFold

    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xtr, Xte = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te], STEP, [None])
    ytr, yte = y[tr], y[te]
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    # --- 표현 · 유사도 · test 쪽 local 통계 --------------------------------
    V = build_spaces(Xs, Xtr, Xte, E, tr, te)
    Sq = all_sims({k: v[1] for k, v in V.items()}, {k: v[0] for k, v in V.items()})
    bank_te = local_bank(Sq, ytr)

    # 이웃이 정말 다른 사업 계열인가 — GroupKFold 가 보장하지만 실측해서 적는다.
    gtr, gte = groups[tr], groups[te]
    nb = bank_te[("emb", 30)]["_idx"]
    same_group = int(sum(int((gtr[nb[r]] == gte[r]).sum()) for r in range(len(te))))

    # --- M73 기준선 --------------------------------------------------------
    B = m73_block(Xtr, ytr, Xte)
    zte = M73.to_bucket(yte, B["edges"])

    # --- inner OOF: 선택과 train 쪽 local feature 의 유일한 출처 ------------
    ntr = len(tr)
    in_glob = np.zeros(ntr)
    in_soft = np.zeros(ntr)
    in_bank = {(sp, k): {nm: np.zeros(ntr) for nm in LOCAL_STAT_NAMES}
               for sp in SPACES for k in KS}
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        ib = m73_block(Xtr.iloc[a], ytr[a], Xtr.iloc[b])
        in_glob[b] = ib["global"]
        in_soft[b] = ib["soft"]
        Sb = all_sims({k: v[0][b] for k, v in V.items()},
                      {k: v[0][a] for k, v in V.items()})
        for key, stt in local_bank(Sb, ytr[a]).items():
            for nm in LOCAL_STAT_NAMES:
                in_bank[key][nm][b] = stt[nm]

    def train_frame(sp, ks):
        return pd.DataFrame({"loc_%s_k%d" % (nm, k): in_bank[(sp, k)][nm]
                             for k in ks for nm in LOCAL_STAT_NAMES})

    # --- 구조 A: local 통계를 feature 로 (사전 고정 구성) -------------------
    A_out, A_sens = {}, {}
    for sp, ks in FEAT_CONFIGS:
        Xa = pd.concat([Xtr.reset_index(drop=True), train_frame(sp, ks)], axis=1)
        Xb = pd.concat([Xte.reset_index(drop=True),
                        local_columns(bank_te, sp, ks)], axis=1)
        blk = m73_block(Xa, ytr, Xb)
        A_out["A/%s/multiK" % sp] = {"global": blk["global"], "soft": blk["soft"]}
    for sp, ks in FEAT_SENS:
        Xa = pd.concat([Xtr.reset_index(drop=True), train_frame(sp, ks)], axis=1)
        Xb = pd.concat([Xte.reset_index(drop=True),
                        local_columns(bank_te, sp, ks)], axis=1)
        A_sens["Aglob/%s/k%s" % (sp, ",".join(str(k) for k in ks))] = (
            F.make_point_model().fit(Xa, ytr).predict(Xb))

    # --- 구조 B: nested 로 (표현,K,추정량,alpha) 를 고른다 ------------------
    best, best_mae = None, np.inf
    for sp in SPACES:
        for k in KS:
            for est in ESTIMATORS:
                lp = in_bank[(sp, k)][est]
                for al in ALPHAS:
                    mae = float(np.abs(al * in_soft + (1 - al) * lp - ytr).mean())
                    if mae < best_mae - 1e-12:
                        best, best_mae = (sp, int(k), est, float(al)), mae
    # 좁은 격자 — 표현·K 를 사전 고정하고 alpha 만 고른다(과탐색 대조군)
    best_a, best_a_mae = None, np.inf
    for al in ALPHAS:
        lp = in_bank[("emb", 30)]["median"]
        mae = float(np.abs(al * in_soft + (1 - al) * lp - ytr).mean())
        if mae < best_a_mae - 1e-12:
            best_a, best_a_mae = float(al), mae

    # --- 실험 2: cluster expert --------------------------------------------
    Rtr, Rte = cluster_space(Xs, E, tr, te)
    clus = {K: cluster_experts(Xtr, ytr, Xte, Rtr, Rte, st[tr], st[te], K, B["soft"])
            for K in CLUSTER_KS}

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in B["edges"]],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "global_MAE": round(float(np.abs(B["global"] - yte).mean()), 4),
           "m73_soft_MAE": round(float(np.abs(B["soft"] - yte).mean()), 4),
           "inner_m73_soft_MAE": round(float(np.abs(in_soft - ytr).mean()), 4),
           "inner_global_MAE": round(float(np.abs(in_glob - ytr).mean()), 4),
           "nested_pick": {"space": best[0], "K": best[1], "estimator": best[2],
                           "alpha": best[3], "inner_MAE": round(best_mae, 4)},
           "nested_alpha_only": {"alpha": best_a, "inner_MAE": round(best_a_mae, 4)},
           "neighbors_sharing_group": same_group,
           "cluster_diag": {str(K): clus[K]["diag"] for K in CLUSTER_KS},
           "cluster_fallback_rate": {str(K): round(float(1 - clus[K]["used"].mean()), 4)
                                     for K in CLUSTER_KS},
           "seconds": round(time.time() - t0, 1)}

    return {"te": np.asarray(te), "base": base_te, "z_true": zte,
            "p_glob": B["global"], "p_soft": B["soft"], "table": B["table"],
            "proba": B["proba"],
            "bank": {"%s|%d|%s" % (sp, k, est): bank_te[(sp, k)][est]
                     for sp in SPACES for k in KS for est in ESTIMATORS},
            "A": A_out, "A_sens": A_sens,
            "clus": {K: {"hard": clus[K]["hard"], "soft": clus[K]["soft"],
                         "used": clus[K]["used"]} for K in CLUSTER_KS},
            "pick": best, "pick_alpha": best_a, "rec": rec}


# ============================================================ 체크포인트
# 한 fold 가 여러 분 걸린다. 끊겼을 때 처음부터 다시 돌리지 않도록 **모델
# 출력만** fold 단위로 저장한다. blend·gating 은 numpy 라 매번 다시 만든다.
def ckpt_signature(fp):
    """이 서명이 다르면 체크포인트를 읽지 않는다 — 코드·데이터·격자·seed 전부 포함."""
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb_point": F.XGB_POINT, "spaces": SPACES, "ks": list(KS),
        "estimators": list(ESTIMATORS), "alphas": [float(a) for a in ALPHAS],
        "feat_configs": [[s, list(k)] for s, k in FEAT_CONFIGS],
        "feat_sens": [[s, list(k)] for s, k in FEAT_SENS],
        "cluster_ks": list(CLUSTER_KS), "min_cluster": MIN_CLUSTER,
        "emb_svd": EMB_SVD_FOR_CLUSTER, "tau": WMEAN_TAU,
        "encoder": EMB_MODEL, "pooling": EMB_POOLING,
        "inner_splits": INNER_SPLITS, "step": STEP, "cuts": list(CUTS),
        "feature_version": F.FEATURE_VERSION, "layer_version": SF.LAYER_VERSION,
        "semantic_version": SB.SEMANTIC_VERSION, "body_svd": M69.BODY_SVD,
        "seed": F.PIPELINE_SEED, "n_splits": F.N_SPLITS,
    }, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ckpt_paths(sig, tag, i):
    d = os.path.join(CKPT_DIR, "%s__%s" % (sig, tag))
    return d, os.path.join(d, "fold%d.npz" % i), os.path.join(d, "fold%d.json" % i)


def ckpt_load(sig, tag, i):
    import json as _json
    _, npz, js = ckpt_paths(sig, tag, i)
    if not (os.path.exists(npz) and os.path.exists(js)):
        return None
    try:
        z = np.load(npz)
        meta = _json.load(io.open(js, encoding="utf-8"))
    except Exception:
        return None
    out = {"te": z["te"], "base": z["base"], "z_true": z["z_true"],
           "p_glob": z["p_glob"], "p_soft": z["p_soft"], "table": z["table"],
           "proba": z["proba"],
           "bank": {k[5:]: z[k] for k in z.files if k.startswith("bank|")},
           "A": {}, "A_sens": {k[6:]: z[k] for k in z.files if k.startswith("asens|")},
           "clus": {}, "pick": tuple(meta["pick"]), "pick_alpha": meta["pick_alpha"],
           "rec": meta["rec"]}
    for k in z.files:
        if k.startswith("aglob|"):
            out["A"].setdefault(k[6:], {})["global"] = z[k]
        elif k.startswith("asoft|"):
            out["A"].setdefault(k[6:], {})["soft"] = z[k]
        elif k.startswith("clus|"):
            _, K, which = k.split("|")
            out["clus"].setdefault(int(K), {})[which] = (
                z[k].astype(bool) if which == "used" else z[k])
    return out


def ckpt_save(sig, tag, i, fo):
    import json as _json
    d, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(d, exist_ok=True)
    arr = {"te": fo["te"], "base": fo["base"], "z_true": fo["z_true"],
           "p_glob": fo["p_glob"], "p_soft": fo["p_soft"], "table": fo["table"],
           "proba": fo["proba"]}
    arr.update({"bank|" + k: v for k, v in fo["bank"].items()})
    arr.update({"asens|" + k: v for k, v in fo["A_sens"].items()})
    for k, v in fo["A"].items():
        arr["aglob|" + k] = v["global"]
        arr["asoft|" + k] = v["soft"]
    for K, v in fo["clus"].items():
        for w, a in v.items():
            arr["clus|%d|%s" % (K, w)] = a
    np.savez_compressed(npz + ".tmp.npz", **arr)
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"pick": list(fo["pick"]), "pick_alpha": fo["pick_alpha"],
                             "rec": fo["rec"]}, ensure_ascii=False, default=str))
    # 두 파일을 다 쓴 뒤에 바꿔 단다 — 쓰다 끊긴 반쪽 체크포인트를 읽지 않는다.
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ============================================================ 한 split 전체
def run_split(Xs, y, groups, titles, body, E, NB, cats, st, sig, tag,
              verbose=True, use_ckpt=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    z_true = np.zeros(n, dtype=int)
    base = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    p_glob = np.zeros(n)
    p_soft = np.zeros(n)
    bank, pred, used = {}, {}, {}
    picks, picks_alpha, per_fold = [], [], []

    def put(store, key, idx, val):
        if key not in store:
            store[key] = (np.zeros(n, dtype=bool) if val.dtype == bool
                          else np.zeros(n))
        store[key][idx] = val

    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(Xs, y, groups, titles, body, E, NB, cats, st, tr, te, i)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        fold_id[te] = i
        base[te] = fo["base"]
        z_true[te] = fo["z_true"]
        p_glob[te] = fo["p_glob"]
        p_soft[te] = fo["p_soft"]
        for k, v in fo["bank"].items():
            bank.setdefault(k, np.zeros(n))[te] = v
        ones = np.ones(len(te), dtype=bool)

        # --- 구조 A (사전 고정 구성) ---------------------------------------
        for k, v in fo["A"].items():
            put(pred, k, te, v["soft"])
            put(used, k, te, ones)
            put(pred, k.replace("A/", "Aglobal/", 1), te, v["global"])
            put(used, k.replace("A/", "Aglobal/", 1), te, ones)
        for k, v in fo["A_sens"].items():
            put(pred, k, te, v)
            put(used, k, te, ones)

        # --- 구조 B (nested 선택) ------------------------------------------
        sp, K, est, al = fo["pick"]
        lp = fo["bank"]["%s|%d|%s" % (sp, int(K), est)]
        put(pred, "B/nested", te, float(al) * fo["p_soft"] + (1 - float(al)) * lp)
        put(used, "B/nested", te, ones)
        # K 민감도는 '순수 local' 이 아니라 **실제 후보**에서 재야 의미가 있다.
        # fold 가 고른 (표현,추정량,alpha) 를 그대로 두고 K 만 갈아 끼운다.
        for kk in KS:
            lpk = fo["bank"]["%s|%d|%s" % (sp, kk, est)]
            put(pred, "Ksens/blend/k%d" % kk, te,
                float(al) * fo["p_soft"] + (1 - float(al)) * lpk)
            put(used, "Ksens/blend/k%d" % kk, te, ones)

        alp = float(fo["pick_alpha"])
        lp2 = fo["bank"]["emb|30|median"]
        put(pred, "B/nested_alpha(emb,30,median)", te,
            alp * fo["p_soft"] + (1 - alp) * lp2)
        put(used, "B/nested_alpha(emb,30,median)", te, ones)
        picks.append(list(fo["pick"]))
        picks_alpha.append(alp)

        # --- 진단용: 순수 local · 고정 sweep --------------------------------
        for spx in SPACES:
            for kk in KS:
                put(pred, "local_only/%s/k%d" % (spx, kk), te,
                    fo["bank"]["%s|%d|median" % (spx, kk)])
                put(used, "local_only/%s/k%d" % (spx, kk), te, ones)
        for spx, kk, est2, al2 in SWEEP:
            lp3 = fo["bank"]["%s|%d|%s" % (spx, kk, est2)]
            key = "sweep/%s/k%d/%s/a%.2f" % (spx, kk, est2, al2)
            put(pred, key, te, al2 * fo["p_soft"] + (1 - al2) * lp3)
            put(used, key, te, ones)

        # --- 실험 2 ---------------------------------------------------------
        for K2, v in fo["clus"].items():
            put(pred, "clus%d/hard" % K2, te, v["hard"])
            put(used, "clus%d/hard" % K2, te, v["used"])
            put(pred, "clus%d/soft" % K2, te, v["soft"])
            put(used, "clus%d/soft" % K2, te, v["used"])
            for bl in CLUSTER_BLENDS:
                put(pred, "clus%d/blend@%.2f" % (K2, bl), te,
                    bl * fo["p_soft"] + (1 - bl) * v["soft"])
                put(used, "clus%d/blend@%.2f" % (K2, bl), te, v["used"])

        rec = fo["rec"]
        rec["from_checkpoint"] = bool(cached)
        per_fold.append(rec)
        if verbose:
            np_ = rec["nested_pick"]
            print("   fold %d  M73soft %.4f  global %.4f  nested (%s,K=%d,%s,a=%.1f)"
                  "  (%s)" % (i, rec["m73_soft_MAE"], rec["global_MAE"],
                              np_["space"], np_["K"], np_["estimator"], np_["alpha"],
                              "체크포인트 재사용" if cached
                              else "%.0f초" % rec["seconds"]))
    return dict(z_true=z_true, base=base, fold_id=fold_id, p_glob=p_glob,
                p_soft=p_soft, bank=bank, pred=pred, used=used, picks=picks,
                picks_alpha=picks_alpha, folds=per_fold)


# ============================================================ 지표
def fold_maes(y, p, fold_id):
    return [round(float(np.abs(p[fold_id == i] - y[fold_id == i]).mean()), 4)
            for i in sorted(set(fold_id.tolist()))]


def cohort_mae(d, y, p):
    out = {}
    for col in ("cohort", "evidence_source"):
        rows = {}
        for k, idx in d.groupby(col, observed=True).groups.items():
            i = d.index.get_indexer(idx)
            rows[str(k)] = {"n": int(len(i)),
                            "MAE": round(float(np.abs(p[i] - y[i]).mean()), 4)}
        out[col] = rows
    return out


def summarize(d, y, R):
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]

    def block(p, ref=None):
        m = M45.point_metrics(y, p)
        m["improvement"] = round(float((b - m["MAE_log10"]) / b), 4)
        m["per_fold_MAE"] = fold_maes(y, p, fid)
        m["fold_std"] = round(float(np.std(m["per_fold_MAE"])), 4)
        m["buckets"] = M73.bucket_metrics(y, p, R["z_true"])
        m["cohort"] = cohort_mae(d, y, p)
        if ref is not None:
            m["vs_base"] = M73.paired_test(y, p, ref)
            rf = fold_maes(y, ref, fid)
            m["fold_wins_vs_base"] = int(sum(1 for a, c in zip(m["per_fold_MAE"], rf)
                                             if a < c))
        return m

    variants = {}
    for key, p in R["pred"].items():
        m = block(p, R["p_soft"])
        u = R["used"][key]
        m["route_share"] = round(float(u.mean()), 4)
        if u.any() and not u.all():
            e_new, e_old = np.abs(p[u] - y[u]), np.abs(R["p_soft"][u] - y[u])
            m["on_routed"] = {"n": int(u.sum()),
                              "MAE_new": round(float(e_new.mean()), 4),
                              "MAE_base": round(float(e_old.mean()), 4),
                              "win_rate": round(float((e_new < e_old).mean()), 4)}
        variants[key] = m
    return {"baseline_MAE": round(b, 4), "global": block(R["p_glob"]),
            "m73_soft": block(R["p_soft"]), "variants": variants,
            "folds": R["folds"]}


def k_sensitivity(y, R):
    """K 를 바꿨을 때 순수 local 예측이 얼마나 흔들리는가 (이웃 신호의 진단)."""
    return {sp: {"K=%d" % k: round(float(np.abs(
        R["pred"]["local_only/%s/k%d" % (sp, k)] - y).mean()), 4) for k in KS}
        for sp in SPACES}


def blend_k_curve(y, R):
    """통과기준 6번의 근거 — **최종 후보**가 K 에 얼마나 흔들리는가.

    순수 local 예측의 K 곡선은 약한 예측기의 곡선이라 당연히 크게 움직인다.
    우리가 알고 싶은 것은 '서빙할 예측이 K 선택에 좌우되는가'이므로, fold 가
    고른 (표현·추정량·alpha) 를 고정한 채 K 만 갈아 끼워 잰다.
    """
    return {"K=%d" % k: round(float(np.abs(
        R["pred"]["Ksens/blend/k%d" % k] - y).mean()), 4) for k in KS}


def honest_variants(res):
    """정직한 후보 = nested 로 고른 것 + 사전 고정 규칙.

    sweep · local_only · Aglob 은 같은 OOF 에서 고르고 같은 OOF 로 재는 표라
    후보에서 뺀다. 진단용 곡선으로만 남긴다.
    """
    return [k for k in res["variants"]
            if k.startswith(("B/nested", "A/")) or
            (k.startswith("clus") and any(t in k for t in ("/hard", "/soft", "blend")))]


# ============================================================ main
def main():
    t0 = time.time()
    print("== 데이터 — M73 과 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    st = d["support_type"].astype(str).to_numpy()
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    print("\n== 원천 feature 층 + M72 임베딩")
    NB, body, _src = SF.build(d)
    E, emeta = SB.embed(list(body), model_name=EMB_MODEL, pooling=EMB_POOLING)
    print("   feature %d개 + 본문 SVD%d / 임베딩 %s (캐시 %s)"
          % (len(SF.columns_upto(STEP)), M69.BODY_SVD, E.shape, emeta["cached"]))

    sig = ckpt_signature(fp)
    print("\n== 체크포인트 서명 %s  ->  %s" % (sig, os.path.relpath(CKPT_DIR, C.ROOT)))

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        print("\n== 5-fold [%s]" % gname)
        R = run_split(Xs, y, groups[gname], titles, body, E, NB, cats, st, sig, gname)
        res = summarize(d, y, R)
        res["k_sensitivity"] = k_sensitivity(y, R)
        res["blend_k_curve"] = blend_k_curve(y, R)
        res["nested_picks"] = R["picks"]
        res["nested_alpha_picks"] = R["picks_alpha"]
        results[gname] = res
        raws[gname] = R
        print("   M73 soft %.4f / global %.4f / 비교군 baseline %.4f"
              % (res["m73_soft"]["MAE_log10"], res["global"]["MAE_log10"],
                 res["baseline_MAE"]))
        for k in sorted(res["variants"],
                        key=lambda k: res["variants"][k]["MAE_log10"])[:8]:
            m = res["variants"][k]
            print("      %-40s %.4f  Δ %+0.4f  fold승 %d/5"
                  % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"],
                     m["fold_wins_vs_base"]))

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]

    honest = honest_variants(ps)
    best = min(honest, key=lambda k: ps["variants"][k]["MAE_log10"])
    print("\n== 정직한 후보 중 최저 MAE: %s (%.4f) vs M73 soft %.4f"
          % (best, ps["variants"][best]["MAE_log10"], ps["m73_soft"]["MAE_log10"]))

    # ---------------------------------------------------------- 재현성
    # 체크포인트 namespace 를 `__repro` 로 따로 둔다 — 같은 파일을 다시 읽으면
    # 같은 숫자를 자기 자신과 비교하는 것이라 점검이 무의미해진다.
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    R2 = run_split(Xs, y, groups["program_stem"], titles, body, E, NB, cats, st,
                   sig, "program_stem__repro", verbose=False)
    repro = {"m73_soft": bool(np.allclose(R2["p_soft"], Rp["p_soft"])),
             "global": bool(np.allclose(R2["p_glob"], Rp["p_glob"])),
             best: bool(np.allclose(R2["pred"][best], Rp["pred"][best]))}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ---------------------------------------------------------- M73 재현 대조
    try:
        old = pd.read_parquet(os.path.join(C.PROC, "m73_routing_oof.parquet"))
        m73_match = {
            "row_id_identical": bool((old["row_id"].to_numpy()
                                      == d["row_id"].to_numpy()).all()),
            "soft_ordinal_max_abs_diff": round(float(np.max(np.abs(
                old["pred_soft__ordinal_xgb"].to_numpy() - Rp["p_soft"]))), 6),
            "global_max_abs_diff": round(float(np.max(np.abs(
                old["pred_global"].to_numpy() - Rp["p_glob"]))), 6)}
    except Exception as e:
        m73_match = {"error": str(e)}

    # ---------------------------------------------------------- 누수 점검
    same_group_total = int(sum(f["neighbors_sharing_group"] for f in ps["folds"]))
    leak = {
        "이웃 pool": "언제나 train 쪽 행만 — outer test 끼리 이웃이 되지 않는다",
        "자기 자신 이웃 포함": "불가 — query 와 pool 이 분리된 행 집합이다",
        "같은 그룹 이웃 (emb/K=30 실측)":
            "%d 건 (GroupKFold 가 이미 분리)" % same_group_total,
        "local 금액 통계 입력": "pool 쪽 y 만 (test y 는 최종 metric 에만)",
        "train 행의 local feature": "outer train 안 inner GroupKFold(%d) OOF" % INNER_SPLITS,
        "similarity 표현 적합":
            "emb 은 frozen pretrained, 제목 SVD·구조화 scaler 는 fold train 에서만",
        "clustering 입력": "임베딩 SVD + 구조화 feature — 금액 target 미사용",
        "cluster 별 금액 분산": "배정이 끝난 뒤의 진단값 — clustering 입력이 아니다",
        "K·alpha·표현 선택 입력": "inner OOF MAE 만",
        "fold 별 nested 선택": str(ps["nested_picks"]),
        "선택이 fold 마다 흔들린다(전체 사전선택 아님)":
            str(len({tuple(p) for p in ps["nested_picks"]}) > 1),
    }
    leak_checks = {
        "이웃이 같은 사업 계열을 포함하지 않는다": same_group_total == 0,
        "M73 soft/ordinal 재현 (0.3563)":
            abs(ps["m73_soft"]["MAE_log10"]
                - M73_PUBLISHED["soft_ordinal_MAE"]) < 0.005,
        "M69 global 재현 (0.3719)":
            abs(ps["global"]["MAE_log10"] - M73_PUBLISHED["global_MAE"]) < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-40s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ---------------------------------------------------------- 승격 판정
    Bm = ps["variants"][best]
    v = Bm["vs_base"]
    nt_best = nt["variants"].get(best)
    ks_spread = {sp: round(max(r.values()) - min(r.values()), 4)
                 for sp, r in ps["k_sensitivity"].items()}
    kc = ps["blend_k_curve"]
    kc_spread = round(max(kc.values()) - min(kc.values()), 4)
    coh, base_coh = Bm["cohort"]["cohort"], ps["m73_soft"]["cohort"]["cohort"]
    both_cohorts = all(coh[k]["MAE"] <= base_coh[k]["MAE"] for k in coh)
    var_red = [r["var_reduction"] for f in ps["folds"]
               for r in f["cluster_diag"].get("3", []) if r.get("clustered")]
    checks = {
        "1. OOF MAE < 0.3563 (M73)": Bm["MAE_log10"] < ps["m73_soft"]["MAE_log10"],
        "2. 엄격 split 에서도 개선":
            bool(nt_best and nt_best["MAE_log10"] < nt["m73_soft"]["MAE_log10"]),
        "3. 5개 fold 중 4개 이상 개선": Bm["fold_wins_vs_base"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. bizinfo/taxonomy 한쪽에만 의존하지 않는다": bool(both_cohorts),
        # 기준 0.02 는 M73 의 paired CI 반폭(≈0.007)의 3배다. K 를 어떻게 골라도
        # 검출 가능한 차이의 몇 배 안에서 움직인다면 '과도하게 민감'하지 않다고 본다.
        "6. K 변화에 과도하게 민감하지 않다 (후보 K곡선 spread < 0.02)":
            bool(kc_spread < 0.02),
        "7. leakage audit PASS": bool(leak_pass),
        "8. reproducibility PASS": all(repro.values()),
        "9. 1차 목표 MAE < 0.35": Bm["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("9.")]
    verdict = ("승격 후보 (M73 대체)" if all(checks[k] for k in core)
               else "현행 유지 (M73 soft/ordinal_xgb)")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ---------------------------------------------------------- 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "pred_global": Rp["p_glob"], "pred_m73_soft": Rp["p_soft"],
           "cohort": d["cohort"].to_numpy(),
           "support_type": st, "evidence_source": d["evidence_source"].to_numpy()}
    for k, p in Rp["bank"].items():
        out["local_" + k.replace("|", "_")] = p
    for k, p in Rp["pred"].items():
        out["pred_" + (k.replace("/", "__").replace("@", "_").replace(",", "_")
                       .replace("(", "").replace(")", ""))] = p
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "유사사업 local cohort · support_type 내부 subgroup expert 가 "
                   "M73 soft/ordinal_xgb(0.3563) 를 이기는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"],
            "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "regressor": F.XGB_POINT, "bucket_cuts": list(CUTS), "baseline": BASE,
        },
        "changed": "이웃 정보를 예측에 넣는 방법 — similarity local cohort · "
                   "support_type 내부 clustering expert",
        "experiment1": {
            "spaces": SPACES, "K": list(KS), "estimators": list(ESTIMATORS),
            "alphas": [float(a) for a in ALPHAS], "wmean_tau": WMEAN_TAU,
            "local_stats": list(LOCAL_STAT_NAMES),
            "feature_configs": [[s, list(k)] for s, k in FEAT_CONFIGS],
            "encoder": EMB_MODEL, "pooling": EMB_POOLING,
        },
        "experiment2": {
            "cluster_ks": list(CLUSTER_KS), "min_cluster": MIN_CLUSTER,
            "representation": "임베딩 SVD%d + 구조화 표준화 + support_method/unit one-hot"
                              % EMB_SVD_FOR_CLUSTER,
            "method": "support_type 별 KMeans(n_init=10)",
            "routing": "hard(배정) / soft(역제곱거리 가중) / blend(M73 과 고정비율)",
            "blend_weights_on_m73": list(CLUSTER_BLENDS),
            "fallback": "cluster n<%d 이거나 support_type n_train<%d*K 이면 M73 soft"
                        % (MIN_CLUSTER, MIN_CLUSTER),
            "not_selected": "blend 비중을 nested 로 고르지 않았다 — 사전 고정값 2개만 잰다",
        },
        "selection_protocol": {
            "nested": "outer train 안 GroupKFold(%d) inner OOF 에서만 (표현,K,추정량,"
                      "alpha) 를 고른다. 승격 판정은 이 값으로만." % INNER_SPLITS,
            "sweep": "고정 (표현,K,alpha) · local_only · Aglob 은 전체 OOF 에 그대로 "
                     "적용한 진단 곡선이다. 여기서 최저값을 골라 승격 근거로 쓰지 않는다.",
            "fixed": "구조 A 의 feature 구성과 실험 2 의 cluster 규칙은 사전 고정이라 "
                     "고르는 행위가 없다.",
        },
        "checkpoint": {"signature": sig, "dir": os.path.relpath(CKPT_DIR, C.ROOT),
                       "code_version": CODE_VERSION},
        "results": results,
        "best_honest_variant": best,
        "k_sensitivity_spread": ks_spread,
        "blend_k_curve_spread": kc_spread,
        "cluster_var_reduction_K3": {
            "n": len(var_red),
            "median": round(float(np.median(var_red)), 4) if var_red else None,
            "min": round(float(np.min(var_red)), 4) if var_red else None,
            "max": round(float(np.max(var_red)), 4) if var_red else None},
        "reproducibility": repro,
        "m73_reproduction": m73_match,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(x) for k, x in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "published_m73": M73_PUBLISHED,
        "goals": {"primary": "MAE < 0.35", "final": "MAE < 0.30"},
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m75_m2_similarity_local.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ============================================================ MD 보고서
def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    s, g = ps["m73_soft"], ps["global"]
    best = p["best_honest_variant"]
    Bm = ps["variants"][best]
    honest = honest_variants(ps)
    L = []
    A = L.append

    A("# M75 — 유사사업 local cohort · 중분류 내부 subgroup expert\n")
    A("> 질문 1: **신규사업과 실제로 유사한 과거 사업 Top-K 의 금액 패턴을 직접")
    A("> 활용하면 M73(0.3563)보다 정확한 지원규모 예측이 가능한가?**")
    A(">")
    A("> 질문 2: **같은 지원유형 안에서 사업 내용이 비슷한 subgroup 을 다시 만들면")
    A("> 금액 패턴이 더 균질해지고 subgroup expert 가 M73 을 이기는가?**\n")

    u = p["unchanged"]
    A("## 0. 같은 조건 / 바뀐 것\n")
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("baseline %s — 매 fold 에서 같이 학습해 paired 로 잰다" % u["baseline"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("어디서 골랐는가가 이 실험의 핵심 규율이다.\n")
    A("```text")
    A("nested  %s" % p["selection_protocol"]["nested"])
    A("sweep   %s" % p["selection_protocol"]["sweep"])
    A("fixed   %s" % p["selection_protocol"]["fixed"])
    A("```\n")

    A("## 1. 기준선 재현\n")
    A("| | MAE(log10) | fold σ | 2배 이내 | 3배 이내 | M73 공표치 |")
    A("|---|---:|---:|---:|---:|---:|")
    A("| global (M69 단일 XGB) | %.4f | %.4f | %.1f%% | %.1f%% | %.4f |"
      % (g["MAE_log10"], g["fold_std"], 100 * g["within_2x"], 100 * g["within_3x"],
         p["published_m73"]["global_MAE"]))
    A("| **%s (baseline)** | **%.4f** | %.4f | %.1f%% | %.1f%% | %.4f |"
      % (BASE, s["MAE_log10"], s["fold_std"], 100 * s["within_2x"],
         100 * s["within_3x"], p["published_m73"]["soft_ordinal_MAE"]))
    A("| 비교군 중앙값 baseline | %.4f | — | — | — | — |" % ps["baseline_MAE"])
    A("")
    mm = p["m73_reproduction"]
    if "error" not in mm:
        A("> M73 저장 OOF 와 행 단위 대조: row_id 일치 %s · `soft/ordinal_xgb` 최대"
          " 절대차 %.2e · global 최대 절대차 %.2e.\n"
          % (mm["row_id_identical"], mm["soft_ordinal_max_abs_diff"],
             mm["global_max_abs_diff"]))

    A("## 2. 실험 1 — similarity 기반 local cohort\n")
    A("### 2.1 정직한 후보 (nested 선택 · 사전 고정 구성)\n")
    A("| 변형 | MAE | ΔMAE vs M73 | 95% CI | fold승 | wilcoxon p | 2배 이내 |")
    A("|---|---:|---:|---|---:|---:|---:|")
    e1 = [k for k in ps["variants"] if k.startswith(("B/nested", "A/"))]
    for k in sorted(e1, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %d/5 | %s | %.1f%% |"
          % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"], m["vs_base"]["ci95"][0],
             m["vs_base"]["ci95"][1], m["fold_wins_vs_base"],
             m["vs_base"]["wilcoxon_p"], 100 * m["within_2x"]))
    A("")
    A("### 2.2 fold 별로 nested 가 고른 값\n")
    A("```text")
    A("(표현, K, 추정량, alpha = M73 쪽 가중치)")
    for i, pk in enumerate(ps["nested_picks"]):
        A("fold %d  %s" % (i, tuple(pk)))
    A("alpha 만 고른 대조군(emb, K=30, median): %s" % ps["nested_alpha_picks"])
    A("```\n")
    A("### 2.3 순수 local 예측만 썼을 때 — 이웃이 신호를 갖는가 (진단)\n")
    A("| 표현 | " + " | ".join("K=%d" % k for k in KS) + " |")
    A("|---|" + "---:|" * len(KS))
    for sp, row in ps["k_sensitivity"].items():
        A("| %s | " % sp + " | ".join("%.4f" % row["K=%d" % k] for k in KS) + " |")
    A("")
    A("> M73 baseline 이 %.4f 입니다. 이 표의 목적은 '이웃만으로 얼마나 가는가'와"
      " 'K 에 얼마나 민감한가'이지 후보를 고르는 것이 아닙니다.\n" % s["MAE_log10"])
    A("### 2.4 최종 후보의 K 민감도 (통과기준 6번의 근거)\n")
    A("fold 가 고른 (표현·추정량·alpha) 를 고정한 채 K 만 갈아 끼운 값입니다.")
    A("서빙할 예측이 K 선택에 좌우되는지를 재는 자리입니다.\n")
    A("| " + " | ".join("K=%d" % k for k in KS) + " | spread |")
    A("|" + "---:|" * (len(KS) + 1))
    kc = ps["blend_k_curve"]
    A("| " + " | ".join("%.4f" % kc["K=%d" % k] for k in KS)
      + " | %.4f |" % p["blend_k_curve_spread"])
    A("")
    A("### 2.5 고정 blend sweep (진단용 곡선 · 승격 근거 아님)\n")
    A("| 변형 | MAE | ΔMAE | fold승 |")
    A("|---|---:|---:|---:|")
    for k in sorted([k for k in ps["variants"] if k.startswith("sweep/")]):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"], m["fold_wins_vs_base"]))
    A("")
    A("### 2.6 구조 A 의 표현·K 민감도 (global 회귀 하나로만 — 진단)\n")
    A("| 구성 | MAE | ΔMAE vs M73 |")
    A("|---|---:|---:|")
    for k in sorted([k for k in ps["variants"] if k.startswith("Aglob")]):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f |" % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"]))
    A("")

    A("## 3. 실험 2 — support_type 내부 clustering + subgroup expert\n")
    A("### 3.1 결과\n")
    A("| 변형 | MAE | ΔMAE vs M73 | 95% CI | fold승 | expert 적용 | 적용행 승률 |")
    A("|---|---:|---:|---|---:|---:|---:|")
    for k in sorted([k for k in ps["variants"] if k.startswith("clus")],
                    key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        r = m.get("on_routed")
        A("| `%s` | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %d/5 | %.1f%% | %s |"
          % (k, m["MAE_log10"], m["vs_base"]["delta_MAE"], m["vs_base"]["ci95"][0],
             m["vs_base"]["ci95"][1], m["fold_wins_vs_base"], 100 * m["route_share"],
             ("%.3f" % r["win_rate"]) if r else "—"))
    A("")
    A("### 3.2 핵심 진단 — cluster 내부 금액 분산이 줄었는가 (K=3, fold 0)\n")
    A("| support_type | n_train | cluster 크기 | silhouette | 유형 전체 분산 | "
      "cluster 내부 분산 | 감소율 | cluster 중앙값 |")
    A("|---|---:|---|---:|---:|---:|---:|---|")
    for r in ps["folds"][0]["cluster_diag"].get("3", []):
        if not r.get("clustered"):
            continue
        A("| %s | %d | %s | %.3f | %.4f | %.4f | %+.1f%% | %s |"
          % (r["support_type"], r["n_train"], r["sizes"], r["silhouette"],
             r["var_support_type"], r["var_within_cluster"],
             100 * r["var_reduction"], r["cluster_median"]))
    A("")
    vr = p["cluster_var_reduction_K3"]
    A("> 5개 fold 전체에서 clustering 이 성립한 %d개 (fold × support_type) 조합의 "
      "분산 감소율: 중앙값 %s (최소 %s / 최대 %s).\n"
      % (vr["n"], vr["median"], vr["min"], vr["max"]))
    A("### 3.3 fallback 비율\n")
    A("| fold | " + " | ".join("K=%d" % k for k in CLUSTER_KS) + " |")
    A("|---|" + "---:|" * len(CLUSTER_KS))
    for f in ps["folds"]:
        A("| %d | " % f["fold"] + " | ".join(
            "%.1f%%" % (100 * float(f["cluster_fallback_rate"][str(k)]))
            for k in CLUSTER_KS) + " |")
    A("")

    A("## 4. fold 별 MAE — 최고 후보 vs baseline\n")
    A("| fold | 경계(원) | 비교군 baseline | M73 soft | `%s` |" % best)
    A("|---|---|---:|---:|---:|")
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %.4f | %.4f |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             f["baseline_MAE"], s["per_fold_MAE"][i], Bm["per_fold_MAE"][i]))
    A("")
    A("## 5. 구간별 · 출처별 MAE\n")
    A("| 구간 | n | M73 soft | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |"
          % (b, s["buckets"][b]["n"], s["buckets"][b]["MAE_log10"],
             Bm["buckets"][b]["MAE_log10"]))
    A("")
    A("| 출처 | n | M73 soft | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for k, r in s["cohort"]["cohort"].items():
        A("| %s | %d | %.4f | %.4f |"
          % (k, r["n"], r["MAE"], Bm["cohort"]["cohort"][k]["MAE"]))
    A("")

    A("## 6. 엄격 split (normalized_title)\n")
    A("| | MAE(log10) |")
    A("|---|---:|")
    A("| global | %.4f |" % nt["global"]["MAE_log10"])
    A("| %s (baseline) | %.4f |" % (BASE, nt["m73_soft"]["MAE_log10"]))
    for k in sorted(honest, key=lambda k: ps["variants"][k]["MAE_log10"])[:8]:
        if k in nt["variants"]:
            A("| `%s` | %.4f |" % (k, nt["variants"][k]["MAE_log10"]))
    A("")

    A("## 7. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    for k, v in p["leakage_checks"].items():
        A("| %s | %s |" % (k, "PASS" if v else "FAIL"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")

    A("## 8. 승격 점검표\n")
    A("대상: `%s` (정직한 후보 중 OOF MAE 최저)\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")

    A("## 9. 최종 비교표\n")
    A("| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI |")
    A("|---|---:|---:|---:|---:|---|")
    A("| M73 soft/ordinal (baseline) | %.4f | %.4f | %.1f%% | — | — |"
      % (s["MAE_log10"], nt["m73_soft"]["MAE_log10"], 100 * s["within_2x"]))
    for k in sorted(honest, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        A("| %s | %.4f | %s | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (k, m["MAE_log10"],
             ("%.4f" % nt["variants"][k]["MAE_log10"]) if k in nt["variants"] else "—",
             100 * m["within_2x"], m["fold_wins_vs_base"],
             m["vs_base"]["ci95"][0], m["vs_base"]["ci95"][1]))
    A("")

    A("## 결론\n")
    A("```text")
    A("M73 soft/ordinal    MAE = %.4f  (strict %.4f)"
      % (s["MAE_log10"], nt["m73_soft"]["MAE_log10"]))
    A("최고 정직 후보      %s" % best)
    A("                    MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (Bm["MAE_log10"], Bm["vs_base"]["delta_MAE"], Bm["vs_base"]["ci95"][0],
         Bm["vs_base"]["ci95"][1]))
    A("후보 K곡선 spread   %.4f  (순수 local %s)"
      % (p["blend_k_curve_spread"], p["k_sensitivity_spread"]))
    A("cluster 분산 감소   중앙값 %s" % vr["median"])
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
