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

from common import PROC, REPORTS, save_report, mark_outliers, SANE_RANGE
from amount_parser import parse_support

warnings.filterwarnings("ignore")

DETAIL = PROC + "/announcement_detail_enriched.parquet"

# 지원성격 라벨의 출처. --support-type-source 로 고른다.
#
# 왜 기본값이 m08 인가
#     예전에는 dl05(KLUE-RoBERTa) 산출물을 고정으로 읽었다. 그런데 dl05 는
#     deep-learning(5단계) 산출물이고 이 스크립트는 timeseries-analysis(4단계)에
#     있다. 상류가 하류 산출물을 읽는 역류였고, 파일이 없으면 try/except 가
#     조용히 삼켜 support_type 이 통째로 결측이 됐다(실측: 이 브랜치에서 100%,
#     deep-learning 에서 70%). 결측인 채로 a05 까지 흘러가도 티가 나지 않는다.
#     m08 산출물은 machine-learning(3단계) 것이라 이 브랜치에 반드시 있다.
#     기본값을 m08 로 두면 어느 브랜치에서 돌려도 같은 결과가 나온다.
#
# dl05 를 쓰려면 deep-learning 에서 --support-type-source dl05 를 명시한다.
# 다만 dl05 는 98.3% 를 '신뢰'로 매기는데 검증된 적이 없다. 같은 표본에서
# m08 은 50.6% 만 신뢰로 매기고 그 중 78.3% 가 맞았다(M13). 과신 의심이 있어
# 재학습·검증 전까지는 기본값으로 삼지 않는다.
SUPPORT_TYPE_SOURCES = {
    "m08": PROC + "/announcement_detail_with_support_type_v2.parquet",
    "dl05": PROC + "/openapi_support_type_roberta.parquet",
}
COLMAP = {
    "m08": {"pred": "support_type_pred", "conf": "support_type_confidence",
            "status": "support_type_status"},
    "dl05": {"pred": "support_type_pred", "conf": "confidence", "status": "status"},
}
MASTER = PROC + "/announcement_master.parquet"
SAMPLE = PROC + "/list_sample.parquet"
# E02(pdf-inspector + rhwp)가 있으면 우선 사용한다. HWP 표가 실제로 읽힌다.
# 단 PDF 는 EXCLUDE_EXT 로 걸러낸다(아래 근거 참조).
DOCS_V2 = REPORTS + "/e02_documents.jsonl"
DOCS_API = REPORTS + "/e01_documents.jsonl"
DOCS_LIST = REPORTS + "/e01_documents_list.jsonl"

OUT_LONG = PROC + "/support_amount_observations.parquet"
OUT_TS = PROC + "/timeseries_support_amount.parquet"

TYPES = ["per_company", "per_project", "total_budget", "periodic"]


# PDF 는 표 셀이 뭉쳐 나와 금액이 왜곡된다. 실측:
#   - 한 셀에 금액 3개 이상 뭉친 문서가 표 보유 PDF 의 13.6%
#     (HWP 는 0%. rhwp 가 셀 단위로 정확히 읽는다)
#   - 그 문서에서 나온 per_company 관측은 중앙값 2배, p95 3.8배로 부풀려짐
#     (병합없음 1,500만/20.0억 vs 병합있음 3,000만/76.1억)
#   - 예: "238만원|119만원|476만원|1,900만원" 4행이 한 셀로 뭉쳐 39억원으로 파싱
# 병합 탐지 규칙에 의존해 일부만 거르는 것보다, 오류가 0인 HWP 계열만 쓰는 편이
# 경계가 깨끗하다. 관측은 3,786 -> 2,706건으로 줄지만 신뢰도를 택한다.
EXCLUDE_EXT = {"pdf"}


