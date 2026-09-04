"""F06 — 모델 2~4 공통 '사업 설계' feature 테이블.

모델 2(설계유형 군집)·3(지원규모 상대비교)·4(설계 이상탐지)는 모두
"이 사업이 어떻게 설계됐는가"를 수치·범주로 읽는다. 세 스크립트가 각자
전처리하면 결과가 어긋나므로 여기서 한 번만 만든다.

두 코호트를 나눠 담는다. 합치지 않는 이유는 feature 구성이 근본적으로 다르기 때문이다.

    taxonomy (2023 중앙부처 엑셀 1,505건)
        업종·정책목적·지원대상·지원규모 원문이 전부 있다. 지원성격도 실측 라벨이다.
        모델 2·4 의 학습 코호트.

    bizinfo (Open API + 목록 표본)
        원문에서 금액만 건졌다. 업종·정책목적이 없고 지원성격은 모델 1 예측이다.
        모델 3 의 관측 보강, 모델 4 의 적용 대상.

여기서 새로 파생하는 것 (원본에 없던 것)
    support_method    grant/loan/guarantee/voucher/service/mixed/other
    support_unit      company/project/team/person
    per_recipient     기업(과제)당 실지원액 — amount_type 별로 계산식이 다르다
    agency_type       central/local/public

support_method 를 지원성격이 아니라 '원문 텍스트'에서 먼저 뽑는 이유:
    설계서는 비교군을 '지원성격 + 지원방식' 2단으로 자르라고 한다. 그런데
    지원방식을 지원성격에서 유도하면 두 축이 같은 축이 되어 비교군이
    1단으로 붕괴한다. 텍스트에서 독립적으로 뽑은 뒤, 두 축이 실제로
    얼마나 겹치는지(Cramer's V)를 s04e 에서 측정해 판단 근거로 남긴다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amount_parser as AP
import common as C
from m01_support_type import coarsen

TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DET = os.path.join(C.PROC, "announcement_detail_with_support_type_v2.parquet")
OBS = os.path.join(C.PROC, "support_amount_observations.parquet")
LST = os.path.join(C.PROC, "list_sample_support_type.parquet")
OUT = os.path.join(C.PROC, "design_features.parquet")
OUT_V2 = os.path.join(C.PROC, "design_features_v2.parquet")


# ------------------------------------------------------------- 지원방식
# 순서가 곧 우선순위다. 금융수단(융자·보증·바우처)은 표현이 명확해 오탐이 적고,
# 서비스형은 '컨설팅 비용을 현금 지원'하는 사업과 헷갈려 마지막에 둔다.
METHOD_RULES = [
    ("loan",      r"융자|대출|이차보전|정책자금|운전자금|시설자금|상환|금리|대여"),
    ("guarantee", r"보증서|신용보증|기술보증|보증\s*지원|보증료|보험료\s*지원|수출보험"),
    ("voucher",   r"바우처|이용권|쿠폰|포인트\s*지급"),
    ("service",   r"컨설팅|멘토링|교육|연수|훈련|상담|자문|진단|시험\s*분석|시험\s*·?\s*평가|"
                  r"입주\s*공간|보육|알선|매칭\s*상담|대행|번역|인증\s*취득\s*지원"),
    ("grant",     r"보조금|출연금|지원금|사업비|자부담|국비|시상금|장려금|수당"),
]
METHOD_RE = [(m, re.compile(p)) for m, p in METHOD_RULES]
# 현금이 기업에 넘어갔다는 증거. 지원규모 원문에만 찾는다 — 사업내용 설명문에는
# '사업비'가 문맥 없이 등장해 서비스형 사업까지 grant 로 끌어온다.
CASH_RE = re.compile(r"자부담|보조금|출연금|지원금|사업비|국비|시상금|장려금|수당|한도")


def derive_method(scale_text, context_text, amount_type):
    """지원방식을 뽑는다.

    두 텍스트를 역할을 나눠 쓴다.
        scale_text   지원규모 원문 — '기업이 돈을 받는가'의 1차 증거
        context_text 사업내용·목적 — '무엇을 제공하는가'의 증거

    한 텍스트에 둘 다 넣으면 교육사업 설명문의 '사업비'가 잡혀 mixed 로 뭉개진다.
    """
    scale = scale_text if isinstance(scale_text, str) else ""
    ctx = context_text if isinstance(context_text, str) else ""
    both = scale + " \n" + ctx
    if not both.strip():
        return "other", []

    hits = [m for m, rx in METHOD_RE if rx.search(both)]
    # 금융수단은 표현이 명확해 오탐이 적다. 잡히면 그것이 사업의 성격을 규정한다
    # (융자사업 설명문에도 '사업비'는 등장한다).
    for m in ("loan", "guarantee", "voucher"):
        if m in hits:
            return m, hits

    cash = bool(CASH_RE.search(scale)) or amount_type in (
        "per_company", "per_project", "total_budget")
    service = "service" in hits
    if cash and service:
        # 현금이 확정돼 있으면 서비스는 사용처일 뿐 지급수단이 아니다.
        # 금액 근거가 없는 채로 둘 다 잡힌 경우만 진짜 혼합으로 본다.
        return ("grant" if amount_type in ("per_company", "per_project", "total_budget")
                else "mixed"), hits
    if cash:
        return "grant", hits
    if service:
        return "service", hits
    return "other", hits


# ------------------------------------------------------------- 지원단위
UNIT_RULES = [
    ("company", r"개사|개\s*기업|기업\s*당|업체\s*당|사\s*당|참여기업"),
    ("project", r"개\s*과제|과제\s*당|건\s*당|\d\s*건"),
    ("team",    r"개\s*팀|팀\s*당"),
    ("person",  r"\d\s*명|1\s*인\s*당|인\s*당|명\s*당"),
]
UNIT_RE = [(u, re.compile(p)) for u, p in UNIT_RULES]
TYPE_TO_UNIT = {"per_company": "company", "per_project": "project", "periodic": "person"}


def derive_unit(text, amount_type):
    if isinstance(text, str):
        for u, rx in UNIT_RE:
            if rx.search(text):
                return u
    return TYPE_TO_UNIT.get(amount_type)


# ------------------------------------------------------------- 기관 유형
LOCAL_RE = re.compile(r"(특별시|광역시|특별자치|도청|시청|군청|구청|"
                      r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
                      r"전북|전남|경북|경남|제주)")
CENTRAL_RE = re.compile(r"(부$|처$|청$|위원회$|중기부|산업부|과기부|문체부|농식품부|"
                        r"환경부|고용부|해수부|특허청|복지부|국토부|기재부|교육부|"
                        r"방사청|산림청|농진청|중소벤처기업부)")


def derive_agency_type(agency):
    if not isinstance(agency, str) or not agency.strip():
        return None
    a = agency.strip()
    if CENTRAL_RE.search(a):
        return "central"
    if LOCAL_RE.search(a):
        return "local"
    return "public"


# ------------------------------------------------------------- 금액 의미 보정
# 2023 중앙부처 엑셀의 '지원규모' 칼럼은 관행이 일정하다.
#     "N개, X백만원 이내, 자부담 Y%"  →  N개사에 각 X원 이내
# 즉 이 칼럼의 금액은 애초에 기업(과제)당 값이지 총예산이 아니다.
# 실측 근거: 1,173건 중 total_budget 으로 잡힌 것이 10건뿐이고, 자부담률이
# 744건에 붙어 있다(자부담은 개별 기업 관점에서만 성립하는 개념이다).
#
# 범용 파서(amount_parser)는 '기업당' 같은 명시 문구를 요구해 이 관행을 못 읽고
# 1,044건을 unknown 으로 남겼다. 그 결과 per_recipient 가 5%밖에 안 나온다.
# 여기서 코호트 관행으로 보정하되, 보정한 건은 amount_type_source 로 표시해
# 하류 모델이 파서 판정과 관행 판정을 구분할 수 있게 남긴다.
COUNT_LEAD_RE = re.compile(r"(\d{1,4}(?:,\d{3})*)\s*(개사|개\s*과제|개\s*팀|개인|명|개)")


def resolve_amount_type(amount_type, scale_text, unit):
    """taxonomy 코호트 전용. unknown 을 관행에 따라 per_company/per_project 로 해석."""
    if amount_type != "unknown" or not isinstance(scale_text, str):
        return amount_type, "parser"
    if not CASH_RE.search(scale_text) and not COUNT_LEAD_RE.search(scale_text):
        return amount_type, "parser"
    if unit in ("project", "team"):
        return "per_project", "scale_convention"
    return "per_company", "scale_convention"


# ------------------------------------------------------------- 기업당 지원액
def per_recipient(amount_type, amount_max, count):
    """지원규모를 하나의 숫자로 뭉개지 않되, 비교 가능한 축 하나는 만들어야 한다.

    total_budget 을 지원건수로 나눈 값과 원문에 적힌 기업당 한도는 의미가 다르다
    (전자는 평균, 후자는 상한). 어느 경로로 나왔는지 basis 로 함께 남긴다.
    """
    if pd.isna(amount_max):
        return np.nan, None
    if amount_type in ("per_company", "per_project"):
        return float(amount_max), "stated_cap"
    if amount_type == "total_budget" and pd.notna(count) and count > 0:
        return float(amount_max) / float(count), "budget_div_count"
    return np.nan, None


# ------------------------------------------------------------- 근거문 (M65 근본 수정)
#
# 원래 이 파일의 `_pack_bizinfo` 는 목록 표본의 근거문 자리에 **제목을** 넣어
# 저장했다. 금액·지원비율·지원기업수는 F05 가 공고문 원문에서 뽑았는데 그 원문이
# 여기로 넘어오지 않아, 근거문에서 도출하는 두 파생값이 제목만 보고 만들어졌다 —
# 그 둘이 하필 모델 2·3 **비교군 사다리의 축**인 `support_method` 와
# `support_unit` 이다(M62 가 848행 오분류를 실측). Open API 행에도 같은 결함이
# 약하게 있었다: 문서에서 금액을 뽑고 근거문에는 CSV 요약을 저장했다.
#
# 고치는 방식은 하나다 — **F05 와 같은 문서 선택 규칙**(E01/E02 · 공고당 최장
# 문서 · PDF 제외)으로 원문을 붙이고, 금액을 뽑은 그 텍스트에서 파생값을 만든다.
# 아래 함수들은 M62 가 사후 보정 계층에서 쓰던 것을 원천으로 올린 것이다.
#
# `--legacy` 로 돌리면 이 절을 전부 건너뛰고 수정 전 동작을 그대로 재현한다.
# 얼어 있는 `design_features.parquet` 의 지문(M56 manifest)을 언제든 다시
# 만들어 볼 수 있어야 하기 때문이다.

SCOPE_RE = re.compile(r"지원\s*규모|지원\s*내용|선정\s*규모|모집\s*규모|"
                      r"지원\s*기업\s*수|지원\s*금액|지원\s*한도")
SCOPE_WINDOW = 160
SCOPE_MAX = 2000
RATIO_RANGE = (0.0, 100.0)
WEAK_DURATION_BASIS = {"bare"}


def load_document_texts():
    """F05 와 같은 규칙으로 공고문 원문을 고른다 (E01/E02 · 최장 · PDF 제외).

    F05 를 import 해서 쓰는 이유: 문서 선택 규칙이 두 곳에 따로 있으면
    '금액을 뽑은 문서'와 '근거문으로 저장한 문서'가 갈릴 수 있다. 그 갈림이
    바로 이번에 고치는 결함이다.
    """
    import f05_amount_observations as F5

    docs = {}
    for src, legacy in (("list", F5.DOCS_LIST), ("api", F5.DOCS_API)):
        picked, _ = F5.pick_docs(src, legacy)
        for k, v in picked.items():
            docs.setdefault(str(k), v.get("text"))
    return docs


def amount_context(text):
    """파서가 실제로 고른 금액 표현의 문맥. parse_support 의 선택 규칙과 같다."""
    if not isinstance(text, str) or not text.strip():
        return ""
    cands = AP.extract_amounts(text)
    if not cands:
        return ""
    cands.sort(key=lambda c: (AP._PRIORITY.index(c["type"]),
                              -(c["max"] or c["min"] or 0)))
    return cands[0]["context"]


def scope_text(text, window=SCOPE_WINDOW, cap=SCOPE_MAX):
    """'지원규모' 계열 표제 뒤 window 자만 이어 붙인다.

    문서 전체를 훑지 않는 이유: 2,700자짜리 공고문에는 '참여기업'이 어디든 한
    번은 나온다. 그러면 규칙이 아니라 문서 길이가 단위를 정한다.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    return " ".join(text[m.start():m.start() + window]
                    for m in SCOPE_RE.finditer(text))[:cap]


