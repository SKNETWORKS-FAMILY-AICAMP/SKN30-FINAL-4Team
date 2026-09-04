r"""m3_lab — Model 3 실험용 공통 harness. M58~M61 이 같은 자를 쓰게 한다.

왜 모듈로 빼는가
    지시서 Part B 는 cohort 정교화 -> multi-prototype -> thin cohort fallback
    -> scaling/distance 를 **차례로** 비교하라고 한다. 네 실험이 각자 채점
    코드를 들고 있으면 "A 는 Top30 을 30 으로, B 는 39 로 쟀다" 같은 차이가
    결과 차이로 둔갑한다. 그래서 채점·평가를 한 곳에 둔다.

무엇을 파라미터로 여는가 (M44 Freeze 대비)

    ladder      비교군 사다리. 현행은 [성격x방식] -> [성격] -> 전체.
    min_cohort  각 단계를 쓸 수 있는 최소 표본수. 현행 20.
    scaler      수치축 표준화. 현행 standard.
    n_proto     비교군 대표를 몇 개로 볼 것인가. 현행 1(평균).

    나머지는 M44 Freeze 그대로다 — 거리만 점수에 쓰고 방향은 설명 전용,
    점수는 비교군 내부 거리분포의 백분위, 수치/범주 블록 노름 정규화.

결측 필드는 그 단계를 **건너뛴다** (지시서 4절: "실제 서비스에서 안정적으로
확보 가능한 필드만 사용한다")
    `지원단위` 는 pool 의 15%, `기관계열` 은 37%가 비어 있다. 결측을 하나의
    범주('미상')로 묶으면 "단위를 모르는 사업끼리" 라는 실체 없는 비교군이
    생긴다. 그래서 그 행은 해당 단계를 못 쓰고 **상위 단계로 물러난다.**
    이렇게 하면 필드 가용성의 대가가 fallback 비율로 그대로 드러난다.

판정 문턱을 결과보다 먼저 못박는다 (지시서 9절)
    KEEP_TOP30    Top30 겹침이 이 값 이상이면 "바뀐 게 없다"로 본다.
                  M44 가 잰 재표집 유지율 0.918 에서 가져온다.
    KEEP_SPEARMAN M48 이 잰 80% 재표집 순위상관 0.969.
    ROC 는 보조지표다. 올라도 위 둘이 깨지면 reject 한다.
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
from scipy.stats import spearmanr
from sklearn.cluster import KMeans
from sklearn.metrics import average_precision_score, roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m13_m3_anomaly import MIN_AXES, SRC, prepare              # noqa: F401
from m38_m3_vector_direction import CAT, MIN_COHORT, NUM
from m43_m3_label_rule_v2 import OUT as HOLDOUT2_V2
from m47_m3_sensitivity import build_vectors_v

SEED = 42
TOPK = [10, 20, 30, 39]         # 39 ~= pool 의 2% (운영 검토 상위 비율)
FRACS = [0.9, 0.8, 0.7, 0.5]
N_ITER = 15
BUDGET_FRAC = 0.20              # M44 와 같은 경고 예산
THIN_MAX = 30                   # '얇은 비교군'의 기준 (M48 이 흔들림을 관측한 구간)

# 사전 고정 문턱 — 결과를 보고 바꾸지 않는다
KEEP_TOP30 = 0.918              # M44 재표집 유지율
KEEP_SPEARMAN = 0.969           # M48 80% 재표집 순위상관

AXIS_KR = {"log_per_recipient": "기업당 지원액", "log_support_count": "지원 기업수",
           "project_duration": "사업기간", "support_ratio": "지원비율"}
LOG_AXES = {"log_per_recipient", "log_support_count"}

# 지시서 4절의 후보. A2 는 현행(A0)과 같아 중복이므로 만들지 않는다.
LADDERS = {
    "A0 현행 (성격x방식)": [["support_type", "support_method"], ["support_type"]],
    "A1 성격": [["support_type"]],
    "A3 +지원단위": [["support_type", "support_method", "support_unit"],
                  ["support_type", "support_method"], ["support_type"]],
    "A4 +기관계열": [["support_type", "support_method", "support_unit", "agency_type"],
                  ["support_type", "support_method", "support_unit"],
                  ["support_type", "support_method"], ["support_type"]],
}
BASE_LADDER = "A0 현행 (성격x방식)"


# --------------------------------------------------------------- 비교군 키
def level_keys(df, cols):
    """한 단계의 비교군 키. **하나라도 결측이면 None** — 그 행은 이 단계를
    쓰지 못하고 상위 단계로 물러난다."""
    ok = np.ones(len(df), bool)
    parts = []
    for c in cols:
        v = df[c]
        ok &= v.notna().to_numpy()
        # fillna 를 먼저 하는 이유: pandas 의 str dtype 은 astype(str) 로도
        # 결측을 문자열로 바꾸지 않아 join 에서 터진다. 어차피 ok 로 지운다.
        parts.append(v.fillna("__NA__").astype(str).to_numpy(dtype=object))
    key = np.array(["|".join(p) for p in zip(*parts)], dtype=object)
    key[~ok] = None
    return pd.Series(key, index=df.index)


def resolver(fit, ladder, min_cohort):
    """fit 의 표본수로 각 단계의 사용 가능 여부를 정하고, 임의의 키 조합을
    (단계이름, 키) 로 떨어뜨리는 함수를 돌려준다."""
    fit_keys = [level_keys(fit, cols) for cols in ladder]
    counts = [k.value_counts() for k in fit_keys]
    names = ["L%d %s" % (i + 1, "x".join(cols)) for i, cols in enumerate(ladder)]

    def resolve(row_keys):
        for i, k in enumerate(row_keys):
            if k is not None and counts[i].get(k, 0) >= min_cohort:
                return (names[i], k)
        return ("L0 전체", "ALL")

    return resolve, fit_keys, names


def centroids(M, n_proto, proto_min, seed=SEED):
    """비교군 대표. n_proto=1 이면 평균 하나, 아니면 KMeans 중심 여러 개.

    작은 비교군에는 적용하지 않는다 — 20건을 3덩이로 쪼개면 덩이당 7건이라
    중심 자체가 표본 잡음이 된다 (지시서 5절 '충분히 큰 cohort 에서만').
    """
    if n_proto <= 1 or len(M) < proto_min:
        return M.mean(0)[None, :]
    k = min(n_proto, max(2, len(M) // 10))
    km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(M)
    return km.cluster_centers_


def _nearest(P, X):
    """각 행에서 가장 가까운 대표까지의 거리와 그 대표 index."""
    d = np.linalg.norm(X[:, None, :] - P[None, :, :], axis=2)
    j = np.argmin(d, axis=1)
    return d[np.arange(len(X)), j], j


LOG1P_AXES = ["project_duration", "support_ratio"]   # 이미 log 인 두 축은 제외


def apply_log(df, axes):
    """실험 D 용 — 원 스케일 축에만 log1p 를 건다. 음수는 건드리지 않는다."""
    if not axes:
        return df
    d = df.copy()
    for a in axes:
        v = d[a].to_numpy(dtype=float)
        d[a] = np.where(v >= 0, np.log1p(v), v)
    return d


def _whitener(Xtr, groups, fit_res, metric, n_num, ridge=1e-3):
    """비교군 **내부** 잔차로 만든 화이트닝 행렬.

    거리 함수를 바꾸는 대신 좌표계를 바꾼다 — 그러면 백분위·비교군·설명
    로직을 하나도 손대지 않고 metric 만 갈아끼울 수 있다.

    비교군 내부 잔차를 쓰는 이유: 전체 분산으로 정규화하면 비교군 사이의
    차이(예: 융자와 보조금의 금액대 차이)까지 축소해 정작 재려는 신호를
    깎는다. 우리가 없애고 싶은 것은 **비교군 안에서 원래 넓게 퍼지는 축**의
    과대 기여다 (M51 이 금액 축에서 관측한 신호/잡음비 문제).
    """
    if metric == "euclidean":
        return None
    R = np.vstack([Xtr[i] - groups[gk]["P"][0] for i, gk in enumerate(fit_res)])
    if metric == "diag":
        s = R.std(0)
        s = np.where(s < 1e-9, 1.0, s)
        return np.diag(1.0 / s)
    if metric == "mahalanobis":
        # 수치 블록에만 건다. 범주 one-hot 은 공분산이 특이해지기 쉽고,
        # 거기까지 화이트닝하면 희귀 범주가 폭발한다.
        S = np.cov(R[:, :n_num], rowvar=False)
        S = S + ridge * np.trace(S) / max(1, n_num) * np.eye(n_num)
        vals, vecs = np.linalg.eigh(S)
        Wn = vecs @ np.diag(1.0 / np.sqrt(np.maximum(vals, 1e-12))) @ vecs.T
        W = np.eye(Xtr.shape[1])
        W[:n_num, :n_num] = Wn
        return W
    raise ValueError(metric)


def score_pool(fit, apply_df, ladder=None, min_cohort=MIN_COHORT, scaler="standard",
               n_proto=1, proto_min=100, num_features=None, metric="euclidean",
               log_axes=None):
    """fit 으로 비교군 대표를 만들고 apply_df 전체를 채점한다.

    점수 = 비교군 내부 거리분포에서의 백분위 -> pool 전체 rank(pct).
    비교군마다 퍼진 정도가 달라 절대거리를 쓰면 넓은 비교군만 계속 걸린다.
    """
    ladder = LADDERS[BASE_LADDER] if ladder is None else ladder
    if log_axes:
        fit, apply_df = apply_log(fit, log_axes), apply_log(apply_df, log_axes)
    Xtr, Xap, n_num = build_vectors_v(fit, apply_df, scaler, num_features)
    resolve, fit_keys, names = resolver(fit, ladder, min_cohort)
    ap_keys = [level_keys(apply_df, cols) for cols in ladder]

    fit_res = [resolve(t) for t in zip(*[k.tolist() for k in fit_keys])]
    ap_res = [resolve(t) for t in zip(*[k.tolist() for k in ap_keys])]

    def build_groups(Xt):
        g = {}
        for gk in set(fit_res) | set(ap_res):
            lvl, key = gk
            if lvl == "L0 전체":
                mask = np.ones(len(fit), bool)
            else:
                mask = (fit_keys[names.index(lvl)] == key).to_numpy()
            M = Xt[mask]
            P = centroids(M, n_proto, proto_min)
            d, j = _nearest(P, M)
            # 수치축만의 퍼짐도 따로 둔다. 범주축으로 비교군을 나누면 그 축의
            # 기여가 0 이 되므로 전체 퍼짐은 저절로 줄어든다(기계적). 수치축
            # 퍼짐은 그 효과를 받지 않아 동질성을 tautology 없이 잰다.
            dn = (np.linalg.norm(M[:, :n_num] - P[j][:, :n_num], axis=1)
                  if len(M) else np.zeros(0))
            g[gk] = {"P": P, "dist": d, "n": int(mask.sum()),
                     "spread": float(d.mean()) if len(d) else 0.0,
                     "spread_num": float(dn.mean()) if len(dn) else 0.0,
                     "med_num": (np.median(M[:, :n_num], axis=0) if len(M)
                                 else np.zeros(n_num))}
        return g

    groups = build_groups(Xtr)
    if metric != "euclidean":
        # 좌표계를 바꾼 뒤 비교군을 다시 만든다 (평균은 선형이라 위치는
        # 같지만 거리분포가 달라진다)
        W = _whitener(Xtr, groups, fit_res, metric, n_num)
        Xtr, Xap = Xtr @ W.T, Xap @ W.T
        groups = build_groups(Xtr)

    pct = np.empty(len(apply_df))
    D = np.empty((len(apply_df), Xap.shape[1]))
    cohort_n = np.empty(len(apply_df), int)
    for i, gk in enumerate(ap_res):
        g = groups[gk]
        d, j = _nearest(g["P"], Xap[i:i + 1])
        pct[i] = float((g["dist"] <= d[0]).mean()) * 100
        D[i] = Xap[i] - g["P"][j[0]]
        cohort_n[i] = g["n"]

    rid = apply_df["row_id"].to_numpy()
    return {
        "score": pd.Series(pd.Series(pct).rank(pct=True).to_numpy(), index=rid),
        "level": pd.Series([r[0] for r in ap_res], index=rid),
        "cohort_key": pd.Series([r[1] for r in ap_res], index=rid),
        "cohort_n": pd.Series(cohort_n, index=rid),
        "D": D, "Xap": Xap, "n_num": n_num, "groups": groups,
        "level_names": names,
    }


# ------------------------------------------------------------------ 비교
def compare(base, var, topk=TOPK):
    out = {"spearman": round(float(spearmanr(base.to_numpy(),
                                             var.loc[base.index].to_numpy()).statistic), 4)}
    for k in topk:
        b = set(base.sort_values(ascending=False).head(k).index)
        v = set(var.sort_values(ascending=False).head(k).index)
        out["top%d_overlap" % k] = round(len(b & v) / k, 4)
    return out


def cohort_profile(res):
    """비교군 구성 — 지시서 10절이 요구하는 줄들 + **동질성**.

    동질성을 따로 재는 이유
        비교군을 굵게 잡으면 표본이 커져 안정성 지표는 저절로 좋아진다.
        그런데 실험 A 의 목적은 안정성이 아니라 **더 동질적인 비교군**이다
        (지시서 4절). 두 축을 같이 보지 않으면 "전부 한 덩이로 묶으면
        제일 안정적" 이라는 결론에 도달한다.

        범주블록 점유율   차이벡터 D 중 범주 one-hot(지원방식·금액형태·
                        지원단위)이 차지하는 비율. 이 값이 크다는 것은
                        "이 사업이 드문 설계다"가 아니라 "이 사업은 융자인데
                        비교군은 보조금이다" 를 재고 있다는 뜻이다.
                        비교군이 이질적일수록 올라간다.
        비교군 퍼짐      비교군 내부 평균 거리. 굵게 묶을수록 커진다.
    """
    lvl = res["level"]
    n = res["cohort_n"]
    D, n_num = res["D"], res["n_num"]
    sq = D ** 2
    tot = sq.sum(1)
    tot[tot < 1e-12] = 1.0
    cat_share = float((sq[:, n_num:].sum(1) / tot).mean())
    # 행 단위로 평균낸다 — 비교군 하나가 1,948행을 담든 20행을 담든 한 표가
    # 되면 큰 비교군이 지워진다
    key = list(zip(res["level"], res["cohort_key"]))
    spread = float(np.mean([res["groups"][k]["spread"] for k in key]))
    spread_num = float(np.mean([res["groups"][k]["spread_num"] for k in key]))
    return {
        "level_dist": {k: int(v) for k, v in lvl.value_counts().sort_index().items()},
        "n_global_fallback": int((lvl == "L0 전체").sum()),
        "n_thin": int(((n <= THIN_MAX) & (lvl != "L0 전체")).sum()),
        "cohort_size_median": int(n.median()),
        "cohort_size_min": int(n.min()),
        "cohort_size_p10": int(n.quantile(0.10)),
        "n_distinct_cohorts": int(res["cohort_key"].nunique()),
        "cat_block_share_of_D": round(cat_share, 4),
        "within_cohort_spread": round(spread, 4),
        "within_cohort_spread_num": round(spread_num, 4),
    }


def resample_stability(train, fracs=FRACS, n_iter=N_ITER, seed=SEED, **kw):
    """대표벡터를 만드는 표본을 줄여 다시 만든다. 이 방식에는 난수 초기값이
    없으므로(멀티프로토타입은 KMeans 시드를 고정한다) 흔들림의 원인은 표본이다."""
    rng = np.random.default_rng(seed)
    base = score_pool(train, train, **kw)["score"]
    out = {}
    for frac in fracs:
        rho, tops = [], {k: [] for k in TOPK}
        for _ in range(n_iter):
            sub = train.sample(frac=frac, random_state=int(rng.integers(1e9)))
            s = score_pool(sub, train, **kw)["score"]
            c = compare(base, s)
            rho.append(c["spearman"])
            for k in TOPK:
                tops[k].append(c["top%d_overlap" % k])
        out["frac_%.1f" % frac] = {
            "spearman_mean": round(float(np.mean(rho)), 4),
            "spearman_min": round(float(np.min(rho)), 4),
            **{"top%d_mean" % k: round(float(np.mean(v)), 4) for k, v in tops.items()},
            **{"top%d_min" % k: round(float(np.min(v)), 4) for k, v in tops.items()},
        }
    return out


def rank_volatility(train, n_iter=N_ITER, frac=0.8, seed=SEED, **kw):
    """80% 재표집에서 각 행의 백분위 순위가 평균 몇 점 움직이는가 (M48 §8.6).

    resample_stability 가 **목록 전체**를 보는 반면 이쪽은 **행마다** 본다.
    실험 C 의 질문("얇은 비교군이 흔들림을 독점하는가")은 행 단위로만
    답할 수 있다 — Top30 겹침은 얇은 비교군이 상위에 없으면 아무것도
    말해주지 않는다.
    """
    rng = np.random.default_rng(seed)
    base = score_pool(train, train, **kw)
    base_r = base["score"].rank(pct=True)
    ranks = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e9)))
        s = score_pool(sub, train, **kw)["score"]
        ranks.append(s.loc[base_r.index].rank(pct=True))
    vol = (pd.concat(ranks, axis=1).sub(base_r, axis=0).abs().mean(axis=1) * 100)

    n, lvl = base["cohort_n"], base["level"]
    thin = (n <= THIN_MAX) & (lvl != "L0 전체")
    glob = lvl == "L0 전체"
    per = (pd.DataFrame({"key": base["cohort_key"], "level": lvl, "n": n, "vol": vol})
           .groupby(["level", "key"]).agg(n=("n", "first"), vol=("vol", "mean")))
    per = per.sort_values("vol", ascending=False)
    return {
        "overall_mean": round(float(vol.mean()), 4),
        "overall_median": round(float(vol.median()), 4),
        "thin_mean": round(float(vol[thin].mean()), 4) if thin.any() else None,
        "thin_n_rows": int(thin.sum()),
        "nonthin_mean": round(float(vol[~thin & ~glob].mean()), 4),
        "global_mean": round(float(vol[glob].mean()), 4) if glob.any() else None,
        "worst_cohorts": [{"cohort": str(k[1]), "level": str(k[0]), "n": int(r["n"]),
                           "volatility": round(float(r["vol"]), 2)}
                          for k, r in per.head(6).iterrows()],
    }


# ------------------------------------------------------- synthetic stress
def perturb(row, axis, mult):
    r = row.copy()
    if axis in LOG_AXES:
        r[axis] = r[axis] + np.log10(mult)
    else:
        v = r[axis] * mult
        r[axis] = min(v, 100.0) if axis == "support_ratio" else v
    return r


MULTS = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
N_CASES = 60


def synthetic_stress(train, n_cases=N_CASES, seed=SEED, **kw):
    """설계축을 인위적으로 흔들었을 때 점수가 상식대로 반응하는가 (M49 와 동일).

    x축은 배수가 아니라 **비교군 중앙값에서의 거리**다. 원래 값이 이미
    중앙에서 벗어난 사업이 있어, 배수를 x축으로 쓰면 멀쩡한 모델도
    단조성이 깨진 것처럼 보인다.
    """
    key = train["support_type"].astype(str) + "|" + train["support_method"].astype(str)
    big = key.value_counts()
    pick = train[key.isin(big[big >= 50].index)].sample(n=n_cases, random_state=seed)
    pick = pick.reset_index(drop=True)

    rows, meta = [], []
    for ci, (_, r) in enumerate(pick.iterrows()):
        for axis in NUM:
            if pd.isna(r[axis]):
                continue
            for m in MULTS:
                rows.append(perturb(r, axis, m))
                meta.append({"case": ci, "axis": axis, "mult": m})
    pert = pd.DataFrame(rows).reset_index(drop=True)
    pert["row_id"] = ["SYN%05d" % i for i in range(len(pert))]
    meta = pd.DataFrame(meta)

    res = score_pool(train, pert, **kw)
    Xap, n_num = res["Xap"], res["n_num"]
    meta["score"] = res["score"].to_numpy()
    # x축은 배수가 아니라 **그 행이 실제로 떨어진 비교군의 중앙값에서의 거리**.
    # score_pool 이 돌려준 좌표계를 그대로 쓴다 — scaling·metric 을 바꾼
    # 변형에서도 같은 자로 재기 위해서다.
    lvl, ck = res["level"].to_numpy(), res["cohort_key"].to_numpy()
    ai = {a: i for i, a in enumerate(NUM)}
    meta["dev"] = [abs(Xap[i, ai[meta.loc[i, "axis"]]]
                       - res["groups"][(lvl[i], ck[i])]["med_num"][ai[meta.loc[i, "axis"]]])
                   for i in range(len(meta))]
    Dn = res["D"][:, :n_num]
    meta["argmax_axis"] = [NUM[int(np.argmax(np.abs(Dn[i])))] for i in range(len(meta))]

    mono, attrib = {}, {}
    for axis, g in meta.groupby("axis"):
        rhos = []
        for _, gg in g.groupby("case"):
            if gg["dev"].nunique() > 1:
                r = float(spearmanr(gg["dev"], gg["score"]).statistic)
                if not np.isnan(r):
                    rhos.append(r)
        mono[axis] = {"n_cases": len(rhos),
                      "spearman_mean": round(float(np.mean(rhos)), 4),
                      "positive_rate": round(float(np.mean([x > 0 for x in rhos])), 4)}
        ext = g[g["mult"].isin([0.1, 10.0])]
        attrib[axis] = round(float((ext["argmax_axis"] == axis).mean()), 4)
    return {"monotonicity": mono, "axis_attribution": attrib,
            "min_positive_rate": round(min(v["positive_rate"] for v in mono.values()), 4),
            "mean_axis_attribution": round(float(np.mean(list(attrib.values()))), 4)}


# ------------------------------------------------------- feature 의존도
def feature_dependency(train, **kw):
    """특정 feature 하나가 점수를 지배하는가 (지시서 8절).

    두 각도에서 본다.
        기여도 점유율   상위 39건의 contribution_j = D_j^2 / sum(D^2) 평균.
                       한 축이 절반을 넘으면 사실상 그 축 하나짜리 모델이다.
        축 제거 영향    그 축을 빼면 순위가 얼마나 바뀌는가. 지배축을 빼면
                       순위가 무너진다.
    """
    res = score_pool(train, train, **kw)
    D, n_num = res["D"], res["n_num"]
    sq = D ** 2
    tot = sq.sum(1, keepdims=True)
    tot[tot < 1e-12] = 1.0
    share = sq / tot
    top = res["score"].sort_values(ascending=False).head(39).index
    pos = pd.Index(train["row_id"]).get_indexer(top)

    by_axis = {NUM[j]: round(float(share[pos, j].mean()), 4) for j in range(n_num)}
    cat_share = round(float(share[pos, n_num:].sum(1).mean()), 4)

    ablate = {}
    for f in NUM:
        v = score_pool(train, train, num_features=[x for x in NUM if x != f],
                       **{k: x for k, x in kw.items() if k != "num_features"})["score"]
        ablate[f] = compare(res["score"], v, topk=[30])
    return {
        "top39_contribution_share": by_axis,
        "top39_categorical_share": cat_share,
        "max_axis_share": round(max(by_axis.values()), 4),
        "dominant_axis": max(by_axis, key=by_axis.get),
        "ablation": ablate,
        "min_ablation_spearman": round(min(v["spearman"] for v in ablate.values()), 4),
    }


# --------------------------------------------------------- attribution 안정성
def attribution_stability(train, n_iter=10, frac=0.8, top_k=30, seed=SEED, **kw):
    """상위 목록에 대해 '어느 설계축이 원인인가'가 재표집에도 유지되는가.

    점수가 안정적이어도 설명이 매번 다른 축을 지목하면 담당자에게 나가는
    문장이 흔들린다. M51 이 채택한 기여도 방식의 top1 축으로 잰다.
    """
    rng = np.random.default_rng(seed)
    base = score_pool(train, train, **kw)
    rid = pd.Index(train["row_id"])
    top = base["score"].sort_values(ascending=False).head(top_k).index
    pos = rid.get_indexer(top)
    base_top1 = np.argmax(np.abs(base["D"][:, :base["n_num"]]), axis=1)[pos]

    agree = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e9)))
        r = score_pool(sub, train, **kw)
        t1 = np.argmax(np.abs(r["D"][:, :r["n_num"]]), axis=1)[pos]
        agree.append(float((t1 == base_top1).mean()))
    return {"top_k": top_k, "n_iter": n_iter,
            "top1_axis_agreement_mean": round(float(np.mean(agree)), 4),
            "top1_axis_agreement_min": round(float(np.min(agree)), 4)}


# ----------------------------------------------------------- 라벨 (참고용)
def load_labels(train):
    lab = pd.read_csv(HOLDOUT2_V2, encoding="utf-8-sig")
    main = lab[lab["v2_라벨"].isin(["normal", "atypical_design"])]
    main = main[main["row_id"].isin(set(train["row_id"]))]
    return (set(lab["row_id"]), main["row_id"].tolist(),
            (main["v2_라벨"] == "atypical_design").to_numpy(int))


def eval_labeled(score, ids, y):
    """탐색적 보조지표. 양성 5건이라 변형 간 우열을 가릴 힘이 없다 (M44)."""
    sc = score.loc[ids].to_numpy(float)
    k = max(1, int(round(BUDGET_FRAC * len(y))))
    flag = sc >= np.sort(sc)[::-1][k - 1]
    tp = int((flag & (y == 1)).sum())
    fp = int((flag & (y == 0)).sum())
    fn = int((~flag & (y == 1)).sum())
    return {"roc_auc": round(float(roc_auc_score(y, sc)), 4),
            "pr_auc": round(float(average_precision_score(y, sc)), 4),
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None}


def load_pool(path=None):
    """평가 pool. path 를 주면 다른 데이터셋(예: M62 수정본)에서 같은 규칙으로
    만든다 — 규칙을 바꾸지 않고 입력만 바꿔 재평가하기 위해서다."""
    df = prepare(pd.read_parquet(path or SRC))
    return df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)


# ------------------------------------------------------------------ 판정
def verdict(cmp_base, stab, syn, dep, attr, base_stab, base_syn, base_dep, base_attr):
    """지시서 9절의 필수조건. 하나라도 깨지면 REJECT — ROC 는 보지 않는다.

        1 ranking stability 악화 없음
        2 synthetic perturbation monotonicity 유지
        3 attribution 해석 가능성 유지
        4 특정 feature dependency 악화 없음
        5 fallback 비율 비정상 증가 없음   (호출부에서 cohort_profile 로 판정)
    """
    fails = []
    if stab["frac_0.8"]["spearman_mean"] < base_stab["frac_0.8"]["spearman_mean"] - 0.01:
        fails.append("재표집 순위상관 악화")
    if stab["frac_0.8"]["top30_mean"] < base_stab["frac_0.8"]["top30_mean"] - 0.05:
        fails.append("재표집 Top30 악화")
    if syn["min_positive_rate"] < min(1.0, base_syn["min_positive_rate"]) - 0.02:
        fails.append("synthetic 단조성 악화")
    if attr["top1_axis_agreement_mean"] < base_attr["top1_axis_agreement_mean"] - 0.05:
        fails.append("attribution 흔들림 증가")
    if dep["max_axis_share"] > base_dep["max_axis_share"] + 0.05:
        fails.append("특정 축 지배 심화")
    return fails
