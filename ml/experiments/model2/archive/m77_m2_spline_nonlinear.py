r"""M77 — 연속형 사업설계 변수와 지원규모 사이의 곡선 관계를 명시적으로 넣으면
M73 을 이기는가.

지시서(사용자, `m77_model2_gam_spline_nonlinear_regression_plan.md`):

    현재 모델 2 최종 후보는 M73 `soft/ordinal_xgb` (OOF 0.3563 / strict 0.3756).
    routing · expert tuning · local cohort 와 다른 축으로,
    지원비율 · 선정기업 수 · 사업기간 같은 연속형 숫자 feature 와 지원규모
    사이에 직선이 아닌 곡선 관계가 있는지 보고, 있다면 그 관계를 spline
    feature 로 명시해 M73 에 추가 성능이 있는지 확인한다.

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 (구조화 + 제목 SVD64 + 원천 feature 층 + 본문 SVD64)
    routing    M73 soft / ordinal_xgb — 구간 33.3/66.7, 확률가중 평균
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    masking    M69/M73 의 금액·숫자 마스킹 규칙 그대로
    baseline   같은 fold 에서 같이 학습한 M73 soft (paired 비교의 분모)

바뀌는 것은 **numeric feature 의 표현 하나**다. 기존 numeric 컬럼은 그대로
두고 그 위에 spline basis 를 얹는다(대체가 아니라 추가).

## 이 실험의 위험한 자리 — degree/knots 를 어디서 고르는가

지시서 공통원칙: "test fold 를 보고 knot / degree / smoothing 선택 금지".
threshold 를 OOF 에서 고르고 같은 OOF 로 재면 반드시 좋아 보인다(M68b 의 λ,
M73 의 threshold). 그래서 M73 과 같은 두 층으로 나눈다.

    승격 후보   spec 을 **사전에 하나로 고정**한다 — degree=3, n_knots=4.
                지시서 후보 격자(degree 2·3 × knots 3·4·5)의 한가운데 값이고
                데이터를 보기 전에 정했다. 승격 판정은 이 값으로만 한다.
    sweep       나머지 격자를 전체 OOF 에 적용한 진단용 표. '곡선 표현이
                설정에 얼마나 민감한가'를 보는 곡선이지 후보가 아니다.
                여기서 최저값을 골라 승격 근거로 쓰면 안 된다.

## spline 을 어떻게 만드는가

    적합 범위   outer train 의 **결측 아닌 값에만** 적합한다. test 는 transform 만.
    결측 처리   결측 행의 spline 컬럼은 NaN 으로 둔다. 대치하지 않는다 —
                XGB 가 결측을 자체 분기로 처리하고, 중앙값 대치는 '결측'을
                '중앙값 근처'로 위장시켜 곡선을 왜곡한다.
    knots       quantile (데이터 분위수). 치우친 분포에서 균등 knot 은
                꼬리에 knot 을 낭비한다.
    바깥값      extrapolation="linear" — test 에 train 범위 밖 값이 오면
                상수로 꺾지 않고 직선으로 잇는다.
    선변환      support_count 만 log1p. 중앙값 10 / 최대 80,000 (8,000배)라
                원 스케일 quantile knot 도 마지막 구간이 19~80,000 을 한 칸에
                담는다. 나머지 셋은 원 스케일.

## GAM 을 무엇으로 구현했는가

pygam 이 이 환경에 없다(설치 시 scipy 1.17 과 충돌 위험). 대신 같은 수식을
직접 세운다 — feature 마다 B-spline basis 를 만들고 릿지 벌점을 준
**가법 spline 회귀**다.

    y = f1(x1) + f2(x2) + f3(x3) + ...  각 fj = B-spline basis · 계수

    smoothing   RidgeCV — outer train 안에서만 alpha 를 고른다
    edf         trace(H) = Σ s²/(s²+α)  (SVD 로 계산)
    신뢰띠      fold 5개 곡선의 ±1.96σ. 부트스트랩이 아니라 fold 간 변동이라
                '이 곡선이 fold 를 바꿔도 같은 모양인가'를 직접 답한다

산출
    ml/data/processed/m77_spline_oof.parquet
    ml/reports/m77_m2_spline_nonlinear.json / .md
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
OUT_OOF = os.path.join(C.PROC, "m77_spline_oof.parquet")
MD = C.report_path("m77_m2_spline_nonlinear.md")

BUCKETS = M73.BUCKETS
STEP = "G"

# M73 공표치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M73_PUBLISHED = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                 "within_2x": 0.564, "within_3x": 0.742}
BASE = "S0/M73 baseline"

# ------------------------------------------------------------ spline 대상
# 지시서 '대상 numeric feature' 우선순위 순. 괄호 안은 이 데이터셋의 컬럼명.
#   selected_count   -> support_count
#   support_rate     -> support_ratio
#   project_duration -> project_duration
#   self_burden_rate -> self_burden_ratio  (진단만, spline 후보에는 넣지 않는다)
NUM_LABEL = {"support_count": "selected_count(선정기업 수)",
             "support_ratio": "support_rate(지원비율)",
             "project_duration": "project_duration(사업기간)",
             "self_burden_ratio": "self_burden_rate(자부담비율)"}
DIAG_FEATURES = ["support_count", "support_ratio", "project_duration",
                 "self_burden_ratio"]
SPLINE_FEATURES = ["support_count", "support_ratio", "project_duration"]
LOG1P_FEATURES = {"support_count"}      # 위 docstring 의 이유

# 승격 후보의 spline spec — 데이터를 보기 전에 고정한다.
SPEC = {"degree": 3, "n_knots": 4}
# 진단용 격자. SPEC 은 이 안에 이미 들어 있다.
SWEEP = [{"degree": 2, "n_knots": 3}, {"degree": 2, "n_knots": 5},
         {"degree": 3, "n_knots": 3}, {"degree": 3, "n_knots": 5}]

# 후보 정의: 이름 -> (spline 대상 feature 목록, spec)
#   지시서 '한 feature 씩 먼저 검증하고, 각각 유효할 때만 결합한다'
CANDIDATES = [
    (BASE, [], SPEC),
    ("S1/count", ["support_count"], SPEC),
    ("S2/rate", ["support_ratio"], SPEC),
    ("S3/duration", ["project_duration"], SPEC),
    ("S4/all", SPLINE_FEATURES, SPEC),
]
SWEEP_CANDIDATES = [("SW/all/d%dk%d" % (s["degree"], s["n_knots"]),
                     SPLINE_FEATURES, s) for s in SWEEP]

# GAM / polynomial 설정
GAM_SPEC = {"degree": 3, "n_knots": 8}   # 곡선을 '보기' 위한 것이라 후보보다 촘촘
GAM_ALPHAS = tuple(np.logspace(-3, 4, 22))
POLY_DEGREES = (2, 3)
CURVE_GRID = 40                          # 곡선 출력 격자점 수

CODE_VERSION = "m77-v1"
CKPT_DIR = os.path.join(C.PROC, "m77_ckpt")


# ============================================================ spline 만들기
def raw_numeric(Xs, col):
    return Xs[col].to_numpy(dtype=float)


def pre_transform(col, v):
    """spline 을 태우기 전의 선변환. 단조변환이라 y 를 보지 않는다."""
    return np.log1p(v) if col in LOG1P_FEATURES else v


def fit_spline(v_tr, degree, n_knots):
    """outer train 의 결측 아닌 값에만 적합. test 는 transform 만 받는다."""
    from sklearn.preprocessing import SplineTransformer

    m = np.isfinite(v_tr)
    if m.sum() < (n_knots + degree + 5) or np.unique(v_tr[m]).size <= degree + 1:
        return None                       # 이 fold 에서는 basis 를 만들지 않는다
    st = SplineTransformer(n_knots=n_knots, degree=degree, knots="quantile",
                           extrapolation="linear", include_bias=False)
    try:
        st.fit(v_tr[m].reshape(-1, 1))
    except Exception:
        return None                       # quantile knot 중복 등 — 이 fold 는 건너뛴다
    return st


def apply_spline(st, v):
    """결측 행은 NaN 으로 남긴다 — 대치하지 않는 것이 이 실험의 규율."""
    out = np.full((len(v), st.n_features_out_), np.nan)
    m = np.isfinite(v)
    if m.any():
        out[m] = st.transform(v[m].reshape(-1, 1))
    return out


def spline_columns(Xs, tr, te, feats, spec):
    """(train 프레임, test 프레임, 적합기록). feats 가 비면 빈 프레임."""
    A, B, info = [], [], {}
    for c in feats:
        v = pre_transform(c, raw_numeric(Xs, c))
        st = fit_spline(v[tr], spec["degree"], spec["n_knots"])
        if st is None:
            info[c] = {"fitted": False}
            continue
        names = ["sp_%s_%02d" % (c, i) for i in range(st.n_features_out_)]
        A.append(pd.DataFrame(apply_spline(st, v[tr]), columns=names))
        B.append(pd.DataFrame(apply_spline(st, v[te]), columns=names))
        knots = np.unique(st.bsplines_[0].t)
        info[c] = {"fitted": True, "n_cols": int(st.n_features_out_),
                   "log1p": c in LOG1P_FEATURES,
                   "knots": [round(float(x), 4) for x in knots],
                   "train_coverage": round(float(np.isfinite(v[tr]).mean()), 4)}
    if not A:
        n_tr, n_te = len(tr), len(te)
        return pd.DataFrame(index=range(n_tr)), pd.DataFrame(index=range(n_te)), info
    return (pd.concat(A, axis=1).reset_index(drop=True),
            pd.concat(B, axis=1).reset_index(drop=True), info)


def augment(Xtr, Xte, sa, sb):
    if sa.shape[1] == 0:
        return Xtr, Xte
    return (pd.concat([Xtr.reset_index(drop=True), sa], axis=1),
            pd.concat([Xte.reset_index(drop=True), sb], axis=1))


# ============================================================ Exp0 진단
def bin_table(v, y, q=10):
    """분위 구간별 n / median / mean / IQR. 지시서 Experiment 0 의 출력."""
    m = np.isfinite(v)
    vv, yy = v[m], y[m]
    edges = np.unique(np.percentile(vv, np.linspace(0, 100, q + 1)))
    if len(edges) < 3:
        return []
    idx = np.clip(np.digitize(vv, edges[1:-1], right=False), 0, len(edges) - 2)
    rows = []
    for k in range(len(edges) - 1):
        s = idx == k
        if s.sum() == 0:
            continue
        t = yy[s]
        rows.append({"bin": "[%.4g, %.4g%s" % (edges[k], edges[k + 1],
                                               "]" if k == len(edges) - 2 else ")"),
                     "n": int(s.sum()),
                     "x_median": round(float(np.median(vv[s])), 4),
                     "y_median": round(float(np.median(t)), 4),
                     "y_mean": round(float(t.mean()), 4),
                     "y_iqr": round(float(np.percentile(t, 75) -
                                          np.percentile(t, 25)), 4),
                     "median_won": int(round(10 ** float(np.median(t))))})
    # 구간 사이 기울기 — '기울기가 구간마다 달라지는가'가 이 실험의 질문이다
    for i in range(1, len(rows)):
        dx = rows[i]["x_median"] - rows[i - 1]["x_median"]
        dy = rows[i]["y_median"] - rows[i - 1]["y_median"]
        rows[i]["slope_vs_prev"] = round(float(dy / dx), 6) if dx else None
    return rows


def univariate_cv(v, y, groups, spec=SPEC):
    """feature 하나로만 예측했을 때 상수 / 직선 / spline / isotonic 의 CV MAE.

    '곡선이 실제로 무엇을 벌어주는가'를 재는 유일하게 정직한 칸이다. 전체 데이터에
    선을 그려놓고 눈으로 휘었다고 말하는 것과 다르다 — 곡선의 이득은 fold 밖에서
    남아야 이득이다. 결측 행은 이 진단에서 제외한다(모델이 아니라 관계의 진단이다).
    """
    from sklearn.linear_model import LinearRegression, RidgeCV
    from sklearn.isotonic import IsotonicRegression
    from sklearn.model_selection import GroupKFold

    m = np.isfinite(v)
    vv, yy, gg = v[m], y[m], groups[m]
    n = len(vv)
    if n < 100 or len(np.unique(gg)) < F.N_SPLITS:
        return None
    out = {k: np.zeros(n) for k in ("const", "linear", "spline", "isotonic")}
    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(vv, yy, gg):
        a, b, ya = vv[tr].reshape(-1, 1), vv[te].reshape(-1, 1), yy[tr]
        out["const"][te] = np.median(ya)
        out["linear"][te] = LinearRegression().fit(a, ya).predict(b)
        st = fit_spline(vv[tr], spec["degree"], spec["n_knots"])
        if st is None:
            out["spline"][te] = np.median(ya)
        else:
            out["spline"][te] = RidgeCV(alphas=GAM_ALPHAS).fit(
                st.transform(a), ya).predict(st.transform(b))
        iso = IsotonicRegression(out_of_bounds="clip").fit(vv[tr], ya)
        out["isotonic"][te] = iso.predict(vv[te])
    mae = {k: round(float(np.abs(p - yy).mean()), 4) for k, p in out.items()}
    return {"n": int(n), "MAE": mae,
            "curve_gain_vs_linear": round(mae["linear"] - mae["spline"], 4),
            "linear_gain_vs_const": round(mae["const"] - mae["linear"], 4),
            "isotonic_gain_vs_linear": round(mae["linear"] - mae["isotonic"], 4)}


def numeric_diagnostic(Xs, y, groups):
    from scipy import stats

    out = {}
    for c in DIAG_FEATURES:
        v0 = raw_numeric(Xs, c)
        v = pre_transform(c, v0)
        m = np.isfinite(v)
        pe = stats.pearsonr(v[m], y[m])
        sp = stats.spearmanr(v[m], y[m])
        out[c] = {
            "label": NUM_LABEL[c],
            "pre_transform": "log1p" if c in LOG1P_FEATURES else "none",
            "coverage": round(float(m.mean()), 4),
            "n_unique": int(np.unique(v0[m]).size),
            "pearson_r": round(float(pe[0]), 4),
            "spearman_rho": round(float(sp[0]), 4),
            # 단조성은 있는데 선형성이 약하면 |rho| > |r| 로 나타난다
            "monotone_minus_linear": round(float(abs(sp[0]) - abs(pe[0])), 4),
            "bins": bin_table(v0, y, q=10),
            "univariate_cv": univariate_cv(v, y, groups),
        }
    return out


# ============================================================ Exp2/3 GAM·다항
def _onehot(Xtr, Xte):
    from sklearn.preprocessing import OneHotEncoder

    cat = [c for c in Xtr.columns if str(Xtr[c].dtype) == "category"]
    if not cat:
        return None, None
    enc = OneHotEncoder(handle_unknown="ignore", sparse_output=False, min_frequency=5)
    return enc.fit_transform(Xtr[cat].astype(str)), enc.transform(Xte[cat].astype(str))


def _basis(kind, col, v_tr, v_te, spec):
    """가법 항 하나의 basis. 결측은 0 + 결측표시 컬럼으로 분리한다.

    XGB 와 달리 선형모델은 NaN 을 못 먹는다. 중앙값으로 채우면 '결측'이
    '중앙값'인 척하므로, 0 으로 채우고 '결측이다'를 별도 컬럼으로 준다.
    """
    m_tr, m_te = np.isfinite(v_tr), np.isfinite(v_te)
    if kind == "spline":
        st = fit_spline(v_tr, spec["degree"], spec["n_knots"])
        if st is None:
            return None
        A = np.zeros((len(v_tr), st.n_features_out_))
        B = np.zeros((len(v_te), st.n_features_out_))
        A[m_tr] = st.transform(v_tr[m_tr].reshape(-1, 1))
        B[m_te] = st.transform(v_te[m_te].reshape(-1, 1))
        maker = lambda g: st.transform(np.asarray(g).reshape(-1, 1))    # noqa: E731
    else:                                    # polynomial degree d
        d = spec["degree"]
        mu = float(np.mean(v_tr[m_tr])) if m_tr.any() else 0.0
        sd = float(np.std(v_tr[m_tr])) or 1.0
        f = lambda g: np.column_stack([((np.asarray(g, dtype=float) - mu) / sd) ** k
                                       for k in range(1, d + 1)])       # noqa: E731
        A, B = np.zeros((len(v_tr), d)), np.zeros((len(v_te), d))
        A[m_tr] = f(v_tr[m_tr])
        B[m_te] = f(v_te[m_te])
        maker = f
    A = np.hstack([A, (~m_tr).astype(float)[:, None]])
    B = np.hstack([B, (~m_te).astype(float)[:, None]])
    return {"A": A, "B": B, "maker": maker, "width": A.shape[1] - 1}


def additive_fit(kind, Xs, Xtr_cat, Xte_cat, tr, te, ytr, spec,
                 with_cat=False, extra_tr=None, extra_te=None, grids=None):
    """가법 spline/다항 회귀 한 fold. 반환에 곡선·edf 를 함께 싣는다."""
    from sklearn.linear_model import RidgeCV

    blocks, cols, curves = [], [], {}
    for c in DIAG_FEATURES:
        v = pre_transform(c, raw_numeric(Xs, c))
        bl = _basis(kind, c, v[tr], v[te], spec)
        if bl is None:
            continue
        blocks.append(bl)
        cols.append(c)
    A = np.hstack([b["A"] for b in blocks])
    B = np.hstack([b["B"] for b in blocks])
    n_num = A.shape[1]
    if with_cat:
        ca, cb = _onehot(Xtr_cat, Xte_cat)
        if ca is not None:
            A, B = np.hstack([A, ca]), np.hstack([B, cb])
    if extra_tr is not None:
        A = np.hstack([A, np.asarray(extra_tr).reshape(-1, 1)])
        B = np.hstack([B, np.asarray(extra_te).reshape(-1, 1)])
    mu = A.mean(0)
    A, B = A - mu, B - mu
    r = RidgeCV(alphas=GAM_ALPHAS).fit(A, ytr)
    pred = r.predict(B)

    # edf = trace(H) = Σ s²/(s²+α)  — 곡선이 실제로 몇 자유도를 쓰는가
    s = np.linalg.svd(A, compute_uv=False)
    edf = float((s ** 2 / (s ** 2 + r.alpha_)).sum())

    # feature 별 부분효과 곡선 f_j(x). 다른 항은 고정하므로 basis·계수만 쓴다.
    if grids is not None:
        off = 0
        for c, bl in zip(cols, blocks):
            w = bl["width"]
            beta = r.coef_[off:off + w]
            G = bl["maker"](grids[c])
            f = G @ beta
            curves[c] = (f - f.mean()).tolist()
            off += bl["width"] + 1
    return {"pred": pred, "alpha": float(r.alpha_), "edf": round(edf, 2),
            "n_terms": int(A.shape[1]), "n_numeric_terms": int(n_num),
            "curves": curves}


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


def expert_spline_block(Xs, Xtr, Xte, tr, te, ytr, assign, spec=SPEC):
    """Exp4 — expert 마다 다른 feature 에 spline. 조건부 실험이라 따로 뗐다.

    assign: {0: [feature...], 1: [...], 2: [...]}  구간 -> spline 대상
    Stage 1 확률과 global 은 S0 것을 그대로 쓴다 — 바뀌는 축은 expert 하나다.
    """
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    tab = np.zeros((len(Xte), 3))
    for k in range(3):
        sa, sb, _ = spline_columns(Xs, tr, te, assign.get(k, []), spec)
        a, b = augment(Xtr, Xte, sa, sb)
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(a.iloc[m], ytr[m]).predict(b)
    return tab


def fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i, cand, grids,
                 exp4_assign=None):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                STEP, [None])
    ytr, yte = y[tr], y[te]
    edges = M73.bucket_edges(ytr)
    zte = M73.to_bucket(yte, edges)
    base_te = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

    pred, glob, proba, sp_info, ncols = {}, {}, {}, {}, {}
    table0 = None
    for name, feats, spec in cand:
        sa, sb, info = spline_columns(Xs, tr, te, feats, spec)
        a, b = augment(Xb_tr, Xb_te, sa, sb)
        blk = m73_block(a, ytr, b)
        pred[name] = blk["soft"]
        glob[name] = blk["global"]
        proba[name] = blk["proba"]
        sp_info[name] = info
        ncols[name] = int(a.shape[1])
        if name == BASE:
            table0 = blk["table"]

    assert table0 is not None, "BASE 후보가 목록에 없다 — table0 을 만들 수 없다"

    # --- Exp2 GAM / Exp3 polynomial — 같은 fold, 같은 train ----------------
    add = {}
    add["G1"] = additive_fit("spline", Xs, Xb_tr, Xb_te, tr, te, ytr, GAM_SPEC,
                             with_cat=False, grids=grids)
    add["G2"] = additive_fit("spline", Xs, Xtr0, Xte0, tr, te, ytr, GAM_SPEC,
                             with_cat=True, grids=grids)
    # G3 = G2 + M73 global 예측을 항으로. train 쪽 global 은 in-sample 이라
    # 그대로 쓰면 곡선이 그 완벽한 항에 다 흡수된다. train 예측은 inner
    # GroupKFold(3) OOF 로 만들어 test 와 같은 성격의 값으로 맞춘다.
    from sklearn.model_selection import GroupKFold
    gtr = groups[tr]
    in_glob = np.zeros(len(tr))
    ns = min(3, len(np.unique(gtr)))
    for a_, b_ in GroupKFold(n_splits=ns).split(Xb_tr, ytr, gtr):
        in_glob[b_] = F.make_point_model().fit(Xb_tr.iloc[a_],
                                               ytr[a_]).predict(Xb_tr.iloc[b_])
    add["G3"] = additive_fit("spline", Xs, Xtr0, Xte0, tr, te, ytr, GAM_SPEC,
                             with_cat=True, extra_tr=in_glob,
                             extra_te=glob[BASE], grids=None)
    for dg in POLY_DEGREES:
        add["P%d" % dg] = additive_fit("poly", Xs, Xtr0, Xte0, tr, te, ytr,
                                       {"degree": dg}, with_cat=True, grids=None)

    exp4 = None
    if exp4_assign is not None:
        tab4 = expert_spline_block(Xs, Xb_tr, Xb_te, tr, te, ytr, exp4_assign)
        exp4 = M73.route_soft(tab4, proba[BASE])

    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
           "edges_won": [int(round(10 ** e)) for e in edges],
           "baseline_MAE": round(float(np.abs(base_te - yte).mean()), 4),
           "n_features": ncols,
           "spline_fit": sp_info,
           "gam": {k: {"alpha": v["alpha"], "edf": v["edf"],
                       "n_terms": v["n_terms"]} for k, v in add.items()},
           "MAE": {k: round(float(np.abs(p - yte).mean()), 4)
                   for k, p in pred.items()},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "base": base_te, "z_true": zte,
            "pred": pred, "glob": glob, "proba": proba, "table0": table0,
            "add_pred": {k: v["pred"] for k, v in add.items()},
            "curves": {k: v["curves"] for k, v in add.items() if v["curves"]},
            "exp4": exp4, "rec": rec}


# ============================================================ 체크포인트
def ckpt_signature(fp, cand, exp4):
    import hashlib
    import json as _json
    blob = _json.dumps({
        "code": CODE_VERSION, "dataset_sha256": fp["sha256"],
        "xgb_point": F.XGB_POINT, "spec": SPEC, "sweep": SWEEP,
        "cand": [[n, f, s] for n, f, s in cand], "exp4": exp4,
        "spline_features": SPLINE_FEATURES, "log1p": sorted(LOG1P_FEATURES),
        "diag": DIAG_FEATURES, "gam": GAM_SPEC, "poly": list(POLY_DEGREES),
        "step": STEP, "cuts": list(M73.CUTS), "curve_grid": CURVE_GRID,
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
        z = np.load(npz, allow_pickle=False)
        meta = _json.load(io.open(js, encoding="utf-8"))
    except Exception:
        return None
    o = {"te": z["te"], "base": z["base"], "z_true": z["z_true"],
         "table0": z["table0"], "rec": meta["rec"], "curves": meta["curves"],
         "pred": {k[5:]: z[k] for k in z.files if k.startswith("pred_")},
         "glob": {k[5:]: z[k] for k in z.files if k.startswith("glob_")},
         "proba": {k[6:]: z[k] for k in z.files if k.startswith("proba_")},
         "add_pred": {k[4:]: z[k] for k in z.files if k.startswith("add_")}}
    o["exp4"] = z["exp4"] if "exp4" in z.files else None
    return o


def ckpt_save(sig, tag, i, fo):
    import json as _json
    d, npz, js = ckpt_paths(sig, tag, i)
    os.makedirs(d, exist_ok=True)
    arr = {"te": fo["te"], "base": fo["base"], "z_true": fo["z_true"],
           "table0": fo["table0"]}
    arr.update({"pred_" + k: v for k, v in fo["pred"].items()})
    arr.update({"glob_" + k: v for k, v in fo["glob"].items()})
    arr.update({"proba_" + k: v for k, v in fo["proba"].items()})
    arr.update({"add_" + k: v for k, v in fo["add_pred"].items()})
    if fo["exp4"] is not None:
        arr["exp4"] = fo["exp4"]
    np.savez_compressed(npz + ".tmp.npz", **arr)
    with io.open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write(_json.dumps({"rec": fo["rec"], "curves": fo["curves"]},
                            ensure_ascii=False, default=str))
    os.replace(npz + ".tmp.npz", npz)
    os.replace(js + ".tmp", js)


# ============================================================ split 실행
def run_split(Xs, y, groups, titles, body, NB, cats, grids, sig, tag, cand,
              exp4_assign=None, verbose=True, use_ckpt=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    names = [c[0] for c in cand]
    add_names = ["G1", "G2", "G3"] + ["P%d" % d for d in POLY_DEGREES]
    R = {"z_true": np.zeros(n, dtype=int), "base": np.zeros(n),
         "fold_id": np.zeros(n, dtype=int), "table0": np.zeros((n, 3)),
         "pred": {s: np.zeros(n) for s in names},
         "glob": {s: np.zeros(n) for s in names},
         "proba": {s: np.zeros((n, 3)) for s in names},
         "add": {s: np.zeros(n) for s in add_names},
         "exp4": (np.zeros(n) if exp4_assign is not None else None),
         "curves": [], "folds": []}
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = ckpt_load(sig, tag, i) if use_ckpt else None
        cached = fo is not None
        if not cached:
            fo = fold_compute(Xs, y, groups, titles, body, NB, cats, tr, te, i,
                              cand, grids, exp4_assign)
            if use_ckpt:
                ckpt_save(sig, tag, i, fo)
        te = fo["te"]
        R["fold_id"][te] = i
        R["base"][te] = fo["base"]
        R["z_true"][te] = fo["z_true"]
        R["table0"][te] = fo["table0"]
        for s in names:
            R["pred"][s][te] = fo["pred"][s]
            R["glob"][s][te] = fo["glob"][s]
            R["proba"][s][te] = fo["proba"][s]
        for s in add_names:
            R["add"][s][te] = fo["add_pred"][s]
        if exp4_assign is not None and fo["exp4"] is not None:
            R["exp4"][te] = fo["exp4"]
        R["curves"].append(fo["curves"])
        rec = dict(fo["rec"])
        rec["from_checkpoint"] = bool(cached)
        R["folds"].append(rec)
        if verbose:
            print("   fold %d  cut %s  %s  (%s)"
                  % (i, rec["edges_won"],
                     "  ".join("%s %.4f" % (k, v) for k, v in rec["MAE"].items()),
                     "체크포인트 재사용" if cached else "%.0fs" % rec["seconds"]))
    return R


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


def block(d, y, R, p, ref=None):
    b = float(np.abs(R["base"] - y).mean())
    fid = R["fold_id"]
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


def summarize(d, y, R):
    ref = R["pred"][BASE]
    out = {"baseline_MAE": round(float(np.abs(R["base"] - y).mean()), 4),
           "variants": {s: block(d, y, R, p, None if s == BASE else ref)
                        for s, p in R["pred"].items()},
           "global_of_base": block(d, y, R, R["glob"][BASE]),
           "additive": {s: block(d, y, R, p, ref) for s, p in R["add"].items()},
           "stage1_acc": {s: round(float((pr.argmax(1) == R["z_true"]).mean()), 4)
                          for s, pr in R["proba"].items()},
           "folds": R["folds"]}
    rows = np.arange(len(y))
    o = M45.point_metrics(y, R["table0"][rows, R["z_true"]])
    o["note"] = "실제 구간을 안다고 가정한 상한 — 서빙 불가, 진단 전용"
    out["oracle_ceiling"] = o
    if R["exp4"] is not None:
        out["variants"]["S5/expert-wise"] = block(d, y, R, R["exp4"], ref)
    return out


def curve_summary(R, grids):
    """fold 5개의 곡선을 겹쳐 평균 · 표준편차(신뢰띠) 로 요약한다."""
    out = {}
    for model in ("G1", "G2"):
        per_feat = {}
        for c in DIAG_FEATURES:
            arr = [np.asarray(f[model][c]) for f in R["curves"]
                   if model in f and c in f[model]]
            if len(arr) < 2:
                continue
            A = np.vstack(arr)
            mu, sd = A.mean(0), A.std(0)
            gx = np.asarray(grids[c])
            ox = np.expm1(gx) if c in LOG1P_FEATURES else gx
            per_feat[c] = {
                "x_scale": "log1p" if c in LOG1P_FEATURES else "raw",
                "grid": [round(float(x), 4) for x in gx],
                "grid_original": [round(float(x), 4) for x in ox],
                "mean": [round(float(x), 4) for x in mu],
                "band_lo": [round(float(x), 4) for x in mu - 1.96 * sd],
                "band_hi": [round(float(x), 4) for x in mu + 1.96 * sd],
                "fold_sd_mean": round(float(sd.mean()), 4),
                "range_log10": round(float(mu.max() - mu.min()), 4),
                "range_x": round(float(10 ** (mu.max() - mu.min())), 2),
                # 곡선이 fold 를 바꿔도 같은 모양인가 — 폭 대비 흔들림
                "stability": round(float((mu.max() - mu.min()) /
                                         (sd.mean() + 1e-9)), 2),
                "monotone": bool(np.all(np.diff(mu) >= -1e-9) or
                                 np.all(np.diff(mu) <= 1e-9)),
                "n_sign_changes": int((np.diff(np.sign(np.diff(mu))) != 0).sum()),
            }
        out[model] = per_feat
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

    # 곡선 출력 격자 — fold 마다 같은 x 에서 재야 곡선을 겹쳐 볼 수 있다
    grids = {}
    for c in DIAG_FEATURES:
        v = pre_transform(c, raw_numeric(Xs, c))
        m = np.isfinite(v)
        grids[c] = np.linspace(np.percentile(v[m], 1), np.percentile(v[m], 99),
                               CURVE_GRID)

    # ---------------------------------------------------- Experiment 0
    print("\n== Experiment 0 — numeric 관계 진단 (곡선이 있기는 한가)")
    diag = numeric_diagnostic(Xs, y, groups["program_stem"])
    for c, v in diag.items():
        u = v["univariate_cv"]
        print("   %-18s 커버 %.1f%%  고유값 %3d  r=%+.3f  rho=%+.3f"
              % (c, 100 * v["coverage"], v["n_unique"], v["pearson_r"],
                 v["spearman_rho"]))
        if u:
            print("      단변량 CV MAE  상수 %.4f  직선 %.4f  spline %.4f  "
                  "isotonic %.4f   (곡선이득 %+.4f)"
                  % (u["MAE"]["const"], u["MAE"]["linear"], u["MAE"]["spline"],
                     u["MAE"]["isotonic"], u["curve_gain_vs_linear"]))
    curve_gains = {c: (v["univariate_cv"] or {}).get("curve_gain_vs_linear", 0.0)
                   for c, v in diag.items()}
    diag_verdict = ("곡선 관계 있음" if max(curve_gains.values()) >= 0.005
                    else "곡선 관계 약함")
    print("   진단: %s (최대 곡선이득 %+.4f)"
          % (diag_verdict, max(curve_gains.values())))

    # ---------------------------------------------------- Experiment 1·2·3
    cand = CANDIDATES + SWEEP_CANDIDATES
    sig = ckpt_signature(fp, cand, None)
    print("\n== 체크포인트 서명 %s  ->  %s" % (sig, os.path.relpath(CKPT_DIR, C.ROOT)))

    results, raws = {}, {}
    for gname in ("program_stem", "normalized_title"):
        # 엄격 split 에서는 sweep 을 돌리지 않는다. sweep 은 진단용 곡선이라
        # 두 split 에서 다 잴 이유가 없고, 승격 후보는 5개뿐이다.
        c = cand if gname == "program_stem" else CANDIDATES
        s = sig if gname == "program_stem" else ckpt_signature(fp, c, None)
        print("\n== 5-fold [%s] — 후보 %d개 × M73 블록" % (gname, len(c)))
        R = run_split(Xs, y, groups[gname], titles, body, NB, cats, grids, s,
                      gname, c)
        results[gname] = summarize(d, y, R)
        raws[gname] = R

    ps, nt = results["program_stem"], results["normalized_title"]
    Rp = raws["program_stem"]
    base_mae = ps["variants"][BASE]["MAE_log10"]

    print("\n== Experiment 1 — spline feature (baseline %.4f)" % base_mae)
    print("   %-22s %8s %9s %8s %7s %s" % ("후보", "MAE", "Δ", "strict",
                                           "fold승", "95%CI"))
    for name, _f, _s in CANDIDATES:
        m = ps["variants"][name]
        v = m.get("vs_base")
        sm = nt["variants"].get(name, {}).get("MAE_log10")
        print("   %-22s %8.4f %+9.4f %8s %6s  %s"
              % (name, m["MAE_log10"], v["delta_MAE"] if v else 0.0,
                 ("%.4f" % sm) if sm else "—",
                 ("%d/5" % m["fold_wins_vs_base"]) if v else "—",
                 ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—"))
    print("   ---- degree/knots sweep (진단용, 승격 근거 아님)")
    for name, _f, _s in SWEEP_CANDIDATES:
        m = ps["variants"][name]
        print("   %-22s %8.4f %+9.4f  %d/5"
              % (name, m["MAE_log10"], m["vs_base"]["delta_MAE"],
                 m["fold_wins_vs_base"]))

    curves = curve_summary(Rp, grids)
    print("\n== Experiment 2 — GAM (가법 spline 회귀)")
    for k in ("G1", "G2", "G3"):
        m = ps["additive"][k]
        e = [f["gam"][k]["edf"] for f in ps["folds"]]
        print("   %-4s MAE %.4f  strict %.4f  edf %s"
              % (k, m["MAE_log10"], nt["additive"][k]["MAE_log10"],
                 " ".join("%.0f" % x for x in e)))
    print("   ---- 곡선 안정성 (G2, fold 5개 겹침)")
    for c, v in curves.get("G2", {}).items():
        print("      %-18s 진폭 %.3f log10 (%.1f배)  fold σ %.3f  "
              "안정성 %.1f  단조 %s  변곡 %d"
              % (c, v["range_log10"], v["range_x"], v["fold_sd_mean"],
                 v["stability"], v["monotone"], v["n_sign_changes"]))

    print("\n== Experiment 3 — polynomial baseline")
    for dg in POLY_DEGREES:
        m = ps["additive"]["P%d" % dg]
        print("   P%d  MAE %.4f  strict %.4f"
              % (dg, m["MAE_log10"], nt["additive"]["P%d" % dg]["MAE_log10"]))

    # ---------------------------------------------------- Experiment 4 (조건부)
    exp1_best = min([c[0] for c in CANDIDATES if c[0] != BASE],
                    key=lambda k: ps["variants"][k]["MAE_log10"])
    exp1_ok = (ps["variants"][exp1_best]["MAE_log10"] < base_mae
               and ps["variants"][exp1_best]["vs_base"]["ci95"][1] < 0)
    exp4_note = None
    if exp1_ok:
        print("\n== Experiment 4 — expert 별 spline (Exp1 통과로 실행)")
        assign = {0: ["support_count"], 1: ["support_ratio"], 2: ["project_duration"]}
        s4 = ckpt_signature(fp, [CANDIDATES[0]], assign)
        R4 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats,
                       grids, s4, "program_stem__exp4", [CANDIDATES[0]],
                       exp4_assign=assign)
        r4 = summarize(d, y, R4)
        results["program_stem"]["variants"]["S5/expert-wise"] = \
            r4["variants"]["S5/expert-wise"]
        ps = results["program_stem"]
        m = ps["variants"]["S5/expert-wise"]
        print("   S5/expert-wise MAE %.4f  Δ %+0.4f"
              % (m["MAE_log10"], m["vs_base"]["delta_MAE"]))
        exp4_note = "실행됨 (Exp1 통과)"
    else:
        exp4_note = ("미실행 — 지시서 '조건부 실험'. Exp1 최고 후보 %s 가 "
                     "baseline 을 유의하게 이기지 못했다" % exp1_best)
        print("\n== Experiment 4 — %s" % exp4_note)

    # ---------------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 program_stem 을 한 번 더 (독립 실행)")
    repro_cand = [c for c in CANDIDATES if c[0] in (BASE, exp1_best)]
    R2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats, grids,
                   sig, "program_stem__repro", repro_cand, verbose=False)
    repro = {k: bool(np.allclose(R2["pred"][k], Rp["pred"][k]))
             for k in (BASE, exp1_best)}
    repro["GAM G2"] = bool(np.allclose(R2["add"]["G2"], Rp["add"]["G2"]))
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ---------------------------------------------------- 누수 점검
    leak = {
        "spline 적합 입력": "outer train 의 결측 아닌 값만. y 를 보지 않는다",
        "knot 위치": "outer train 분위수 (fold 마다 다시 계산)",
        "degree/knots 선택": "데이터 보기 전 고정 (degree=3, n_knots=4). "
                             "sweep 표는 진단용으로 격리",
        "결측 처리": "spline 컬럼은 NaN 유지 — 대치 없음(대치하면 결측이 "
                     "중앙값으로 위장한다)",
        "선변환(log1p)": "단조변환, y 미사용",
        "GAM smoothing alpha": "outer train 안 RidgeCV",
        "G3 의 global 항": "train 쪽은 inner GroupKFold(3) OOF — in-sample "
                           "예측을 항으로 넣지 않는다",
        "구간 경계": "fold train 의 y 만 (M73 과 동일)",
        "test y 의 용도": "최종 metric · oracle 상한 · 구간별 집계뿐",
    }
    leak_checks = {
        "spline 이 outer test 를 보지 않았다": True,
        "baseline 이 M73 공표치(0.3563)를 재현": abs(base_mae - 0.3563) < 0.005,
        "재현성 PASS": all(repro.values()),
    }
    leak_pass = all(leak_checks.values())
    print("\n== 누수 점검")
    for k, v in leak.items():
        print("   %-24s %s" % (k, v))
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))

    # ---------------------------------------------------- 승격 판정
    honest = [c[0] for c in CANDIDATES if c[0] != BASE]
    if "S5/expert-wise" in ps["variants"]:
        honest.append("S5/expert-wise")
    best = min(honest, key=lambda k: ps["variants"][k]["MAE_log10"])
    B = ps["variants"][best]
    v = B["vs_base"]
    nt_best = nt["variants"].get(best)
    nt_base = nt["variants"][BASE]["MAE_log10"]
    per_feat = [ps["variants"][k]["MAE_log10"] for k in ("S1/count", "S2/rate",
                                                        "S3/duration")]
    checks = {
        "1. OOF MAE < 0.3563": B["MAE_log10"] < M73_PUBLISHED["MAE_log10"],
        "1b. 같은 fold baseline 보다 낮다": B["MAE_log10"] < base_mae,
        "2. strict split 에서도 개선":
            bool(nt_best and nt_best["MAE_log10"] < nt_base),
        "3. 5개 fold 중 4개 이상 개선": B["fold_wins_vs_base"] >= 4,
        "4. paired 95% CI 가 0 아래": v["ci95"][1] < 0,
        "5. 한 feature 에만 의존하지 않는다":
            bool(sum(1 for x in per_feat if x < base_mae) >= 2),
        "6. leakage audit PASS": bool(leak_pass),
        "7. reproducibility PASS": all(repro.values()),
        "8. 개선폭 > 0.003 (지시서 중단기준)": (-v["delta_MAE"]) > 0.003,
        "9. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("9.")]
    verdict = ("승격 후보 (M73 대체)" if all(checks[k] for k in core)
               else "현행 유지 — M73 `soft/ordinal_xgb`")
    print("\n== 승격 점검표 — 대상: %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    # ---------------------------------------------------- 산출물
    out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
           "z_true": Rp["z_true"], "pred_baseline": Rp["base"],
           "cohort": d["cohort"].to_numpy(),
           "evidence_source": d["evidence_source"].to_numpy()}
    for s, p in Rp["pred"].items():
        out["pred_" + s.replace("/", "__")] = p
    for s, p in Rp["add"].items():
        out["pred_" + s] = p
    if Rp["exp4"] is not None:
        out["pred_S5__expert_wise"] = Rp["exp4"]
    pd.DataFrame(out).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "연속형 사업설계 변수(선정기업 수·지원비율·사업기간)와 지원규모 "
                   "사이의 비선형 곡선 관계를 명시적으로 모델링하면 M73(0.3563)에 "
                   "추가 성능이 있는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"],
            "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": "M69 G 단계 (%s + 원천층 %s + 본문 SVD%d)"
                        % (F.FEATURE_VERSION, SF.LAYER_VERSION, M69.BODY_SVD),
            "routing": "M73 soft / ordinal_xgb (구간 33.3/66.7)",
            "regressor": F.XGB_POINT, "bucket_cuts": list(M73.CUTS),
        },
        "changed": "numeric feature 의 표현 하나 — 기존 컬럼 위에 spline basis 추가",
        "spline_protocol": {
            "candidate_spec": SPEC,
            "spec_fixed_before_data": True,
            "sweep": [dict(s) for s in SWEEP],
            "sweep_role": "진단용 곡선. 여기서 최저값을 골라 승격 근거로 쓰지 않는다",
            "knots": "quantile (outer train 분위수)",
            "extrapolation": "linear",
            "missing": "NaN 유지 — XGB 자체 분기. 대치하지 않는다",
            "log1p_features": sorted(LOG1P_FEATURES),
            "targets": SPLINE_FEATURES,
        },
        "gam_implementation": {
            "library": "pygam 미설치 — sklearn SplineTransformer + RidgeCV 로 "
                       "동일 수식(가법 spline 회귀)을 직접 구성",
            "spec": GAM_SPEC, "alphas": "logspace(-3, 4, 22), outer train RidgeCV",
            "edf": "trace(H) = Σ s²/(s²+α)",
            "band": "fold 5개 곡선의 ±1.96σ",
            "G1": "numeric only", "G2": "numeric + categorical one-hot",
            "G3": "G2 + M73 global 예측 항 (train 쪽은 inner OOF)",
        },
        "diagnostic": diag,
        "diagnostic_verdict": diag_verdict,
        "curve_gains": {k: round(float(v), 4) for k, v in curve_gains.items()},
        "results": results,
        "curves": curves,
        "best_candidate": best,
        "exp1_best": exp1_best,
        "exp4": exp4_note,
        "reproducibility": repro,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(x) for k, x in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "goals": {"primary": "MAE < 0.35", "final": "MAE < 0.30"},
        "published_m73": M73_PUBLISHED,
        "checkpoint": {"signature": sig, "dir": os.path.relpath(CKPT_DIR, C.ROOT),
                       "code_version": CODE_VERSION},
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m77_m2_spline_nonlinear.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ============================================================ MD 보고서
def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    base = ps["variants"][BASE]
    L = []
    A = L.append
    A("# M77 — 곡선형 회귀 / 비선형 수치 feature (spline · GAM · polynomial)\n")
    A("> 질문: **선정기업 수 · 지원비율 · 사업기간과 지원규모 사이에 직선이 아닌")
    A("> 곡선 관계가 있는가, 있다면 그것을 명시해 M73 0.3563 을 이길 수 있는가?**\n")

    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("routing  %s" % u["routing"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    sp = p["spline_protocol"]
    A("spline 을 어디서 적합하고 degree/knots 를 어디서 골랐는지가 이 실험의 규율이다.\n")
    A("```text")
    A("승격 후보 spec  degree=%d, n_knots=%d  (데이터 보기 전 고정)"
      % (sp["candidate_spec"]["degree"], sp["candidate_spec"]["n_knots"]))
    A("sweep           %s" % sp["sweep_role"])
    A("knots           %s" % sp["knots"])
    A("결측            %s" % sp["missing"])
    A("선변환          log1p: %s" % ", ".join(sp["log1p_features"]))
    A("```\n")

    A("## 1. Experiment 0 — 곡선 관계가 있기는 한가\n")
    A("각 feature 를 10분위로 나눈 표는 아래 1-2 에 있다. 먼저 **곡선이 실제로")
    A("무엇을 벌어주는지**를 fold 밖에서 잰다 — feature 하나만으로 예측했을 때의")
    A("GroupKFold(5) CV MAE 다.\n")
    A("| feature | 커버리지 | 고유값 | Pearson r | Spearman ρ | 상수 | 직선 | "
      "spline | isotonic | 곡선이득 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c, v in p["diagnostic"].items():
        cv = v["univariate_cv"] or {}
        m = cv.get("MAE", {})
        A("| %s | %.1f%% | %d | %+.3f | %+.3f | %s | %s | %s | %s | %s |"
          % (v["label"], 100 * v["coverage"], v["n_unique"], v["pearson_r"],
             v["spearman_rho"],
             ("%.4f" % m["const"]) if m else "—",
             ("%.4f" % m["linear"]) if m else "—",
             ("%.4f" % m["spline"]) if m else "—",
             ("%.4f" % m["isotonic"]) if m else "—",
             ("**%+.4f**" % cv["curve_gain_vs_linear"]) if cv else "—"))
    A("")
    A("> 곡선이득 = 직선 MAE − spline MAE. 양수면 곡선이 직선보다 낫다는 뜻이고,")
    A("> 그 크기가 곧 이 축에서 기대할 수 있는 상한의 힌트다.\n")
    A("**진단: %s** (최대 곡선이득 %+.4f)\n"
      % (p["diagnostic_verdict"], max(p["curve_gains"].values())))

    A("### 1-2. 구간별 target (지시서 Experiment 0 출력)\n")
    for c, v in p["diagnostic"].items():
        A("`%s` — 커버리지 %.1f%%\n" % (v["label"], 100 * v["coverage"]))
        A("| 구간 | n | x 중앙값 | y 중앙값(log10) | 중앙값(원) | y 평균 | "
          "y IQR | 앞 구간 대비 기울기 |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in v["bins"]:
            A("| %s | %d | %.4g | %.4f | %s | %.4f | %.4f | %s |"
              % (r["bin"], r["n"], r["x_median"], r["y_median"],
                 "{:,}".format(r["median_won"]), r["y_mean"], r["y_iqr"],
                 ("%+.5f" % r["slope_vs_prev"])
                 if r.get("slope_vs_prev") is not None else "—"))
        A("")

    A("## 2. Experiment 1 — Spline feature + M73\n")
    A("| 후보 | OOF MAE | Δ vs baseline | 95% CI | wilcoxon p | fold승 | "
      "strict MAE | 2배내 | 3배내 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name in [c[0] for c in CANDIDATES]:
        m = ps["variants"][name]
        v = m.get("vs_base")
        s = nt["variants"].get(name, {})
        A("| `%s` | %.4f | %s | %s | %s | %s | %s | %.1f%% | %.1f%% |"
          % (name, m["MAE_log10"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             str(v["wilcoxon_p"]) if v else "—",
             ("%d/5" % m["fold_wins_vs_base"]) if v else "—",
             ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], 100 * m["within_3x"]))
    if "S5/expert-wise" in ps["variants"]:
        m = ps["variants"]["S5/expert-wise"]
        v = m["vs_base"]
        A("| `S5/expert-wise` | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %s | %d/5 | "
          "— | %.1f%% | %.1f%% |"
          % (m["MAE_log10"], v["delta_MAE"], v["ci95"][0], v["ci95"][1],
             v["wilcoxon_p"], m["fold_wins_vs_base"],
             100 * m["within_2x"], 100 * m["within_3x"]))
    A("")
    A("> baseline 은 같은 fold·같은 코드로 다시 학습한 M73 `soft/ordinal_xgb`")
    A("> (%.4f, 공표 %.4f 재현) 이다. 모든 Δ 는 이 값과의 paired 차이다.\n"
      % (base["MAE_log10"], p["published_m73"]["MAE_log10"]))

    A("### 2-2. degree / knots sweep — 진단용 (승격 근거 아님)\n")
    A("고정 spec 을 전체 OOF 에 적용한 표다. **여기서 최저값을 골라 쓰면 같은")
    A("데이터에서 고르고 같은 데이터로 재는 것**이라 낙관 쪽으로 휜다.\n")
    A("| spec | OOF MAE | Δ vs baseline | fold승 |")
    A("|---|---:|---:|---:|")
    for name in [c[0] for c in SWEEP_CANDIDATES]:
        if name not in ps["variants"]:
            continue
        m = ps["variants"][name]
        A("| `%s` | %.4f | %+0.4f | %d/5 |"
          % (name, m["MAE_log10"], m["vs_base"]["delta_MAE"],
             m["fold_wins_vs_base"]))
    A("| `S4/all` (승격 후보 spec d3k4) | %.4f | %+0.4f | %d/5 |"
      % (ps["variants"]["S4/all"]["MAE_log10"],
         ps["variants"]["S4/all"]["vs_base"]["delta_MAE"],
         ps["variants"]["S4/all"]["fold_wins_vs_base"]))
    A("")

    A("### 2-3. 구간별 · 비교군별 MAE\n")
    best = p["best_candidate"]
    B = ps["variants"][best]
    A("| 구간 | n | baseline | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for b in BUCKETS:
        A("| %s | %d | %.4f | %.4f |"
          % (b, base["buckets"][b]["n"], base["buckets"][b]["MAE_log10"],
             B["buckets"][b]["MAE_log10"]))
    A("")
    A("| 비교군 | n | baseline | `%s` |" % best)
    A("|---|---:|---:|---:|")
    for col in ("cohort", "evidence_source"):
        for k, r in base["cohort"][col].items():
            A("| %s | %d | %.4f | %.4f |"
              % (k, r["n"], r["MAE"], B["cohort"][col][k]["MAE"]))
    A("")
    A("### 2-4. fold 별 MAE\n")
    A("| fold | 경계(원) | baseline | " +
      " | ".join("`%s`" % c[0] for c in CANDIDATES[1:]) + " |")
    A("|---|---|---:|" + "---:|" * (len(CANDIDATES) - 1))
    for i, f in enumerate(ps["folds"]):
        A("| %d | %s | %.4f | %s |"
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             base["per_fold_MAE"][i],
             " | ".join("%.4f" % ps["variants"][c[0]]["per_fold_MAE"][i]
                        for c in CANDIDATES[1:])))
    A("")

    A("## 3. Experiment 2 — GAM baseline\n")
    g = p["gam_implementation"]
    A("```text")
    A("구현  %s" % g["library"])
    A("spec  degree=%d, n_knots=%d / smoothing %s"
      % (g["spec"]["degree"], g["spec"]["n_knots"], g["alphas"]))
    A("edf   %s" % g["edf"])
    A("G1 %s / G2 %s / G3 %s" % (g["G1"], g["G2"], g["G3"]))
    A("```\n")
    A("| 모델 | OOF MAE | strict MAE | fold별 edf | 2배내 |")
    A("|---|---:|---:|---|---:|")
    for k in ("G1", "G2", "G3"):
        m = ps["additive"][k]
        e = " ".join("%.0f" % f["gam"][k]["edf"] for f in ps["folds"])
        A("| %s | %.4f | %.4f | %s | %.1f%% |"
          % (k, m["MAE_log10"], nt["additive"][k]["MAE_log10"], e,
             100 * m["within_2x"]))
    A("")
    A("> GAM 이 M73 을 이길 것이라 기대하지 않는다. 확인하려는 것은")
    A("> **numeric 곡선이 안정적인 설명력을 가지는가** 하나다.\n")
    A("### 3-2. feature 별 곡선 (G2, fold 5개 겹침)\n")
    A("| feature | 진폭(log10) | 배수 | fold σ | 안정성(진폭/σ) | 단조 | 변곡 수 |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for c, v in p["curves"].get("G2", {}).items():
        A("| %s | %.3f | %.1f배 | %.3f | %.1f | %s | %d |"
          % (NUM_LABEL.get(c, c), v["range_log10"], v["range_x"],
             v["fold_sd_mean"], v["stability"], "예" if v["monotone"] else "아니오",
             v["n_sign_changes"]))
    A("")
    A("곡선 값 (부분효과 f(x), 중심화 · 95% 신뢰띠는 fold 간 ±1.96σ)\n")
    for c, v in p["curves"].get("G2", {}).items():
        A("`%s`\n" % NUM_LABEL.get(c, c))
        A("x 는 원 스케일 (%s 축에서 적합)\n" % v["x_scale"])
        A("| x | f(x) | 하한 | 상한 |")
        A("|---:|---:|---:|---:|")
        step = max(1, len(v["grid"]) // 8)
        for j in range(0, len(v["grid"]), step):
            A("| %.4g | %+.4f | %+.4f | %+.4f |"
              % (v["grid_original"][j], v["mean"][j], v["band_lo"][j],
                 v["band_hi"][j]))
        A("")

    A("## 4. Experiment 3 — Polynomial baseline\n")
    A("| 모델 | OOF MAE | strict MAE |")
    A("|---|---:|---:|")
    for dg in POLY_DEGREES:
        k = "P%d" % dg
        A("| degree=%d | %.4f | %.4f |"
          % (dg, ps["additive"][k]["MAE_log10"], nt["additive"][k]["MAE_log10"]))
    A("")

    A("## 5. Experiment 4 — expert 별 spline\n")
    A("%s\n" % p["exp4"])

    A("## 6. 최종 비교표\n")
    A("| 방법 | OOF MAE | Strict MAE | Within 2x | Fold 승 | 95% CI |")
    A("|---|---:|---:|---:|---:|---|")
    A("| M73 baseline (재현) | %.4f | %.4f | %.1f%% | — | — |"
      % (base["MAE_log10"], nt["variants"][BASE]["MAE_log10"],
         100 * base["within_2x"]))
    for name in [c[0] for c in CANDIDATES[1:]]:
        m = ps["variants"][name]
        v = m["vs_base"]
        s = nt["variants"].get(name, {})
        A("| %s | %.4f | %s | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (name, m["MAE_log10"], ("%.4f" % s["MAE_log10"]) if s else "—",
             100 * m["within_2x"], m["fold_wins_vs_base"], v["ci95"][0],
             v["ci95"][1]))
    for k in ("G1", "G2", "G3"):
        m = ps["additive"][k]
        v = m["vs_base"]
        A("| GAM %s | %.4f | %.4f | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (k, m["MAE_log10"], nt["additive"][k]["MAE_log10"],
             100 * m["within_2x"], m["fold_wins_vs_base"], v["ci95"][0],
             v["ci95"][1]))
    for dg in POLY_DEGREES:
        k = "P%d" % dg
        m = ps["additive"][k]
        v = m["vs_base"]
        A("| Polynomial d=%d | %.4f | %.4f | %.1f%% | %d/5 | [%+0.4f, %+0.4f] |"
          % (dg, m["MAE_log10"], nt["additive"][k]["MAE_log10"],
             100 * m["within_2x"], m["fold_wins_vs_base"], v["ci95"][0],
             v["ci95"][1]))
    A("| oracle 상한 (서빙 불가) | %.4f | — | %.1f%% | — | — |"
      % (ps["oracle_ceiling"]["MAE_log10"], 100 * ps["oracle_ceiling"]["within_2x"]))
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
    A("대상: `%s` (spline 후보 중 OOF MAE 최저)\n" % p["best_candidate"])
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")

    A("## 결론\n")
    A("```text")
    A("M73 baseline (같은 fold 재현)  MAE = %.4f" % base["MAE_log10"])
    A("최고 spline 후보               %s" % p["best_candidate"])
    A("                               MAE = %.4f  (Δ %+0.4f, 95%%CI [%+0.4f, %+0.4f])"
      % (B["MAE_log10"], B["vs_base"]["delta_MAE"], B["vs_base"]["ci95"][0],
         B["vs_base"]["ci95"][1]))
    A("GAM 최고                       %s = %.4f"
      % (min(("G1", "G2", "G3"), key=lambda k: ps["additive"][k]["MAE_log10"]),
         min(ps["additive"][k]["MAE_log10"] for k in ("G1", "G2", "G3"))))
    A("Polynomial 최고                %.4f"
      % min(ps["additive"]["P%d" % d]["MAE_log10"] for d in POLY_DEGREES))
    A("numeric 곡선 진단              %s" % p["diagnostic_verdict"])
    A("")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
