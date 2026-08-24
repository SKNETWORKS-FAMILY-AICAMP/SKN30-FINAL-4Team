"""N01 — 모델 2~4 공통 '사업 설계' feature 테이블.

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
    얼마나 겹치는지(Cramer's V)를 n02 에서 측정해 판단 근거로 남긴다.
"""
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m06_support_type import coarsen

TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DET = os.path.join(C.PROC, "announcement_detail_with_support_type_v2.parquet")
OBS = os.path.join(C.PROC, "support_amount_observations.parquet")
LST = os.path.join(C.PROC, "list_sample_support_type.parquet")
OUT = os.path.join(C.PROC, "design_features.parquet")


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


COLS = ["row_id", "cohort", "year", "title", "program_stem", "support_type",
        "support_type_source", "support_type_confidence", "category_large",
        "industry", "policy_purpose", "support_target", "agency", "executor",
        "agency_type", "support_method", "method_hits", "support_unit",
        "amount_type", "amount_type_source", "amount_max", "amount_min", "amount_unit_raw",
        "support_count", "support_ratio", "self_burden_ratio", "project_duration",
        "per_recipient", "per_recipient_basis", "extraction_confidence",
        "evidence_text", "source_url"]


# ------------------------------------------------------------- 코호트 빌더
def build_taxonomy():
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
        "project_duration": t["support_period_year"].values,
        "per_recipient": [v for v, _ in pr],
        "per_recipient_basis": [b for _, b in pr],
        "extraction_confidence": t["extraction_confidence"].values,
        "evidence_text": t["scale_text"].values,
        "source_url": t["source_url"].values,
    })[COLS]


def _pack_bizinfo(df, evidence, agency, executor, duration):
    # bizinfo 는 지원규모 원문과 사업설명이 한 덩어리로 들어온다. 나눌 근거가 없으니
    # 같은 텍스트를 두 역할에 함께 넘긴다(taxonomy 만큼 정밀하지 않음을 인정한다).
    txt = evidence.fillna("")
    mm = [derive_method(s, s, a) for s, a in zip(txt, df["amount_type"])]
    pr = [per_recipient(a, m, c) for a, m, c in
          zip(df["amount_type"], df["amount_max"], df["support_count"])]
    n = len(df)
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
        "support_unit": [derive_unit(s, a) for s, a in zip(txt, df["amount_type"])],
        "amount_type": df["amount_type"].values,
        "amount_type_source": "parser",
        "amount_max": df["amount_max"].values,
        "amount_min": df["amount_min"].values,
        "amount_unit_raw": [None] * n,
        "support_count": df["support_count"].values,
        "support_ratio": df["support_ratio"].values,
        "self_burden_ratio": np.nan,
        "project_duration": duration.values,
        "per_recipient": [v for v, _ in pr],
        "per_recipient_basis": [b for _, b in pr],
        "extraction_confidence": np.nan,
        "evidence_text": txt.values,
        "source_url": [None] * n,
    })[COLS]


def build_bizinfo():
    d = pd.read_parquet(DET)
    obs = pd.read_parquet(OBS)

    ao = obs[obs["source"] == "openapi"].merge(
        d[["announcement_id", "title", "agency", "executor", "summary_text",
           "support_amount_raw", "support_period_year", "support_type_pred",
           "support_type_confidence"]],
        on="announcement_id", how="left")
    api = _pack_bizinfo(
        ao,
        ao["support_amount_raw"].fillna("") + " \n" + ao["summary_text"].fillna(""),
        ao["agency"], ao["executor"], ao["support_period_year"])

    # 목록 표본은 지원성격 예측(LST)과 금액 관측(OBS)이 따로 있다. 금액이 있는 쪽만 쓴다.
    lst = pd.read_parquet(LST)[["announcement_id", "title", "support_type_pred",
                                "support_type_confidence"]]
    lo = obs[obs["source"] == "list_sample"].merge(lst, on="announcement_id", how="left")
    empty = pd.Series([None] * len(lo), index=lo.index)
    lst_df = _pack_bizinfo(lo, lo["title"].fillna(""), empty, empty,
                           pd.Series([np.nan] * len(lo), index=lo.index))
    return pd.concat([api, lst_df], ignore_index=True)


def main():
    df = pd.concat([build_taxonomy(), build_bizinfo()], ignore_index=True)

    # 파싱 오류 금액은 지우지 않고 플래그만 (common.SANE_RANGE 규율 유지)
    df["amount_outlier"] = C.mark_outliers(df, amount_col="amount_max",
                                           type_col="amount_type")
    df.to_parquet(OUT, index=False)
    print("[data] %s  %s" % (OUT, df.shape))

    for c, g in df.groupby("cohort"):
        print("\n== %s  %d건" % (c, len(g)))
        print("  지원방식:", dict(g["support_method"].value_counts()))
        print("  지원단위:", dict(g["support_unit"].value_counts(dropna=False)))
        print("  기관유형:", dict(g["agency_type"].value_counts(dropna=False)))
        print("  per_recipient:", dict(g["per_recipient_basis"].value_counts(dropna=False)))
        print("  금액 파싱오류 플래그: %d건" % int(g["amount_outlier"].sum()))

    C.save_report("n01_design_features.json", {
        "rows": int(len(df)),
        "by_cohort": {k: int(v) for k, v in df["cohort"].value_counts().items()},
        "derived": ["support_method", "support_unit", "per_recipient", "agency_type"],
        "method_dist": {k: {kk: int(vv) for kk, vv in
                            g["support_method"].value_counts().items()}
                        for k, g in df.groupby("cohort")},
        "per_recipient_coverage": {k: int(g["per_recipient"].notna().sum())
                                   for k, g in df.groupby("cohort")},
        "amount_outlier": int(df["amount_outlier"].sum()),
    })


if __name__ == "__main__":
    main()
