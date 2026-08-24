"""F03 — business_taxonomy (설계서 4.3) + 라벨 누수 진단.

중앙부처 엑셀은 중분류/업종 라벨의 유일한 출처다. 2022·2023 두 해를 병합해 쓴다.
본문 구조(【공고이름】/【사업개요】/①~④)가 두 해 완전히 같아 같은 파서로 처리된다.
컬럼명만 다른데(2022 사업내용/대유형/중유형) common.read_excel_2022 이 맞춰준다.

주의 1 — 라벨 누수
    `사업개요` 첫 줄 【사업개요】에 대분류/중분류/업종/부처가 그대로 적혀 있어,
    이 줄을 제거하지 않고 학습하면 분류기가 정답을 그대로 읽는다.
    본문(①목적/②내용/③대상)만 모델 입력으로 남긴다.

주의 2 — 연도 간 중복
    두 해에 같은 사업이 반복 공고된다(제목에서 연도만 빼면 524건이 겹친다).
    본문까지 사실상 동일한 건을 양쪽 다 넣으면 CV 에서 같은 텍스트가 학습·검증에
    나눠 들어가 성능이 부풀려진다. 실측으로 macro F1 이 0.64 -> 0.77 로 뛰었는데,
    그룹 분리 CV 로 다시 재니 0.645 였다. 즉 대부분이 누수였다.
    그래서 본문 유사도 0.9 이상인 2022 건은 버리고 2023 쪽만 남긴다.
    유사도가 그보다 낮은 건(사업 내용이 실제로 바뀐 건)은 둘 다 남기되,
    `program_stem` 으로 묶어 두어 평가 때 그룹 분리에 쓸 수 있게 한다.
"""
import difflib
import re

import pandas as pd

from common import (PROC, norm_category, read_excel, read_excel_2022,
                    save_report)
from amount_parser import parse_support

TITLE_RE = re.compile(r"【공고이름】\s*(.*)")
META_RE = re.compile(r"【사업개요】\s*(.*)")
YEAR_RE = re.compile(r"20\d{2}\s*년\s*")
SEC = {
    "purpose": re.compile(r"①\s*목적\s*[:：]?\s*(.*?)(?=\n\s*[②③④]|\Z)", re.S),
    "content": re.compile(r"②\s*내용\s*[:：]?\s*(.*?)(?=\n\s*[①③④]|\Z)", re.S),
    "target_text": re.compile(r"③\s*대상\s*[:：]?\s*(.*?)(?=\n\s*[①②④]|\Z)", re.S),
    "scale_text": re.compile(r"④\s*규모\s*[:：]?\s*(.*?)(?=\n\s*[①②③]|\Z)", re.S),
}

DUP_THRESHOLD = 0.9   # 본문 유사도가 이 이상이면 같은 사업의 재공고로 본다


def _sec(rx, t):
    m = rx.search(t)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def program_stem(title):
    """'2022년 수출바우처' 와 '2023년 수출바우처' 를 같은 사업으로 묶기 위한 키."""
    return YEAR_RE.sub("", title or "").strip()


LEAK_COLS = ("대분류", "중분류", "업종")


def parse_year(df, year):
    """한 해치 엑셀을 공통 스키마로 파싱.

    누수 여부는 행별 플래그(_leak_*)로 남긴다. 카운터를 돌려 합산하면 중복 제거
    전 건수로 세고 제거 후 건수로 나누게 돼 100%를 넘는 값이 나온다.
    """
    rows = []
    for i, r in df.iterrows():
        text = str(r["사업개요"])
        tm, mm = TITLE_RE.search(text), META_RE.search(text)
        meta = mm.group(1).strip() if mm else ""
        flags = {}
        for col in LEAK_COLS:
            v = r.get(col)
            flags[f"_leak_{col}"] = bool(
                isinstance(v, str) and v.strip() and v.strip() in meta)
        secs = {k: _sec(rx, text) for k, rx in SEC.items()}
        title = tm.group(1).strip() if tm else ""
        rows.append({
            "row_id": f"EXCEL{year}_{i:04d}",
            "source_year": year,
            "title": title,
            "program_stem": program_stem(title),
            "large_category": norm_category(r.get("대분류")),
            "middle_category": r.get("중분류") or None,
            "industry": r.get("업종") or None,
            "agency": r.get("부처") or None,
            "executor": r.get("수행기관") or None,
            "business_type": r.get("사업유형") or None,   # 2022 에는 없는 컬럼
            "source_url": r.get("공고링크") or None,
            "meta_line_leak": meta,
            **flags,
            **secs,
            **parse_support(secs["scale_text"]),
        })
    return pd.DataFrame(rows)