# 지원방식의 '무엇을 제공하는가' 증거를 찾을 구역. 여기도 문서 전체를 쓰지
# 않는다 — 공고문에는 신청서류·유의사항·관련법령이 붙어 있고 거기에 `금리`·
# `상환`·`벌금` 같은 단어가 문맥 없이 등장한다. METHOD_RULES 의 금융수단은
# 한 번만 걸려도 무조건 이기므로(오탐이 적다는 전제), 문서 전체를 넘기면
# **문서 길이가 지원방식을 정하게 된다.** 실측: 문서 전체를 ctx 로 넘겼더니
# `2026년 3차 방산 중소기업 컨설팅 지원사업` 이 loan 으로 떨어졌다.
METHOD_CTX_RE = re.compile(r"지원\s*내용|지원\s*분야|지원\s*사항|지원\s*형태|"
                           r"사업\s*개요|사업\s*내용|추진\s*내용|지원\s*방식")
METHOD_CTX_WINDOW = 300
METHOD_CTX_MAX = 3000


def method_context(text, fallback=""):
    """지원방식 판정용 문맥. 표제가 없으면 fallback(요약문) 또는 문서 앞부분."""
    if not isinstance(text, str) or not text.strip():
        return fallback or ""
    hit = " ".join(text[m.start():m.start() + METHOD_CTX_WINDOW]
                   for m in METHOD_CTX_RE.finditer(text))[:METHOD_CTX_MAX]
    if hit.strip():
        return hit
    return fallback if (fallback or "").strip() else text[:METHOD_CTX_MAX]


