"""모델 2 전용 금액 타깃 감사·교정 층 (M71).

지시서(사용자, `model2_target_quality_bizinfo_audit_experiment_plan.md`):

    M69 는 코호트별로 taxonomy 0.3292 / bizinfo 0.4182 다. 구조나 텍스트 표현을
    더 바꾸지 말고, **bizinfo 금액 타깃이 원문과 일치하는지 점검하고** 잘못된
    타깃을 수정하거나 불확실한 타깃을 분리한 뒤 M69 를 재학습하라.

## 왜 `amount_parser` 를 고치지 않는가

지시서 1장이 "Model 1, Model 3 는 건드리지 않음"을 첫 원칙으로 뒀다.
`amount_parser` 는 **모델 3(설계 이상탐지)의 관측 파이프라인이 함께 쓰는
공용 파서**다(F05 -> F06 -> M12/M66). 거기를 고치면 모델 2 를 고치려다
모델 3 의 입력이 조용히 바뀐다. 그래서 파서는 그대로 두고, **모델 2 가
타깃을 읽는 자리에만 얹는 재선택 층**을 여기 따로 둔다.

## 이 파일이 보는 것과 보지 않는 것

    본다      원문 텍스트, 금액 후보의 앞뒤 문맥, 행의 구조화 필드
    안 본다   타깃 값 y, OOF 예측, 오차

지시서 7장·11장이 "target 값을 보고 rule 을 만들지 않음"을 못 박았다. 규칙을
**발견한** 표본은 고오차 행이지만(지시서 3장이 그렇게 뽑으라고 지정했다),
규칙 자체는 전부 텍스트 의미다 — 이 파일 어디에서도 y 를 참조하지 않는다.
그래서 규칙이 옳은지는 오차가 줄었는가가 아니라 **근거 문자열을 사람이 보고**
판정해야 하고, 그래서 모든 교정에 `evidence_before` / `evidence_after` 를
같이 남긴다.

## 무엇을 고치기로 했고 무엇을 안 고치기로 했는가

전체 bizinfo 901행에서 문맥 플래그별 발생률과 평균 OOF 오차를 먼저 쟀다.
**고오차와 실제로 연결된 것만** 교정 대상으로 삼았다.

    cost          58행 (6.4%)  평균오차 0.662 (미해당 0.401)  -> 교정
    total_before  27행 (3.0%)  평균오차 0.691 (미해당 0.410)  -> 교정
    anchor 없음  257행 (28.5%) 평균오차 0.471 (해당 0.397)   -> 교정
    fin         101행 (11.2%)  평균오차 0.310 (미해당 0.432)  -> **교정 안 함**
    qual         17행 (1.9%)   평균오차 0.172 (미해당 0.423)  -> **교정 안 함**

`fin`(융자·보증 한도)은 지시서 4장이 지목한 후보였지만 **평균오차가 오히려
낮다.** 융자 사업에서는 대출한도가 곧 지원규모라 그 값이 맞는 값이다. 개별
고오차 사례 몇 건만 보고 일괄 규칙을 만들었다면 101행을 잘못 건드렸을 것이다.
`qual`(매출액 등 신청자격 금액)도 같은 이유로 두었다 — 애초에 파서가 그것을
타깃으로 고른 경우가 드물다.
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

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amount_parser as AP

AUDIT_VERSION = "m2-target-audit-v1"

# ------------------------------------------------------------ 문맥 규칙
# 기업이 **내는** 돈, 또는 설명을 위한 예시 금액. 지원액이 아니다.
#   실측: "기술평가 수수료 : 건당 20만원(기업부담 10만원)" 이 per_project 타깃으로
#         뽑혀 실제 지원(2,300억 융자)과 3.4 log10 어긋났다.
#
# 창을 좁게 잡는 것이 핵심이다. 처음에는 문맥 30자 전체에서 찾았더니
# "설비 교체비용이 10억원 이상인 경우 업체당 최대 5억원 까지 지급" 의 **맞는
# 타깃 5억원**이 '비용이' 때문에 오탐으로 걸려 1천만원짜리 문턱값으로
# 교정됐다. 비용 표지는 **그 숫자가 무엇인지 규정하는 자리**, 곧 숫자
# 바로 앞에 있어야 한다. 앞선 조건절에 있는 것은 다른 숫자의 설명이다.
COST_RE = re.compile(r"수수료|비용\s*[:：은는]|비용\s*발생|기업\s*부담|업체\s*부담|자부담금|"
                     r"본인\s*부담|납부|참가비|분담금|부담금\s*[:：]|"
                     r"예\s*시|\(예\)|예\)\s")
NEAR_WINDOW = 14          # 숫자 바로 앞 몇 자를 '그 숫자를 규정하는 말'로 볼 것인가

# 신청자격을 가르는 금액(매출액·자본금 등). 지원액이 아니다.
QUAL_RE = re.compile(r"매출액|자산\s*총액|자본금|평균\s*매출|연\s*매출|부채|"
                     r"신용\s*등급|자기\s*자본|매출\s*규모")

# 금융 한도. **교정하지 않는다** — 융자 사업에서는 이것이 곧 지원규모다.
# 라벨에만 남겨 하류가 구분할 수 있게 한다.
FIN_RE = re.compile(r"대출\s*한도|보증\s*한도|보증\s*금액|융자\s*한도|이자\s*차액|"
                    r"이차\s*보전|대출\s*금리|보증료|담보")

# 숫자 **바로 앞**의 총액 표지. "총 100억원 내외(과제당 총 50억원)" 에서
# 100억을 과제당 금액으로 읽는 것을 막는다.
TOTAL_BEFORE_RE = re.compile(r"(총|총액|전체|합계)\s*$")

# 단위 앵커. 숫자 **앞쪽에** 있어야 그 숫자의 단위를 규정한다.
ANCHOR_RULES = [
    ("company", r"(기업|업체|개사|사)\s*당|1\s*개\s*기업|참여기업\s*당"),
    ("project", r"(과제|건|프로젝트)\s*당|1\s*건"),
    ("team",    r"팀\s*당"),
    ("person",  r"(1\s*인|인|명)\s*당"),
]
ANCHOR_RE = [(u, re.compile(p)) for u, p in ANCHOR_RULES]
ANCHOR_WINDOW = 40        # 숫자 앞 몇 자까지를 '앵커가 붙었다'고 볼 것인가

LABELS = ["CORRECT", "FIXABLE", "AMBIGUOUS", "NO_EVIDENCE", "SEMANTIC_MISMATCH"]
ERROR_TYPES = ["unit_conversion", "total_vs_per_recipient", "loan_vs_grant",
               "project_vs_recipient", "selected_count_confusion",
               "table_parsing", "multi_amount_candidate", "cost_not_support",
               "evidence_missing", "other"]

# 지시서 8장 V4 의 가중치. 라벨이 곧 신뢰도다.
WEIGHTS = {"CORRECT": 1.0, "FIXABLE": 1.0, "AMBIGUOUS": 0.4,
           "NO_EVIDENCE": 0.0, "SEMANTIC_MISMATCH": 0.0}
# 지시서 8장 V3 이 남기는 라벨
KEEP_LABELS = {"CORRECT", "FIXABLE"}


def _val(c):
    v = c["max"] if c["max"] is not None else c["min"]
    return float(v) if v else np.nan


def annotate(text):
    """원문의 모든 금액 후보에 문맥 플래그를 붙여 돌려준다.

    `AP.extract_amounts` 가 준 후보에 **숫자 앞쪽 문맥**을 새로 붙인다. 파서의
    `_classify_type` 은 앞 30자 + 뒤 10자를 한 창으로 보기 때문에 숫자 뒤에
    오는 '과제당' 까지 근거로 삼는다. 그것이 총액을 단위당 금액으로 읽는
    경로다 — 여기서는 앞뒤를 나눠 본다.
    """
    txt = str(text or "")
    out = []
    for m in AP.AMOUNT_RE.finditer(txt):
        raw = m.group(0)
        pre = txt[max(0, m.start() - ANCHOR_WINDOW):m.start()]
        ctx = txt[max(0, m.start() - 30):m.end() + 10]
        unit = m.group("unit")
        lo = AP._to_won(m.group("lo"), unit)
        hi = AP._to_won(m.group("hi"), unit) if m.group("hi") else None
        if lo is None:
            continue
        if hi is not None:
            vmin, vmax = min(lo, hi), max(lo, hi)
        elif AP.MAX_HINT.search(ctx):
            vmin, vmax = None, lo
        elif AP.MIN_HINT.search(ctx):
            vmin, vmax = lo, None
        else:
            vmin, vmax = lo, lo
        anchor_unit = None
        for u, rx in ANCHOR_RE:
            if rx.search(pre):
                anchor_unit = u
                break
        near = pre[-NEAR_WINDOW:]
        out.append({
            "raw": raw, "min": vmin, "max": vmax, "unit": unit,
            "type": AP._classify_type(txt, m.span())[0],
            "context": ctx.strip(), "pre": pre.strip(),
            "cost": bool(COST_RE.search(near)),
            "qual": bool(QUAL_RE.search(near)),
            "fin": bool(FIN_RE.search(ctx)),
            "total_before": bool(TOTAL_BEFORE_RE.search(pre.rstrip())),
            "anchor_unit": anchor_unit,
        })
    return out


def parser_choice(cands):
    """`AP.parse_support` 가 골랐을 후보 = 현행 타깃. 정렬 규칙을 그대로 쓴다."""
    if not cands:
        return None
    c = sorted(cands, key=lambda x: (AP._PRIORITY.index(x["type"]),
                                     -(x["max"] or x["min"] or 0)))
    return c[0]


def reselect(cands, want_unit):
    """교정 후보를 고른다. 규칙은 셋뿐이고 전부 텍스트 문맥이다.

        1. 기업이 내는 돈·예시 금액(cost)과 신청자격 금액(qual)은 후보에서 뺀다
        2. 숫자 바로 앞이 '총' 이면 단위당 금액이 아니다
        3. 남은 것 중 **단위 앵커가 숫자 앞에 붙은** 후보를 고르고,
           그 앵커가 행의 지원단위와 같으면 우선한다

    같은 앵커가 여럿이면 파서와 같은 규칙(큰 값 우선)을 쓴다 — 여기서 새 기준을
    만들면 그것도 검증되지 않은 규칙이 하나 더 생기는 것이다.
    """
    pool = [c for c in cands if not c["cost"] and not c["qual"] and not c["total_before"]]
    if not pool:
        return None
    anchored = [c for c in pool if c["anchor_unit"]]
    same = [c for c in anchored if c["anchor_unit"] == want_unit]
    for group in (same, anchored):
        if group:
            return max(group, key=lambda c: (c["max"] or c["min"] or 0))
    return None


def audit_row(text, row):
    """한 행의 감사 라벨·오류유형·교정값·근거 (지시서 5·6장)."""
    cands = annotate(text)
    cur = parser_choice(cands)
    out = {
        "n_candidates": len(cands),
        "label": None, "error_type": None,
        "target_current": _val(cur) if cur else np.nan,
        "target_corrected": np.nan,
        "evidence_before": (cur["context"] if cur else ""),
        "evidence_after": "",
        "flag_cost": bool(cur and cur["cost"]),
        "flag_qual": bool(cur and cur["qual"]),
        "flag_fin": bool(cur and cur["fin"]),
        "flag_total_before": bool(cur and cur["total_before"]),
        "flag_anchor": bool(cur and cur["anchor_unit"]),
        "anchor_unit": (cur["anchor_unit"] if cur else None),
    }
    if cur is None:
        out.update(label="NO_EVIDENCE", error_type="evidence_missing")
        return out

    want = row.get("support_unit")
    bad = cur["cost"] or cur["qual"] or cur["total_before"]
    unit_mismatch = bool(cur["anchor_unit"] and want and cur["anchor_unit"] != want)

    # 앵커('기업당' 등)를 요구할지는 **근거문이 무엇인가**에 달렸다.
    #
    #   curated 지원규모 열(taxonomy scale_text, 중앙값 239자)
    #       그 열 전체가 지원규모를 적은 칸이다. "6개, 60~100백만원, 자부담 20%"
    #       처럼 앵커 없이 금액만 적는 것이 정상 표기다. 여기에 앵커를 요구하면
    #       멀쩡한 976행 중 283행이 AMBIGUOUS 로 떨어진다 — 파싱 오류를 세는
    #       것이 아니라 표기 관습을 세게 된다.
    #   공고문 원문(bizinfo document, 중앙값 2,594자)
    #       숫자가 어디에나 있다. 앵커가 없으면 그 숫자가 지원액이라는 근거가
    #       없는 것이 맞다.
    curated = str(row.get("evidence_source") or "") == "scale_text"
    anchor_ok = cur["anchor_unit"] is not None or curated
    out["anchor_required"] = not curated

    if not bad and not unit_mismatch and anchor_ok:
        out.update(label="CORRECT", error_type=None)
        return out

    fix = reselect(cands, want)
    if fix is not None and _val(fix) != _val(cur):
        out.update(label="FIXABLE",
                   target_corrected=_val(fix),
                   evidence_after=fix["context"],
                   error_type=("cost_not_support" if cur["cost"] else
                               "total_vs_per_recipient" if cur["total_before"] else
                               "project_vs_recipient" if unit_mismatch else
                               "multi_amount_candidate"))
        return out
    if bad:
        # 잘못된 후보인 것은 분명한데 대체할 근거가 없다
        out.update(label="SEMANTIC_MISMATCH" if cur["fin"] else "NO_EVIDENCE",
                   error_type=("cost_not_support" if cur["cost"] else
                               "total_vs_per_recipient" if cur["total_before"] else
                               "other"))
        return out
    if len(cands) > 1:
        out.update(label="AMBIGUOUS", error_type="multi_amount_candidate")
        return out
    out.update(label="CORRECT", error_type=None)
    return out


def audit(d, texts):
    """모델링 프레임 전체를 감사한다. 행 순서는 `d` 그대로."""
    rows = [audit_row(t, d.iloc[i]) for i, t in enumerate(texts)]
    A = pd.DataFrame(rows)
    A.insert(0, "row_id", d["row_id"].to_numpy())
    A.insert(1, "cohort", d["cohort"].to_numpy())
    A["weight"] = A["label"].map(WEIGHTS).astype(float)
    A["keep"] = A["label"].isin(KEEP_LABELS)
    return A


def manifest():
    return {
        "audit_version": AUDIT_VERSION,
        "parser_untouched": "amount_parser.py 를 수정하지 않는다 — 모델 3 의 "
                            "관측 파이프라인(F05->F06->M12/M66)이 같은 파서를 쓴다",
        "inputs_seen": ["원문 텍스트", "금액 후보의 앞뒤 문맥", "행의 구조화 필드"],
        "inputs_not_seen": ["타깃 y", "OOF 예측", "오차"],
        "corrected_flags": ["cost", "total_before", "unit anchor 불일치/부재"],
        "not_corrected_flags": {
            "fin": "융자 사업에서는 대출한도가 곧 지원규모다. 전체 101행 평균오차가 "
                   "오히려 낮아(0.310 vs 0.432) 일괄 교정 대상이 아니다",
            "qual": "파서가 신청자격 금액을 타깃으로 고른 경우가 드물다(17행, 평균오차 0.172)",
        },
        "labels": list(LABELS),
        "error_types": list(ERROR_TYPES),
        "weights": dict(WEIGHTS),
        "keep_labels": sorted(KEEP_LABELS),
    }