def drop_reannouncements(old, new):
    """new(2023)에 사실상 같은 본문이 이미 있는 old(2022) 행을 버린다."""
    body_new = {}
    for stem, body in zip(new["program_stem"], new["_body"]):
        body_new.setdefault(stem, []).append(body)

    def is_dup(r):
        for b in body_new.get(r["program_stem"], ()):
            if difflib.SequenceMatcher(None, r["_body"], b).ratio() >= DUP_THRESHOLD:
                return True
        return False

    mask = old.apply(is_dup, axis=1)
    return old[~mask], int(mask.sum())


def main():
    raw22 = read_excel_2022().dropna(subset=["사업개요"]).reset_index(drop=True)
    raw23 = read_excel().dropna(subset=["사업개요"]).reset_index(drop=True)
    n_raw = len(raw22) + len(raw23)

    t22 = parse_year(raw22, 2022)
    t23 = parse_year(raw23, 2023)
    for d in (t22, t23):
        d["_body"] = d["purpose"] + " " + d["content"] + " " + d["target_text"]

    t22_kept, n_dup = drop_reannouncements(t22, t23)
    t = pd.concat([t23, t22_kept], ignore_index=True).drop(columns=["_body"])
    leak = {c: int(t[f"_leak_{c}"].sum()) for c in LEAK_COLS}
    t = t.drop(columns=[f"_leak_{c}" for c in LEAK_COLS])

    # 모델 입력: 메타 줄 제외. 규모는 모델2 타깃 원천이라 입력에서 분리 유지.
    t["text_for_model"] = (t["title"] + "\n" + t["purpose"] + "\n"
                           + t["content"] + "\n" + t["target_text"]).str.strip()

    out = f"{PROC}/business_taxonomy.parquet"
    t.to_parquet(out, index=False)

    n_shared = int(t.groupby("program_stem")["source_year"].nunique().gt(1).sum())
    save_report("f03_taxonomy.json", {
        "sources": {"2022": len(raw22), "2023": len(raw23)},
        "rows_raw": n_raw, "rows_final": len(t),
        "reannouncement_dedup": {
            "설명": "제목에서 연도를 뺀 사업명이 같고 본문 유사도가 임계값 이상인 "
                    "2022 건은 2023 재공고로 보고 제외",
            "threshold": DUP_THRESHOLD,
            "dropped_2022_rows": n_dup,
            "kept_2022_rows": int(len(t22_kept)),
            "근거": "중복을 남기면 CV 에서 같은 텍스트가 학습·검증에 나뉘어 들어가 "
                    "macro F1 이 0.637 -> 0.770 으로 부풀려졌다(그룹분리 CV 는 0.645)",
        },
        "program_stem_shared_across_years": n_shared,
        "label_leak": {
            "설명": "사업개요 첫 줄 【사업개요】에 대분류/중분류/업종이 그대로 기재됨",
            **{f"{k}_노출률": round(v / len(t), 4) for k, v in leak.items()},
            "조치": "text_for_model에서 메타 줄 제외, 본문(①②③)만 사용",
        },
        "section_coverage": {k: round(float((t[k] != "").mean()), 4) for k in SEC},
        "by_year": t["source_year"].value_counts().sort_index().to_dict(),
        "large_category": t["large_category"].value_counts(dropna=False).to_dict(),
        "business_type": t["business_type"].value_counts(dropna=False).to_dict(),
        "middle_category_n": int(t["middle_category"].nunique()),
        "industry_n": int(t["industry"].nunique()),
        "amount_extracted": int(t["support_amount_max"].notna().sum()),
        "amount_type_dist": t["support_amount_type"].value_counts(dropna=False).to_dict(),
        "mean_confidence": round(float(t["extraction_confidence"].mean()), 4),
        "output": out,
    })
    print(f"business_taxonomy {len(t):,}행 → {out}")
    print(f"  [원천] 2022 {len(raw22):,}행 + 2023 {len(raw23):,}행")
    print(f"  [중복] 재공고로 판정해 제외한 2022 행 {n_dup:,}건 (유사도 ≥ {DUP_THRESHOLD})")
    print(f"  [연도] " + " / ".join(f"{y} {n:,}행" for y, n in
                                    t["source_year"].value_counts().sort_index().items()))
    print(f"  [누수] 대분류 노출률 {leak['대분류']/len(t):.1%} / 중분류 {leak['중분류']/len(t):.1%} / 업종 {leak['업종']/len(t):.1%}")
    print(f"  중분류 {t['middle_category'].nunique()}종 / 업종 {t['industry'].nunique()}종")
    print(f"  두 해에 모두 나온 사업 {n_shared:,}개 (program_stem 기준, 그룹분리 CV 용)")
    print(f"  금액 추출 {int(t['support_amount_max'].notna().sum())}건 / 신뢰도 {t['extraction_confidence'].mean():.2f}")


if __name__ == "__main__":
    main()
