"""S03B — announcement_detail (설계서 4.2). Open API 1,570건 텍스트/지원규모 구조화.

해시태그 첫 토큰이 분야 라벨과 동일해 그대로 쓰면 라벨 누수다.
분야명과 겹치는 토큰을 제거한 hashtags_safe를 별도로 만든다.
"""
import pandas as pd

from common import (CANON_CATEGORIES, PROC, norm_category, read_api,
                    save_report, strip_html)
from amount_parser import parse_support

REGIONS = {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기",
           "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"}
import re
BRACKET = re.compile(r"^\s*[\[\(]([^\]\)]{1,12})[\]\)]")
ALIAS = {"서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구",
         "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
         "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기",
         "강원도": "강원", "강원특별자치도": "강원", "충청북도": "충북",
         "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
         "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
         "제주특별자치도": "제주", "제주도": "제주"}


def region_of(title, agency):
    if isinstance(title, str):
        m = BRACKET.match(title)
        if m:
            t = m.group(1).strip()
            if t in REGIONS:
                return t
            if t in ALIAS:
                return ALIAS[t]
    if isinstance(agency, str):
        for full, short in ALIAS.items():
            if agency.startswith(full):
                return short
        for short in REGIONS:
            if agency.startswith(short):
                return short
    return None


def main():
    df = read_api()
    n_raw = len(df)
    n_dup = int(df["pblancId"].duplicated().sum())
    df = df.drop_duplicates(subset="pblancId", keep="first").reset_index(drop=True)

    summary = df["bsnsSumryCn"].map(strip_html)
    reqst = df["reqstMthPapersCn"].map(strip_html)
    amt = pd.DataFrame([parse_support(f"{s}\n{r}") for s, r in zip(summary, reqst)])

    period = df["reqstBeginEndDe"].astype(str).str.split("~", n=1, expand=True)
    d = pd.DataFrame({
        "announcement_id": df["pblancId"],
        "title": df["pblancNm"].astype(str).str.strip(),
        "category_large": df["pldirSportRealmLclasCodeNm"].map(norm_category),
        "agency": df["jrsdInsttNm"], "executor": df["excInsttNm"],
        "summary_text": summary,
        "target_text": df["trgetNm"].fillna("").astype(str),
        "hashtags": df["hashtags"].fillna("").astype(str),
        "reqst_text": reqst,
        "application_period": df["reqstBeginEndDe"],
        "application_start": pd.to_datetime(period[0].str.strip(), errors="coerce"),
        "application_end": (pd.to_datetime(period[1].str.strip(), errors="coerce")
                            if period.shape[1] > 1 else pd.NaT),
        "created_at": pd.to_datetime(df["creatPnttm"], errors="coerce"),
        "inquiry_count": pd.to_numeric(df["inqireCo"], errors="coerce"),
        "attach_file": df["printFileNm"].fillna("").astype(str),
        "attach_url": df["printFlpthNm"].fillna("").astype(str),
        "source_url": df["pblancUrl"],
    })
    d = pd.concat([d, amt], axis=1)
    d["region"] = [region_of(t, a) for t, a in zip(d["title"], d["agency"])]
    d["year"] = d["created_at"].dt.year
    d["month"] = d["created_at"].dt.month
    d["summary_len"] = d["summary_text"].str.len()

    banned = set(CANON_CATEGORIES)
    d["hashtags_safe"] = d["hashtags"].apply(
        lambda s: ",".join(x.strip() for x in s.split(",")
                           if x.strip() and x.strip() not in banned))
    d["text_for_model"] = (d["title"] + "\n" + d["summary_text"] + "\n"
                           + d["target_text"] + "\n" + d["hashtags_safe"]).str.strip()

    leak = float(d.apply(lambda r: isinstance(r["category_large"], str)
                         and r["category_large"] in [x.strip() for x in r["hashtags"].split(",")],
                         axis=1).mean())

    out = f"{PROC}/announcement_detail.parquet"
    d.to_parquet(out, index=False)
    TYPED = ["per_company", "per_project", "total_budget", "periodic"]
    save_report("s03b_table_detail.json", {
        "rows_raw": n_raw, "rows_final": len(d), "duplicate_dropped": n_dup,
        "hashtag_label_leak_rate": round(leak, 4),
        "hashtag_leak_note": "hashtags 첫 토큰이 분야 라벨과 동일 → hashtags_safe 사용",
        "created_year_dist": d["year"].value_counts().sort_index().to_dict(),
        "category_dist": d["category_large"].value_counts(dropna=False).to_dict(),
        "summary_len_median": int(d["summary_len"].median()),
        "amount_extracted": int(d["support_amount_max"].notna().sum()),
        "typed_amount": int(d["support_amount_type"].isin(TYPED).sum()),
        "amount_type_dist": d["support_amount_type"].value_counts(dropna=False).to_dict(),
        "mean_confidence": round(float(d["extraction_confidence"].mean()), 4),
        "region_coverage": round(float(d["region"].notna().mean()), 4),
        "note": "지원규모는 CSV 요약 기준. 공고 원문 추출본(S02A) 병합 후 재계산 예정",
        "output": out,
    })
    print(f"announcement_detail {len(d):,}행 → {out}")
    print(f"  [누수] 해시태그 분야 노출률 {leak:.1%} → hashtags_safe 생성")
    print(f"  금액 {int(d['support_amount_max'].notna().sum())}건 "
          f"(의미확정 {int(d['support_amount_type'].isin(TYPED).sum())}건) / 신뢰도 {d['extraction_confidence'].mean():.2f}")


if __name__ == "__main__":
    main()
