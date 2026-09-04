r"""M82 — 수치-문맥 근접(Proximity) feature. 전체본문 SVD 가 잃어버릴 수 있는
"어떤 수치가 어떤 의미의 단어 근처에 있었는가"를 명시적으로 복원한다.

지시서(사용자, `m81_m82_model2_data_integrity_and_proximity_features_plan.md`):

    TF-IDF/SVD 는 본문 전체 문맥을 압축한다. 그 과정에서 "70% 지원", "30개사
    선정", "12개월 지원" 같은 관계가 뭉개질 수 있다. target 금액 자체가 아니라
    지원비율·선정기업수·사업기간·지원방식·지원단위 같은 **비-target 수치/맥락**만
    명시적으로 뽑아 M73 0.3563 을 이길 수 있는지 본다.

바꾸지 않는 것 (M73 과 비교가 성립하려면)

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap — 손대지 않음
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    baseline   M69 G 단계(구조화+제목SVD64+원천층+본문SVD64) 그대로 유지
    회귀 구조  M73 `soft/ordinal_xgb` 재현 블록(global+구간expert3+ordinal stage1)

바뀌는 것은 그 위에 얹는 feature 층 하나(P1/P2)뿐이다. 회귀모델·routing·
split·baseline feature 는 M73/M77/M78/M79 가 쓴 것과 같은 재현 경로
(`m73_block`, `M69.assemble`)를 그대로 가져온다.

## 가장 중요한 leakage 규칙

정규식은 **원(KRW) 금액을 절대 추출하지 않는다** — %, 개수, 개월/년만 본다.
금액 후보는 `amount_parser` 로만 다루고 M82 는 건드리지 않는다. target
`per_recipient` 값이 feature 안에 다시 나타나는 경로 자체가 구조적으로 없다
(정규식 타입이 다르다 — 원화 단위 vs 비율/개수/기간).

## Window Size

지시서는 20/30/50 을 train-side nested validation 에서 고르라고 했다.
여기서는 자원 제약으로 **완전한 fold-내부 nested 서치 대신, 데이터를 보기
전에 30자를 승격 후보 스펙으로 고정**하고(M77 이 spline degree=3 을 고정한
것과 같은 방식) 20/50 은 진단용 sweep 으로만 남긴다 — sweep 최저값을 승격
근거로 쓰지 않는다.

산출
    ml/data/processed/m82_proximity_audit.parquet
    ml/reports/m82_m2_proximity_features.json / .md
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

import os
import re
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
OUT_PROX = os.path.join(C.PROC, "m82_proximity_audit.parquet")
MD = C.report_path("m82_m2_proximity_features.md")

M73_BASELINE = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                "within_2x": 0.564, "within_3x": 0.742}
STEP = "G"
WINDOW_PRIMARY = 30                     # 데이터 보기 전 고정 (승격 후보)
WINDOW_SWEEP = (20, 30, 50)             # 진단용
PROX_SVD = 16                           # 본문 SVD64 보다 훨씬 작게 — 짧은 문맥
SEED = F.PIPELINE_SEED

# ------------------------------------------------------------ 정규식
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
COUNT_RE = re.compile(r"(\d{1,4})\s*(?:개사|개\s*기업|개\s*과제|개\s*팀|개\s*업체)")
MONTH_RE = re.compile(r"(\d{1,3})\s*개월")
YEAR_RE = re.compile(r"(\d{1,2})\s*년(?:간)?")

SUPPORT_KW = re.compile(r"정부\s*지원|지원\s*비율|지원율|보조\s*비율")
BURDEN_KW = re.compile(r"자부담|본인\s*부담|민간\s*부담")
SELECT_KW = re.compile(r"선정|모집|지원\s*대상")
PERIOD_KW = re.compile(r"사업\s*기간|지원\s*기간|수행\s*기간|협약\s*기간")

LOAN_KW = re.compile(r"융자")
GRANT_KW = re.compile(r"보조금|출연금")
VOUCHER_KW = re.compile(r"바우처")
PER_COMPANY_KW = re.compile(r"(기업|업체|개사)\s*당")
PER_PROJECT_KW = re.compile(r"(과제|건|프로젝트)\s*당")


def _nearest(text, num_re, kw_re, window):
    """`kw_re` 가 매치된 수치 후보 중 키워드에 가장 가까운 것 하나.

    반환: (value, distance, n_candidates, n_keyword_hits, context_window)
    수치 후보 자체가 없으면 전부 None/0.
    """
    nums = list(num_re.finditer(text))
    if not nums:
        return None, None, 0, len(kw_re.findall(text)), ""
    kw_spans = [m.span() for m in kw_re.finditer(text)]
    n_kw = len(kw_spans)
    if n_kw == 0:
        return None, None, len(nums), 0, ""
    best = None
    for m in nums:
        ns, ne = m.span()
        d = min(abs(ns - ks[1]) if ks[1] <= ns else
                (abs(ks[0] - ne) if ks[0] >= ne else 0)
                for ks in kw_spans)
        if best is None or d < best[1]:
            best = (m, d)
    m, dist = best
    if dist > window:
        return None, None, len(nums), n_kw, ""
    val = float(m.group(1))
    ctx = text[max(0, m.start() - window):m.end() + window]
    return val, dist, len(nums), n_kw, ctx


def extract_row(text, window):
    t = str(text or "")
    out = {}
    sr = _nearest(t, PCT_RE, SUPPORT_KW, window)
    out["prox_support_rate"] = sr[0]
    out["prox_support_dist"] = sr[1]
    out["prox_support_candidates"] = sr[2]
    out["prox_support_kw_hits"] = sr[3]
    out["ctx_support"] = sr[4]

    br = _nearest(t, PCT_RE, BURDEN_KW, window)
    out["prox_self_burden_rate"] = br[0]
    out["prox_burden_dist"] = br[1]
    out["prox_burden_candidates"] = br[2]
    out["ctx_burden"] = br[4]

    ct = _nearest(t, COUNT_RE, SELECT_KW, window)
    out["prox_selected_count"] = ct[0]
    out["prox_count_dist"] = ct[1]
    out["prox_count_candidates"] = ct[2]
    out["ctx_count"] = ct[4]

    mo = _nearest(t, MONTH_RE, PERIOD_KW, window)
    yr = _nearest(t, YEAR_RE, PERIOD_KW, window)
    dur_m, dur_dist, ctx_dur = None, None, ""
    if mo[0] is not None and (yr[0] is None or (mo[1] or 1e9) <= (yr[1] or 1e9)):
        dur_m, dur_dist, ctx_dur = mo[0], mo[1], mo[4]
    elif yr[0] is not None:
        dur_m, dur_dist, ctx_dur = yr[0] * 12.0, yr[1], yr[4]
    out["prox_duration_months"] = dur_m
    out["prox_duration_dist"] = dur_dist
    out["prox_duration_candidates"] = (mo[2] or 0) + (yr[2] or 0)
    out["ctx_duration"] = ctx_dur

    out["has_loan_context"] = int(bool(LOAN_KW.search(t)))
    out["has_grant_context"] = int(bool(GRANT_KW.search(t)))
    out["has_voucher_context"] = int(bool(VOUCHER_KW.search(t)))
    out["has_per_company_context"] = int(bool(PER_COMPANY_KW.search(t)))
    out["has_per_project_context"] = int(bool(PER_PROJECT_KW.search(t)))

    out["has_percent_near_support"] = int(sr[0] is not None)
    out["has_percent_near_self_burden"] = int(br[0] is not None)
    out["has_count_near_selection"] = int(ct[0] is not None)
    out["has_month_near_duration"] = int(mo[0] is not None)
    out["has_year_near_duration"] = int(yr[0] is not None)

    n_amb = sum(1 for c in (out["prox_support_candidates"], out["prox_burden_candidates"],
                            out["prox_count_candidates"]) if (c or 0) > 1)
    out["ambiguity_flag"] = int(n_amb > 0)
    return out


NUMERIC_COLS = ["prox_support_rate", "prox_support_dist", "prox_support_candidates",
                "prox_support_kw_hits", "prox_self_burden_rate", "prox_burden_dist",
                "prox_burden_candidates", "prox_selected_count", "prox_count_dist",
                "prox_count_candidates", "prox_duration_months", "prox_duration_dist",
                "prox_duration_candidates"]
FLAG_COLS = ["has_loan_context", "has_grant_context", "has_voucher_context",
            "has_per_company_context", "has_per_project_context",
            "has_percent_near_support", "has_percent_near_self_burden",
            "has_count_near_selection", "has_month_near_duration",
            "has_year_near_duration", "ambiguity_flag"]
CTX_COLS = ["ctx_support", "ctx_burden", "ctx_count", "ctx_duration"]


def build_proximity(texts, window):
    rows = [extract_row(t, window) for t in texts]
    P = pd.DataFrame(rows)
    for c in NUMERIC_COLS:
        P[c] = pd.to_numeric(P[c], errors="coerce")
    raw_ctx = (P[CTX_COLS].fillna("").agg(" ".join, axis=1)).str.strip()
    # P2(TF-IDF/SVD)는 수치 '값'이 아니라 그 주변 단어의 의미를 압축하는 게
    # 목적이다(값은 이미 P1 이 명시 feature 로 갖고 있다). window 를 raw src
    # 에서 그대로 잘라 왔기 때문에, 이 window 안에 target 금액 표현이 우연히
    # 같이 들어올 수 있다(예: "...지원비율 70% 이내이며 기업당 5억원 한도...").
    # M69/M71 이 본문 SVD 에 쓰는 것과 같은 규율(`SF.mask_text`) 로 숫자를
    # 전부 지운 뒤에만 TF-IDF 에 넘긴다 — 그래야 target 이 문자 n-gram 으로
    # 새는 경로를 원천 차단한다.
    P["prox_context_text"] = raw_ctx.apply(lambda t: SF.mask_text(t, cap=2000))
    return P


# ------------------------------------------------------------ leakage 점검
def leakage_audit(P, target_won):
    """추출한 수치가 target(원화)과 값이 겹치는지 — 겹칠 수 없는 타입이지만
    구조적으로도 비어 있는지 직접 센다."""
    hit = 0
    for c in ("prox_support_rate", "prox_self_burden_rate", "prox_selected_count",
             "prox_duration_months"):
        v = P[c].to_numpy(dtype=float)
        m = np.isfinite(v)
        hit += int(np.isclose(v[m], target_won[m], rtol=1e-6).sum())
    return hit


# ------------------------------------------------------------ 모델 블록 (M73 재현)
def m73_block(Xtr, ytr, Xte):
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    g = F.make_point_model().fit(Xtr, ytr).predict(Xte)
    tab = np.zeros((len(Xte), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)
    pr = M73.stage1_proba("ordinal_xgb", Xtr, ztr, Xte)
    return M73.route_soft(tab, pr)


def fit_prox_svd(text_tr, text_te, n_components=PROX_SVD, return_objects=False):
    """proximity 문맥 TF-IDF -> SVD. `return_objects` 는 서빙 번들용."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    nonempty = sum(1 for t in text_tr if t.strip())
    if nonempty < max(10, n_components * 2):
        z = (np.zeros((len(text_tr), n_components)),
             np.zeros((len(text_te), n_components)))
        return (z[0], z[1], None) if return_objects else z
    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=2,
                        max_features=8000, sublinear_tf=True)
    A = v.fit_transform(text_tr)
    k = min(n_components, max(1, min(A.shape) - 1))
    svd = TruncatedSVD(n_components=k, random_state=SEED)
    ta = svd.fit_transform(A)
    tb = svd.transform(v.transform(text_te))
    if k < n_components:
        ta = np.pad(ta, ((0, 0), (0, n_components - k)))
        tb = np.pad(tb, ((0, 0), (0, n_components - k)))
    return (ta, tb, (v, svd, n_components)) if return_objects else (ta, tb)


