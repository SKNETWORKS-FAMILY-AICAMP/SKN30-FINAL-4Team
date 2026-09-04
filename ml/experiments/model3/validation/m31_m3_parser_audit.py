"""M31 — 모델 3 hold-out 50건 파서 오류 감사 (계획서 Task 1).

계획서 §0 의 진단: 사람이 '비전형'이라고 본 15건 중 8건이 설계가 드문 게
아니라 값이 잘못 들어온 행이었다. 그러면 지금 재는 것은 이상탐지 성능이
아니라 파서 고장률이다. 모델을 더 만지기 전에 입력을 먼저 고쳐야 한다.

이 스크립트가 하는 일 — **읽기만 한다. 공용 파서를 고치지 않는다.**
    ① 50건의 원천 텍스트를 되찾는다 (taxonomy=규모절 / bizinfo=사업개요+신청방법)
    ② 현재 amount_parser 로 다시 파싱해 저장된 값과 같은지 확인한다(재현성)
    ③ 후보 교정 규칙을 적용해 어떻게 달라지는지 나란히 낸다
    ④ 차이가 난 행에 계획서 §4.1 의 오류 유형을 붙인다

    Task 2 에서 여기 검증된 규칙만 amount_parser.py 로 승격시킨다. 감사와
    수정을 한 커밋에 섞으면 "고치고 나서 고쳤다고 확인"하는 자기증명이 된다.

후보 교정 규칙 — 왜 이렇게 고치는가
    R1 year_as_duration
        PERIOD_RE 의 (\\d{1,2}) 에 왼쪽 경계가 없어 '2026년' 의 뒤 두 자리를
        기간으로 읽는다. search() 라 첫 매치가 이기므로 뒤에 진짜 '보증기간
        5년' 이 있어도 제목의 연도가 먼저 먹는다.
        → 왼쪽 경계 (?<![\\d,.]) 를 넣어 숫자 중간에서 시작하지 못하게 한다.
        → 그 다음 '기간' 문맥이 앞에 있는 매치를 우선한다. 문맥도 없고
          이내/간/동안 힌트도 없으면 값을 만들지 않는다(추측하지 않는다).
          '업력 3년 이상' 같은 자격요건을 사업기간으로 읽는 것을 막는 장치다.

    R2 count_comma
        COUNT_RE 의 (\\d{1,4}) 가 천단위 콤마를 못 읽어 앞자리를 버린다.
        '1,500개사'→500(3배 오류), '3,000개사'→0(0<c 조건에 걸려 유실).
        분모가 망가지면 f06 의 per_recipient = total_budget / count 가 그대로
        폭발한다. M30 이 FN 으로 적은 '기업당 한도 166.7억' 이 이 모양이다.
        → 콤마를 허용해 온전한 수를 잡고, 5000 초과는 버리지 말고 flag 한다.

    R3 duration_range
        위 둘을 고쳐도 남는 값 중 상식 밖(<=0 또는 >10년)은 삭제가 아니라
        review flag 로 둔다. 계획서 §4.3 — 10년 이상이 실제로 가능한 사업이
        있다.

    R4 per_recipient_sanity
        파서가 아니라 f06 파생값 문제라 여기서는 '진단만' 한다.
        total_budget/count 로 만든 기업당 지원액이 common.SANE_RANGE 밖이면
        데이터오류 후보로 표시한다.
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
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from amount_parser import parse_support

HOLDOUT = os.path.join(C.DATA, "labels", "m3_anomaly_holdout_50.csv")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DET = os.path.join(C.PROC, "announcement_detail.parquet")
FEAT = os.path.join(C.PROC, "design_features.parquet")
OUT_CSV = C.report_path("m31_m3_parser_audit.csv")

LABEL_COL = "판단(정상/비전형)"
SUB_COL = "하위유형"


# ------------------------------------------------------------- 후보 교정 규칙
# 연도 표기 2종. 기간 후보에서 걸러낼 때 쓴다.
#   '2026년'  네 자리 그대로
#   "'21년"   아포스트로피 축약 — 실측: "'19~'21년 평균 매출액" 이 21년으로 읽혔다
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*년|['’]\d{2}\s*년")
# 왼쪽 경계를 넣어 '2026년' 의 '26' 에서 시작하지 못하게 한다.
# 소수 기간('0.5년')도 실제로 쓰이므로 받아준다.
PERIOD_V2_RE = re.compile(
    r"(?<![\d,.'’])(\d{1,2}(?:\.\d)?)\s*(?P<u>년|개월)\s*(?P<hint>이내|간|동안|이하|미만)?")
# '기간' 문맥. 매치 앞 window 안에 이게 있으면 사업기간으로 본다.
DURATION_CTX_RE = re.compile(
    r"(?:사업|지원|협약|수행|보증|대출|융자|거치|상환|교육|훈련|파견|근무|계약|"
    r"임차|입주|약정|공급|운영|고용)\s*기간|기간\s*[:：]")
# 자격요건·통계기간에 붙는 '년'. 사업기간이 아니다. 앞 문맥으로 본다.
DISQUALIFY_CTX_RE = re.compile(
    r"업력|창업\s*[후내]|설립\s*[후내]|경력|재직|거주|매출액|이상\s*경과|"
    r"최근\s*\d|연속\s*\d")
# 뒤 문맥으로 드러나는 자격요건. '3년 미만 초기창업기업' 처럼 기간 뒤에 대상이
# 붙으면 그건 사업기간이 아니라 신청자격이다.
#   실측: 달성군 창업패키지의 '3년 미만 초기창업기업' 이 사업기간 3년으로 읽혔다.
DISQUALIFY_AFTER_RE = re.compile(
    r"^\s*(?:미만|이내|이상|이하)?\s*(?:초기|예비|신생)?\s*"
    r"(?:창업\s*)?(?:기업|업체|법인|사업자|소상공인|자|스타트업)")

# 천단위 콤마를 허용하고, 왼쪽 경계로 숫자 중간 시작을 막는다.
#   '개' 단독 대안 뒤에 한글이 오면 단위어다 — (?![가-힣]) 로 잘라낸다.
#   실측: '12개소'(금융회사 수) -> 12, '6개월'(상환기간) -> 6 이 지원기업수로
#   들어가 total_budget/count 를 166억·91.7억으로 폭발시켰다. M30 이 '총액/기업수
#   처리 오류'로 적은 두 건의 진짜 원인은 분자가 아니라 분모였다.
#
# 여기서 멈춘다 — '첫 매치가 이긴다' 는 순서 규칙은 그대로 둔다.
#   한때 '단위가 명시된 매치를 우선하고 맨 개는 문맥을 요구' 하도록 조여 봤다.
#   울산 설계지원(12개월->15개사)·순천 청년일자리(24개월->10개 기업) 두 건은
#   맞혔지만, taxonomy 의 scale_text 는 '50개, 21백만원 이내' 처럼 개수가 문두에
#   오는 관행이라 앞 문맥이 존재하지 않는다. 그 결과 농기자재 50개사·디딤돌
#   900과제·Collabo 375과제 같은 **정답이 통째로 날아갔다.**
#   긴 HWP 안에서 여러 후보 중 무엇이 '그' 지원기업수인지는 파서 버그가 아니라
#   정보검색 문제다. 50건에 맞춰 순서 규칙을 손보면 hold-out 을 튜닝셋으로
#   쓰는 것과 같다(계획서 §11.2). 패턴만 바로잡고 선택 규칙은 남겨 둔다.
COUNT_V2_RE = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{1,5})\s*"
    r"(?:개사내외|개내외|개사|개\s*기업|개\s*과제|개\s*팀|개(?![가-힣]))")

DURATION_SANE = (0.0, 10.0)      # 벗어나면 삭제가 아니라 review flag (계획서 §4.3)
COUNT_SANE = (0, 5000)           # 초과는 flag. 기존 코드는 조용히 버렸다


def period_v2(text):
    """사업기간(년) 과 신뢰등급. 못 찾으면 (None, 사유).

    계획서 §4.3 은 "삭제가 아니라 review flag" 를 지시한다. 그래서 근거가
    약한 값도 버리지 않고 등급만 낮춰 남긴다 — 하류가 등급을 보고 결정한다.

        context     앞에 '사업기간/지원기간/…' 문맥이 있다        (높음)
        hint_only   '이내/간/동안' 힌트만 있다                    (보통)
        bare        맨 숫자+년/개월                               (낮음)

    반대로 **연도 표기와 자격요건은 등급이 아니라 제거**다. 기간이 아닌 것을
    낮은 등급으로 남기면 이상탐지가 그대로 '드문 설계'로 읽는다.
    """
    if not isinstance(text, str) or not text.strip():
        return None, "no_text"
    years = [m.span() for m in YEAR_RE.finditer(text)]
    hits = {}
    for m in PERIOD_V2_RE.finditer(text):
        if any(m.start() >= s and m.end() <= e for s, e in years):
            continue                                    # 연도 표기 자체
        before = text[max(0, m.start() - 20):m.start()]
        if DISQUALIFY_CTX_RE.search(before):
            continue                                    # 업력 3년 이상 등
        has_ctx = bool(DURATION_CTX_RE.search(before))
        # 기간 문맥이 앞에 있으면 뒤에 뭐가 오든 사업기간이다. 문맥이 없을 때만
        # 뒤 문맥으로 자격요건인지 판정한다.
        if not has_ctx and DISQUALIFY_AFTER_RE.match(text[m.end():m.end() + 20]):
            continue                                    # 3년 미만 초기창업기업 등
        v = float(m.group(1))
        v = v if m.group("u") == "년" else round(v / 12, 2)
        tier = ("context" if has_ctx
                else "hint_only" if m.group("hint") else "bare")
        hits.setdefault(tier, v)
    for tier in ("context", "hint_only", "bare"):
        if tier in hits:
            return hits[tier], tier
    return None, "no_duration_evidence"


def count_v2(text):
    """지원 기업/과제 수. 콤마를 읽고, 상한 초과는 버리지 않고 flag.

    선택 규칙(첫 매치)은 기존과 같다. 패턴만 고쳤다 — 위 주석 참조.
    """
    if not isinstance(text, str) or not text.strip():
        return None, False
    for m in COUNT_V2_RE.finditer(text):
        c = int(m.group(1).replace(",", ""))
        if c > 0:
            return c, c > COUNT_SANE[1]
    return None, False


# ------------------------------------------------------------- 원천 텍스트 복구
# 금액축과 기간축은 **서로 다른 텍스트에서** 나온다. 이걸 하나로 뭉치면
# "고쳤더니 달라졌다"가 파서 수정 때문인지 입력이 바뀌어서인지 구분이 안 된다.
#
#   금액·기업수·지원비율
#       taxonomy   F03: parse_support(scale_text)              — 엑셀 '④ 규모' 절
#       bizinfo    F05: parse_support(doc["text"])             — HWP 원문 전체
#                  (원문이 없는 건만 F02 의 summary+reqst 값을 그대로 승계)
#   사업기간(project_duration)
#       taxonomy   F03 이 같은 scale_text 에서 뽑은 값
#       bizinfo    F06 이 announcement_detail 에서 가져온다 = F02 의 summary+reqst
#                  (목록 표본은 F06 이 아예 NaN 을 넣는다 — 감사 대상 아님)
def source_texts():
    """row_id -> dict(cohort, amount_text, duration_text, doc_found)."""
    out = {}
    t = pd.read_parquet(TAX)
    for rid, s in zip(t["row_id"], t["scale_text"]):
        s = s if isinstance(s, str) else ""
        out[rid] = {"cohort": "taxonomy", "amount_text": s,
                    "duration_text": s, "doc_found": None}

    docs = {}
    for path in (C.report_path("e01_documents.jsonl"),):
        if not os.path.exists(path):
            continue
        import json
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("n_chars", 0) <= 0:
                    continue
                if (r.get("ext") or "").lower() == "pdf":   # F05 EXCLUDE_EXT
                    continue
                pid = str(r["announcement_id"])
                if pid not in docs or r["n_chars"] > docs[pid]["n_chars"]:
                    docs[pid] = r

    d = pd.read_parquet(DET)
    detail_text = {str(a): "%s\n%s" % (s if isinstance(s, str) else "",
                                       r if isinstance(r, str) else "")
                   for a, s, r in zip(d["announcement_id"], d["summary_text"],
                                      d["reqst_text"])}
    ids = set(detail_text) | set(docs)
    for pid in ids:
        doc = docs.get(pid)
        out[pid] = {
            "cohort": "bizinfo",
            "amount_text": doc["text"] if doc else detail_text.get(pid, ""),
            "duration_text": detail_text.get(pid, ""),
            "doc_found": bool(doc),
        }
    return out


ERROR_TYPES = {
    "year_as_duration_error": "공고연도를 사업기간으로 읽음 (2026년->26, '21년->21)",
    "duration_unsupported": "기간 근거가 없는데 값이 만들어짐",
    "duration_range_error": "기간이 상식 범위(0~10년) 밖",
    "selected_count_error": "기업수 오파싱 — 단위어 오인(12개소/6개월) 또는 콤마 절단",
    "total_vs_per_recipient_error": "총예산/기업수 파생 기업당액이 상식 범위 밖",
}


def main():
    hold = pd.read_csv(HOLDOUT, encoding="utf-8-sig")
    src = source_texts()
    feat = pd.read_parquet(FEAT).drop_duplicates("row_id").set_index("row_id")

    rows = []
    for _, h in hold.iterrows():
        rid = h["row_id"]
        s = src.get(rid, {"cohort": None, "amount_text": "",
                          "duration_text": "", "doc_found": None})
        a_text, d_text = s["amount_text"], s["duration_text"]
        f = feat.loc[rid] if rid in feat.index else None

        # 저장된 값이 정답이다. 재파싱값과 어긋나면 감사 자체가 성립하지 않으므로
        # 저장값을 기준으로 삼고, 재현 실패는 따로 표시한다.
        old_dur = float(f["project_duration"]) if f is not None and pd.notna(
            f["project_duration"]) else None
        old_cnt = int(f["support_count"]) if f is not None and pd.notna(
            f["support_count"]) else None
        repro = parse_support(a_text)

        new_dur, dur_why = period_v2(d_text)
        new_cnt, cnt_flag = count_v2(a_text)

        errs = []
        # R1 — 연도 오파싱. 저장된 기간이 원문 연도의 뒤 두 자리와 같으면 확정.
        if old_dur is not None:
            yrs = {int(re.sub(r"\D", "", m.group(0))[-2:])
                   for m in YEAR_RE.finditer(d_text)}
            if any(abs(old_dur - y) < 1e-9 for y in yrs):
                errs.append("year_as_duration_error")
            elif not (DURATION_SANE[0] < old_dur <= DURATION_SANE[1]):
                errs.append("duration_range_error")
            if new_dur is None and not errs:
                errs.append("duration_unsupported")
        # R2 — 기업수 절단/유실
        if (old_cnt or None) != (new_cnt or None):
            errs.append("selected_count_error")
        # R4 — 파생 기업당 지원액 상식 검산 (f06 파생값 문제)
        pr = float(f["per_recipient"]) if f is not None and pd.notna(
            f["per_recipient"]) else None
        basis = f["per_recipient_basis"] if f is not None else None
        lo, hi = C.SANE_RANGE["per_company"]
        if pr is not None and not (lo <= pr <= hi):
            errs.append("total_vs_per_recipient_error")

        # 기업수를 고치면 budget_div_count 파생값이 따라 움직인다. 얼마나
        # 회복되는지 보여야 Task 2 의 효과를 판단할 수 있다.
        pr_new = pr
        if basis == "budget_div_count":
            amax = float(f["amount_max"]) if pd.notna(f["amount_max"]) else None
            pr_new = (amax / new_cnt) if (amax and new_cnt) else None
        if pr_new is not None and not (lo <= pr_new <= hi):
            pr_new = None                                # 계획서 §13 DATA_ERROR

        rows.append({
            "row_id": rid,
            "cohort": s["cohort"],
            "원문있음": s["doc_found"],
            "사업명": h["사업명"],
            "사람라벨": h[LABEL_COL],
            "하위유형": h[SUB_COL],
            "기간_원문": (d_text or "").replace("\n", " ")[:200],
            "기간_현재": old_dur,
            "기간_교정": new_dur,
            "기간_근거": dur_why,
            "기업수_현재": old_cnt,
            "기업수_교정": new_cnt,
            "기업수_상한초과": cnt_flag,
            "기업당액_현재": pr,
            "기업당액_교정": pr_new,
            "기업당액_산출": basis,
            "amount_type": f["amount_type"] if f is not None else None,
            "재파싱_금액": repro["support_amount_max"],
            "error_types": "|".join(dict.fromkeys(errs)),
        })

    a = pd.DataFrame(rows)
    a.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("M31 — hold-out 50건 파서 감사")
    print("  [csv] %s" % OUT_CSV)
    print()
    print("== 오류 유형별 건수 (한 행에 여러 개 붙을 수 있음)")
    flat = [e for s in a["error_types"] for e in s.split("|") if e]
    vc = pd.Series(flat).value_counts()
    for k, v in vc.items():
        print("  %-32s %2d건  %s" % (k, v, ERROR_TYPES.get(k, "")))
    n_bad = int((a["error_types"] != "").sum())
    print("\n  오류가 하나라도 있는 행: %d / %d (%.0f%%)"
          % (n_bad, len(a), n_bad / len(a) * 100))

    print("\n== 사람 라벨 x 파서 오류")
    ct = pd.crosstab(a["사람라벨"], a["error_types"] != "")
    ct.columns = ["오류없음", "오류있음"][: ct.shape[1]]
    print(ct.to_string())

    print("\n== 비전형 15건의 내역 재확인")
    biz = a[a["사람라벨"] == "비전형"]
    print("  하위유형: %s" % dict(biz["하위유형"].value_counts()))
    print("  파서오류 있는 건: %d / %d" % (int((biz["error_types"] != "").sum()), len(biz)))

    def changed(col_old, col_new):
        o = pd.to_numeric(a[col_old], errors="coerce")
        n = pd.to_numeric(a[col_new], errors="coerce")
        return a[~((o.isna() & n.isna()) | np.isclose(o, n, equal_nan=True))]

    print("\n== 기간 교정 전후 (달라진 행만)")
    for _, r in changed("기간_현재", "기간_교정").iterrows():
        print("  %-44s %6s -> %-6s (%s)"
              % (str(r["사업명"])[:42], r["기간_현재"], r["기간_교정"], r["기간_근거"]))

    print("\n== 기업수 교정 전후 (달라진 행만)")
    for _, r in changed("기업수_현재", "기업수_교정").iterrows():
        print("  %-44s %6s -> %-6s [%s]"
              % (str(r["사업명"])[:42], r["기업수_현재"], r["기업수_교정"],
                 r["기업당액_산출"]))

    print("\n== 기업당 지원액 교정 전후 (달라진 행만)")
    for _, r in changed("기업당액_현재", "기업당액_교정").iterrows():
        f = lambda v: "%.2f억" % (v / 1e8) if pd.notna(v) else "제거(DATA_ERROR)"
        print("  %-44s %14s -> %s"
              % (str(r["사업명"])[:42], f(r["기업당액_현재"]), f(r["기업당액_교정"])))

    print("\n== 기간 신뢰등급 분포 (교정 후)")
    print("  %s" % dict(a["기간_근거"].value_counts()))

    full = full_corpus_impact(src)
    print("\n== 전체 코퍼스 영향 (hold-out 50건 밖까지) — Task 3 규모 산정")
    for k, v in full.items():
        print("  %-34s %s" % (k, v))

    write_md(a, vc, n_bad, full)

    C.save_report("m31_m3_parser_audit.json", {
        "n_rows": int(len(a)),
        "n_rows_with_error": n_bad,
        "error_counts": {k: int(v) for k, v in vc.items()},
        "error_types": ERROR_TYPES,
        "by_human_label": {str(k): {"n": int(len(g)),
                                    "with_error": int((g["error_types"] != "").sum())}
                           for k, g in a.groupby("사람라벨")},
        "full_corpus_impact": full,
        "csv_path": OUT_CSV,
        "scope": "감사 전용 — amount_parser.py 는 수정하지 않았다 (Task 2 에서 승격)",
    })


def full_corpus_impact(src):
    """50건 밖에서도 같은 버그가 얼마나 터지는지. Task 3 재생성 규모 산정용."""
    feat = pd.read_parquet(FEAT).drop_duplicates("row_id")
    n_dur_bad = n_dur_have = n_cnt_bad = n_cnt_have = 0
    for _, f in feat.iterrows():
        s = src.get(f["row_id"])
        if s is None:
            continue
        if pd.notna(f["project_duration"]):
            n_dur_have += 1
            yrs = {int(re.sub(r"\D", "", m.group(0))[-2:])
                   for m in YEAR_RE.finditer(s["duration_text"])}
            if any(abs(float(f["project_duration"]) - y) < 1e-9 for y in yrs):
                n_dur_bad += 1
        if pd.notna(f["support_count"]):
            n_cnt_have += 1
            new, _ = count_v2(s["amount_text"])
            if new != int(f["support_count"]):
                n_cnt_bad += 1
    lo, hi = C.SANE_RANGE["per_company"]
    pr = pd.to_numeric(feat["per_recipient"], errors="coerce")
    n_pr_bad = int(((pr.notna()) & ~pr.between(lo, hi)).sum())
    return {
        "사업기간 보유행": n_dur_have,
        "  연도 오파싱": "%d건 (%.0f%%)" % (n_dur_bad, n_dur_bad / max(n_dur_have, 1) * 100),
        "지원기업수 보유행": n_cnt_have,
        "  교정 시 변동": "%d건 (%.0f%%)" % (n_cnt_bad, n_cnt_bad / max(n_cnt_have, 1) * 100),
        "기업당액 상식범위 밖": n_pr_bad,
    }

def write_md(a, vc, n_bad, full):
    L = ["# M31 — 모델 3 hold-out 50건 파서 오류 감사", "",
         "> 계획서 Task 1. **읽기만 했습니다 — `amount_parser.py` 는 아직",
         "> 고치지 않았습니다.** 감사와 수정을 한 커밋에 섞으면 \"고치고 나서",
         "> 고쳤다고 확인\"하는 자기증명이 됩니다. Task 2 에서 승격합니다.", "",
         "## 1. 결론", "",
         "계획서는 파서 오류를 1종(`year_as_duration`)으로 봤습니다. **실제로는",
         "4종이고, M30 이 \"총액/기업수 처리 오류\"로 적은 건의 진짜 원인은",
         "분자가 아니라 분모였습니다.**", "",
         "| # | 버그 | 위치 | 실측 |", "|---|---|---|---|",
         "| 1 | 연도 뒤 두 자리를 기간으로 | `PERIOD_RE` `(\\d{1,2})` 에 왼쪽 경계 없음 | `2026년` → 26년 |",
         "| 2 | 축약 연도를 기간으로 | 같은 곳 | `'19~'21년 평균 매출액` → 21년 |",
         "| 3 | 단위어를 기업수로 | `COUNT_RE` 마지막 대안이 맨 `개` | `12개소`·`6개월` → 12·6 |",
         "| 4 | 천단위 콤마 절단 | `COUNT_RE` `(\\d{1,4})` | `1,500개사` → 500 |", "",
         "3번이 `per_recipient = total_budget / support_count` 의 분모를 망가뜨려",
         "기업당 지원액을 166억·91.7억으로 폭발시켰습니다.", "",
         "## 2. hold-out 50건", "",
         "| 오류 유형 | 건수 | 설명 |", "|---|---:|---|"]
    for k, v in vc.items():
        L.append("| `%s` | %d | %s |" % (k, v, ERROR_TYPES.get(k, "")))
    L += ["", "오류가 하나라도 있는 행: **%d / %d (%.0f%%)**"
          % (n_bad, len(a), n_bad / len(a) * 100), "",
          "### 사람 라벨과의 관계", "",
          "| 사람 라벨 | 건수 | 파서 오류 있음 |", "|---|---:|---:|"]
    for k, g in a.groupby("사람라벨"):
        L.append("| %s | %d | %d |" % (k, len(g), int((g["error_types"] != "").sum())))
    L += ["",
          "**정상 17건 중 오류는 2건, 비전형 15건 중 8건입니다.** 사람이 '드문",
          "설계'라고 본 것과 파서가 고장난 곳이 같은 자리에 몰려 있습니다.",
          "지금 재고 있는 것은 이상탐지 성능이 아니라 파서 고장률입니다.", "",
          "## 3. 지원기업수 13건 전수 확인", "",
          "교정으로 값이 바뀐 13건에서 **구 파서가 잡은 값은 하나도 빠짐없이**",
          "**`개`+한글 오탐이었습니다.** 진짜 지원기업수는 한 건도 없었습니다.", "",
          "```text",
          "12개월간 · 33개월 · 12개소 · 3개월 · 1개의 파일 · 6개월 · 1개월간",
          "3개월  · 3개월 · 24개월 · 33개월 · 3개년",
          "```", "",
          "교정 후: 11건은 근거 없음(nan), 2건은 정답 복구",
          "(`수요업체 15개사 내외`, `지원인원 : 10개 기업`).", "",
          "## 4. 전체 코퍼스 영향 — Task 3 규모", "",
          "| 항목 | 값 |", "|---|---|"]
    for k, v in full.items():
        L.append("| %s | %s |" % (k.strip(), v))
    L += ["",
          "**사업기간을 가진 954행의 절반이 공고연도입니다.** 모델 3 는",
          "`project_duration` 을 비교 축으로 쓰므로, 이 축은 지금 사실상",
          "'공고연도 축' 입니다.", "",
          "## 5. 고치지 않기로 한 것", "",
          "긴 HWP 안에 지원기업수 후보가 여럿일 때 무엇이 '그' 값인지는",
          "파서 버그가 아니라 정보검색 문제입니다. 한때 '단위 명시 매치 우선 +",
          "맨 `개` 는 문맥 요구' 로 조여 봤더니 울산·순천 2건은 맞혔지만,",
          "taxonomy 의 `scale_text` 는 `50개, 21백만원 이내` 처럼 개수가 문두에",
          "오는 관행이라 앞 문맥이 없어 **농기자재 50개사·디딤돌 900과제·",
          "Collabo 375과제 같은 정답이 통째로 날아갔습니다.**", "",
          "50건에 맞춰 선택 규칙을 손보면 hold-out 을 튜닝셋으로 쓰는 것과",
          "같습니다(계획서 §11.2). **패턴만 바로잡고 선택 규칙(첫 매치)은",
          "그대로 뒀습니다.**", "",
          "## 6. 다음", "",
          "```text",
          "Task 2  검증된 4개 규칙을 amount_parser.py 로 승격",
          "Task 3  전체 feature 재생성 (F02 -> F03 -> F05 -> F06)",
          "Task 4  50건 relabel 시트 — normal/atypical_design/data_error/uncertain",
          "```", ""]
    p = C.report_path("m31_m3_parser_audit.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
