r"""M83/M84 — 수치-맥락 축을 더 정교하게. Typed relation + Section-aware proximity.

지시서(사용자, `m82_final_result_and_next_experiments.md` 7~11장):

    M82 가 성공했으므로 모델군을 또 바꾸지 말고 **성공한 수치-맥락 축을 더
    정교하게** 만든다.
      M83  숫자와 키워드 사이의 '관계'를 type 별로 직접 정의
           (수치 종류 + 단위 + 주변 semantic keyword + 거리 + 방향)
      M84  같은 70% 라도 '지원내용' 안의 70% 와 '신청자격' 안의 70% 는 다르다
           — proximity 에 section 정보를 얹는다

바꾸지 않는 것 — baseline 이 M73 이 아니라 **M82/P3** 로 바뀐 것만 다르다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    회귀 구조  M73 `soft/ordinal_xgb` 재현 블록 (M82 와 같은 `m73_block`)
    baseline   B0 = M82/P3 = M69 G단계 + explicit proximity + masked proximity SVD
               (지시서 11장: primary 0.3518 / strict 0.3751)

## 후보

    B0  M82/P3                          (새 baseline, 매 fold 같이 학습)
    R1  B0 + M83 typed relation
    R2  B0 + M84 section-aware
    R3  B0 + M83 + M84

네 후보를 **한 fold 루프에서** 잰다 — 따로 돌리면 fold·SVD 적합이 달라져 차이가
feature 때문인지 우연 때문인지 알 수 없게 된다(M67 이 세운 규율).

## leakage 규칙 (M82 와 동일 + 강화)

    - 정규식은 원(KRW) 금액 '값'을 추출하지 않는다 — %, 개수, 개월/년, 그리고
      '거리/방향' 같은 위치 정보만 본다
    - 금액 표현은 **위치만** 쓴다(예: '기업당' 에서 가장 가까운 금액 표현까지의
      거리). 그 금액의 자릿수·값은 어떤 feature 에도 들어가지 않는다
    - TF-IDF 에 넘기는 모든 문맥 텍스트는 `SF.mask_text` 로 [AMOUNT]/# 치환
    - digit-residue audit 으로 숫자가 남지 않았음을 매 실행 확인

산출
    ml/data/processed/m83_m84_features.parquet
    ml/data/processed/m83_m84_oof.parquet
    ml/reports/m83_m84_typed_section_proximity.json / .md
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
import m82_m2_proximity_features as M82

SRC = F6.OUT_V2
OUT_FEAT = os.path.join(C.PROC, "m83_m84_features.parquet")
OUT_OOF = os.path.join(C.PROC, "m83_m84_oof.parquet")
MD = C.report_path("m83_m84_typed_section_proximity.md")

# 지시서 11장 — 이제 baseline 은 M73 이 아니라 M82/P3
P3_BASELINE = {"primary_MAE": 0.3518, "strict_MAE": 0.3751,
               "within_2x": 0.5717, "within_3x": 0.7437}
M73_BASELINE = {"primary_MAE": 0.3563, "strict_MAE": 0.3756}
PRACTICAL_DELTA = -0.002        # 지시서 11장 실질 기준(-0.002 ~ -0.003)
SEC_SVD = 8                     # 섹션별 proximity SVD 성분 수 (본문 64 대비 작게)

# ============================================================ M83 — typed relation
# 관계 하나 = (수치 종류, 단위, 주변 semantic keyword, 거리, 방향).
# 키워드를 '어느 것이 맞았는가'까지 구분한다 — M82 는 or 로 뭉쳐 있었다.
NUM_TYPES = {
    "pct":   re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%"),
    "count": re.compile(r"(\d{1,4})\s*(?:개사|개\s*기업|개\s*과제|개\s*팀|개\s*업체|개사내외)"),
    "month": re.compile(r"(\d{1,3})\s*개월"),
    "year":  re.compile(r"(\d{1,2})\s*년(?:간)?"),
}

# relation 정의: 이름 -> (수치종류, 키워드 변형 목록)
# 키워드 변형에 순서가 있다 = kw_id. 어떤 표현이 맞았는지가 신호다.
RELATIONS = {
    "support_rate":     ("pct",   [r"정부\s*지원", r"지원\s*비율", r"지원율",
                                   r"보조\s*비율", r"국비", r"지원\s*금액의"]),
    "self_burden_rate": ("pct",   [r"자부담", r"본인\s*부담", r"민간\s*부담",
                                   r"기업\s*부담", r"대응\s*자금"]),
    "selected_count":   ("count", [r"선정", r"모집", r"지원\s*대상", r"내외",
                                   r"규모"]),
    "duration_month":   ("month", [r"사업\s*기간", r"지원\s*기간", r"수행\s*기간",
                                   r"협약\s*기간", r"대출\s*기간"]),
    "duration_year":    ("year",  [r"사업\s*기간", r"지원\s*기간", r"수행\s*기간",
                                   r"협약\s*기간", r"대출\s*기간"]),
}
RELATIONS_RE = {k: (nt, [re.compile(p) for p in pats])
                for k, (nt, pats) in RELATIONS.items()}

# 단위/방식 키워드 — 값이 아니라 **금액 표현까지의 거리**만 쓴다.
UNIT_KWS = {
    "unit_company": re.compile(r"(?:기업|업체|개사|사)\s*당|1\s*개\s*기업"),
    "unit_project": re.compile(r"(?:과제|건|프로젝트)\s*당"),
    "unit_team":    re.compile(r"팀\s*당|컨소시엄\s*당"),
    "loan":         re.compile(r"융자|대출|이차\s*보전|정책\s*자금"),
    "grant":        re.compile(r"보조금|출연금|지원금"),
    "voucher":      re.compile(r"바우처|쿠폰"),
}
# 금액 표현의 **위치**만 찾는다 (값은 읽지 않는다).
AMOUNT_POS_RE = re.compile(r"\d[\d,\.]*\s*(?:천원|만원|백만원|천만원|억원|조원|원)")

REL_WINDOW = 30                 # M82 승격 스펙과 같은 창


def _rel_one(text, num_re, kw_res, window):
    """가장 가까운 (수치, 키워드) 쌍 하나. 방향과 어떤 키워드였는지까지 돌려준다."""
    nums = list(num_re.finditer(text))
    if not nums:
        return None
    best = None
    for kw_id, kw_re in enumerate(kw_res):
        for km in kw_re.finditer(text):
            ks, ke = km.span()
            for nm in nums:
                ns, ne = nm.span()
                if ne <= ks:
                    dist, direction = ks - ne, 1        # 수치가 키워드 앞
                elif ns >= ke:
                    dist, direction = ns - ke, -1       # 수치가 키워드 뒤
                else:
                    dist, direction = 0, 0              # 겹침
                if dist > window:
                    continue
                if best is None or dist < best["dist"]:
                    best = {"value": float(nm.group(1)), "dist": int(dist),
                            "direction": int(direction), "kw_id": int(kw_id),
                            "span": nm.span(), "kw_span": (ks, ke)}
    return best


def _nearest_amount_dist(text, kw_re, cap=400):
    """키워드에서 가장 가까운 **금액 표현까지의 거리**. 금액의 값은 읽지 않는다."""
    kws = list(kw_re.finditer(text))
    if not kws:
        return None, 0
    amts = list(AMOUNT_POS_RE.finditer(text))
    if not amts:
        return None, len(kws)
    best = None
    for km in kws:
        ks, ke = km.span()
        for am in amts:
            as_, ae = am.span()
            d = (ks - ae) if ae <= ks else ((as_ - ke) if as_ >= ke else 0)
            if best is None or d < best:
                best = int(d)
    return (min(best, cap) if best is not None else None), len(kws)


def build_m83(texts, window=REL_WINDOW):
    rows = []
    for t in texts:
        t = str(t or "")
        out = {}
        for name, (nt, kw_res) in RELATIONS_RE.items():
            r = _rel_one(t, NUM_TYPES[nt], kw_res, window)
            out["rel_%s_value" % name] = (r["value"] if r else np.nan)
            out["rel_%s_dist" % name] = (r["dist"] if r else np.nan)
            out["rel_%s_dir" % name] = (r["direction"] if r else np.nan)
            out["rel_%s_kwid" % name] = (r["kw_id"] if r else np.nan)
            out["rel_%s_complete" % name] = int(r is not None)
        for name, kw_re in UNIT_KWS.items():
            dist, n = _nearest_amount_dist(t, kw_re)
            out["rel_%s_amtdist" % name] = (dist if dist is not None else np.nan)
            out["rel_%s_n" % name] = int(n)
        # 관계 밀도 — 몇 종류의 관계가 동시에 성립했는가
        out["rel_n_complete"] = int(sum(out["rel_%s_complete" % k] for k in RELATIONS_RE))
        rows.append(out)
    R = pd.DataFrame(rows)
    return R


# ============================================================ M84 — section-aware
# 실측 헤더 빈도(전체 1,877행 상위): 지원대상 476 · 지원내용 473 · 지원규모 464 ·
# 신청방법 342 · 사업기간 219 · 신청자격 117 ... 이 어휘를 그대로 버킷으로 쓴다.
SECTION_BUCKETS = [
    ("scale",     r"지원\s*규모|지원\s*금액|지원\s*한도|융자\s*규모|융자\s*한도|"
                  r"대출\s*한도|지원\s*내역|보조\s*금액"),
    ("content",   r"지원\s*내용|지원\s*분야|지원\s*사항|사업\s*내용|지원\s*항목"),
    ("target",    r"지원\s*대상|신청\s*자격|지원\s*자격|참여\s*자격|모집\s*대상"),
    ("period",    r"사업\s*기간|지원\s*기간|대출\s*기간|협약\s*기간|수행\s*기간"),
    ("condition", r"지원\s*조건|융자\s*방식|대출\s*금리|지원\s*방식|자부담"),
    ("apply",     r"신청\s*방법|신청\s*기간|접수\s*기간|접수\s*방법|제출\s*서류|모집\s*기간"),
    ("select",    r"선정\s*방법|평가\s*방법|선정\s*절차|심사"),
    ("exclude",   r"지원\s*제외|제외\s*대상"),
    ("purpose",   r"사업\s*목적|사업\s*개요|추진\s*배경"),
]
SECTION_RE = [(k, re.compile(p)) for k, p in SECTION_BUCKETS]
SECTION_NAMES = [k for k, _ in SECTION_BUCKETS]
# 섹션별 proximity TF-IDF 를 따로 만들 대상 (지시서 8장 M84 예시)
SEC_TFIDF = ["scale", "content", "period"]


def section_index(text):
    """(위치, 섹션) 목록. 헤더가 나온 자리부터 다음 헤더 전까지가 그 섹션이다."""
    hits = []
    for name, rx in SECTION_RE:
        for m in rx.finditer(text):
            hits.append((m.start(), name))
    hits.sort()
    return hits


def section_at(hits, pos):
    """offset `pos` 가 속한 섹션. 첫 헤더보다 앞이면 'head'."""
    if not hits:
        return "none"
    lo, sec = 0, "head"
    for p, name in hits:
        if p <= pos:
            sec = name
            lo = p
        else:
            break
    return sec


def build_m84(texts, window=REL_WINDOW):
    """섹션 플래그 + '어느 섹션에서 잡힌 수치인가' + 섹션별 문맥 텍스트."""
    rows, sec_texts = [], {s: [] for s in SEC_TFIDF}
    for t in texts:
        t = str(t or "")
        hits = section_index(t)
        out = {"sec_n_headers": len(hits),
               "sec_n_distinct": len({s for _, s in hits})}
        for s in SECTION_NAMES:
            out["sec_has_%s" % s] = int(any(x == s for _, x in hits))

        # 각 수치 종류가 '어느 섹션에서' 잡혔는지 — one-hot (category dtype 은
        # fold 마다 level 이 달라질 수 있어 int 플래그로 고정한다)
        buckets = {s: [] for s in SEC_TFIDF}
        for nt_name, num_re in NUM_TYPES.items():
            found = {s: 0 for s in SECTION_NAMES + ["head", "none"]}
            for m in num_re.finditer(t):
                sec = section_at(hits, m.start())
                found[sec] = found.get(sec, 0) + 1
                if sec in buckets:
                    buckets[sec].append(t[max(0, m.start() - window):m.end() + window])
            for s in SECTION_NAMES:
                out["sec_%s_in_%s" % (nt_name, s)] = int(found.get(s, 0))
            out["sec_%s_in_head" % nt_name] = int(found.get("head", 0))
        rows.append(out)
        for s in SEC_TFIDF:
            # TF-IDF 에 넘기기 전 반드시 마스킹 — M82 에서 고친 것과 같은 규율
            sec_texts[s].append(SF.mask_text(" ".join(buckets[s]), cap=2000))
    S = pd.DataFrame(rows)
    for s in SEC_TFIDF:
        S["sectext_%s" % s] = sec_texts[s]
    return S


# ============================================================ fold 계산
def _fold_matrices(Xs, y, titles, body, NB, P, R83, S84, tr, te):
    """B0(=P3) 설계행렬과 R1/R2/R3 증분 블록."""
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                M82.STEP, [None])
    # --- B0 = M82/P3 --------------------------------------------------------
    p_tr = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_te = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[te].reset_index(drop=True)
    a, b = M82.augment(Xb_tr, Xb_te, p_tr, p_te)
    sv_tr, sv_te = M82.fit_prox_svd(P["prox_context_text"].to_numpy()[tr],
                                    P["prox_context_text"].to_numpy()[te])
    nm = ["proxsvd%02d" % j for j in range(sv_tr.shape[1])]
    B0_tr, B0_te = M82.augment(a, b, pd.DataFrame(sv_tr, columns=nm),
                               pd.DataFrame(sv_te, columns=nm))

    # --- M83 typed relation (fold 적합 없음 — 텍스트 결정론) ----------------
    r83_tr = R83.iloc[tr].reset_index(drop=True)
    r83_te = R83.iloc[te].reset_index(drop=True)

    # --- M84 section-aware: 플래그 + 섹션별 TF-IDF/SVD (train 에만 적합) ----
    s_cols = [c for c in S84.columns if not c.startswith("sectext_")]
    s84_tr = [S84[s_cols].iloc[tr].reset_index(drop=True)]
    s84_te = [S84[s_cols].iloc[te].reset_index(drop=True)]
    for s in SEC_TFIDF:
        txt = S84["sectext_%s" % s].to_numpy()
        ta, tb = M82.fit_prox_svd(txt[tr], txt[te], n_components=SEC_SVD)
        cols = ["sec_%s_svd%02d" % (s, j) for j in range(ta.shape[1])]
        s84_tr.append(pd.DataFrame(ta, columns=cols))
        s84_te.append(pd.DataFrame(tb, columns=cols))
    s84_tr = pd.concat(s84_tr, axis=1)
    s84_te = pd.concat(s84_te, axis=1)

    X = {"B0": (B0_tr, B0_te)}
    X["R1"] = M82.augment(B0_tr, B0_te, r83_tr, r83_te)
    X["R2"] = M82.augment(B0_tr, B0_te, s84_tr, s84_te)
    r3_tr, r3_te = M82.augment(B0_tr, B0_te, r83_tr, r83_te)
    X["R3"] = M82.augment(r3_tr, r3_te, s84_tr, s84_te)
    return X


def fold_compute(Xs, y, titles, body, NB, P, R83, S84, tr, te, i):
    t0 = time.time()
    X = _fold_matrices(Xs, y, titles, body, NB, P, R83, S84, tr, te)
    ytr, yte = y[tr], y[te]
    pred, dims = {}, {}
    for k, (a, b) in X.items():
        pred[k] = M82.m73_block(a, ytr, b)
        dims[k] = int(a.shape[1])
    rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)), "dims": dims,
           "MAE": {k: round(float(np.abs(p - yte).mean()), 4) for k, p in pred.items()},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "pred": pred, "rec": rec}


def run_split(Xs, y, groups, titles, body, NB, P, R83, S84, verbose=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    fold_id = np.zeros(n, dtype=int)
    pred = {k: np.zeros(n) for k in ("B0", "R1", "R2", "R3")}
    folds = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = fold_compute(Xs, y, titles, body, NB, P, R83, S84, tr, te, i)
        fold_id[fo["te"]] = i
        for k in pred:
            pred[k][fo["te"]] = fo["pred"][k]
        folds.append(fo["rec"])
        if verbose:
            print("   fold %d  %s  dims %s  (%.0fs)"
                  % (i, fo["rec"]["MAE"], fo["rec"]["dims"], fo["rec"]["seconds"]))
    return {"fold_id": fold_id, "pred": pred, "folds": folds}


# ============================================================ main
def main():
    t0 = time.time()
    print("== 데이터 — M82 와 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    NB, body, src = SF.build(d)
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"]))

    # ---------------------------------------------- Exp0 — feature 생성/진단
    print("\n== Exp0 — feature 생성")
    ts = time.time()
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)          # B0 재료
    R83 = build_m83(src)
    S84 = build_m84(src)
    print("   P(M82) %d열 · M83 %d열 · M84 %d열(+섹션 TF-IDF %d×%d) / %.0f초"
          % (len(M82.NUMERIC_COLS + M82.FLAG_COLS), R83.shape[1],
             len([c for c in S84.columns if not c.startswith("sectext_")]),
             len(SEC_TFIDF), SEC_SVD, time.time() - ts))

    rel_cov = {k: round(float(R83["rel_%s_complete" % k].mean()), 4) for k in RELATIONS_RE}
    sec_cov = {s: round(float(S84["sec_has_%s" % s].mean()), 4) for s in SECTION_NAMES}
    print("   M83 relation 성립률 %s" % rel_cov)
    print("   M84 섹션 검출률   %s" % sec_cov)
    print("   M84 헤더 있는 문서 %.1f%% · 평균 헤더 %.1f개"
          % (100 * float((S84["sec_n_headers"] > 0).mean()), float(S84["sec_n_headers"].mean())))

    # ---------------------------------------------- leakage audit
    target_won = d["per_recipient"].to_numpy(dtype=float)
    val_cols = [c for c in R83.columns if c.endswith("_value")]
    leak_val = 0
    for c in val_cols:
        v = R83[c].to_numpy(dtype=float)
        m = np.isfinite(v)
        leak_val += int(np.isclose(v[m], target_won[m], rtol=1e-6).sum())
    digit_res = 0
    for s in SEC_TFIDF:
        digit_res += int(S84["sectext_%s" % s].str.contains(r"\d", regex=True).sum())
    digit_res += int(P["prox_context_text"].str.contains(r"\d", regex=True).sum())
    amt_val_cols = [c for c in R83.columns if c.endswith("_amtdist")]
    print("   leakage — relation 값이 target 과 일치: %d (0 이어야 함)" % leak_val)
    print("   leakage — TF-IDF 문맥에 숫자 잔존 행: %d (0 이어야 함)" % digit_res)
    print("   leakage — 금액은 '거리'만 사용(%d열), 값·자릿수 미사용" % len(amt_val_cols))

    feat = pd.concat([pd.DataFrame({"row_id": d["row_id"].to_numpy()}), R83,
                      S84.drop(columns=["sectext_%s" % s for s in SEC_TFIDF])], axis=1)
    feat.to_parquet(OUT_FEAT, index=False)
    print("   [feat] %s" % OUT_FEAT)

    # ---------------------------------------------- 5-fold × 2 split
    print("\n== B0(M82/P3) · R1(M83) · R2(M84) · R3(M83+M84) — 5-fold [program_stem]")
    Rp = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, R83, S84)
    print("\n== 5-fold [normalized_title] (strict)")
    Rn = run_split(Xs, y, groups["normalized_title"], titles, body, NB, P, R83, S84,
                   verbose=False)

    def summarize(R):
        out = {}
        for k, p in R["pred"].items():
            out[k] = {"MAE_log10": round(float(np.abs(p - y).mean()), 4),
                      "within_2x": round(M82.within_x(y, p, 2), 4),
                      "within_3x": round(M82.within_x(y, p, 3), 4)}
            if k != "B0":
                out[k]["vs_B0"] = M82.paired_test(y, p, R["pred"]["B0"])
                out[k]["fold_wins_vs_B0"] = M82.fold_wins(y, p, R["pred"]["B0"], R["fold_id"])
        return out

    sp, sn = summarize(Rp), summarize(Rn)
    print("\n== 결과 [program_stem]")
    for k in ("B0", "R1", "R2", "R3"):
        b = sp[k]
        ex = ("  Δ%+0.4f CI[%+0.4f,%+0.4f] p=%s fold승%d/5"
              % (b["vs_B0"]["delta_MAE"], b["vs_B0"]["ci95"][0], b["vs_B0"]["ci95"][1],
                 b["vs_B0"]["wilcoxon_p"], b["fold_wins_vs_B0"])) if k != "B0" else ""
        print("   %-3s MAE %.4f  2x %.1f%%  3x %.1f%%%s"
              % (k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"], ex))
    print("\n== 결과 [normalized_title] (strict)")
    for k in ("B0", "R1", "R2", "R3"):
        print("   %-3s MAE %.4f" % (k, sn[k]["MAE_log10"]))

    # ---------------------------------------------- 승격 판정 (지시서 11장)
    best = min(("R1", "R2", "R3"), key=lambda k: sp[k]["MAE_log10"])
    B = sp[best]
    checks = {
        "1. primary MAE < 0.3518 (M82/P3)": B["MAE_log10"] < P3_BASELINE["primary_MAE"],
        "1b. 같은 fold B0 보다 낮다": B["vs_B0"]["delta_MAE"] < 0,
        "2. strict MAE <= 0.3751": sn[best]["MAE_log10"] <= P3_BASELINE["strict_MAE"],
        "3. 4/5 이상 fold 개선": B["fold_wins_vs_B0"] >= 4,
        "4. paired CI < 0": B["vs_B0"]["ci95"][1] < 0,
        "5. leakage audit PASS": (leak_val == 0 and digit_res == 0),
        "6. 실질 기준 ΔMAE <= -0.002": B["vs_B0"]["delta_MAE"] <= PRACTICAL_DELTA,
    }
    print("\n== 재현성 — program_stem 한 번 더 (독립 학습)")
    Rp2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, R83, S84,
                    verbose=False)
    repro = {k: bool(np.array_equal(Rp2["pred"][k], Rp["pred"][k]))
             for k in ("B0", "R1", "R2", "R3")}
    checks["7. reproducibility PASS"] = all(repro.values())
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    verdict = ("승격 후보 (%s)" % best) if all(checks.values()) else "현행 유지 — M82/P3"
    print("\n== 승격 점검표 — 대상 %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
                  "cohort": d["cohort"].to_numpy(),
                  **{"pred_%s" % k: v for k, v in Rp["pred"].items()}}
                 ).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "M82 가 연 수치-맥락 축을 typed relation(M83) · section(M84) 으로 정교화",
        "baseline": "M82/P3 (primary 0.3518 / strict 0.3751)",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"],
                      "rows": fp["rows_after_filters"],
                      "regressor": "M73 soft/ordinal_xgb 재현 블록"},
        "p3_baseline": P3_BASELINE, "m73_baseline": M73_BASELINE,
        "m83": {"relations": {k: v[0] for k, v in RELATIONS.items()},
                "n_cols": int(R83.shape[1]), "relation_coverage": rel_cov,
                "window": REL_WINDOW},
        "m84": {"sections": SECTION_NAMES, "section_coverage": sec_cov,
                "tfidf_sections": SEC_TFIDF, "svd_per_section": SEC_SVD,
                "docs_with_header": round(float((S84["sec_n_headers"] > 0).mean()), 4)},
        "leakage": {"relation_value_vs_target": leak_val,
                    "tfidf_digit_residue": digit_res,
                    "amount_used_as": "거리(위치)만 — 값·자릿수 미사용"},
        "results": {"program_stem": sp, "normalized_title": sn},
        "fold_dims": Rp["folds"][0]["dims"],
        "best_candidate": best,
        "reproducibility": repro,
        "promotion_checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict,
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m83_m84_typed_section_proximity.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    L, a = [], None
    L.append("# M83 / M84 — Typed Proximity Relation · Section-aware Proximity\n")
    L.append("> 질문: **M82 가 연 '수치-맥락' 축을 관계 type(M83)과 문서 섹션(M84)으로 "
             "더 정교하게 만들면 M82/P3(0.3518)를 다시 이기는가?**\n")
    L.append("## 0. 후보\n")
    L.append("```text\nB0  M82/P3 (새 baseline)\nR1  B0 + M83 typed relation\n"
             "R2  B0 + M84 section-aware\nR3  B0 + M83 + M84\n```\n")
    L.append("## 1. feature 진단\n")
    L.append("M83 relation 성립률: `%s`\n" % p["m83"]["relation_coverage"])
    L.append("M84 섹션 검출률: `%s`\n" % p["m84"]["section_coverage"])
    L.append("헤더가 있는 문서: **%.1f%%**\n" % (100 * p["m84"]["docs_with_header"]))
    L.append("leakage — relation 값 target 일치 **%d건** · TF-IDF 숫자 잔존 **%d행** "
             "· 금액은 %s\n" % (p["leakage"]["relation_value_vs_target"],
                              p["leakage"]["tfidf_digit_residue"],
                              p["leakage"]["amount_used_as"]))
    L.append("\n## 2. 결과 [program_stem]\n")
    L.append("| 변형 | MAE | 2x | 3x | Δ vs B0 | 95% CI | fold승 |")
    L.append("|---|---:|---:|---:|---:|---|---:|")
    for k in ("B0", "R1", "R2", "R3"):
        b = p["results"]["program_stem"][k]
        if k == "B0":
            L.append("| B0 (M82/P3) | %.4f | %.1f%% | %.1f%% | — | — | — |"
                     % (b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"]))
        else:
            L.append("| %s | %.4f | %.1f%% | %.1f%% | %+0.4f | [%+0.4f, %+0.4f] | %d/5 |"
                     % (k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"],
                        b["vs_B0"]["delta_MAE"], b["vs_B0"]["ci95"][0],
                        b["vs_B0"]["ci95"][1], b["fold_wins_vs_B0"]))
    L.append("\n## 3. 결과 [normalized_title] (strict)\n")
    L.append("```text")
    for k in ("B0", "R1", "R2", "R3"):
        L.append("%-3s %.4f" % (k, p["results"]["normalized_title"][k]["MAE_log10"]))
    L.append("```\n")
    L.append("## 4. 승격 점검표 — 대상 %s\n" % p["best_candidate"])
    for k, ok in p["promotion_checks"].items():
        L.append("- [%s] %s" % ("x" if ok else " ", k))
    L.append("\n## 판정\n\n```text\n%s\n```\n" % p["verdict"])
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)


if __name__ == "__main__":
    main()