def augment(Xtr, Xte, atr, ate):
    if atr is None or atr.shape[1] == 0:
        return Xtr, Xte
    return (pd.concat([Xtr.reset_index(drop=True), atr.reset_index(drop=True)], axis=1),
            pd.concat([Xte.reset_index(drop=True), ate.reset_index(drop=True)], axis=1))


def fold_compute(Xs, y, groups, titles, body, NB, cats, P, tr, te, i):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te], STEP, [None])
    ytr, yte = y[tr], y[te]

    p_num_tr = P[NUMERIC_COLS + FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_num_te = P[NUMERIC_COLS + FLAG_COLS].iloc[te].reset_index(drop=True)
    X_p1_tr, X_p1_te = augment(Xb_tr, Xb_te, p_num_tr, p_num_te)

    ctx_tr = P["prox_context_text"].to_numpy()[tr]
    ctx_te = P["prox_context_text"].to_numpy()[te]
    svd_tr, svd_te = fit_prox_svd(ctx_tr, ctx_te)
    svd_names = ["proxsvd%02d" % i for i in range(svd_tr.shape[1])]
    svd_tr_df = pd.DataFrame(svd_tr, columns=svd_names)
    svd_te_df = pd.DataFrame(svd_te, columns=svd_names)
    X_p2_tr, X_p2_te = augment(Xb_tr, Xb_te, svd_tr_df, svd_te_df)
    X_p3_tr, X_p3_te = augment(X_p1_tr, X_p1_te, svd_tr_df, svd_te_df)

    pred = {
        "P0": m73_block(Xb_tr, ytr, Xb_te),
        "P1": m73_block(X_p1_tr, ytr, X_p1_te),
        "P2": m73_block(X_p2_tr, ytr, X_p2_te),
        "P3": m73_block(X_p3_tr, ytr, X_p3_te),
    }
    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
          "MAE": {k: round(float(np.abs(p - yte).mean()), 4) for k, p in pred.items()},
          "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "pred": pred, "rec": rec}


def run_split(Xs, y, groups, titles, body, NB, cats, P, verbose=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    fold_id = np.zeros(n, dtype=int)
    pred = {k: np.zeros(n) for k in ("P0", "P1", "P2", "P3")}
    per_fold = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = fold_compute(Xs, y, groups, titles, body, NB, cats, P, tr, te, i)
        fold_id[fo["te"]] = i
        for k in pred:
            pred[k][fo["te"]] = fo["pred"][k]
        per_fold.append(fo["rec"])
        if verbose:
            print("   fold %d  %s  (%.0fs)" % (i, fo["rec"]["MAE"], fo["rec"]["seconds"]))
    return {"fold_id": fold_id, "pred": pred, "folds": per_fold}


# ------------------------------------------------------------ 지표
def paired_test(y, p_new, p_old):
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n_better": int((d < 0).sum()), "n_worse": int((d > 0).sum())}


def fold_wins(y, p_new, p_old, fold_id):
    wins = 0
    for i in sorted(set(fold_id.tolist())):
        s = fold_id == i
        wins += int(np.abs(p_new[s] - y[s]).mean() < np.abs(p_old[s] - y[s]).mean())
    return wins


def within_x(y, p, x):
    return float((np.abs(p - y) <= np.log10(x)).mean())


# ------------------------------------------------------------ main
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
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"]))
    NB, body, src = SF.build(d)

    # ---------------------------------------------- Exp0 — proximity pattern audit
    print("\n== Exp0 — proximity pattern audit (window=%d, 원문 src 기준)" % WINDOW_PRIMARY)
    P = build_proximity(src, WINDOW_PRIMARY)
    P.insert(0, "row_id", d["row_id"].to_numpy())
    coverage = {c: round(float(P[c].notna().mean()), 4) for c in
               ["prox_support_rate", "prox_self_burden_rate", "prox_selected_count",
                "prox_duration_months"]}
    flag_rate = {c: round(float(P[c].mean()), 4) for c in FLAG_COLS}
    print("   coverage(값 존재 비율) %s" % coverage)
    print("   flag 비율 %s" % flag_rate)

    target_won = d["per_recipient"].to_numpy(dtype=float) if "per_recipient" in d.columns \
        else (10 ** y)
    leak_hits = leakage_audit(P, target_won)
    ctx_digit_residue = int(P["prox_context_text"].str.contains(r"\d", regex=True).sum())
    print("   leakage audit — target 과 값이 겹치는 proximity 칸 수: %d (0 이어야 함)" % leak_hits)
    print("   leakage audit — prox_context_text 에 숫자가 남은 행 수: %d (0 이어야 함, "
          "SF.mask_text 로 [AMOUNT]/# 치환 확인)" % ctx_digit_residue)

    P.to_parquet(OUT_PROX, index=False)
    print("   [prox] %s" % OUT_PROX)

    # window sweep (진단용 — coverage/ambiguity 만, 승격 근거 아님)
    sweep = {}
    for w in WINDOW_SWEEP:
        Pw = build_proximity(src, w)
        sweep[str(w)] = {
            "coverage_support_rate": round(float(Pw["prox_support_rate"].notna().mean()), 4),
            "ambiguity_rate": round(float(Pw["ambiguity_flag"].mean()), 4),
        }
    print("   window sweep(진단용) %s" % sweep)

    # ---------------------------------------------- Exp1~3 — P0~P3 5-fold
    print("\n== P0~P3 5-fold [program_stem]")
    Rp = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats, P)
    print("\n== P0~P3 5-fold [normalized_title] (strict)")
    Rn = run_split(Xs, y, groups["normalized_title"], titles, body, NB, cats, P, verbose=False)

    def summarize(R):
        out = {}
        for k, p in R["pred"].items():
            out[k] = {"MAE_log10": round(float(np.abs(p - y).mean()), 4),
                      "within_2x": round(within_x(y, p, 2), 4),
                      "within_3x": round(within_x(y, p, 3), 4)}
            if k != "P0":
                out[k]["vs_P0"] = paired_test(y, p, R["pred"]["P0"])
                out[k]["fold_wins_vs_P0"] = fold_wins(y, p, R["pred"]["P0"], R["fold_id"])
        return out

    sp, sn = summarize(Rp), summarize(Rn)
    print("\n== 결과 [program_stem]")
    for k in ("P0", "P1", "P2", "P3"):
        b = sp[k]
        extra = ("  Δ%+0.4f CI[%+0.4f,%+0.4f] p=%s fold승%d/5"
                % (b["vs_P0"]["delta_MAE"], b["vs_P0"]["ci95"][0], b["vs_P0"]["ci95"][1],
                   b["vs_P0"]["wilcoxon_p"], b["fold_wins_vs_P0"])) if k != "P0" else ""
        print("   %-3s MAE %.4f  2x %.1f%%  3x %.1f%%%s"
              % (k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"], extra))

    best = min(("P1", "P2", "P3"), key=lambda k: sp[k]["MAE_log10"])
    B = sp[best]
    checks = {
        "1. OOF MAE < 0.3563": B["MAE_log10"] < M73_BASELINE["MAE_log10"],
        "2. strict 에서도 개선": sn[best]["MAE_log10"] < sn["P0"]["MAE_log10"],
        "3. 최소 4/5 fold 개선": B["fold_wins_vs_P0"] >= 4,
        "4. CI 가 0 아래": B["vs_P0"]["ci95"][1] < 0,
        "5. target amount leakage 0건": leak_hits == 0 and ctx_digit_residue == 0,
        "6. parser ambiguity 과도하지 않음(<0.5)": sweep[str(WINDOW_PRIMARY)]["ambiguity_rate"] < 0.5,
        "7. 실질기준 ΔMAE <= -0.003": B["vs_P0"]["delta_MAE"] <= -0.003,
        "8. reproducibility": True,
    }
    core = [k for k in checks if k != "8. reproducibility"]
    # 재현성 — program_stem 한 번 더 (P1 만, 대표로)
    Rp2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, cats, P, verbose=False)
    repro = bool(np.allclose(Rp2["pred"]["P1"], Rp["pred"]["P1"]))
    checks["8. reproducibility"] = repro
    verdict = ("승격 후보 (%s)" % best) if all(checks[k] for k in checks) else "현행 유지 — M73 원본"

    print("\n== 승격 점검표 — 대상 %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    payload = {
        "purpose": "전체본문 SVD 가 잃어버릴 수 있는 수치-맥락 결합을 명시 feature 로 복원",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"],
                      "baseline": "M73 soft/ordinal_xgb 재현 블록 + M69 G단계 feature"},
        "m73_baseline": M73_BASELINE,
        "window_primary": WINDOW_PRIMARY, "window_sweep_diagnostic": sweep,
        "coverage": coverage, "flag_rate": flag_rate,
        "leakage_hits": leak_hits, "ctx_digit_residue": ctx_digit_residue,
        "results": {"program_stem": sp, "normalized_title": sn},
        "best_candidate": best,
        "promotion_checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict,
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m82_m2_proximity_features.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    L = []
    a = L.append
    a("# M82 — 수치-문맥 근접(Proximity) feature\n")
    a("> 질문: **전체본문 SVD 가 놓친 수치-의미단어 근접 관계를 명시 feature 로 "
      "복원하면 M73 0.3563 을 이기는가?**\n")
    a("## Exp0 — 패턴 커버리지\n")
    a("| 항목 | 값 |\n|---|---:|")
    for k, v in p["coverage"].items():
        a("| %s | %.1f%% |" % (k, 100 * v))
    a("\nwindow sweep(진단용, 승격 근거 아님): `%s`\n" % p["window_sweep_diagnostic"])
    a("target(원) leakage 검사: 값 일치 **%d건**, context 숫자 잔존 **%d행** (모두 0 이어야 통과)\n"
      % (p["leakage_hits"], p["ctx_digit_residue"]))
    a("\n## P0~P3 결과 [program_stem]\n")
    a("| 변형 | MAE | 2x | 3x | Δ vs P0 | 95%% CI | fold승 |\n|---|---:|---:|---:|---:|---|---:|")
    for k in ("P0", "P1", "P2", "P3"):
        b = p["results"]["program_stem"][k]
        if k == "P0":
            a("| P0 (M73 baseline) | %.4f | %.1f%% | %.1f%% | — | — | — |"
              % (b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"]))
        else:
            a("| %s | %.4f | %.1f%% | %.1f%% | %+0.4f | [%+0.4f, %+0.4f] | %d/5 |"
              % (k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"],
                 b["vs_P0"]["delta_MAE"], b["vs_P0"]["ci95"][0], b["vs_P0"]["ci95"][1],
                 b["fold_wins_vs_P0"]))
    a("\n## 승격 점검표 — 대상 %s\n" % p["best_candidate"])
    for k, ok in p["promotion_checks"].items():
        a("- [%s] %s" % ("x" if ok else " ", k))
    a("\n## 판정\n\n```text\n%s\n```\n" % p["verdict"])
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)


if __name__ == "__main__":
    main()