# UNIT_RULES 를 두 단으로 가른다. 어휘는 위와 같고 **우선순위만** 다르다.
#   PER_UNIT    '~당' 표현. 금액이 무엇 하나당 붙는지를 직접 말한다.
#   COUNT_UNIT  '몇 개사/몇 개 과제'. 지원 대상의 개수지 금액의 단위는 아니다.
# UNIT_RULES 는 company 를 맨 앞에 두어 `개사` 가 `과제당` 을 이긴다. 실측:
#   "지원규모 : 과제당 최대 90,000천원 이내 / 2개사"  ->  company (오답)
PER_UNIT_RULES = [
    ("company", r"기업\s*당|업체\s*당|개사\s*당|사\s*당"),
    ("project", r"과제\s*당|건\s*당|프로젝트\s*당"),
    ("team",    r"팀\s*당"),
    ("person",  r"1\s*인\s*당|인\s*당|명\s*당"),
]
COUNT_UNIT_RULES = [
    ("company", r"개사|개\s*기업|참여기업"),
    ("project", r"개\s*과제|\d\s*건"),
    ("team",    r"개\s*팀"),
    ("person",  r"\d\s*명"),
]
UNIT_RE_GRADED = [[(u, re.compile(p)) for u, p in rules]
                  for rules in (PER_UNIT_RULES, COUNT_UNIT_RULES)]


