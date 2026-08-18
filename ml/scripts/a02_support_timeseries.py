"""A02 — 지원규모 시계열 집계 (설계서 v3 4.6 / 15 / 16장).

설계서 v3에서 모델 3은 '지원규모 3등급 분류'가 아니라
'정보추출 → 의미별 정규화 → 월별 집계 → 시계열 분석 → 조건부 예측'으로 재정의됐다.
이 스크립트는 그 중 집계 단계를 담당한다.

핵심 원칙 (설계서 6.1 / 24장 규칙 8)
    per_company / per_project / total_budget / periodic / ratio 를 절대 섞지 않는다.
    같은 support_amount 컬럼에 넣고 평균 내면 무의미한 값이 나온다.
    따라서 집계 단위에 amount_type 을 반드시 포함한다.

집계 단위 (설계서 16장)
    1차 MVP : 월 × 대분류 × 지원규모타입
    2차     : 월 × 대분류 × 지원성격 × 지원규모타입
              (모델 1 신뢰등급이 충분한 건에 한해)

지표 (설계서 15장)
    지원규모는 이상치가 강해 평균만 쓰지 않고 중앙값·분위수를 함께 낸다.
"""
import argparse
import json
import os
import warnings

import numpy as np
import pandas as pd

from common import PROC, REPORTS, save_report
from amount_parser import parse_support

warnings.filterwarnings("ignore")

DETAIL = PROC + "/announcement_detail_enriched.parquet"
CLASSIFIED = PROC + "/announcement_detail_with_support_type.parquet"
MASTER = PROC + "/announcement_master.parquet"
SAMPLE = PROC + "/list_sample.parquet"
# E02(pdf-inspector + rhwp)가 있으면 우선 사용한다. 표 구조가 보존돼 있어
# 지원규모 파싱 결과가 E01(PyMuPDF + pyhwp)보다 낫다.
DOCS_V2 = REPORTS + "/e02_documents.jsonl"
DOCS_API = REPORTS + "/e01_documents.jsonl"
DOCS_LIST = REPORTS + "/e01_documents_list.jsonl"

OUT_LONG = PROC + "/support_amount_observations.parquet"
OUT_TS = PROC + "/timeseries_support_amount.parquet"

TYPES = ["per_company", "per_project", "total_budget", "periodic"]


def load_docs(path, source=None):
    """공고 1건에 문서가 여러 개면 가장 긴 것을 대표로.

    source 를 주면 해당 출처(api/list)만 추린다. E02 는 두 출처를 한 파일에 담는다.
    """
    best = {}
    if not os.path.exists(path):
        return best
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            if source and r.get("source") != source:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def pick_docs(source, legacy_path):
    """E02 가 있으면 그것을, 없으면 E01 결과를 쓴다."""
    if os.path.exists(DOCS_V2):
        d = load_docs(DOCS_V2, source=source)
        if d:
            return d, "e02"
    return load_docs(legacy_path), "e01"


def build_observations():
    """공고 1건 = 1행. 날짜·대분류·지원성격·금액(의미별)을 붙인 관측 테이블."""
    rows = []

    # --- Open API (대부분 2026년, 지원성격 추론값 보유)
    d = pd.read_parquet(DETAIL)
    try:
        cls = pd.read_parquet(CLASSIFIED)[
            ["announcement_id", "support_type_pred", "support_type_confidence",
             "support_type_status"]]
        d = d.merge(cls, on="announcement_id", how="left")
    except Exception:
        d["support_type_pred"] = np.nan
        d["support_type_confidence"] = np.nan
        d["support_type_status"] = np.nan

    # 원문이 있으면 재파싱한다(E02는 표 구조가 살아 있어 결과가 낫다).
    # 없으면 parquet에 이미 들어있는 값을 쓴다.
    api_docs, api_ver = pick_docs("api", DOCS_API)
    for _, r in d.iterrows():
        if pd.isna(r.get("created_at")):
            continue
        doc = api_docs.get(str(r["announcement_id"]))
        if doc is not None:
            amt = parse_support(doc["text"])
            amax, amin = amt["support_amount_max"], amt["support_amount_min"]
            atype = amt["support_amount_type"]
            ratio, cnt = amt["support_ratio"], amt["support_count"]
            src_text = "doc_" + api_ver
        else:
            amax, amin = r.get("support_amount_max"), r.get("support_amount_min")
            atype = r.get("support_amount_type")
            ratio, cnt = r.get("support_ratio"), r.get("support_count")
            src_text = "csv_summary"
        if pd.isna(amax) or amax is None:
            continue
        rows.append({
            "announcement_id": r["announcement_id"],
            "date": r["created_at"],
            "large_category": r.get("category_large"),
            "support_type": r.get("support_type_pred"),
            "support_type_status": r.get("support_type_status"),
            "amount_type": atype,
            "amount_max": amax,
            "amount_min": amin,
            "support_ratio": ratio,
            "support_count": cnt,
            "text_source": src_text,
            "source": "openapi",
        })

    # --- 목록 표본 (2019~2025, 장기 시계열의 핵심)
    docs, list_ver = pick_docs("list", DOCS_LIST)
    if docs:
        m = pd.read_parquet(MASTER)[
            ["announcement_id", "registered_date", "category_large"]]
        m = m[m["announcement_id"].astype(str).isin(docs.keys())]
        for _, r in m.iterrows():
            doc = docs.get(str(r["announcement_id"]))
            if doc is None:
                continue
            amt = parse_support(doc["text"])
            if amt["support_amount_max"] is None:
                continue
            rows.append({
                "announcement_id": r["announcement_id"],
                "date": r["registered_date"],
                "large_category": r["category_large"],
                "support_type": np.nan,          # 목록에는 지원성격 라벨 없음
                "support_type_status": np.nan,
                "amount_type": amt["support_amount_type"],
                "amount_max": amt["support_amount_max"],
                "amount_min": amt["support_amount_min"],
                "support_ratio": amt["support_ratio"],
                "support_count": amt["support_count"],
                "text_source": "doc_" + list_ver,
                "source": "list_sample",
            })

    obs = pd.DataFrame(rows)
    if obs.empty:
        return obs
    obs["date"] = pd.to_datetime(obs["date"], errors="coerce")
    obs = obs.dropna(subset=["date"])
    obs["ym"] = obs["date"].dt.to_period("M").astype(str)
    obs["year"] = obs["date"].dt.year
    return obs.reset_index(drop=True)


