"""F04 — 공고문 원문 추출본을 announcement_detail에 병합하고 지원규모를 재계산.

CSV 요약(bsnsSumryCn)에는 "④ 규모" 한 줄뿐이라 금액의 의미가 자주 unknown으로 남는다.
공고문 원문에는 지원조건이 표로 들어있어 의미 확정률이 크게 오른다.

병합 규칙: 의미가 확정된(typed) 금액을 우선한다.
  1) 원문에서 typed 금액이 나오면 그것을 채택
  2) 아니면 CSV에서 typed 금액
  3) 둘 다 unknown이면 추출 신뢰도가 높은 쪽
출처를 support_source 컬럼에 남겨 이후 분석에서 구분할 수 있게 한다.
"""
import json
import os

import pandas as pd

from common import PROC, REPORTS, save_report
from amount_parser import parse_support

DETAIL = PROC + "/announcement_detail.parquet"
DOCS = REPORTS + "/e01_documents.jsonl"
OUT = PROC + "/announcement_detail_enriched.parquet"

TYPED = {"per_company", "per_project", "total_budget", "periodic"}
AMT_COLS = ["support_amount_raw", "support_amount_min", "support_amount_max",
            "support_amount_unit", "support_amount_type", "support_ratio",
            "support_count", "self_payment_ratio", "support_period_year",
            "n_amount_candidates", "extraction_confidence"]


def load_docs():
    """공고 1건에 문서가 여러 개면 가장 긴 본문을 대표로 삼는다."""
    best = {}
    with open(DOCS, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def main():
    d = pd.read_parquet(DETAIL)
    docs = load_docs()
    print("원문 보유 공고 %d건 / detail %d행" % (len(docs), len(d)))

    doc_text, doc_chars, doc_ocr, parsed = [], [], [], []
    for pid in d["announcement_id"].astype(str):
        r = docs.get(pid)
        if r is None:
            doc_text.append("")
            doc_chars.append(0)
            doc_ocr.append(False)
            parsed.append(None)
        else:
            doc_text.append(r["text"])
            doc_chars.append(r["n_chars"])
            doc_ocr.append(bool(r.get("needs_ocr")))
            parsed.append(parse_support(r["text"]))

    d["doc_text"] = doc_text
    d["doc_chars"] = doc_chars
    d["doc_needs_ocr"] = doc_ocr
    d["has_document"] = d["doc_chars"] > 0

    rows, source = [], []
    for i, p in enumerate(parsed):
        csv_rec = {c: d.iloc[i][c] for c in AMT_COLS}
        if p is None:
            rows.append(csv_rec)
            source.append("csv_only")
            continue
        csv_typed = csv_rec["support_amount_type"] in TYPED
        doc_typed = p["support_amount_type"] in TYPED
        if doc_typed and not csv_typed:
            pick, src = p, "doc"
        elif csv_typed and not doc_typed:
            pick, src = csv_rec, "csv"
        elif doc_typed and csv_typed:
            pick, src = (p, "doc") if p["extraction_confidence"] >= csv_rec["extraction_confidence"] else (csv_rec, "csv")
        else:
            pick, src = (p, "doc") if p["extraction_confidence"] > csv_rec["extraction_confidence"] else (csv_rec, "csv")
        rows.append({c: pick[c] for c in AMT_COLS})
        source.append(src)

    merged = pd.DataFrame(rows).add_prefix("m_")
    for c in AMT_COLS:
        d[c] = merged["m_" + c].values
    d["support_source"] = source

    d.to_parquet(OUT, index=False)

    has = d["has_document"]
    def rate(mask, col_typed=True):
        sub = d[mask]
        if col_typed:
            return round(float(sub["support_amount_type"].isin(TYPED).mean()), 4)
        return round(float(sub["support_amount_max"].notna().mean()), 4)

    rep = {
        "rows": len(d),
        "with_document": int(has.sum()),
        "document_coverage": round(float(has.mean()), 4),
        "needs_ocr": int(d["doc_needs_ocr"].sum()),
        "doc_chars_median": int(d.loc[has, "doc_chars"].median()) if has.any() else 0,
        "support_source_dist": d["support_source"].value_counts().to_dict(),
        "amount_detected_all": round(float(d["support_amount_max"].notna().mean()), 4),
        "amount_typed_all": round(float(d["support_amount_type"].isin(TYPED).mean()), 4),
        "amount_typed_with_doc": rate(has),
        "amount_typed_without_doc": rate(~has),
        "type_dist": d["support_amount_type"].value_counts(dropna=False).to_dict(),
        "mean_confidence": round(float(d["extraction_confidence"].mean()), 4),
        "ratio_extracted": int(d["support_ratio"].notna().sum()),
        "count_extracted": int(d["support_count"].notna().sum()),
        "output": OUT,
    }
    save_report("f04_merge_documents.json", rep)

    print("원문 보유 %d건 (%.1f%%) / OCR대기 %d건"
          % (rep["with_document"], rep["document_coverage"] * 100, rep["needs_ocr"]))
    print("금액 의미 확정률: 원문보유 %.1f%% vs 원문없음 %.1f%% (전체 %.1f%%)"
          % (rep["amount_typed_with_doc"] * 100, rep["amount_typed_without_doc"] * 100,
             rep["amount_typed_all"] * 100))
    print("의미별 분포:", rep["type_dist"])
    print("→ %s" % OUT)


if __name__ == "__main__":
    main()
