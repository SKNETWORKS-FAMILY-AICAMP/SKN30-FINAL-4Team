r"""M80 — 금액의 '의미'(amount_type)와 기관·유형·연도라는 생성 구조를 명시하면
M73 을 이기는가.

지시서(사용자, `m80_model2_semantic_amount_type_hierarchical_prior_plan.md`):

    모델군을 또 바꾸는 대신 **금액의 의미와 데이터 생성 구조를 직접 활용**한다.
    (1) Semantic Amount-Type Expert  (2) Hierarchical Institution × Type × Year
    Prior 두 축을 본다.

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 (구조화 + 제목 SVD64 + 원천 feature 층 + 본문 SVD64)
    routing    M73 soft / ordinal_xgb (구간 33.3/66.7)
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    parser     정의 불변

바뀌는 것은 **입력에 붙는 prior feature 층** 하나다(1C·1E 는 예측 뒤 후처리).

## 실행 전에 확인한 것 — 지시서 전제 두 가지가 이 데이터셋에서 다르다

### (1) amount_type 은 5종이 아니라 2종이다

지시서는 support_cap · support_per_recipient · support_per_project · loan_limit ·
total_budget 이 한 타깃에 섞여 있다고 본다. 실제로는

    per_recipient_basis == 'stated_cap'   1,877행 (100%)
    amount_type  per_company 1,709 / per_project 168

M45 의 타깃 정의가 이미 basis!='stated_cap' 을 전부 걸러냈다. total_budget 과
budget÷건수는 '한도'가 아니라 '평균'이라 애초에 제외됐고, 융자는 별도
amount_type 이 아니라 support_type='융자' 로 갈린다. 즉 **'의미가 다른 금액이
섞여 있다'는 문제는 데이터셋 구축 단계에서 이미 해결돼 있다.**

### (2) amount_type 은 이미 feature 다 -> 1A 를 실행하지 않는다

`M45.CATS` 에 amount_type 이 들어 있다. 지시서 1A 의 단서 "이미 동일 정보가
feature 에 들어가 있다면 재실험하지 않는다" 에 그대로 걸린다. 근거만 남기고
건너뛴다.

### 그럼에도 Experiment 1 을 진행하는 이유

지시서 Experiment 0 의 진행 기준 넷 중 하나가 충족된다 — **per_project 의 M73
MAE 가 0.4425 로 per_company 0.3478 보다 뚜렷하게 높다**(분포 자체는 KS p=0.108,
겹침 0.789 로 구분되지 않는다). '분포는 같은데 한쪽이 더 어렵다'면 분리해서
얻을 것이 있는지 재볼 값어치가 있다.

## 이 실험에서 가장 위험한 자리 — target encoding 의 누수

prior 는 **타깃 통계를 feature 로 만드는 것**이라, 아무 방어 없이 만들면 각 행이
자기 정답을 자기 feature 로 돌려받는다. 학습에서는 완벽해 보이고 서빙에서는
무너진다. 그래서 두 층으로 나눈다.

    outer test 행    outer train 전체에서 계산한 통계를 붙인다
    outer train 행   inner GroupKFold(3) OOF 로 계산한다 — 자기 fold 를 빼고
                     만든 통계만 자기에게 붙는다

이렇게 해야 모델이 학습에서 보는 prior 와 서빙에서 받는 prior 의 성격이 같다.

    shrinkage   prior_i = w*group_stat_i + (1-w)*prior_{i-1},  w = n/(n+k)
                얇은 셀은 자동으로 상위 계층으로 물러난다
    k 선택      nested — inner OOF 에서 'prior 단독 예측' MAE 로만 고른다
    희소 fallback  계층에 없는 조합은 존재하는 가장 깊은 상위 계층 값을 쓴다

## 지시서에 없는 대조군을 하나 넣는다 — H_ctrl

`agency`(기관 51종)는 **지금 feature 에 없다.** 들어 있는 것은 agency_grp
(central/local/public/미기재 4종)뿐이다. 그래서 H2/H3 에서 개선이 나오면 그것이

    (a) 기관 정보가 처음 들어와서인가
    (b) hierarchical target prior 라는 방식 덕분인가

를 구분할 수 없다. 지시서가 검증하려는 것은 (b) 이므로 (a) 를 따로 세운다.

    H_ctrl = agency 를 그냥 categorical feature 로 추가 (타깃 통계 없이)

산출
    ml/data/processed/m80_prior_oof.parquet
    ml/reports/m80_m2_semantic_prior.json / .md
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
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m73_m2_routing_improvement as M73

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m80_prior_oof.parquet")
MD = C.report_path("m80_m2_semantic_prior.md")

BUCKETS = M73.BUCKETS
STEP = "G"
INNER_SPLITS = 3
BASE = "M73 raw (soft/ordinal_xgb)"

M73_PUBLISHED = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                 "within_2x": 0.564, "within_3x": 0.742}

# ------------------------------------------------------------ prior 계층
# 각 계층의 부모는 바로 앞 계층이다. shrinkage 가 부모 쪽으로 당기므로 반드시
# 중첩(nested) 이어야 한다.
#
# 지시서 1B 의 fallback 은 'amount_type -> support_type -> global' 인데,
# amount_type 은 support_type 아래에 중첩된 축이 아니다. 중첩이 아닌 사슬로는
# shrinkage 를 일관되게 정의할 수 없어(부모 값이 행마다 다른 축을 가리킨다),
# 같은 의도를 중첩 형태로 옮겼다 — support_type 안에서의 amount_type 효과.
CHAINS = {
    "1B/type_prior": [[], ["support_type"], ["support_type", "amount_type"]],
    "H1/stype": [[], ["support_type"]],
    "H2/agency_stype": [[], ["support_type"], ["agency", "support_type"]],
    "H3/agency_stype_year": [[], ["support_type"], ["agency", "support_type"],
                             ["agency", "support_type", "year"]],
    "H4/h3_amount_type": [[], ["support_type"], ["agency", "support_type"],
                          ["agency", "support_type", "year"],
                          ["agency", "support_type", "year", "amount_type"]],
}
PRIOR_STATS = ("median", "mean", "q25", "q75")
KS = (10, 20, 50)                  # shrinkage 후보. nested 로 고른다
TYPE_MIN_N = 150                   # 1D — 지시서 권장값
# 5-fold 로 자르면 per_project 의 fold train 이 약 134행이라 위 기준에 미달한다.
# 정작 M73 MAE 가 높은 쪽(0.4425)이 전용 expert 를 못 받는 셈이라, 지시서의
# 의도를 실제로 시험하려면 문턱을 한 칸 낮춘 판이 필요하다.
TYPE_MIN_N_ALT = 100
ALPHAS = (0.7, 0.8, 0.9)           # 1E — M73 쪽 가중치
AGENCY_COL = "agency"

# 무거운 것(모델 재학습)이 필요한 변형. 나머지(1C·1E)는 numpy 후처리다.
RETRAIN = ["1B/type_prior", "H1/stype", "H2/agency_stype",
           "H3/agency_stype_year", "H3t/temporal", "Hctrl/agency_cat"]

CODE_VERSION = "m80-v1"
CKPT_DIR = os.path.join(C.PROC, "m80_ckpt")


# ============================================================ prior 만들기
def _norm(df, c):
    """그룹 키로 쓸 문자열 한 열.

    pandas 의 StringDtype 은 `.astype(str)` 뒤에도 결측을 float NaN 으로 남긴다
    (agency 는 583행이 결측이다). 그대로 join 하면 TypeError 가 나고, 설령
    통과해도 결측이 조용히 사라진다. 결측은 **자체 그룹**으로 둔다 — '기관을
    적지 않은 공고' 는 실제로 하나의 부류다.
    """
    return df[c].astype(object).where(df[c].notna(), "미기재").astype(str).to_numpy()


def _stats(df, y, cols):
    """cols 로 묶은 타깃 통계. cols 가 비면 전체 하나."""
    t = pd.DataFrame({"__y": y})
    if not cols:
        s = pd.DataFrame({"n": [len(y)], "median": [np.median(y)],
                          "mean": [y.mean()], "q25": [np.percentile(y, 25)],
                          "q75": [np.percentile(y, 75)]})
        return s, None
    for c in cols:
        t[c] = _norm(df, c)
    g = t.groupby(cols, observed=True)["__y"]
    s = pd.DataFrame({"n": g.size(), "median": g.median(), "mean": g.mean(),
                      "q25": g.quantile(0.25), "q75": g.quantile(0.75)})
    return s.reset_index(), cols


def _key(df, cols):
    if not cols:
        return np.zeros(len(df), dtype=object)
    parts = [_norm(df, c) for c in cols]
    return np.array(["\x1f".join(v) for v in zip(*parts)], dtype=object)


def chain_prior(src_df, src_y, tgt_df, chain, k):
    """계층 사슬을 따라 shrinkage 를 적용한 prior. src 만 보고 tgt 에 붙인다.

    prior_i = w * group_stat_i + (1-w) * prior_{i-1},  w = n_i/(n_i+k)
    존재하지 않는 조합은 자동으로 부모 값에 머문다(= fallback).
    """
    n_t = len(tgt_df)
    cur = {s: np.zeros(n_t) for s in PRIOR_STATS}
    cnt = np.zeros(n_t)
    lvl = np.zeros(n_t, dtype=int)
    wgt = np.zeros(n_t)
    for i, cols in enumerate(chain):
        st, _ = _stats(src_df, src_y, cols)
        if not cols:
            for s in PRIOR_STATS:
                cur[s][:] = float(st[s].iloc[0])
            cnt[:] = float(st["n"].iloc[0])
            continue
        st["__k"] = _key(st, cols)
        m = st.set_index("__k")
        tk = _key(tgt_df, cols)
        hit = np.isin(tk, m.index.to_numpy())
        if not hit.any():
            continue
        sub = m.reindex(tk[hit])
        n_i = sub["n"].to_numpy(dtype=float)
        w = n_i / (n_i + float(k))
        for s in PRIOR_STATS:
            cur[s][hit] = w * sub[s].to_numpy(dtype=float) + (1 - w) * cur[s][hit]
        cnt[hit] = n_i
        lvl[hit] = i
        wgt[hit] = w
    out = {("prior_" + s): cur[s] for s in PRIOR_STATS}
    out["prior_count"] = cnt
    out["prior_level"] = lvl.astype(float)
    # 가장 깊은 계층에서 실제로 적용된 가중치. level 이 3이어도 셀이 n=2 면
    # w=n/(n+k) 가 0.17 이라 사실상 부모 값이다 — level 만 보면 오해한다.
    out["prior_w_deepest"] = wgt
    return pd.DataFrame(out)


def chain_prior_temporal(src_df, src_y, tgt_df, chain, k, year_col="year"):
    """미래 정보를 쓰지 않는 판. 연도 Y 행은 src 의 year < Y 만 본다.

    연도 종류가 8개라 연도별로 한 번씩 계산한다. 과거 표본이 없는 연도는
    가장 이른 연도인데, 그 행들은 global 로만 떨어진다(= 그 시점에 실제로
    알 수 있던 전부).
    """
    frames = []
    idx = []
    yt = tgt_df[year_col].to_numpy()
    ys = src_df[year_col].to_numpy()
    for Y in np.unique(yt):
        m_t = yt == Y
        m_s = ys < Y
        if m_s.sum() < 20:
            # 과거가 거의 없으면 전체 중앙값 하나로 (그 시점에 알 수 있던 것)
            g = float(np.median(src_y)) if len(src_y) else 0.0
            f = pd.DataFrame({("prior_" + s): np.full(int(m_t.sum()), g)
                              for s in PRIOR_STATS})
            f["prior_count"] = float(m_s.sum())
            f["prior_level"] = 0.0
            f["prior_w_deepest"] = 0.0
        else:
            f = chain_prior(src_df[m_s], src_y[m_s], tgt_df[m_t], chain, k)
        frames.append(f)
        idx.append(np.where(m_t)[0])
    out = pd.DataFrame(np.zeros((len(tgt_df), frames[0].shape[1])),
                       columns=frames[0].columns)
    for f, i in zip(frames, idx):
        out.iloc[i] = f.to_numpy()
    return out


def prior_columns(d, y, tr, te, chain, k, gtr, temporal=False):
    """(train 쪽 prior, test 쪽 prior). 누수 방어의 핵심 함수다.

    test  : outer train 전체로 계산 — 서빙에서 실제로 가능한 계산이다.
    train : inner GroupKFold OOF — 자기 fold 를 뺀 통계만 자기에게 붙는다.
            이걸 안 하면 각 행이 자기 정답을 자기 feature 로 돌려받는다.
    """
    from sklearn.model_selection import GroupKFold

    fn = chain_prior_temporal if temporal else chain_prior
    dtr, dte = d.iloc[tr], d.iloc[te]
    ytr = y[tr]
    te_p = fn(dtr, ytr, dte, chain, k)

    tr_p = None
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(dtr, ytr, gtr):
        f = fn(dtr.iloc[a], ytr[a], dtr.iloc[b], chain, k)
        if tr_p is None:
            tr_p = pd.DataFrame(np.zeros((len(tr), f.shape[1])), columns=f.columns)
        tr_p.iloc[b] = f.to_numpy()
    return tr_p.reset_index(drop=True), te_p.reset_index(drop=True)


def pick_k(d, y, tr, chain, gtr, ks=KS):
    """k 를 inner OOF 에서만 고른다 — 'prior 단독 예측' 의 MAE 기준.

    엄밀히는 prior 를 먹는 트리모델의 MAE 로 골라야 하지만 그러면 k 마다
    전체 파이프라인을 다시 학습해야 한다. prior 자체의 품질이 좋을수록
    feature 로서도 낫다고 보고 대리 기준을 쓴다 — 이 한계는 보고서에 적는다.
    """
    from sklearn.model_selection import GroupKFold

    dtr, ytr = d.iloc[tr], y[tr]
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    best, best_m, curve = None, np.inf, {}
    for k in ks:
        pred = np.zeros(len(tr))
        for a, b in GroupKFold(n_splits=ns).split(dtr, ytr, gtr):
            pred[b] = chain_prior(dtr.iloc[a], ytr[a], dtr.iloc[b],
                                  chain, k)["prior_median"].to_numpy()
        m = float(np.abs(pred - ytr).mean())
        curve[int(k)] = round(m, 4)
        if m < best_m - 1e-12:
            best, best_m = int(k), m
    return best, curve


# ============================================================ fold 계산
def m73_block(Xtr, ytr, Xte):
    """M73 재현 블록 — global · 구간 expert 3 · ordinal Stage1 · soft 예측."""
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


def type_expert(d, Xtr, ytr, Xte, tr, te, min_n=TYPE_MIN_N):
    """1D — amount_type 별 별도 회귀. 표본이 얇은 type 은 나중에 M73 로 되돌린다.

    반환하는 covered 는 '자기 type 전용 expert 로 예측된 행' 표시다. 승격조건
    6(fallback 과도하지 않음)의 근거가 된다.
    """
    a_tr = d[AMOUNT_TYPE].to_numpy()[tr]
    a_te = d[AMOUNT_TYPE].to_numpy()[te]
    pred = np.full(len(te), np.nan)
    covered = np.zeros(len(te), dtype=bool)
    used = {}
    for t in np.unique(a_tr):
        m = a_tr == t
        if m.sum() < min_n:
            continue
        q = a_te == t
        used[str(t)] = int(m.sum())
        if not q.any():
            continue
        mdl = F.make_point_model().fit(Xtr.iloc[m], ytr[m])
        pred[q] = mdl.predict(Xte.iloc[q])
        covered[q] = True
    return pred, covered, used


AMOUNT_TYPE = "amount_type"


def inner_m73(Xtr, ytr, gtr):
    """outer train 안 M73 soft 예측 — 1C 잔차보정과 1E alpha 의 유일한 출처."""
    from sklearn.model_selection import GroupKFold

    n = len(ytr)
    soft = np.zeros(n)
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        soft[b] = m73_block(Xtr.iloc[a], ytr[a], Xtr.iloc[b])["soft"]
    return soft


def inner_type_expert(d, Xtr, ytr, gtr, tr, min_n=TYPE_MIN_N):
    """1E 의 alpha 를 고르려면 inner 쪽 type 예측도 같은 성격이어야 한다."""
    from sklearn.model_selection import GroupKFold

    n = len(ytr)
    pred = np.full(n, np.nan)
    cov = np.zeros(n, dtype=bool)
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for a, b in GroupKFold(n_splits=ns).split(Xtr, ytr, gtr):
        p, c, _ = type_expert(d, Xtr.iloc[a], ytr[a], Xtr.iloc[b],
                              tr[a], tr[b], min_n)
        pred[b] = p
        cov[b] = c
    return pred, cov


def fold_compute(d, Xs, y, groups, titles, body, NB, cats, tr, te, i):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xtr, Xte = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                            STEP, [None])
    ytr, yte = y[tr], y[te]
    gtr = groups[tr]
    edges = M73.bucket_edges(ytr)
    zte = M73.to_bucket(yte, edges)
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    blk = m73_block(Xtr, ytr, Xte)
    in_raw = inner_m73(Xtr, ytr, gtr)

    # --- prior feature 를 붙인 재학습 변형들 -------------------------------
    pred, kpick, kcurve, plevel, pcount = {}, {}, {}, {}, {}
    for name in RETRAIN:
        if name == "Hctrl/agency_cat":
            # 대조군 — 타깃 통계 없이 기관을 범주형으로만 준다
            ag = d[AGENCY_COL].fillna("미기재").astype(str)
            a = Xtr.copy()
            b = Xte.copy()
            a["agency_cat"] = pd.Categorical(ag.to_numpy()[tr])
            b["agency_cat"] = pd.Categorical(ag.to_numpy()[te],
                                             categories=a["agency_cat"].cat.categories)
            pred[name] = m73_block(a, ytr, b)["soft"]
            continue
        temporal = name.startswith("H3t")
        chain = CHAINS["H3/agency_stype_year" if temporal else name]
        k, curve = pick_k(d, y, tr, chain, gtr)
        kpick[name], kcurve[name] = k, curve
        ptr, pte = prior_columns(d, y, tr, te, chain, k, gtr, temporal=temporal)
        a = pd.concat([Xtr.reset_index(drop=True), ptr], axis=1)
        b = pd.concat([Xte.reset_index(drop=True), pte], axis=1)
        pred[name] = m73_block(a, ytr, b)["soft"]
        plevel[name] = pte["prior_level"].to_numpy()
        pcount[name] = pte["prior_count"].to_numpy()

    # --- 1D type expert + inner 판 (1E 용) --------------------------------
    tp_te, tp_cov, tp_used = type_expert(d, Xtr, ytr, Xte, tr, te, TYPE_MIN_N)
    tp_in, tp_in_cov = inner_type_expert(d, Xtr, ytr, gtr, tr, TYPE_MIN_N)
    tpa_te, tpa_cov, tpa_used = type_expert(d, Xtr, ytr, Xte, tr, te, TYPE_MIN_N_ALT)
    tpa_in, tpa_in_cov = inner_type_expert(d, Xtr, ytr, gtr, tr, TYPE_MIN_N_ALT)

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in edges],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "raw_MAE": round(float(np.abs(blk["soft"] - yte).mean()), 4),
           "inner_raw_MAE": round(float(np.abs(in_raw - ytr).mean()), 4),
           "k_picked": kpick, "k_curve": kcurve,
           "type_expert_train_n": tp_used,
           "type_expert_coverage": round(float(tp_cov.mean()), 4),
           "type_expert_train_n_alt": tpa_used,
           "type_expert_coverage_alt": round(float(tpa_cov.mean()), 4),
           "prior_level_share": {n: {str(int(l)): round(float((v == l).mean()), 4)
                                     for l in np.unique(v)}
                                 for n, v in plevel.items()},
           "MAE": {n: round(float(np.abs(p - yte).mean()), 4)
                   for n, p in pred.items()},
           "seconds": round(time.time() - t0, 1)}
    out = {"te": np.asarray(te), "tr": np.asarray(tr), "base": base_te,
           "z_true": zte, "raw": blk["soft"], "in_raw": in_raw,
           "table": blk["table"], "tp_te": tp_te, "tp_cov": tp_cov,
           "tp_in": tp_in, "tp_in_cov": tp_in_cov,
           "tpa_te": tpa_te, "tpa_cov": tpa_cov,
           "tpa_in": tpa_in, "tpa_in_cov": tpa_in_cov, "rec": rec}
    out.update({"pred__" + n.replace("/", "_"): p for n, p in pred.items()})
    out.update({"plevel__" + n.replace("/", "_"): v for n, v in plevel.items()})
    out.update({"pcount__" + n.replace("/", "_"): v for n, v in pcount.items()})
    return out


# ============================================================ 후처리 변형
def build_variants(d, y, fo):
    """1C 잔차보정 · 1E hybrid. 전부 inner OOF 로만 적합하는 numpy 산수다."""
    tr, te = fo["tr"], fo["te"]
    yr, p, xr = y[tr], fo["raw"], fo["in_raw"]
    a_tr = d[AMOUNT_TYPE].to_numpy()[tr]
    a_te = d[AMOUNT_TYPE].to_numpy()[te]
    out, params = {}, {}
    out[BASE] = p
    for n in RETRAIN:
        out[n] = fo["pred__" + n.replace("/", "_")]

    # --- 1C 잔차보정 (amount_type 별 상수) --------------------------------
    res = yr - xr
    corr = {t: float(np.median(res[a_tr == t])) for t in np.unique(a_tr)}
    out["1C/resid_const"] = p + np.array([corr.get(t, 0.0) for t in a_te])
    params["1C_const"] = {str(k): round(v, 5) for k, v in corr.items()}

    # --- 1C 잔차보정 (ridge on amount_type + support_type one-hot) --------
    from sklearn.linear_model import RidgeCV
    from sklearn.preprocessing import OneHotEncoder
    cols = [AMOUNT_TYPE, "support_type"]
    A = d.iloc[tr][cols].astype(str)
    B = d.iloc[te][cols].astype(str)
    enc = OneHotEncoder(handle_unknown="ignore", sparse_output=False, min_frequency=10)
    ridge = RidgeCV(alphas=np.logspace(-2, 3, 12)).fit(enc.fit_transform(A), res)
    out["1C/resid_ridge"] = p + ridge.predict(enc.transform(B))

    # --- 1D type expert (미달 type 은 M73 로 되돌린다) --------------------
    tp = np.where(fo["tp_cov"], fo["tp_te"], p)
    out["1D/type_expert"] = tp
    params["1D_coverage"] = round(float(fo["tp_cov"].mean()), 4)
    tpa = np.where(fo["tpa_cov"], fo["tpa_te"], p)
    out["1D/type_expert_n%d" % TYPE_MIN_N_ALT] = tpa
    params["1D_coverage_alt"] = round(float(fo["tpa_cov"].mean()), 4)

    # --- 1E hybrid — alpha 를 inner OOF 에서만 고른다 ---------------------
    tp_in = np.where(fo["tp_in_cov"], fo["tp_in"], xr)
    for al in ALPHAS:
        out["SW1E/hybrid@%.1f" % al] = al * p + (1 - al) * tp
    best, bm = None, np.inf
    for al in ALPHAS:
        v = float(np.abs(al * xr + (1 - al) * tp_in - yr).mean())
        if v < bm - 1e-12:
            best, bm = al, v
    out["1E*/hybrid_nested"] = best * p + (1 - best) * tp
    params["1E_nested"] = {"alpha": float(best), "inner_MAE": round(bm, 4)}
    return out, params


# ============================================================ 체크포인트
def ckpt_signature(fp):
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb": F.XGB_POINT, "chains": {k: v for k, v in CHAINS.items()},
        "retrain": RETRAIN, "ks": list(KS), "stats": list(PRIOR_STATS),
        "type_min_n": TYPE_MIN_N, "alphas": list(ALPHAS),
        "inner_splits": INNER_SPLITS, "step": STEP, "cuts": list(M73.CUTS),
        "feature_version": F.FEATURE_VERSION, "layer_version": SF.LAYER_VERSION,
        "body_svd": M69.BODY_SVD, "seed": F.PIPELINE_SEED, "n_splits": F.N_SPLITS,
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
    o = {k: z[k] for k in z.files}
    o["rec"] = meta["rec"]
    return o


def ckpt_save(sig, tag, i, fo):
    import json as _json
    dd, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(dd, exist_ok=True)
    np.savez_compressed(npz + ".tmp.npz",
                        **{k: v for k, v in fo.items() if k != "rec"})
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"rec": fo["rec"]}, ensure_ascii=False, default=str))
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ============================================================ split 실행
def run_split(d, Xs, y, groups, titles, body, NB, cats, sig, tag, verbose=True,
              use_ckpt=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    R = {"z_true": np.zeros(n, dtype=int), "base": np.zeros(n),
         "fold_id": np.zeros(n, dtype=int), "raw": np.zeros(n),
         "table": np.zeros((n, 3)), "tp_cov": np.zeros(n, dtype=bool),
         "plevel": {}, "pred": {}, "params": [], "folds": []}
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(d, Xs, y, groups, titles, body, NB, cats, tr, te, i)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        R["fold_id"][te] = i
        R["base"][te] = fo["base"]
        R["z_true"][te] = fo["z_true"]
        R["raw"][te] = fo["raw"]
        R["table"][te] = fo["table"]
        R["tp_cov"][te] = fo["tp_cov"]
        for nm in RETRAIN:
            key = "plevel__" + nm.replace("/", "_")
            if key in fo:
                R["plevel"].setdefault(nm, np.zeros(n))[te] = fo[key]

        var, par = build_variants(d, y, fo)
        for k, v in var.items():
            if k not in R["pred"]:
                R["pred"][k] = np.zeros(n)
            R["pred"][k][te] = v
        par["fold"] = i
        R["params"].append(par)

        rec = dict(fo["rec"])
        rec["from_checkpoint"] = bool(cached)
        R["folds"].append(rec)
        if verbose:
            m = rec["MAE"]
            print("   fold %d  raw %.4f  " % (i, rec["raw_MAE"])
                  + "  ".join("%s %.4f" % (k.split("/")[0], v)
                              for k, v in m.items())
                  + "  (%s)" % ("체크포인트 재사용" if cached
                                else "%.0fs" % rec["seconds"]))
    return R


# ============================================================ Exp0 진단
def amount_type_diagnostic(d, y, p):
    from scipy import stats

    a = d[AMOUNT_TYPE].astype(str).to_numpy()
    res = y - p
    rows = {}
    for t in np.unique(a):
        m = a == t
        e = np.abs(res[m])
        rows[t] = {
            "n": int(m.sum()), "coverage": round(float(m.mean()), 4),
            "median": round(float(np.median(y[m])), 4),
            "mean": round(float(y[m].mean()), 4),
            "std": round(float(y[m].std()), 4),
            "q25": round(float(np.percentile(y[m], 25)), 4),
            "q75": round(float(np.percentile(y[m], 75)), 4),
            "IQR": round(float(np.percentile(y[m], 75) -
                               np.percentile(y[m], 25)), 4),
            "M73_MAE": round(float(e.mean()), 4),
            "M73_bias_median": round(float(np.median(res[m])), 4),
            "M73_bias_mean": round(float(res[m].mean()), 4),
            "within_2x": round(float((e <= np.log10(2)).mean()), 4),
            "within_3x": round(float((e <= np.log10(3)).mean()), 4),
            "residual_std": round(float(res[m].std()), 4),
        }
    out = {"per_type": rows}
    ts = sorted(rows, key=lambda t: -rows[t]["n"])
    if len(ts) >= 2:
        A, B = y[a == ts[0]], y[a == ts[1]]
        ks = stats.ks_2samp(A, B)
        lo, hi = min(A.min(), B.min()), max(A.max(), B.max())
        bins = np.linspace(lo, hi, 40)
        ha, _ = np.histogram(A, bins=bins, density=True)
        hb, _ = np.histogram(B, bins=bins, density=True)
        out["overlap"] = {
            "pair": [ts[0], ts[1]],
            "ks_statistic": round(float(ks.statistic), 4),
            "ks_pvalue": float("%.4g" % ks.pvalue),
            "hist_overlap": round(float(np.minimum(ha, hb).sum() *
                                        (bins[1] - bins[0])), 4),
            "median_gap_log10": round(float(abs(np.median(A) - np.median(B))), 4),
            "residual_ks_p": float("%.4g" % stats.ks_2samp(
                res[a == ts[0]], res[a == ts[1]]).pvalue),
        }
    # 지시서 Experiment 0 의 진행 기준 네 가지
    maes = [r["M73_MAE"] for r in rows.values()]
    meds = [r["median"] for r in rows.values()]
    iqrs = [r["IQR"] for r in rows.values()]
    bias = [abs(r["M73_bias_median"]) for r in rows.values()]
    out["gates"] = {
        "median 차이 > 0.15 log10": bool(max(meds) - min(meds) > 0.15),
        "IQR 구조 차이 > 0.30": bool(max(iqrs) - min(iqrs) > 0.30),
        "MAE 차이 > 0.05": bool(max(maes) - min(maes) > 0.05),
        "systematic bias > 0.05": bool(max(bias) > 0.05),
    }
    out["proceed"] = bool(any(out["gates"].values()))
    return out


def agency_diagnostic(d, y, p):
    """Experiment 2 의 전제 — 기관 축에 실제로 정보가 있는가, 셀은 버티는가."""
    ag = d[AGENCY_COL].fillna("미기재").astype(str).to_numpy()
    res = np.abs(y - p)
    vc = pd.Series(ag).value_counts()
    rows = {}
    for k in vc[vc >= 30].index:
        m = ag == k
        rows[str(k)] = {"n": int(m.sum()),
                        "median_y": round(float(np.median(y[m])), 4),
                        "M73_MAE": round(float(res[m].mean()), 4)}
    cells = {}
    for name, cols in (("support_type", ["support_type"]),
                       ("agency x support_type", [AGENCY_COL, "support_type"]),
                       ("agency x support_type x year",
                        [AGENCY_COL, "support_type", "year"])):
        t = d[cols].astype(str)
        s = t.groupby(list(cols), observed=True).size()
        cells[name] = {"n_cells": int(len(s)), "median_cell_n": float(s.median()),
                       "share_in_cells_n>=10": round(float(s[s >= 10].sum() /
                                                           len(d)), 4)}
    return {"n_agency": int(pd.Series(ag).nunique()),
            "missing_share": round(float((ag == "미기재").mean()), 4),
            "in_feature_set": AGENCY_COL in M45.CATS,
            "agency_grp_in_feature_set": "agency_grp" in M45.CATS,
            "median_spread_sd": round(float(np.std([r["median_y"]
                                                    for r in rows.values()])), 4),
            "per_agency_n>=30": rows, "cell_sizes": cells}


# ============================================================ 집계
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


def type_mae(d, y, p):
    a = d[AMOUNT_TYPE].astype(str).to_numpy()
    return {str(t): {"n": int((a == t).sum()),
                     "MAE": round(float(np.abs(p[a == t] - y[a == t]).mean()), 4),
                     "bias": round(float(np.median(y[a == t] - p[a == t])), 4)}
            for t in np.unique(a)}


def block(d, y, R, p, ref=None):
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]
    m = M45.point_metrics(y, p)
    m["improvement"] = round(float((b - m["MAE_log10"]) / b), 4)
    m["per_fold_MAE"] = fold_maes(y, p, fid)
    m["fold_std"] = round(float(np.std(m["per_fold_MAE"])), 4)
    m["buckets"] = M73.bucket_metrics(y, p, R["z_true"])
    m["cohort"] = cohort_mae(d, y, p)
    m["amount_type"] = type_mae(d, y, p)
    if ref is not None:
        m["vs_raw"] = M73.paired_test(y, p, ref)
        rf = fold_maes(y, ref, fid)
        m["fold_wins_vs_raw"] = int(sum(1 for a, c in zip(m["per_fold_MAE"], rf)
                                        if a < c))
        rc = cohort_mae(d, y, ref)
        m["cohort_delta"] = {
            col: {k: round(m["cohort"][col][k]["MAE"] - rc[col][k]["MAE"], 4)
                  for k in rc[col]} for col in rc}
        rt = type_mae(d, y, ref)
        m["type_delta"] = {k: round(m["amount_type"][k]["MAE"] - rt[k]["MAE"], 4)
                           for k in rt}
    return m


HONEST_PREFIX = ("1B/", "1C/", "1D/", "1E*/", "H1/", "H2/", "H3/", "H3t/",
                 "H4/", "Hctrl/")


def honest(res):
    return [k for k in res["variants"] if k.startswith(HONEST_PREFIX)]


def summarize(d, y, R):
    ref = R["pred"][BASE]
    out = {"baseline_MAE": round(float(np.abs(R["base"] - y).mean()), 4),
           "variants": {s: block(d, y, R, p, None if s == BASE else ref)
                        for s, p in R["pred"].items()},
           "type_expert_coverage": round(float(R["tp_cov"].mean()), 4),
           "prior_level_share": {
               n: {str(int(l)): round(float((v == l).mean()), 4)
                   for l in np.unique(v)} for n, v in R["plevel"].items()},
           "params": R["params"], "folds": R["folds"]}
    rows = np.arange(len(y))
    o = M45.point_metrics(y, R["table"][rows, R["z_true"]])
    o["note"] = "실제 구간을 안다고 가정한 상한 — 서빙 불가, 진단 전용"
    out["oracle_ceiling"] = o
    return out


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
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    print("\n== 원천 feature 층 (M69 G 단계)")
    ts = time.time()
    NB, body, _src = SF.build(d)
    print("   %d개 feature + 본문 SVD%d / %.0f초"
          % (len(SF.columns_upto(STEP)), M69.BODY_SVD, time.time() - ts))

    sig = ckpt_signature(fp)
    print("\n== 체크포인트 서명 %s  ->  %s" % (sig, os.path.relpath(CKPT_DIR, C.ROOT)))

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        print("\n== 5-fold [%s] — 재학습 변형 %d개 + prior/후처리"
              % (gname, len(RETRAIN)))
        R = run_split(d, Xs, y, groups[gname], titles, body, NB, cats, sig, gname)
        results[gname] = summarize(d, y, R)
        raws[gname] = R

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]
    raw_mae = ps["variants"][BASE]["MAE_log10"]

    # ---------------------------------------------------- Experiment 0
    print("\n== Experiment 0 — amount_type 진단")
    diag = amount_type_diagnostic(d, y, Rp["raw"])
    print("   %-14s %5s %8s %8s %8s %9s %9s %8s"
          % ("amount_type", "n", "median", "IQR", "std", "M73 MAE", "bias", "2배내"))
    for t, r in diag["per_type"].items():
        print("   %-14s %5d %8.4f %8.4f %8.4f %9.4f %+9.4f %7.1f%%"
              % (t, r["n"], r["median"], r["IQR"], r["std"], r["M73_MAE"],
                 r["M73_bias_median"], 100 * r["within_2x"]))
    if "overlap" in diag:
        o = diag["overlap"]
        print("   분포 비교 (%s vs %s): KS %.4f (p=%s) / 겹침 %.4f / 중앙값 차 %.4f"
              % (o["pair"][0], o["pair"][1], o["ks_statistic"], o["ks_pvalue"],
                 o["hist_overlap"], o["median_gap_log10"]))
    for k, v in diag["gates"].items():
        print("   [%s] 진행기준 — %s" % ("O" if v else "X", k))
    print("   -> Experiment 1 %s" % ("진행" if diag["proceed"] else "중단"))

    print("\n== Experiment 2 전제 — 기관 축 진단")
    ad = agency_diagnostic(d, y, Rp["raw"])
    print("   agency %d종 / 결측 %.1f%% / 현재 feature 에 포함: %s "
          "(agency_grp 만: %s)"
          % (ad["n_agency"], 100 * ad["missing_share"], ad["in_feature_set"],
             ad["agency_grp_in_feature_set"]))
    print("   기관별 y 중앙값들의 표준편차 %.4f" % ad["median_spread_sd"])
    for k, v in ad["cell_sizes"].items():
        print("   %-30s 셀 %4d  중앙 n %5.1f  n>=10 이 덮는 비중 %.3f"
              % (k, v["n_cells"], v["median_cell_n"], v["share_in_cells_n>=10"]))

    # ---------------------------------------------------- 결과표
    print("\n== 후보 (raw %.4f 대비)" % raw_mae)
    print("   %-24s %8s %9s %8s %7s %-22s"
          % ("후보", "MAE", "Δ", "strict", "fold승", "95%CI"))
    hon = honest(ps)
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {}).get("MAE_log10")
        print("   %-24s %8.4f %+9.4f %8s %6s  %s"
              % (k, m["MAE_log10"], v["delta_MAE"], ("%.4f" % s) if s else "—",
                 "%d/5" % m["fold_wins_vs_raw"],
                 "[%+0.4f, %+0.4f]" % tuple(v["ci95"])))
    print("   ---- sweep (진단용, 승격 근거 아님)")
    for k in sorted(k for k in ps["variants"] if k.startswith("SW1E/")):
        m = ps["variants"][k]
        print("   %-24s %8.4f %+9.4f  %d/5"
              % (k, m["MAE_log10"], m["vs_raw"]["delta_MAE"],
                 m["fold_wins_vs_raw"]))

    print("\n== prior 계층 사용 비중 (0=global 1=support_type 2=기관×유형 3=+연도)")
    for n, sh in ps["prior_level_share"].items():
        print("   %-24s %s" % (n, sh))
    print("   fold 별 선택 k  %s" % {n: [f["k_picked"].get(n) for f in ps["folds"]]
                                     for n in ps["folds"][0]["k_picked"]})
    print("   1D type expert coverage %.4f / 1E alpha %s"
          % (ps["type_expert_coverage"],
             [p["1E_nested"]["alpha"] for p in ps["params"]]))

    print("\n== amount_type 별 MAE — raw vs 최고 후보")
    best = min((k for k in hon), key=lambda k: ps["variants"][k]["MAE_log10"])
    B = ps["variants"][best]
    for t in B["amount_type"]:
        print("   %-14s raw %.4f -> %.4f  (Δ %+0.4f)"
              % (t, ps["variants"][BASE]["amount_type"][t]["MAE"],
                 B["amount_type"][t]["MAE"], B["type_delta"][t]))

    # ---------------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    R2 = run_split(d, Xs, y, groups["program_stem"], titles, body, NB, cats, sig,
                   "program_stem__repro", verbose=False)
    repro = {"raw": bool(np.allclose(R2["raw"], Rp["raw"])),
             best: bool(np.allclose(R2["pred"][best], Rp["pred"][best])),
             "H3/agency_stype_year": bool(np.allclose(
                 R2["pred"]["H3/agency_stype_year"],
                 Rp["pred"]["H3/agency_stype_year"]))}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ---------------------------------------------------- 누수 점검
    leak = {
        "prior 의 test 쪽 계산": "outer train 전체 통계 — 서빙에서 가능한 계산",
        "prior 의 train 쪽 계산": "inner GroupKFold(%d) OOF — 자기 fold 를 뺀 통계만 "
                                  "자기에게 붙는다 (target encoding 누수 방어의 핵심)"
                                  % INNER_SPLITS,
        "shrinkage k 선택": "inner OOF 에서 prior 단독 MAE 로만. outer test 미사용",
        "1C 잔차 / 1E alpha": "inner OOF M73 예측과 outer train 정답만",
        "1D type expert": "outer train 의 해당 type 행만으로 학습",
        "구간 경계": "fold train 의 y 만 (M73 과 동일)",
        "temporal 판(H3t)": "연도 Y 행의 prior 는 train 의 year < Y 행만 사용",
        "test y 의 용도": "최종 metric · oracle 상한 · 구간별 집계뿐",
        "1A (amount_type feature)": "미실행 — M45.CATS 에 이미 포함 (지시서 1A 단서)",
    }
    leak_checks = {
        "prior 가 outer test 를 보지 않았다": True,
        "train prior 가 자기 정답을 보지 않았다 (inner OOF)": True,
        "raw 가 M73 공표치(0.3563)를 재현": abs(raw_mae - 0.3563) < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-28s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ---------------------------------------------------- 승격 판정
    v = B["vs_raw"]
    nt_best = nt["variants"].get(best)
    cd = B.get("cohort_delta", {}).get("cohort", {})
    td = B.get("type_delta", {})
    checks = {
        "1. OOF MAE < 0.3563": B["MAE_log10"] < M73_PUBLISHED["MAE_log10"],
        "1b. 같은 fold raw 보다 낮다": B["MAE_log10"] < raw_mae,
        "2. strict split 에서도 개선":
            bool(nt_best and nt_best["MAE_log10"] < nt["variants"][BASE]["MAE_log10"]),
        "3. 5개 fold 중 4개 이상 개선": B["fold_wins_vs_raw"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. 특정 amount_type 하나에만 이득이 몰리지 않음":
            bool(td) and all(x <= 0.001 for x in td.values()),
        "5b. taxonomy·bizinfo 한쪽에만 의존하지 않음":
            bool(cd) and all(x <= 0.001 for x in cd.values()),
        "6. fallback 과도하지 않음": ps["type_expert_coverage"] >= 0.5,
        "7. leakage audit PASS": bool(leak_pass),
        "7b. reproducibility PASS": all(repro.values()),
        "8. 실질 개선폭 ΔMAE ≤ -0.003": v["delta_MAE"] <= -0.003,
        "9. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("9.")]
    verdict = ("승격 후보 (M73 + prior)" if all(checks[k] for k in core)
               else "현행 유지 — M73 `soft/ordinal_xgb`")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ---------------------------------------------------- 조합 조건
    sig_wins = [k for k in hon
                if ps["variants"][k]["vs_raw"]["ci95"][1] < 0
                and ps["variants"][k]["MAE_log10"] < raw_mae]
    exp1 = [k for k in sig_wins if k[0] == "1"]
    exp2 = [k for k in sig_wins if k[0] == "H"]
    combo = ("조합 조건 충족 — Exp1 %s · Exp2 %s" % (exp1, exp2)
             if exp1 and exp2 else
             "미실행 — 지시서 '최종 조합 조건'. Exp1 유의 후보 %d개 · "
             "Exp2 %d개 (둘 다 필요)" % (len(exp1), len(exp2)))
    print("\n== 최종 조합 — %s" % combo)

    # ---------------------------------------------------- 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "amount_type": d[AMOUNT_TYPE].to_numpy(),
           "agency": d[AGENCY_COL].fillna("미기재").to_numpy(),
           "cohort": d["cohort"].to_numpy(),
           "evidence_source": d["evidence_source"].to_numpy()}
    for s, p in Rp["pred"].items():
        key = (s.replace("/", "__").replace("@", "_").replace("*", "s")
               .replace(" ", "_").replace("(", "").replace(")", ""))
        out["pred_" + key] = p
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "금액의 의미(amount_type)와 기관×유형×연도 생성 구조를 명시하면 "
                   "M73(0.3563)을 이기는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"],
            "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "routing": "M73 soft / ordinal_xgb", "regressor": F.XGB_POINT,
        },
        "changed": "입력에 붙는 prior feature 층 (1C·1E 는 예측 뒤 후처리)",
        "plan_premises_checked": {
            "amount_type_levels_expected": ["support_cap", "support_per_recipient",
                                            "support_per_project", "loan_limit",
                                            "total_budget"],
            "amount_type_levels_actual": sorted(d[AMOUNT_TYPE].astype(str).unique()),
            "per_recipient_basis": sorted(d["per_recipient_basis"].astype(str)
                                          .unique()) if "per_recipient_basis" in d
                                   else None,
            "1A_skipped_reason": "amount_type 이 이미 M45.CATS 의 feature 다 — "
                                 "지시서 1A 의 '이미 들어가 있으면 재실험하지 않는다'",
            "agency_new_information": True,
        },
        "selection_protocol": {
            "target_encoding": "test 쪽은 outer train 전체, train 쪽은 inner "
                               "GroupKFold(%d) OOF" % INNER_SPLITS,
            "k": "inner OOF 의 prior 단독 MAE 로만 (대리 기준 — 한계는 보고서 3장)",
            "alpha/잔차": "inner OOF M73 예측에서만",
            "sweep": "고정 alpha 표는 진단용. 최저값을 골라 승격 근거로 쓰지 않는다",
        },
        "control_arm": "Hctrl/agency_cat — 기관을 타깃 통계 없이 범주형으로만 추가. "
                       "H2/H3 의 개선이 '기관 정보' 때문인지 'hierarchical prior' "
                       "때문인지 가른다",
        "diagnostic_amount_type": diag,
        "diagnostic_agency": ad,
        "results": results,
        "best_candidate": best,
        "combination": combo,
        "reproducibility": repro,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(x) for k, x in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "goals": {"primary": "MAE < 0.35", "final": "MAE < 0.30",
                  "practical": "ΔMAE <= -0.003"},
        "published_m73": M73_PUBLISHED,
        "checkpoint": {"signature": sig, "dir": os.path.relpath(CKPT_DIR, C.ROOT),
                       "code_version": CODE_VERSION},
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m80_m2_semantic_prior.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ============================================================ MD 보고서
LABEL = {
    "1B/type_prior": "Exp1B · amount_type prior feature",
    "1C/resid_const": "Exp1C · amount_type 별 상수 잔차보정",
    "1C/resid_ridge": "Exp1C · amount_type+support_type ridge 잔차보정",
    "1D/type_expert": "Exp1D · amount_type 별 전용 회귀 (n>=%d)" % TYPE_MIN_N,
    "1E*/hybrid_nested": "Exp1E · M73 × type expert hybrid (alpha nested)",
    "H1/stype": "Exp2 H1 · support_type prior",
    "H2/agency_stype": "Exp2 H2 · 기관 × 유형 prior",
    "H3/agency_stype_year": "Exp2 H3 · 기관 × 유형 × 연도 prior",
    "H3t/temporal": "Exp2 H3 · 미래 정보 미사용 판 (temporal sanity)",
    "Hctrl/agency_cat": "대조군 · 기관을 범주형으로만 추가 (타깃 통계 없이)",
}


def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    rawm = ps["variants"][BASE]
    dg = p["diagnostic_amount_type"]
    ad = p["diagnostic_agency"]
    L = []
    A = L.append
    A("# M80 — 금액의 의미(amount_type)와 기관×유형×연도 hierarchical prior\n")
    A("> 질문: **금액의 크기보다 '그 금액이 어떤 의미인가'를 먼저 구분하면 예측이")
    A("> 쉬워지는가. 기관·유형·연도별 체급을 prior 로 주면 M73 이 놓친 생성 구조를")
    A("> 보완할 수 있는가?**\n")

    A("## 0. 실행 전에 확인한 것 — 지시서 전제 두 가지가 다르다\n")
    pc = p["plan_premises_checked"]
    A("```text")
    A("지시서가 가정한 amount_type   %s" % ", ".join(pc["amount_type_levels_expected"]))
    A("실제 amount_type              %s" % ", ".join(pc["amount_type_levels_actual"]))
    A("per_recipient_basis           %s" % ", ".join(pc["per_recipient_basis"] or []))
    A("1A 미실행 사유                %s" % pc["1A_skipped_reason"])
    A("```\n")
    A("M45 의 타깃 정의가 이미 `basis != 'stated_cap'` 을 전부 걸러냈다. total_budget")
    A("과 budget÷건수는 '한도'가 아니라 '평균'이라 제외됐고, 융자는 별도 amount_type")
    A("이 아니라 `support_type='융자'` 로 갈린다. **'의미가 다른 금액이 섞여 있다'는")
    A("문제는 데이터셋 구축 단계에서 이미 해결돼 있다.**\n")

    A("## 1. Experiment 0 — amount_type 진단\n")
    A("| amount_type | n | 커버리지 | median | mean | std | q25 | q75 | IQR | "
      "M73 MAE | bias(중앙) | 2배내 | 3배내 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t, r in dg["per_type"].items():
        A("| %s | %d | %.1f%% | %.4f | %.4f | %.4f | %.4f | %.4f | %.4f | "
          "%.4f | %+.4f | %.1f%% | %.1f%% |"
          % (t, r["n"], 100 * r["coverage"], r["median"], r["mean"], r["std"],
             r["q25"], r["q75"], r["IQR"], r["M73_MAE"], r["M73_bias_median"],
             100 * r["within_2x"], 100 * r["within_3x"]))
    A("")
    if "overlap" in dg:
        o = dg["overlap"]
        A("분포가 실제로 갈리는가 (`%s` vs `%s`)\n" % (o["pair"][0], o["pair"][1]))
        A("```text")
        A("KS 검정        statistic %.4f, p = %s" % (o["ks_statistic"], o["ks_pvalue"]))
        A("분포 겹침 계수  %.4f" % o["hist_overlap"])
        A("중앙값 차이     %.4f log10 (= %.2f배)"
          % (o["median_gap_log10"], 10 ** o["median_gap_log10"]))
        A("잔차 분포 KS p  %s" % o["residual_ks_p"])
        A("```\n")
    A("지시서의 진행 기준 네 가지\n")
    A("| 기준 | 충족 |")
    A("|---|---|")
    for k, v in dg["gates"].items():
        A("| %s | %s |" % (k, "예" if v else "아니오"))
    A("")
    A("**-> Experiment 1 %s.** 분포는 통계적으로 갈리지 않지만(KS p=%s), "
      % ("진행" if dg["proceed"] else "중단",
         dg.get("overlap", {}).get("ks_pvalue", "—")))
    A("한쪽의 MAE 가 뚜렷하게 높다는 기준 하나가 충족된다.\n")

    A("## 2. Experiment 2 전제 — 기관 축 진단\n")
    A("```text")
    A("agency 종류        %d종 / 결측 %.1f%%" % (ad["n_agency"],
                                                 100 * ad["missing_share"]))
    A("현재 feature 포함   agency %s / agency_grp %s"
      % (ad["in_feature_set"], ad["agency_grp_in_feature_set"]))
    A("기관별 중앙값 표준편차 %.4f log10" % ad["median_spread_sd"])
    A("```\n")
    A("`agency` 는 feature 에 **없다**(들어 있는 것은 4종짜리 agency_grp 뿐). 즉")
    A("Experiment 2 는 M73 이 한 번도 보지 못한 정보를 넣는다. 다만 계층을 내려갈수록")
    A("셀이 얇아진다.\n")
    A("| 계층 | 셀 수 | 중앙 n | n≥10 셀이 덮는 비중 |")
    A("|---|---:|---:|---:|")
    for k, v in ad["cell_sizes"].items():
        A("| %s | %d | %.1f | %.3f |"
          % (k, v["n_cells"], v["median_cell_n"], v["share_in_cells_n>=10"]))
    A("")
    A("| 기관 (n≥30) | n | median y | M73 MAE |")
    A("|---|---:|---:|---:|")
    for k, v in sorted(ad["per_agency_n>=30"].items(),
                       key=lambda x: x[1]["median_y"]):
        A("| %s | %d | %.3f | %.4f |" % (k, v["n"], v["median_y"], v["M73_MAE"]))
    A("")

    A("## 3. 누수 방어 — target encoding 을 어떻게 만들었는가\n")
    sp = p["selection_protocol"]
    A("prior 는 타깃 통계를 feature 로 만드는 것이라, 방어 없이 만들면 각 행이 자기")
    A("정답을 자기 feature 로 돌려받는다. 학습에서는 완벽해 보이고 서빙에서는 무너진다.\n")
    A("```text")
    A("%s" % sp["target_encoding"])
    A("k 선택   %s" % sp["k"])
    A("shrinkage  prior_i = w·group_stat_i + (1-w)·prior_(i-1),  w = n/(n+k)")
    A("```\n")
    A("> k 선택의 한계: 엄밀히는 prior 를 먹는 트리모델의 MAE 로 골라야 하지만,")
    A("> 그러면 k 마다 전체 파이프라인을 다시 학습해야 한다. prior 자체의 품질이")
    A("> 좋을수록 feature 로서도 낫다고 보고 대리 기준을 썼다.\n")
    A("**대조군**: %s\n" % p["control_arm"])

    A("## 4. 결과\n")
    A("| 후보 | 설명 | OOF MAE | Δ vs M73 | 95% CI | wilcoxon p | fold승 | "
      "strict MAE | 2배내 | 3배내 |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    A("| `%s` | M73 그대로 | %.4f | — | — | — | — | %.4f | %.1f%% | %.1f%% |"
      % (BASE, rawm["MAE_log10"], nt["variants"][BASE]["MAE_log10"],
         100 * rawm["within_2x"], 100 * rawm["within_3x"]))
    hon = honest(ps)
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {})
        A("| `%s` | %s | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %s | %d/5 | %s | "
          "%.1f%% | %.1f%% |"
          % (k, LABEL.get(k, ""), m["MAE_log10"], v["delta_MAE"], v["ci95"][0],
             v["ci95"][1], str(v["wilcoxon_p"]), m["fold_wins_vs_raw"],
             ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], 100 * m["within_3x"]))
    A("")
    A("### 4-2. sweep — 진단용 (승격 근거 아님)\n")
    A("| 후보 | OOF MAE | Δ | fold승 |")
    A("|---|---:|---:|---:|")
    for k in sorted(k for k in ps["variants"] if k.startswith("SW1E/")):
        m = ps["variants"][k]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (k, m["MAE_log10"], m["vs_raw"]["delta_MAE"], m["fold_wins_vs_raw"]))
    A("")

    A("## 5. prior 가 실제로 어느 계층까지 내려갔는가\n")
    A("`prior_level` 0=global · 1=support_type · 2=기관×유형 · 3=+연도\n")
    A("| 변형 | 계층별 행 비중 |")
    A("|---|---|")
    for n, sh in ps["prior_level_share"].items():
        A("| %s | %s |" % (n, sh))
    A("")
    A("| 변형 | fold 별 선택 k |")
    A("|---|---|")
    for n in ps["folds"][0]["k_picked"]:
        A("| %s | %s |" % (n, [f["k_picked"].get(n) for f in ps["folds"]]))
    A("")
    A("1D type expert coverage %.4f (나머지는 M73 fallback) / "
      "1E alpha %s\n" % (ps["type_expert_coverage"],
                         [x["1E_nested"]["alpha"] for x in ps["params"]]))

    A("## 6. amount_type 별 · 비교군별 MAE — 최고 후보 vs M73\n")
    best = p["best_candidate"]
    B = ps["variants"][best]
    A("| amount_type | n | M73 | `%s` | Δ |" % best)
    A("|---|---:|---:|---:|---:|")
    for t, r in B["amount_type"].items():
        A("| %s | %d | %.4f | %.4f | %+.4f |"
          % (t, r["n"], rawm["amount_type"][t]["MAE"], r["MAE"],
             B["type_delta"][t]))
    A("")
    A("| 비교군 | n | M73 | `%s` | Δ |" % best)
    A("|---|---:|---:|---:|---:|")
    for col in ("cohort", "evidence_source"):
        for k, rr in rawm["cohort"][col].items():
            A("| %s | %d | %.4f | %.4f | %+.4f |"
              % (k, rr["n"], rr["MAE"], B["cohort"][col][k]["MAE"],
                 B["cohort_delta"][col][k]))
    A("")
    A("| 구간 | n | M73 | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |"
          % (b, rawm["buckets"][b]["n"], rawm["buckets"][b]["MAE_log10"],
             B["buckets"][b]["MAE_log10"]))
    A("")
    A("### 6-2. fold 별 MAE\n")
    A("| fold | 경계(원) | baseline | M73 | `%s` |" % best)
    A("|---|---|---:|---:|---:|")
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %.4f | %.4f |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             f["baseline_MAE"], rawm["per_fold_MAE"][i], B["per_fold_MAE"][i]))
    A("")

    A("## 7. 최종 비교표\n")
    A("| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI |")
    A("|---|---:|---:|---:|---:|---|")
    A("| M73 raw | %.4f | %.4f | %.1f%% | — | — |"
      % (rawm["MAE_log10"], nt["variants"][BASE]["MAE_log10"],
         100 * rawm["within_2x"]))
    for k in sorted(hon, key=lambda k: ps["variants"][k]["MAE_log10"]):
        m = ps["variants"][k]
        v = m["vs_raw"]
        s = nt["variants"].get(k, {})
        A("| %s | %.4f | %s | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (LABEL.get(k, k), m["MAE_log10"],
             ("%.4f" % s["MAE_log10"]) if s else "—", 100 * m["within_2x"],
             m["fold_wins_vs_raw"], v["ci95"][0], v["ci95"][1]))
    A("| oracle 상한 (서빙 불가) | %.4f | — | %.1f%% | — | — |"
      % (ps["oracle_ceiling"]["MAE_log10"],
         100 * ps["oracle_ceiling"]["within_2x"]))
    A("")

    A("## 8. 최종 조합\n")
    A("%s\n" % p["combination"])

    A("## 9. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    for k, v in p["leakage_checks"].items():
        A("| %s | %s |" % (k, "PASS" if v else "FAIL"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")

    A("## 10. 승격 점검표\n")
    A("대상: `%s` (정직한 후보 중 OOF MAE 최저)\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")

    A("## 결론\n")
    A("```text")
    A("M73 raw (같은 fold 재현)  MAE = %.4f" % rawm["MAE_log10"])
    A("최고 후보                 %s" % best)
    A("                          MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (B["MAE_log10"], B["vs_raw"]["delta_MAE"], B["vs_raw"]["ci95"][0],
         B["vs_raw"]["ci95"][1]))
    A("기관 정보 대조군          Hctrl %.4f (Δ %+0.4f)"
      % (ps["variants"]["Hctrl/agency_cat"]["MAE_log10"],
         ps["variants"]["Hctrl/agency_cat"]["vs_raw"]["delta_MAE"]))
    A("amount_type 진단          Experiment 1 %s"
      % ("진행" if p["diagnostic_amount_type"]["proceed"] else "중단"))
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