def aggregate(obs, level="mvp", min_obs=1):
    """설계서 15장 지표로 월별 집계. level='mvp'면 지원성격 축 제외."""
    keys = ["ym", "large_category", "amount_type"]
    if level == "full":
        keys.insert(2, "support_type")

    sub = obs[obs["amount_type"].isin(TYPES)].copy()
    if sub.empty:
        return pd.DataFrame()

    g = sub.groupby(keys, observed=True)["amount_max"]
    agg = g.agg(
        amount_observation_count="count",
        median_amount="median",
        mean_amount="mean",
        p25_amount=lambda s: s.quantile(0.25),
        p75_amount=lambda s: s.quantile(0.75),
        sum_amount="sum",
    ).reset_index()

    # 지원비율 / 지원기업수는 별도 집계 (설계서 15.3, 15.4)
    r = sub.groupby(keys, observed=True)["support_ratio"].median().rename("median_support_ratio")
    c = sub.groupby(keys, observed=True)["support_count"].agg(["median", "sum"])
    c.columns = ["median_support_count", "sum_support_count"]
    agg = agg.merge(r, on=keys, how="left").merge(c, on=keys, how="left")

    return agg[agg["amount_observation_count"] >= min_obs].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-obs", type=int, default=1)
    args = ap.parse_args()

    obs = build_observations()
    if obs.empty:
        print("관측치 없음 — 원문 추출을 먼저 수행해야 한다")
        return

    obs.to_parquet(OUT_LONG, index=False)
    print("관측 테이블 %d건 → %s" % (len(obs), OUT_LONG))
    print()
    print("출처별:", obs["source"].value_counts().to_dict())
    print("금액 의미별:")
    print(obs["amount_type"].value_counts(dropna=False).to_string())
    print()
    print("연도별 관측치 (의미 확정분만):")
    typed = obs[obs["amount_type"].isin(TYPES)]
    print(typed.groupby(["year"]).size().to_string())

    ts = aggregate(obs, "mvp", args.min_obs)
    ts.to_parquet(OUT_TS, index=False)
    print()
    print("=== 1차 MVP 집계: 월 × 대분류 × 금액의미 ===")
    print("%d행 → %s" % (len(ts), OUT_TS))

    # 시계열로 쓸 수 있는지 판정 (설계서 18장 조건 1,3)
    print()
    print("=== 금액의미별 시계열 가용성 ===")
    print("%-14s%8s%10s%12s%14s" % ("금액의미", "관측수", "개월수", "월평균관측", "판정"))
    print("-" * 60)
    avail = {}
    for t in TYPES:
        s = typed[typed["amount_type"] == t]
        if s.empty:
            print("%-14s%8d%10d%12s%14s" % (t, 0, 0, "-", "불가(관측없음)"))
            avail[t] = {"n": 0, "months": 0, "usable": False}
            continue
        months = s["ym"].nunique()
        per_month = len(s) / months if months else 0
        ok = months >= 24 and per_month >= 3
        print("%-14s%8d%10d%12.1f%14s"
              % (t, len(s), months, per_month, "가능" if ok else "부족"))
        avail[t] = {"n": int(len(s)), "months": int(months),
                    "per_month": round(per_month, 2), "usable": bool(ok)}

    save_report("a02_support_timeseries.json", {
        "observations": int(len(obs)),
        "source_dist": obs["source"].value_counts().to_dict(),
        "amount_type_dist": obs["amount_type"].value_counts(dropna=False).to_dict(),
        "typed_observations": int(len(typed)),
        "year_range": [int(typed["year"].min()), int(typed["year"].max())] if len(typed) else None,
        "by_year": typed.groupby("year").size().to_dict(),
        "mvp_rows": int(len(ts)),
        "availability": avail,
        "criteria": "개월수>=24 AND 월평균 관측>=3 이면 시계열 분석 가능(설계서 18장)",
        "note": "설계서 24장 규칙8 — per_company/total_budget/per_project/periodic 을 섞지 않는다",
        "outputs": {"observations": OUT_LONG, "timeseries": OUT_TS},
    })


if __name__ == "__main__":
    main()
