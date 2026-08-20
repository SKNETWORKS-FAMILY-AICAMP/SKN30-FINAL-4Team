"""F03 — business_taxonomy (설계서 4.3) + 라벨 누수 진단.

2023년 중앙부처 엑셀은 중분류/업종 라벨의 유일한 출처다.
단 `사업개요` 첫 줄 【사업개요】에 대분류/중분류/업종/부처가 그대로 적혀 있어,
이 줄을 제거하지 않고 학습하면 분류기가 정답을 그대로 읽는다.
본문(①목적/②내용/③대상)만 모델 입력으로 남긴다.
"""
import re

import pandas as pd

from common import PROC, norm_category, read_excel, save_report
from amount_parser import parse_support

TITLE_RE = re.compile(r"【공고이름】\s*(.*)")
META_RE = re.compile(r"【사업개요】\s*(.*)")
SEC = {
    "purpose": re.compile(r"①\s*목적\s*[:：]?\s*(.*?)(?=\n\s*[②③④]|\Z)", re.S),
    "content": re.compile(r"②\s*내용\s*[:：]?\s*(.*?)(?=\n\s*[①③④]|\Z)", re.S),
    "target_text": re.compile(r"③\s*대상\s*[:：]?\s*(.*?)(?=\n\s*[①②④]|\Z)", re.S),
    "scale_text": re.compile(r"④\s*규모\s*[:：]?\s*(.*?)(?=\n\s*[①②③]|\Z)", re.S),
}


def _sec(rx, t):
    m = rx.search(t)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def main():
    df = read_excel()
    n_raw = len(df)
    df = df.dropna(subset=["사업개요"]).reset_index(drop=True)

    rows, leak = [], {"대분류": 0, "중분류": 0, "업종": 0}
    for i, r in df.iterrows():
        text = str(r["사업개요"])
        tm, mm = TITLE_RE.search(text), META_RE.search(text)
        meta = mm.group(1).strip() if mm else ""
        for col in leak:
            v = r.get(col)
            if isinstance(v, str) and v.strip() and v.strip() in meta:
                leak[col] += 1
        secs = {k: _sec(rx, text) for k, rx in SEC.items()}
        rows.append({
            "row_id": f"EXCEL2023_{i:04d}",
            "title": tm.group(1).strip() if tm else "",
            "large_category": norm_category(r.get("대분류")),
            "middle_category": r.get("중분류") or None,
            "industry": r.get("업종") or None,
            "agency": r.get("부처") or None,
            "executor": r.get("수행기관") or None,
            "business_type": r.get("사업유형") or None,
            "source_url": r.get("공고링크") or None,
            "meta_line_leak": meta,
            **secs,
            **parse_support(secs["scale_text"]),
        })

    t = pd.DataFrame(rows)
    # 모델 입력: 메타 줄 제외. 규모는 모델3 타깃 원천이라 입력에서 분리 유지.
    t["text_for_model"] = (t["title"] + "\n" + t["purpose"] + "\n"
                           + t["content"] + "\n" + t["target_text"]).str.strip()

    out = f"{PROC}/business_taxonomy.parquet"
    t.to_parquet(out, index=False)
    save_report("f03_taxonomy.json", {
        "rows_raw": n_raw, "rows_final": len(t),
        "label_leak": {
            "설명": "사업개요 첫 줄 【사업개요】에 대분류/중분류/업종이 그대로 기재됨",
            **{f"{k}_노출률": round(v / len(t), 4) for k, v in leak.items()},
            "조치": "text_for_model에서 메타 줄 제외, 본문(①②③)만 사용",
        },
        "section_coverage": {k: round(float((t[k] != "").mean()), 4) for k in SEC},
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
    print(f"  [누수] 대분류 노출률 {leak['대분류']/len(t):.1%} / 중분류 {leak['중분류']/len(t):.1%} / 업종 {leak['업종']/len(t):.1%}")
    print(f"  중분류 {t['middle_category'].nunique()}종 / 업종 {t['industry'].nunique()}종")
    print(f"  금액 추출 {int(t['support_amount_max'].notna().sum())}건 / 신뢰도 {t['extraction_confidence'].mean():.2f}")


if __name__ == "__main__":
    main()
