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
# E01(pdf-inspector + rhwp)를 우선 쓰고 없으면 E01로 폴백한다. A02와 같은 규칙.
DOCS_V2 = REPORTS + "/e01_documents.jsonl"
DOCS_LEGACY = REPORTS + "/e01_documents_api.jsonl"
OUT = PROC + "/announcement_detail_enriched.parquet"

# PDF 는 표의 여러 행이 한 셀로 뭉쳐 나와 금액이 왜곡된다.
#   - 한 셀에 금액 3개 이상 병합: 표 보유 PDF 의 13.6% (HWP 는 0.0%)
#   - 예: "238만원|119만원|476만원|1,900만원" 4행이 뭉쳐 39억원으로 파싱
# F05 가 이미 같은 기준으로 PDF 를 빼고 있는데 이 병합본만 PDF 값을 갖고 있으면
# F05 의 요약문 폴백 경로로 오염된 금액이 되돌아온다. 기준을 맞춘다.
EXCLUDE_EXT = {"pdf"}

TYPED = {"per_company", "per_project", "total_budget", "periodic"}
AMT_COLS = ["support_amount_raw", "support_amount_min", "support_amount_max",
            "support_amount_unit", "support_amount_type", "support_ratio",
            "support_count", "self_payment_ratio", "support_period_year",
            "n_amount_candidates", "extraction_confidence"]


def load_docs(path, source=None, exclude_ext=EXCLUDE_EXT):
    """공고 1건에 문서가 여러 개면 가장 긴 본문을 대표로 삼는다.

    source 를 주면 해당 출처만 추린다(E01 는 api/list 를 한 파일에 담는다).
    exclude_ext 에 든 확장자는 대표 선정에서 제외한다.
    """
    best = {}
    if not os.path.exists(path):
        return best
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            if source and r.get("source") not in (None, source):
                continue
            if exclude_ext and (r.get("ext") or "").lower() in exclude_ext:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def pick_docs():
    """E01 가 있으면 그것을, 없으면 E01 결과를 쓴다."""
    d = load_docs(DOCS_V2, source="api")
    if d:
        return d, "e02"
    return load_docs(DOCS_LEGACY), "e01"


def main():
    d = pd.read_parquet(DETAIL)
    docs, docs_ver = pick_docs()
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
        "docs_version": docs_ver,
        "excluded_ext": sorted(EXCLUDE_EXT),
        "exclusion_reason": (
            "PDF 는 표의 여러 행이 한 셀로 뭉쳐 나와 금액이 왜곡된다. 표 보유 PDF 의 "
            "13.6%가 한 셀에 금액 3개 이상 병합되며(HWP 0%), F05 도 같은 기준으로 "
            "PDF 를 제외한다. 이 병합본만 PDF 값을 가지면 F05 의 요약문 폴백 경로로 "
            "오염된 금액이 되돌아온다."),
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
