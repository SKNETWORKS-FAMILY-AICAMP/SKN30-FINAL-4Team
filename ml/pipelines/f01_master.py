"""F01 — announcement_master (설계서 4.1). 장기 공고 목록 97,794건 정제."""
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

import hashlib
import sys

import pandas as pd

from common import PROC, norm_category, pblanc_id, read_list, save_report

REGIONS = {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기",
           "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"}
ALIAS = {"서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구",
         "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
         "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기",
         "강원도": "강원", "강원특별자치도": "강원", "충청북도": "충북",
         "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
         "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
         "제주특별자치도": "제주", "제주도": "제주"}

import re
BRACKET = re.compile(r"^\s*[\[\(]([^\]\)]{1,12})[\]\)]")
YEAR = re.compile(r"(20\d{2})\s*년")
NOISE = re.compile(r"(모집\s*공고|재공고|추가\s*공고|공고문|공고|모집|안내|알림|신청\s*접수|접수)\s*$")


def region_of(title, agency, executor):
    if isinstance(title, str):
        m = BRACKET.match(title)
        if m:
            t = m.group(1).strip()
            if t in REGIONS:
                return t
            if t in ALIAS:
                return ALIAS[t]
    for src in (agency, executor):
        if not isinstance(src, str):
            continue
        for full, short in ALIAS.items():
            if src.startswith(full):
                return short
        for short in REGIONS:
            if src.startswith(short):
                return short
    return None


def core_title(t):
    if not isinstance(t, str):
        return ""
    t = BRACKET.sub("", t).strip()
    t = YEAR.sub("", t)
    for _ in range(3):
        t2 = NOISE.sub("", t).strip(" -·,")
        if t2 == t:
            break
        t = t2
    return re.sub(r"\s+", " ", t).strip()


def main():
    df = read_list()
    n_raw = len(df)
    df["announcement_id"] = df["상세URL"].map(pblanc_id)
    miss = int(df["announcement_id"].isna().sum())
    fb = df["announcement_id"].isna()
    df.loc[fb, "announcement_id"] = df.loc[fb, "상세URL"].map(
        lambda u: "HASH_" + hashlib.md5(str(u).encode()).hexdigest()[:16])
    n_dup = int(df["announcement_id"].duplicated().sum())
    df = df.drop_duplicates(subset="announcement_id", keep="first")

    m = pd.DataFrame({
        "announcement_id": df["announcement_id"],
        "title": df["사업명"].astype(str).str.strip(),
        "category_large": df["분야"].map(norm_category),
        "agency": df["소관기관"].astype(str).str.strip(),
        "executor": df["수행기관"].astype(str).str.strip(),
        "application_start": pd.to_datetime(df["신청시작일자"], errors="coerce"),
        "application_end": pd.to_datetime(df["신청종료일자"], errors="coerce"),
        "registered_date": pd.to_datetime(df["등록일자"], errors="coerce"),
        "source_url": df["상세URL"],
    })
    m["title_core"] = m["title"].map(core_title)
    m["region"] = [region_of(t, a, e) for t, a, e in
                   zip(m["title"], m["agency"], m["executor"])]
    m["year"] = m["registered_date"].dt.year
    m["month"] = m["registered_date"].dt.month
    m["quarter"] = m["registered_date"].dt.quarter
    m["ym"] = m["registered_date"].dt.to_period("M").astype(str)
    d = (m["application_end"] - m["application_start"]).dt.days
    m["apply_days"] = d.where((d >= 0) & (d <= 400))

    out = f"{PROC}/announcement_master.parquet"
    m.to_parquet(out, index=False)
    save_report("f01_master.json", {
        "rows_raw": n_raw, "rows_final": len(m),
        "url_without_pblanc_id": miss, "duplicate_dropped": n_dup,
        "registered_date_null": int(m["registered_date"].isna().sum()),
        "category_null": int(m["category_large"].isna().sum()),
        "region_coverage": round(float(m["region"].notna().mean()), 4),
        "date_min": str(m["registered_date"].min().date()),
        "date_max": str(m["registered_date"].max().date()),
        "by_category": m["category_large"].value_counts().to_dict(),
        "by_year": m["year"].value_counts().sort_index().to_dict(),
        "apply_days_median": float(m["apply_days"].median()),
        "output": out,
    })
    print(f"announcement_master {len(m):,}행 → {out}")
    print(f"  {m['registered_date'].min().date()} ~ {m['registered_date'].max().date()}"
          f" / 지역 {m['region'].notna().mean():.1%}")


if __name__ == "__main__":
    main()
