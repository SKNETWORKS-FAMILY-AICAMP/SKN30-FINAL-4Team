r"""M85 — Table/Layout-aware feature. 표의 행·열·헤더 관계를 살린다.

지시서(사용자, `m85_table_layout_aware_feature_plan.md`):

    같은 값이라도 표에서 어떤 행/열/헤더와 연결되어 있었는지를 보존하면
    M82/P3 보다 지원규모 예측이 더 좋아지는가?

## 이 실험이 먼저 해결해야 했던 것 — 표가 애초에 남아 있는가

E01 v2 는 '표 구조 보존'을 표방하지만 포맷마다 경로가 다르다. 실측(전체
6,325 문서):

    PDF   markdown 표 복원 85%   <- 구조 있음. 그러나 F05 가 PDF 를 버려
                                   모델링 프레임(1,877행)에는 한 행도 없다
    HWP   markdown 표 0%         <- rhwp.extract_text() 가 평문으로 흘림
    HWPX  markdown 표 0%         <- <hp:t> 를 개행으로 이어 붙여 구조 소실

그리고 모델링 document 행 744개는 **전부 HWP(712)/HWPX(32)** 다. 즉 현재
파이프라인의 텍스트만 보면 표는 0% 다 — 지시서의 중단 기준에 그대로 걸린다.

그런데 그것은 **추출 선택의 결과지 원문에 표가 없다는 뜻이 아니다.**

    HWPX 원본  Contents/section*.xml 에 <hp:tbl>/<hp:tr>/<hp:tc> 가 그대로 있다
    HWP  원본  rhwp 의 `to_ir_json()` 이 kind=table 블록을 rows/cols/cells 로
               내준다 (cell 마다 row·col·row_span·col_span·role·text)

지시서 공통 원칙이 "parser 수정이 필요하면 기존 결과를 덮어쓰지 말고 별도
파생 feature 로 생성"이라고 했으므로, E01/F04/F06 을 건드리지 않고 **원본에서
표만 따로 다시 읽는 파생 층**을 여기 둔다.

## 바꾸지 않는 것

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient) — 손대지 않음
    split      GroupKFold(5), program_stem (엄격 기준 normalized_title)
    baseline   T0 = M82/P3 (primary 0.3518 / strict 0.3751)
    회귀 구조  M73 soft/ordinal_xgb 재현 블록
    문서 선택  F04 와 같은 규칙 — 공고당 n_chars 가 가장 큰 첨부 하나

## leakage

    - 셀 텍스트는 feature 로 쓰기 전 `SF.mask_text` 로 [AMOUNT]/# 치환
    - 표에서 읽는 수치는 %/개수/개월뿐. 원(KRW) 금액은 **위치·존재만** 쓴다
    - digit residue / amount regex residue / target exact-string 3종 감사

산출
    ml/data/processed/m85_tables.parquet        (Exp0 감사 + 표 파생 feature)
    ml/reports/m85_table_layout_features.json / .md
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
import json
import os
import re
import sys
import time
import warnings
import zipfile

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
E01 = C.report_path("e01_documents.jsonl")
ATT_ROOT = os.path.join(C.ROOT, "data", "raw", "attachments")
OUT_TBL = os.path.join(C.PROC, "m85_tables.parquet")
OUT_OOF = os.path.join(C.PROC, "m85_oof.parquet")
MD = C.report_path("m85_table_layout_features.md")

P3_BASELINE = {"primary_MAE": 0.3518, "strict_MAE": 0.3751}
PRACTICAL_DELTA = -0.002
TBL_SVD = 12                    # 표 문맥 TF-IDF -> SVD 성분 수
RARE_MIN = 20                   # row×col pair rare bucket 문턱

# 지시서 Experiment 0 — 복원 여부를 확인할 핵심 header/label
KEY_TERMS = ["지원한도", "지원금액", "사업비", "총사업비", "지원비율", "지원기간",
             "선정규모", "기업당", "과제당", "융자한도", "자부담", "정부지원"]

# 헤더/행라벨 의미 버킷 (지시서 Experiment 2·3)
HEADER_ROLES = [
    ("support_limit", r"지원\s*한도|지원\s*금액|지원\s*규모|보조\s*금액|한도"),
    ("support_rate",  r"지원\s*비율|지원율|보조\s*율|비율|자부담"),
    ("duration",      r"지원\s*기간|사업\s*기간|기간|개월"),
    ("selected",      r"선정\s*규모|선정\s*수|모집\s*규모|개사|업체\s*수"),
    ("budget",        r"사업비|총\s*사업비|예산"),
    ("loan",          r"융자\s*한도|대출\s*한도|융자|대출"),
    ("target",        r"지원\s*대상|신청\s*자격|대상"),
    ("category",      r"구분|분야|유형|사업\s*명"),
]
ROW_ROLES = [
    ("per_company", r"(기업|업체|개사|사)\s*당|1\s*개\s*기업"),
    ("per_project", r"(과제|건|프로젝트)\s*당"),
    ("per_team",    r"팀\s*당|컨소시엄\s*당"),
    ("total",       r"총\s*사업비|총액|합계|전체"),
    ("loan",        r"융자|대출"),
    ("grant",       r"보조금|출연금|지원금"),
    ("gov",         r"정부\s*지원|국비"),
    ("self",        r"자부담|민간\s*부담|본인\s*부담"),
]
HEADER_RE = [(k, re.compile(p)) for k, p in HEADER_ROLES]
ROW_RE = [(k, re.compile(p)) for k, p in ROW_ROLES]

PCT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s*%")
CNT_RE = re.compile(r"\d{1,4}\s*(?:개사|개\s*기업|개\s*과제|개\s*팀)")
MON_RE = re.compile(r"\d{1,3}\s*개월|\d{1,2}\s*년")
AMT_RE = re.compile(r"\d[\d,\.]*\s*(?:천원|만원|백만원|천만원|억원|조원|원)")


# ============================================================ 표 파서
def _cell_text(cell):
    out = []
    for b in cell.get("blocks") or []:
        t = b.get("text")
        if t:
            out.append(t)
    return " ".join(out).strip()


def tables_from_ir(ir):
    """rhwp IR -> [{rows, cols, grid[[text]], roles[[role]], spans}] 목록."""
    tbls = []
    for b in ir.get("body") or []:
        if not isinstance(b, dict) or b.get("kind") != "table":
            continue
        nr, nc = int(b.get("rows") or 0), int(b.get("cols") or 0)
        if nr <= 0 or nc <= 0:
            continue
        grid = [["" for _ in range(nc)] for _ in range(nr)]
        roles = [["" for _ in range(nc)] for _ in range(nr)]
        merged = 0
        for c in b.get("cells") or []:
            r, cc = int(c.get("row", 0)), int(c.get("col", 0))
            rs, cs = int(c.get("row_span", 1)), int(c.get("col_span", 1))
            if rs > 1 or cs > 1:
                merged += 1
            if 0 <= r < nr and 0 <= cc < nc:
                grid[r][cc] = _cell_text(c)
                roles[r][cc] = str(c.get("role") or "")
        tbls.append({"rows": nr, "cols": nc, "grid": grid, "roles": roles,
                     "merged": merged})
    return tbls


def tables_from_hwpx_xml(xml):
    """HWPX section XML -> 표 목록. <hp:tbl>/<hp:tr>/<hp:tc> 를 직접 읽는다."""
    tbls = []
    for tm in re.finditer(r"<hp:tbl\b.*?</hp:tbl>", xml, re.S):
        blob = tm.group(0)
        rows = []
        for rm in re.finditer(r"<hp:tr\b.*?</hp:tr>", blob, re.S):
            cells = []
            for cm in re.finditer(r"<hp:tc\b.*?</hp:tc>", rm.group(0), re.S):
                txt = " ".join(re.findall(r"<hp:t>(.*?)</hp:t>", cm.group(0), re.S))
                for a, bch in [("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                               ("&quot;", '"'), ("&apos;", "'")]:
                    txt = txt.replace(a, bch)
                cells.append(re.sub(r"<[^>]+>", "", txt).strip())
            if cells:
                rows.append(cells)
        if not rows:
            continue
        nc = max(len(r) for r in rows)
        grid = [r + [""] * (nc - len(r)) for r in rows]
        tbls.append({"rows": len(grid), "cols": nc, "grid": grid,
                     "roles": [["" for _ in range(nc)] for _ in grid], "merged": 0})
    return tbls


def parse_tables(path):
    """확장자에 맞는 경로로 표를 읽는다. 실패하면 (None, 사유)."""
    ext = path.rsplit(".", 1)[-1].lower()
    try:
        if ext == "hwpx":
            with zipfile.ZipFile(path) as z:
                secs = sorted(n for n in z.namelist()
                              if re.match(r"Contents/section\d+\.xml$", n))
                xml = "".join(z.read(s).decode("utf-8", "replace") for s in secs)
            return tables_from_hwpx_xml(xml), ""
        import rhwp
        return tables_from_ir(json.loads(rhwp.parse(path).to_ir_json())), ""
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:80])


# ============================================================ 표 -> feature
def _role_of(text, rules):
    for k, rx in rules:
        if rx.search(text):
            return k
    return ""


def table_features(tbls):
    """Exp1 presence/role · Exp2 header-cell · Exp3 row×col pair · Exp4 문맥 텍스트."""
    out = {"tbl_has": int(bool(tbls)), "tbl_n": len(tbls),
           "tbl_rows_sum": 0, "tbl_cols_max": 0, "tbl_cells": 0,
           "tbl_merged": 0, "tbl_header_rows": 0}
    for k, _ in HEADER_ROLES:
        out["tblhdr_%s" % k] = 0
    for k, _ in ROW_ROLES:
        out["tblrow_%s" % k] = 0
    for nm in ("amt", "pct", "cnt", "mon"):
        out["tblval_%s" % nm] = 0
    pairs, ctx, terms = [], [], []

    for t in tbls:
        grid, nr, nc = t["grid"], t["rows"], t["cols"]
        out["tbl_rows_sum"] += nr
        out["tbl_cols_max"] = max(out["tbl_cols_max"], nc)
        out["tbl_cells"] += nr * nc
        out["tbl_merged"] += t["merged"]

        # 헤더 행 = 첫 행. role 이 헤더로 표시돼 있으면 그것을 우선한다.
        hdr = list(grid[0]) if nr else []
        if nr and any("head" in (t["roles"][0][j] or "").lower() for j in range(nc)):
            out["tbl_header_rows"] += 1
        col_role = [_role_of(h, HEADER_RE) for h in hdr]
        terms.extend(h for h in hdr if h)
        for cr in col_role:
            if cr:
                out["tblhdr_%s" % cr] = 1

        for i in range(1, nr):
            row = grid[i]
            rlabel = row[0] if row else ""
            if rlabel:
                terms.append(rlabel)
            rrole = _role_of(rlabel, ROW_RE)
            if rrole:
                out["tblrow_%s" % rrole] = 1
            for j in range(nc):
                cell = row[j] if j < len(row) else ""
                if not cell:
                    continue
                if AMT_RE.search(cell):
                    out["tblval_amt"] += 1
                if PCT_RE.search(cell):
                    out["tblval_pct"] += 1
                if CNT_RE.search(cell):
                    out["tblval_cnt"] += 1
                if MON_RE.search(cell):
                    out["tblval_mon"] += 1
                crole = col_role[j] if j < len(col_role) else ""
                if crole and rrole:
                    pairs.append("%s_X_%s" % (rrole, crole))
                # Exp4 — 관계를 보존한 재구성. 값은 마스킹해서 넣는다.
                if (crole or rrole) and (AMT_RE.search(cell) or PCT_RE.search(cell)
                                         or CNT_RE.search(cell) or MON_RE.search(cell)):
                    ctx.append("ROW[%s] COL[%s] VAL[%s]"
                               % (rrole or "none", crole or "none",
                                  SF.mask_text(cell, cap=120)))
    out["tbl_pairs"] = pairs
    out["tbl_ctx"] = SF.mask_text(" ".join(ctx), cap=2000)
    out["tbl_terms"] = " ".join(terms)[:4000]
    return out


# ============================================================ 문서 인덱스
def doc_index():
    """공고 -> 첨부 경로. F04 와 같은 규칙(n_chars 최대) 로 문서 하나를 고른다."""
    best = {}
    with io.open(E01, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("ext") or "").lower() not in ("hwp", "hwpx"):
                continue
            if int(r.get("n_chars") or 0) <= 0:
                continue
            a = str(r.get("announcement_id") or "")
            if a not in best or r["n_chars"] > best[a]["n_chars"]:
                best[a] = r
    idx = {}
    for a, r in best.items():
        p = os.path.join(ATT_ROOT, str(r.get("source") or ""), a,
                         str(r.get("filename") or ""))
        idx[a] = {"path": p, "ext": (r.get("ext") or "").lower(),
                  "n_chars": int(r["n_chars"])}
    return idx


# ============================================================ 모델 fold
def _fold(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i):
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                M82.STEP, [None])
    p_tr = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_te = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[te].reset_index(drop=True)
    a, b = M82.augment(Xb_tr, Xb_te, p_tr, p_te)
    sv_tr, sv_te = M82.fit_prox_svd(P["prox_context_text"].to_numpy()[tr],
                                    P["prox_context_text"].to_numpy()[te])
    nm = ["proxsvd%02d" % j for j in range(sv_tr.shape[1])]
    T0_tr, T0_te = M82.augment(a, b, pd.DataFrame(sv_tr, columns=nm),
                               pd.DataFrame(sv_te, columns=nm))

    def blk(df):
        return df.iloc[tr].reset_index(drop=True), df.iloc[te].reset_index(drop=True)

    X = {"T0": (T0_tr, T0_te)}
    for name, df in (("T1", T1), ("T2", T2), ("T3", T3)):
        atr, ate = blk(df)
        X[name] = M82.augment(T0_tr, T0_te, atr, ate)
    # T4 — 표 문맥 TF-IDF/SVD (fold-local 적합)
    ta, tb = M82.fit_prox_svd(T4txt[tr], T4txt[te], n_components=TBL_SVD)
    tn = ["tblsvd%02d" % j for j in range(ta.shape[1])]
    X["T4"] = M82.augment(T0_tr, T0_te, pd.DataFrame(ta, columns=tn),
                          pd.DataFrame(tb, columns=tn))
    # T5 — 구조 feature 전부 + 표 임베딩
    all_tr = pd.concat([blk(T1)[0], blk(T2)[0], blk(T3)[0],
                        pd.DataFrame(ta, columns=tn)], axis=1)
    all_te = pd.concat([blk(T1)[1], blk(T2)[1], blk(T3)[1],
                        pd.DataFrame(tb, columns=tn)], axis=1)
    X["T5"] = M82.augment(T0_tr, T0_te, all_tr, all_te)

    ytr, yte = y[tr], y[te]
    pred, dims = {}, {}
    for k, (aa, bb) in X.items():
        pred[k] = M82.m73_block(aa, ytr, bb)
        dims[k] = int(aa.shape[1])
    rec = {"fold": i, "dims": dims,
           "MAE": {k: round(float(np.abs(p - yte).mean()), 4) for k, p in pred.items()},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "pred": pred, "rec": rec}


def run_split(Xs, y, groups, titles, body, NB, P, T1, T2, T3, T4txt, verbose=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    keys = ("T0", "T1", "T2", "T3", "T4", "T5")
    pred = {k: np.zeros(n) for k in keys}
    fold_id = np.zeros(n, dtype=int)
    folds = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = _fold(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i)
        fold_id[fo["te"]] = i
        for k in keys:
            pred[k][fo["te"]] = fo["pred"][k]
        folds.append(fo["rec"])
        if verbose:
            print("   fold %d  %s  (%.0fs)" % (i, fo["rec"]["MAE"], fo["rec"]["seconds"]))
    return {"fold_id": fold_id, "pred": pred, "folds": folds}


# ============================================================ main
# ============================================================ feature 블록
def build_blocks(d, verbose=True):
    """Exp0 감사 + T1~T4 feature 블록. screening 과 full validation 이
    같은 블록을 쓰도록 한 곳에서만 만든다."""
    # ---------------------------------------------- Exp0 — Table Recoverability
    print("\n== Exp0 — Table Recoverability Audit (원본 HWP/HWPX 재파싱)")
    idx = doc_index()
    is_doc = (d["evidence_source"] == "document").to_numpy()
    rows, fails, ts = [], [], time.time()
    for i, rid in enumerate(d["row_id"].astype(str).to_numpy()):
        info = idx.get(rid) if is_doc[i] else None
        if info is None or not os.path.exists(info["path"]):
            rows.append({"_ext": "", "_ok": 0, **table_features([])})
            continue
        tbls, err = parse_tables(info["path"])
        if tbls is None:
            fails.append((rid, err))
            rows.append({"_ext": info["ext"], "_ok": 0, **table_features([])})
            continue
        rows.append({"_ext": info["ext"], "_ok": 1, **table_features(tbls)})
        if (i + 1) % 300 == 0:
            print("   ... %d행 (%.0f초)" % (i + 1, time.time() - ts))
    TB = pd.DataFrame(rows)
    print("   표 파싱 %.0f초 / 실패 %d건" % (time.time() - ts, len(fails)))

    doc_n = int(is_doc.sum())
    parsed = TB["_ok"].to_numpy() == 1
    has_t = (TB["tbl_has"].to_numpy() == 1)
    audit = {
        "modeling_rows": int(len(d)), "document_rows": doc_n,
        "ext_mix": TB.loc[is_doc, "_ext"].value_counts().to_dict(),
        "table_parse_failure_rate": round(float(len(fails) / max(1, doc_n)), 4),
        "docs_with_tables": int(has_t.sum()),
        "table_detect_coverage_all_rows": round(float(has_t.mean()), 4),
        "table_detect_coverage_document_rows":
            round(float(has_t[is_doc].mean()), 4) if doc_n else 0.0,
        "avg_tables_per_doc": round(float(TB.loc[has_t, "tbl_n"].mean()), 2) if has_t.any() else 0,
        "avg_rows": round(float(TB.loc[has_t, "tbl_rows_sum"].mean()), 2) if has_t.any() else 0,
        "avg_cols": round(float(TB.loc[has_t, "tbl_cols_max"].mean()), 2) if has_t.any() else 0,
        "cell_count": int(TB["tbl_cells"].sum()),
        "merged_cells": int(TB["tbl_merged"].sum()),
    }
    hdr_cols = ["tblhdr_%s" % k for k, _ in HEADER_ROLES]
    row_cols = ["tblrow_%s" % k for k, _ in ROW_ROLES]
    audit["header_detect_rate"] = round(float((TB[hdr_cols].sum(axis=1) > 0)[has_t].mean()), 4) \
        if has_t.any() else 0.0
    audit["row_label_detect_rate"] = round(float((TB[row_cols].sum(axis=1) > 0)[has_t].mean()), 4) \
        if has_t.any() else 0.0
    audit["header_roles"] = {k: int(TB["tblhdr_%s" % k].sum()) for k, _ in HEADER_ROLES}
    audit["row_roles"] = {k: int(TB["tblrow_%s" % k].sum()) for k, _ in ROW_ROLES}
    audit["values_in_tables"] = {nm: int(TB["tblval_%s" % nm].sum())
                                 for nm in ("amt", "pct", "cnt", "mon")}
    audit["rows_with_amount_in_table"] = int((TB["tblval_amt"] > 0).sum())

    print("   document 행 %d (%s)" % (doc_n, audit["ext_mix"]))
    print("   표 있는 행 %d — document 행 기준 %.1f%% / 전체 %.1f%%"
          % (audit["docs_with_tables"], 100 * audit["table_detect_coverage_document_rows"],
             100 * audit["table_detect_coverage_all_rows"]))
    print("   평균 표 %.1f개 · 행합 %.1f · 최대열 %.1f · 셀 %d (병합 %d)"
          % (audit["avg_tables_per_doc"], audit["avg_rows"], audit["avg_cols"],
             audit["cell_count"], audit["merged_cells"]))
    print("   header 복원율 %.1f%% · row label 복원율 %.1f%%"
          % (100 * audit["header_detect_rate"], 100 * audit["row_label_detect_rate"]))
    print("   header role 분포 %s" % audit["header_roles"])
    print("   row label 분포   %s" % audit["row_roles"])
    print("   표 안 수치 %s · 금액이 표에 있는 행 %d"
          % (audit["values_in_tables"], audit["rows_with_amount_in_table"]))

    # 핵심 용어 복원율 (표 안에서)
    term_hit = {}
    for term in KEY_TERMS:
        rx = re.compile(term.replace("", "").replace(" ", r"\s*"))
        term_hit[term] = int(sum(1 for r in rows
                                 if rx.search(r.get("tbl_terms", "") or "")))
    audit["key_terms_in_table_headers"] = term_hit
    print("   핵심 용어 표 헤더/행라벨 복원 %s" % term_hit)

    # ---------------------------------------------- leakage audit
    tgt_won = d["per_recipient"].to_numpy(dtype=float)
    ctx_txt = pd.Series([r.get("tbl_ctx", "") or "" for r in rows])
    leak = {
        "digit_residue_rows": int(ctx_txt.str.contains(r"\d", regex=True).sum()),
        "amount_regex_residue_rows": int(ctx_txt.str.contains(AMT_RE.pattern, regex=True).sum()),
        "target_exact_string_rows": int(sum(
            1 for t, v in zip(ctx_txt, tgt_won)
            if np.isfinite(v) and str(int(v)) in t)),
    }
    print("   leakage — digit %d · amount regex %d · target exact-string %d (모두 0)"
          % (leak["digit_residue_rows"], leak["amount_regex_residue_rows"],
             leak["target_exact_string_rows"]))

    # ---------------------------------------------- 진행/중단 판정 (지시서)
    go = (audit["table_detect_coverage_document_rows"] >= 0.30
          and audit["header_detect_rate"] >= 0.30
          and audit["row_label_detect_rate"] >= 0.20)
    print("\n== Exp0 진행 판정: %s" % ("진행" if go else "중단"))

    T4txt = ctx_txt.to_numpy()
    pair_series = pd.Series([r.get("tbl_pairs", []) for r in rows])
    TB2 = TB.drop(columns=["tbl_pairs", "tbl_ctx", "tbl_terms", "_ext", "_ok"])
    feat = pd.concat([pd.DataFrame({"row_id": d["row_id"].to_numpy()}), TB2], axis=1)
    feat["tbl_ctx"] = ctx_txt
    feat.to_parquet(OUT_TBL, index=False)
    print("   [tables] %s" % OUT_TBL)

    if not go:
        return {"audit": audit, "leak": leak, "go": False, "has_t": has_t, "TB": TB}

    # ---------------------------------------------- 후보 feature 블록
    t1_cols = ["tbl_has", "tbl_n", "tbl_rows_sum", "tbl_cols_max", "tbl_cells",
               "tbl_merged"]
    T1 = TB[t1_cols].astype(float)
    T2 = TB[hdr_cols + row_cols + ["tblval_amt", "tblval_pct", "tblval_cnt",
                                   "tblval_mon", "tbl_header_rows"]].astype(float)
    # Exp3 — row×col pair. 표본 적은 pair 는 rare 버킷.
    cnt = {}
    for ps in pair_series:
        for p in set(ps):
            cnt[p] = cnt.get(p, 0) + 1
    keep = sorted([k for k, v in cnt.items() if v >= RARE_MIN])
    print("\n   row×col pair 종류 %d개 중 n>=%d 인 %d개 사용"
          % (len(cnt), RARE_MIN, len(keep)))
    T3 = pd.DataFrame({("pair_%s" % k): [int(k in set(ps)) for ps in pair_series]
                       for k in keep}, dtype=float)
    T3["pair_rare_n"] = [sum(1 for p in set(ps) if p not in set(keep)) for ps in pair_series]
    return {"audit": audit, "leak": leak, "go": True, "has_t": has_t, "TB": TB,
            "T1": T1, "T2": T2, "T3": T3, "T4txt": T4txt,
            "pair_kept": keep, "pair_total_kinds": len(cnt)}


def main():
    t0 = time.time()
    print("== 데이터")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    NB, body, src = SF.build(d)
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)
    print("   %s / 행 %d" % (fp["path"], fp["rows_after_filters"]))

    # ---------------------------------------------- Exp0 + feature 블록
    BK = build_blocks(d)
    audit, leak, go, has_t, TB = BK["audit"], BK["leak"], BK["go"], BK["has_t"], BK["TB"]
    payload = {
        "purpose": "표의 행·열·헤더 관계를 feature 로 살려 M82/P3 를 이기는가",
        "extraction_finding": {
            "note": "현재 파이프라인 텍스트에는 표가 남아 있지 않다. 원본 재파싱으로 복원했다.",
            "markdown_table_rate_in_e01": {"pdf": 0.85, "hwp": 0.0, "hwpx": 0.0},
            "modeling_document_rows_ext": audit["ext_mix"],
            "recovery_path": {"hwp": "rhwp to_ir_json (kind=table)",
                              "hwpx": "Contents/section*.xml <hp:tbl>"},
        },
        "exp0_audit": audit, "leakage": leak, "proceed": bool(go),
        "p3_baseline": P3_BASELINE,
    }
    if not go:
        payload["verdict"] = "중단 — 표 복원율이 지시서 진행 기준 미달"
        C.save_report("m85_table_layout_features.json", payload)
        write_md(payload)
        return payload
    T1, T2, T3, T4txt = BK["T1"], BK["T2"], BK["T3"], BK["T4txt"]
    payload["pair_kept"] = BK["pair_kept"]
    payload["pair_total_kinds"] = BK["pair_total_kinds"]

    # ---------------------------------------------- 5-fold × 2 split
    print("\n== T0~T5 — 5-fold [program_stem]")
    Rp = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, T1, T2, T3, T4txt)
    print("\n== 5-fold [normalized_title] (strict)")
    Rn = run_split(Xs, y, groups["normalized_title"], titles, body, NB, P, T1, T2, T3,
                   T4txt, verbose=False)

    coh = d["cohort"].to_numpy()
    ext_arr = TB["_ext"].to_numpy() if "_ext" in TB.columns else np.array([""] * len(d))

    def summarize(R):
        out = {}
        for k, p in R["pred"].items():
            e = {"MAE_log10": round(float(np.abs(p - y).mean()), 4),
                 "within_2x": round(M82.within_x(y, p, 2), 4),
                 "within_3x": round(M82.within_x(y, p, 3), 4)}
            for c in ("taxonomy", "bizinfo"):
                m = coh == c
                e["MAE_%s" % c] = round(float(np.abs(p[m] - y[m]).mean()), 4)
            for nm, m in (("table_present", has_t), ("table_absent", ~has_t)):
                if m.sum():
                    e["MAE_%s" % nm] = round(float(np.abs(p[m] - y[m]).mean()), 4)
            for nm in ("hwp", "hwpx"):
                m = ext_arr == nm
                if m.sum():
                    e["MAE_%s" % nm] = round(float(np.abs(p[m] - y[m]).mean()), 4)
            if k != "T0":
                e["vs_T0"] = M82.paired_test(y, p, R["pred"]["T0"])
                e["fold_wins_vs_T0"] = M82.fold_wins(y, p, R["pred"]["T0"], R["fold_id"])
            out[k] = e
        return out

    sp, sn = summarize(Rp), summarize(Rn)
    print("\n== 결과 [program_stem]")
    for k in ("T0", "T1", "T2", "T3", "T4", "T5"):
        b = sp[k]
        ex = ("  Δ%+0.4f CI[%+0.4f,%+0.4f] p=%s fold승%d/5"
              % (b["vs_T0"]["delta_MAE"], b["vs_T0"]["ci95"][0], b["vs_T0"]["ci95"][1],
                 b["vs_T0"]["wilcoxon_p"], b["fold_wins_vs_T0"])) if k != "T0" else ""
        print("   %-3s MAE %.4f  2x %.1f%%  3x %.1f%%  [표있음 %.4f / 표없음 %.4f]%s"
              % (k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"],
                 b.get("MAE_table_present", float("nan")),
                 b.get("MAE_table_absent", float("nan")), ex))
    print("\n== 결과 [normalized_title] (strict)")
    for k in ("T0", "T1", "T2", "T3", "T4", "T5"):
        print("   %-3s MAE %.4f" % (k, sn[k]["MAE_log10"]))

    best = min(("T1", "T2", "T3", "T4", "T5"), key=lambda k: sp[k]["MAE_log10"])
    B = sp[best]
    print("\n== 재현성 — program_stem 한 번 더")
    Rp2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, T1, T2, T3,
                    T4txt, verbose=False)
    repro = {k: bool(np.array_equal(Rp2["pred"][k], Rp["pred"][k]))
             for k in Rp["pred"]}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # 포맷 편중 — hwp/hwpx 어느 한쪽에만 이득이 몰리지 않았는가
    gain_hwp = sp["T0"].get("MAE_hwp", np.nan) - B.get("MAE_hwp", np.nan)
    gain_hwpx = sp["T0"].get("MAE_hwpx", np.nan) - B.get("MAE_hwpx", np.nan)
    checks = {
        "1. Primary MAE < 0.3518": B["MAE_log10"] < P3_BASELINE["primary_MAE"],
        "1b. 같은 fold T0 보다 낮다": B["vs_T0"]["delta_MAE"] < 0,
        "2. strict MAE <= 0.3751": sn[best]["MAE_log10"] <= P3_BASELINE["strict_MAE"],
        "3. 최소 4/5 fold 개선": B["fold_wins_vs_T0"] >= 4,
        "4. paired 95% CI < 0": B["vs_T0"]["ci95"][1] < 0,
        "5. leakage audit PASS": all(v == 0 for v in leak.values()),
        "6. table coverage 충분": bool(go),
        "7. 특정 포맷에만 이득이 몰리지 않음":
            bool(np.isfinite(gain_hwp) and np.isfinite(gain_hwpx)
                 and gain_hwp > 0 and gain_hwpx > 0),
        "8. reproducibility PASS": all(repro.values()),
        "9. 실질 기준 ΔMAE <= -0.002": B["vs_T0"]["delta_MAE"] <= PRACTICAL_DELTA,
    }
    verdict = ("승격 후보 (%s)" % best) if all(checks.values()) else "현행 유지 — M82/P3"
    print("\n== 승격 점검표 — 대상 %s" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
                  "cohort": coh, "has_table": has_t.astype(int),
                  **{"pred_%s" % k: v for k, v in Rp["pred"].items()}}
                 ).to_parquet(OUT_OOF, index=False)
    payload.update({
        "results": {"program_stem": sp, "normalized_title": sn},
        "fold_dims": Rp["folds"][0]["dims"], "best_candidate": best,
        "reproducibility": repro,
        "format_gain": {"hwp": (None if not np.isfinite(gain_hwp) else round(float(gain_hwp), 4)),
                        "hwpx": (None if not np.isfinite(gain_hwpx) else round(float(gain_hwpx), 4))},
        "promotion_checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict, "seconds": round(time.time() - t0, 1),
    })
    C.save_report("m85_table_layout_features.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    a = p["exp0_audit"]
    L = ["# M85 — Table/Layout-aware Feature\n",
         "> 질문: **같은 값이라도 표에서 어떤 행/열/헤더와 연결돼 있었는지를 "
         "보존하면 M82/P3(0.3518)보다 좋아지는가?**\n",
         "## 0. 먼저 확인한 것 — 표가 남아 있는가\n",
         "E01 텍스트 기준 markdown 표 복원율: PDF **85%** / HWP **0%** / HWPX **0%**. "
         "그런데 모델링 document 행 %d개는 **전부 HWP/HWPX**(`%s`)라 현재 텍스트에는 "
         "표가 한 건도 없다.\n" % (a["document_rows"], a["ext_mix"]),
         "원본을 다시 읽어 복원했다 — HWP `rhwp.to_ir_json()`(kind=table), "
         "HWPX `Contents/section*.xml`의 `<hp:tbl>`.\n",
         "## 1. Exp0 — Table Recoverability Audit\n",
         "| 항목 | 값 |\n|---|---:|",
         "| document 행 | %d |" % a["document_rows"],
         "| 표가 있는 행 | %d |" % a["docs_with_tables"],
         "| table coverage (document 행) | %.1f%% |" % (100 * a["table_detect_coverage_document_rows"]),
         "| table coverage (전체 1,877행) | %.1f%% |" % (100 * a["table_detect_coverage_all_rows"]),
         "| 평균 표 수 | %.1f |" % a["avg_tables_per_doc"],
         "| 평균 행 합 | %.1f |" % a["avg_rows"],
         "| 평균 최대 열 | %.1f |" % a["avg_cols"],
         "| 총 셀 수 | %d |" % a["cell_count"],
         "| 병합 셀 | %d |" % a["merged_cells"],
         "| header 복원율 | %.1f%% |" % (100 * a["header_detect_rate"]),
         "| row label 복원율 | %.1f%% |" % (100 * a["row_label_detect_rate"]),
         "| parse 실패율 | %.1f%% |" % (100 * a["table_parse_failure_rate"]),
         "\nheader role: `%s`\n" % a["header_roles"],
         "row label: `%s`\n" % a["row_roles"],
         "표 안 수치: `%s`\n" % a["values_in_tables"],
         "leakage: digit **%d** · amount regex **%d** · target exact-string **%d** (모두 0이어야 통과)\n"
         % (p["leakage"]["digit_residue_rows"], p["leakage"]["amount_regex_residue_rows"],
            p["leakage"]["target_exact_string_rows"])]
    if not p.get("proceed"):
        L += ["\n## 판정\n\n```text\n%s\n```\n" % p["verdict"]]
        with open(MD, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        print("   [md] %s" % MD)
        return
    L += ["\n## 2. 결과 [program_stem]\n",
          "| 변형 | MAE | 2x | 3x | 표있음 | 표없음 | Δ vs T0 | 95% CI | fold승 |",
          "|---|---:|---:|---:|---:|---:|---:|---|---:|"]
    for k in ("T0", "T1", "T2", "T3", "T4", "T5"):
        b = p["results"]["program_stem"][k]
        base = "| %s | %.4f | %.1f%% | %.1f%% | %.4f | %.4f |" % (
            k, b["MAE_log10"], 100 * b["within_2x"], 100 * b["within_3x"],
            b.get("MAE_table_present", float("nan")), b.get("MAE_table_absent", float("nan")))
        if k == "T0":
            L.append(base + " — | — | — |")
        else:
            L.append(base + " %+0.4f | [%+0.4f, %+0.4f] | %d/5 |" % (
                b["vs_T0"]["delta_MAE"], b["vs_T0"]["ci95"][0], b["vs_T0"]["ci95"][1],
                b["fold_wins_vs_T0"]))
    L += ["\n## 3. strict\n", "```text"]
    for k in ("T0", "T1", "T2", "T3", "T4", "T5"):
        L.append("%-3s %.4f" % (k, p["results"]["normalized_title"][k]["MAE_log10"]))
    L += ["```\n", "## 4. 승격 점검표 — 대상 %s\n" % p["best_candidate"]]
    for k, ok in p["promotion_checks"].items():
        L.append("- [%s] %s" % ("x" if ok else " ", k))
    L += ["\n## 판정\n\n```text\n%s\n```\n" % p["verdict"]]
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)


if __name__ == "__main__":
    main()