def load_docs(path, source=None, exclude_ext=EXCLUDE_EXT):
    """공고 1건에 문서가 여러 개면 가장 긴 것을 대표로.

    source 를 주면 해당 출처(api/list)만 추린다. E02 는 두 출처를 한 파일에 담는다.
    exclude_ext 에 든 확장자는 대표 선정에서 제외한다(기본: pdf).
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
            if exclude_ext and (r.get("ext") or "").lower() in exclude_ext:
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


def attach_support_type(d, source):
    """지원성격 라벨을 붙인다. 파일이 없으면 조용히 넘어가지 않고 멈춘다.

    예전에는 try/except 로 감싸 결측으로 떨어뜨렸는데, 그러면 support_type 이
    통째로 비어도 파이프라인이 끝까지 돌아가 a05 참고범위의 by_type_support 가
    빈 채로 산출된다. 실제로 그렇게 100% 결측인 산출물이 커밋된 적이 있다.
    """
    path = SUPPORT_TYPE_SOURCES[source]
    if not os.path.exists(path):
        raise FileNotFoundError(
            "지원성격 산출물이 없다: %s\n"
            "  --support-type-source m08 은 M08 을, dl05 는 DL05 를 먼저 실행해야 한다.\n"
            "  dl05 산출물은 deep-learning 브랜치에만 있다." % path)
    c = COLMAP[source]
    cls = pd.read_parquet(path)[
        ["announcement_id", c["pred"], c["conf"], c["status"]]]
    cls.columns = ["announcement_id", "support_type_pred",
                   "support_type_confidence", "support_type_status"]
    # 판단보류는 라벨로 쓰지 않는다. 확신 없는 예측으로 참고범위를 나누면
    # 그 구간의 통계가 오염된다.
    hold = cls["support_type_status"] == "판단보류"
    cls.loc[hold, "support_type_pred"] = np.nan
    d = d.merge(cls, on="announcement_id", how="left")
    n = int(d["support_type_pred"].notna().sum())
    print("지원성격 출처 %s — %d/%d건 라벨 부여 (판단보류 %d건 제외)"
          % (source, n, len(d), int(hold.sum())))
    return d


def build_observations(source):
    """공고 1건 = 1행. 날짜·대분류·지원성격·금액(의미별)을 붙인 관측 테이블."""
    rows = []

    # --- Open API (대부분 2026년, 지원성격 추론값 보유)
    d = pd.read_parquet(DETAIL)
    d = attach_support_type(d, source)

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
    # 상식 범위 밖 = 파싱 오류. 행을 지우지 않고 플래그만 붙여 하류(a03/a04/a05)가
    # 같은 기준으로 제외하게 한다. 몇 건을 왜 뺐는지 추적 가능하다.
    obs["is_outlier"] = mark_outliers(obs)
    return obs.reset_index(drop=True)


def aggregate(obs, level="mvp", min_obs=1):
    """설계서 15장 지표로 월별 집계. level='mvp'면 지원성격 축 제외."""
    keys = ["ym", "large_category", "amount_type"]
    if level == "full":
        keys.insert(2, "support_type")

    sub = obs[obs["amount_type"].isin(TYPES) & ~obs.get("is_outlier", False)].copy()
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
    ap.add_argument("--support-type-source", choices=sorted(SUPPORT_TYPE_SOURCES),
                    default="m08",
                    help="지원성격 라벨 출처. 기본 m08(상류라 어느 브랜치에서도 재현됨). "
                         "dl05 는 deep-learning 에서만 쓸 수 있다.")
    args = ap.parse_args()

    obs = build_observations(args.support_type_source)
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
        "support_type_source": args.support_type_source,
        "support_type_source_path": SUPPORT_TYPE_SOURCES[args.support_type_source],
        "support_type_coverage": {
            "labeled": int(obs["support_type"].notna().sum()),
            "total": int(len(obs)),
            "note": ("목록 표본(2019~2025)에는 지원성격 라벨이 원래 없다. "
                     "라벨은 Open API 분에만 붙는다. 판단보류 예측은 라벨로 쓰지 않는다."),
        },
        "outliers_flagged": int(obs["is_outlier"].sum()),
        "outliers_by_type": obs[obs["is_outlier"]]["amount_type"]
                            .value_counts().to_dict(),
        "sane_range": {k: list(v) for k, v in SANE_RANGE.items()},
        "outlier_policy": ("SANE_RANGE 밖은 파싱 오류로 보고 is_outlier 플래그를 붙인다. "
                           "행은 지우지 않으며 집계·STL·예측·참고범위가 모두 이 플래그로 제외한다."),
        "excluded_ext": sorted(EXCLUDE_EXT),
        "exclusion_reason": (
            "PDF 는 표 셀이 뭉쳐 나와 금액이 왜곡된다. 표 보유 PDF 의 13.6%가 "
            "한 셀에 금액 3개 이상 병합되며(HWP 0%), 해당 문서 기반 per_company 는 "
            "중앙값 2배·p95 3.8배로 부풀려졌다. 병합 탐지로 일부만 거르는 대신 "
            "오류가 0인 HWP 계열만 사용한다."),
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