def match_unit(fragment):
    """지원단위와 **어디서 걸렸는지**를 함께 돌려준다.

    규칙으로 채운 값은 '맞는 값'이 아니라 '뽑힌 값'이라, 근거 문자열이 없으면
    감사가 불가능하다(M52 의 근거 등급과 같은 규율).
    """
    if not fragment:
        return None, None, None
    for rules in UNIT_RE_GRADED:
        for u, rx in rules:
            m = rx.search(fragment)
            if m:
                w = fragment[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
                return u, m.group(0), w
    return None, None, None


def derive_unit_graded(text, amount_type):
    """지원단위·근거등급·매치문자열·근거창. 근거 강도 순으로 3단이다.

        1 amount_context          금액 표현의 문맥 (금액과 같은 문장)
        2 scope_section           '지원규모/지원내용/…' 표제 뒤 160자
        3 amount_type_convention  amount_type -> 단위 (TYPE_TO_UNIT, 현행 fallback)
    """
    for frag, tier in ((amount_context(text), "amount_context"),
                       (scope_text(text), "scope_section")):
        u, hit, win = match_unit(frag)
        if u:
            return u, tier, hit, win
    u = TYPE_TO_UNIT.get(amount_type)
    if u:
        return u, "amount_type_convention", "amount_type=%s" % amount_type, ""
    return None, "none", None, ""


def apply_sanity(df):
    """상식 범위 밖 값을 결측으로 돌린다. 몇 건을 왜 뺐는지 세어 함께 돌려준다.

        지원비율 / 자부담률   [0,100] 밖이면 파싱 오류 (M61 이 찾은 -320% 포함)
        사업기간             (0,10] 밖이면서 근거등급이 `bare` 인 것만.
                            context/hint_only 는 10년 융자처럼 실제로 긴 사업이
                            있으므로 남긴다.
    """
    lo, hi = AP.DURATION_SANE
    in_sane = (df["project_duration"] > lo) & (df["project_duration"] <= hi)
    bad_ratio = df["support_ratio"].notna() & ~df["support_ratio"].between(*RATIO_RANGE)
    bad_burden = (df["self_burden_ratio"].notna()
                  & ~df["self_burden_ratio"].between(*RATIO_RANGE))
    bad_dur = (df["project_duration"].notna() & ~in_sane
               & df["duration_basis"].isin(WEAK_DURATION_BASIS))
    ledger = {
        "지원비율_범위밖": int(bad_ratio.sum()),
        "자부담률_범위밖": int(bad_burden.sum()),
        "사업기간_연도오파싱(bare)": int(bad_dur.sum()),
        "사업기간_범위밖_유지(근거있음)": int(
            (df["project_duration"].notna() & ~in_sane
             & ~df["duration_basis"].isin(WEAK_DURATION_BASIS)).sum()),
    }
    df.loc[bad_ratio, "support_ratio"] = np.nan
    df.loc[bad_burden, "self_burden_ratio"] = np.nan
    df.loc[bad_dur, "project_duration"] = np.nan
    df.loc[bad_dur, "duration_basis"] = None
    return df, ledger


PROVENANCE_COLS = ["evidence_source", "support_unit_basis", "support_unit_hit",
                   "support_unit_window"]

COLS = ["row_id", "cohort", "year", "title", "program_stem", "support_type",
        "support_type_source", "support_type_confidence", "category_large",
        "industry", "policy_purpose", "support_target", "agency", "executor",
        "agency_type", "support_method", "method_hits", "support_unit",
        "amount_type", "amount_type_source", "amount_max", "amount_min", "amount_unit_raw",
        "support_count", "support_ratio", "self_burden_ratio", "project_duration",
        "duration_basis", "per_recipient", "per_recipient_basis", "extraction_confidence",
        "evidence_text", "source_url"]


# ------------------------------------------------------------- 코호트 빌더
def _period(texts, frame, year_col="support_period_year",
            basis_col="support_period_basis"):
    """사업기간과 근거등급. 상류 산출물에 등급 컬럼이 있으면 그대로 쓰고,
    없으면 **같은 텍스트에서 다시 뽑는다.**

    `announcement_detail`·`business_taxonomy` 는 파서가 M32 로 갱신되기 전에
    만들어져 `support_period_basis` 가 없다. 상류 컬럼 유무에 이 스크립트가
    흔들리면 안 되므로 여기서 자급한다 — 재현 확인: 얼어 있는
    `design_features.parquet` 의 taxonomy 1,505행·API 738행과 값·등급이 전부
    일치한다.
    """
    if basis_col in frame.columns:
        return frame[year_col], frame[basis_col]
    parsed = [AP.parse_duration(t if isinstance(t, str) else "") for t in texts]
    return (pd.Series([v for v, _ in parsed], index=frame.index),
            pd.Series([b for _, b in parsed], index=frame.index))


def build_taxonomy(fixed=True):
    t = pd.read_parquet(TAX)
    t = t.rename(columns={"large_category": "category_large"})
    t["support_type"] = t["middle_category"].map(coarsen)

    scale = t["scale_text"].fillna("")
    ctx = t["content"].fillna("") + " \n" + t["purpose"].fillna("")
    # 순서가 중요하다. 금액의 의미를 먼저 확정해야 지원방식 판정이 그 결과를 쓴다.
    unit = [derive_unit(s, a) for s, a in zip(scale, t["support_amount_type"])]
    at = [resolve_amount_type(a, s, u) for a, s, u in zip(t["support_amount_type"], scale, unit)]
    amount_type = [a for a, _ in at]

    # 금액 의미가 보정되면 지원단위도 따라온다 (per_company -> company).
    # 관행 보정의 기본값이 '기업당'이므로 여기서 비는 칸이 메워진다.
    unit = [u if u else TYPE_TO_UNIT.get(a) for u, a in zip(unit, amount_type)]

    mm = [derive_method(s, c, a) for s, c, a in zip(scale, ctx, amount_type)]
    pr = [per_recipient(a, m, c) for a, m, c in
          zip(amount_type, t["support_amount_max"], t["support_count"])]

    # taxonomy 는 근거문(`scale_text`)이 원래부터 원문이라 결함이 없다. 고치는
    # 것은 **남은 결측뿐**이다 — 멀쩡한 값을 새 규칙으로 갈아엎으면 '데이터를
    # 고쳤다'가 아니라 '규칙을 바꿨다'가 된다.
    basis = ["unchanged"] * len(unit)
    hit = [None] * len(unit)
    win = [""] * len(unit)
    if fixed:
        for i, u in enumerate(unit):
            if u:
                continue
            unit[i], basis[i], hit[i], win[i] = derive_unit_graded(
                scale.iloc[i], amount_type[i])

    prov = {"evidence_source": "scale_text", "support_unit_basis": basis,
            "support_unit_hit": hit, "support_unit_window": win} if fixed else {}
    tax_dur, tax_bas = _period(scale, t)

    return pd.DataFrame({
        "row_id": t["row_id"].values,
        "cohort": "taxonomy",
        "year": t["source_year"].values,
        "title": t["title"].values,
        "program_stem": t["program_stem"].values,
        "support_type": t["support_type"].values,
        "support_type_source": "label",
        "support_type_confidence": 1.0,
        "category_large": t["category_large"].values,
        "industry": t["industry"].values,
        "policy_purpose": t["purpose"].values,
        "support_target": t["target_text"].values,
        "agency": t["agency"].values,
        "executor": t["executor"].values,
        "agency_type": [derive_agency_type(a) for a in t["agency"]],
        "support_method": [m for m, _ in mm],
        "method_hits": ["|".join(h) for _, h in mm],
        "support_unit": unit,
        "amount_type": amount_type,
        "amount_type_source": [s for _, s in at],
        "amount_max": t["support_amount_max"].values,
        "amount_min": t["support_amount_min"].values,
        "amount_unit_raw": t["support_amount_unit"].values,
        "support_count": t["support_count"].values,
        "support_ratio": t["support_ratio"].values,
        "self_burden_ratio": t["self_payment_ratio"].values,
        "project_duration": tax_dur.values,
        "duration_basis": tax_bas.values,
        "per_recipient": [v for v, _ in pr],
        "per_recipient_basis": [b for _, b in pr],
        "extraction_confidence": t["extraction_confidence"].values,
        "evidence_text": t["scale_text"].values,
        "source_url": t["source_url"].values,
        **prov,
    })[COLS + (PROVENANCE_COLS if fixed else [])]


def _pack_bizinfo(df, evidence, agency, executor, duration, duration_basis=None,
                  fixed=True, evidence_source=None, method_fallback=None):
    """bizinfo 한 덩어리를 설계 feature 행으로 만든다.

    `fixed=False` (legacy)
        지원규모 원문과 사업설명을 나눌 근거가 없다고 보고 같은 텍스트를 두
        역할에 함께 넘긴다. 그런데 목록 표본에서는 그 '같은 텍스트'가 **제목**
        이었다 — 그래서 `derive_method` 가 증거를 하나도 못 보고 금액이 있다는
        이유만으로 거의 전부 `grant` 로 떨어졌다.

    `fixed=True` (기본)
        호출부가 공고문 원문을 넘겨준다. 이제 나눌 근거가 있으므로 역할을
        가른다 — **현금 증거(`scale`)는 금액 문맥과 지원규모 절에서만** 찾고,
        제공물 증거(`context`)만 문서 전체에서 찾는다. 교육사업 설명문의
        '사업비' 한 단어가 `grant` 를 만들어내는 자리를 막기 위해서다
        (이 파일 위쪽 `derive_method` docstring 이 지목한 실패 모드).
        `project_duration` · `self_burden_ratio` 도 같은 원문에서 뽑는다 —
        목록 표본은 이 둘이 전부 결측이었는데, 원문에 없어서가 아니라 F06 이
        그 자리에 NaN 을 넣었기 때문이다.
    """
    txt = evidence.fillna("")
    n = len(df)
    if fixed:
        fb = (method_fallback.fillna("").tolist() if method_fallback is not None
              else [""] * n)
        scale = [amount_context(t) + " " + scope_text(t) for t in txt]
        ctx = [method_context(t, f) for t, f in zip(txt, fb)]
        mm = [derive_method(s, c, a)
              for s, c, a in zip(scale, ctx, df["amount_type"])]
        graded = [derive_unit_graded(t, a) for t, a in zip(txt, df["amount_type"])]
        unit = [g[0] for g in graded]
        dur = list(duration.values)
        dbas = list(duration_basis.values) if duration_basis is not None else [None] * n
        burden = [np.nan] * n
        for i, t in enumerate(txt):
            if pd.isna(dur[i]):
                dur[i], dbas[i] = AP.parse_duration(t)
            m = AP.SELF_RATIO_RE.search(t) if isinstance(t, str) else None
            if m:
                burden[i] = float(m.group(1))
        prov = {"evidence_source": (evidence_source if evidence_source is not None
                                    else "document"),
                "support_unit_basis": [g[1] for g in graded],
                "support_unit_hit": [g[2] for g in graded],
                "support_unit_window": [g[3] for g in graded]}
    else:
        mm = [derive_method(s, s, a) for s, a in zip(txt, df["amount_type"])]
        unit = [derive_unit(t, a) for t, a in zip(txt, df["amount_type"])]
        dur = duration.values
        dbas = (duration_basis.values if duration_basis is not None else [None] * n)
        burden = np.nan
        prov = {}
    pr = [per_recipient(a, m, c) for a, m, c in
          zip(df["amount_type"], df["amount_max"], df["support_count"])]
    return pd.DataFrame({
        "row_id": df["announcement_id"].values,
        "cohort": "bizinfo",
        "year": df["year"].values,
        "title": df["title"].values,
        "program_stem": df["title"].values,
        "support_type": df["support_type_pred"].values,
        "support_type_source": "pred",
        "support_type_confidence": df["support_type_confidence"].values,
        "category_large": df["large_category"].values,
        "industry": [None] * n,
        "policy_purpose": [None] * n,
        "support_target": [None] * n,
        "agency": agency.values,
        "executor": executor.values,
        "agency_type": [derive_agency_type(a) for a in agency],
        "support_method": [m for m, _ in mm],
        "method_hits": ["|".join(h) for _, h in mm],
        "support_unit": unit,
        "amount_type": df["amount_type"].values,
        "amount_type_source": "parser",
        "amount_max": df["amount_max"].values,
        "amount_min": df["amount_min"].values,
        "amount_unit_raw": [None] * n,
        "support_count": df["support_count"].values,
        "support_ratio": df["support_ratio"].values,
        "self_burden_ratio": burden,
        "project_duration": dur,
        "duration_basis": dbas,
        "per_recipient": [v for v, _ in pr],
        "per_recipient_basis": [b for _, b in pr],
        "extraction_confidence": np.nan,
        "evidence_text": txt.values,
        "source_url": [None] * n,
        **prov,
    })[COLS + (PROVENANCE_COLS if fixed else [])]


def build_bizinfo(fixed=True):
    d = pd.read_parquet(DET)
    obs = pd.read_parquet(OBS)
    docs = load_document_texts() if fixed else {}

    d = d.copy()
    # F02 가 파싱에 쓴 텍스트와 같은 것(`summary_text` + `reqst_text`)에서 뽑는다.
    d["support_period_year"], d["support_period_basis"] = _period(
        d["summary_text"].fillna("") + "\n" + d["reqst_text"].fillna(""), d)
    ao = obs[obs["source"] == "openapi"].merge(
        d[["announcement_id", "title", "agency", "executor", "summary_text",
           "support_amount_raw", "support_period_year", "support_period_basis",
           "support_type_pred", "support_type_confidence"]],
        on="announcement_id", how="left")
    api_fallback = (ao["support_amount_raw"].fillna("") + " \n"
                    + ao["summary_text"].fillna(""))
    if fixed:
        # 금액을 뽑은 그 문서를 근거문으로 쓴다. 문서가 없으면 CSV 요약으로 후퇴하고
        # 그 사실을 evidence_source 에 남긴다.
        doc = ao["announcement_id"].astype(str).map(docs)
        api_ev = doc.where(doc.notna(), api_fallback)
        api_src = np.where(doc.notna(), "document", "api_summary")
    else:
        api_ev, api_src = api_fallback, None
    api = _pack_bizinfo(ao, api_ev, ao["agency"], ao["executor"],
                        ao["support_period_year"], ao["support_period_basis"],
                        fixed=fixed, evidence_source=api_src,
                        method_fallback=ao["summary_text"])

    # 목록 표본은 지원성격 예측(LST)과 금액 관측(OBS)이 따로 있다. 금액이 있는 쪽만 쓴다.
    lst = pd.read_parquet(LST)[["announcement_id", "title", "support_type_pred",
                                "support_type_confidence"]]
    lo = obs[obs["source"] == "list_sample"].merge(lst, on="announcement_id", how="left")
    empty = pd.Series([None] * len(lo), index=lo.index)
    if fixed:
        doc = lo["announcement_id"].astype(str).map(docs)
        lst_ev = doc.where(doc.notna(), lo["title"].fillna(""))
        lst_src = np.where(doc.notna(), "document", "title_only")
    else:
        lst_ev, lst_src = lo["title"].fillna(""), None
    lst_df = _pack_bizinfo(lo, lst_ev, empty, empty,
                           pd.Series([np.nan] * len(lo), index=lo.index),
                           fixed=fixed, evidence_source=lst_src)
    return pd.concat([api, lst_df], ignore_index=True)


def main(fixed=True, out=None):
    out_override = out is not None
    out = out or (OUT_V2 if fixed else OUT)
    df = pd.concat([build_taxonomy(fixed), build_bizinfo(fixed)], ignore_index=True)

    sanity = {}
    if fixed:
        df, sanity = apply_sanity(df)

    # 파싱 오류 금액은 지우지 않고 플래그만 (common.SANE_RANGE 규율 유지)
    df["amount_outlier"] = C.mark_outliers(df, amount_col="amount_max",
                                           type_col="amount_type")
    df.to_parquet(out, index=False)
    print("[data] %s  %s  (%s)"
          % (out, df.shape, "근거문 수정본" if fixed else "legacy 재현"))
    if sanity:
        print("  상식범위 정리:", sanity)
        print("  근거문 출처:", dict(df["evidence_source"].value_counts()))

    for c, g in df.groupby("cohort"):
        print("\n== %s  %d건" % (c, len(g)))
        print("  지원방식:", dict(g["support_method"].value_counts()))
        print("  지원단위:", dict(g["support_unit"].value_counts(dropna=False)))
        print("  기관유형:", dict(g["agency_type"].value_counts(dropna=False)))
        print("  per_recipient:", dict(g["per_recipient_basis"].value_counts(dropna=False)))
        print("  금액 파싱오류 플래그: %d건" % int(g["amount_outlier"].sum()))

    if out_override:
        # 임시 경로로 뽑아 본 것(M65 의 재현 검증 등)은 리포트를 덮어쓰지 않는다.
        return df
    C.save_report("f06_design_features%s.json" % ("_v2" if fixed else ""), {
        "output": os.path.relpath(out, C.ROOT),
        "evidence_fix": bool(fixed),
        "rows": int(len(df)),
        "by_cohort": {k: int(v) for k, v in df["cohort"].value_counts().items()},
        "derived": ["support_method", "support_unit", "per_recipient", "agency_type"],
        "method_dist": {k: {kk: int(vv) for kk, vv in
                            g["support_method"].value_counts().items()}
                        for k, g in df.groupby("cohort")},
        "per_recipient_coverage": {k: int(g["per_recipient"].notna().sum())
                                   for k, g in df.groupby("cohort")},
        "amount_outlier": int(df["amount_outlier"].sum()),
        "evidence_source": ({k: int(v) for k, v in
                             df["evidence_source"].value_counts().items()}
                            if fixed else None),
        "support_unit_basis": ({k: int(v) for k, v in
                                df["support_unit_basis"].value_counts().items()}
                               if fixed else None),
        "sanity": sanity or None,
    })
    return df


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy", action="store_true",
                    help="근거문 수정 전 동작을 그대로 재현해 design_features.parquet 을 만든다")
    ap.add_argument("--out", default=None, help="출력 경로 재지정")
    a = ap.parse_args()
    main(fixed=not a.legacy, out=a.out)
