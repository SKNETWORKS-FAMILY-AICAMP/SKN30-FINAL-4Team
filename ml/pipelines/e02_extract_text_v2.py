"""E02 — 공고문 원문 텍스트 추출 v2 (표 구조 보존).

E01(PyMuPDF + LibreOffice) 대비 두 가지를 개선한다.

  PDF  : pdf-inspector (Rust)
         - extract_pages_markdown 이 표를 마크다운 표로 복원한다.
           PyMuPDF 는 셀을 줄바꿈으로 흩어놓아 행-열 관계가 소실됐다.
         - classify_pdf 가 스캔본/텍스트본을 판별한다(신뢰도 포함).
           E01 은 '페이지당 50자 미만' 휴리스틱을 썼다.

  HWP  : rhwp-python (edwardkim/rhwp 의 PyO3 바인딩)
         - pyhwp 는 표 내용을 '<표>' 로 비워버려 지원규모가 통째로 날아갔다.
         - LibreOffice 가 필요 없어 로컬에서 처리된다(원격 박스 불필요).

  HWPX : ZIP+XML 직접 파싱 유지. rhwp 도 지원하므로 실패 시 폴백.

출력: JSONL (공고 1건 = 문서 1개당 1줄)
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

import argparse
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ATT, REPORTS, save_report

HP_T = re.compile(r"<hp:t>(.*?)</hp:t>", re.S)
XML_TAG = re.compile(r"<[^>]+>")
SCAN_CHARS_PER_PAGE = 50


def _unesc(s):
    for a, b in [("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                 ("&quot;", '"'), ("&apos;", "'")]:
        s = s.replace(a, b)
    return s


def extract_pdf(path):
    """pdf-inspector. 표를 마크다운으로 살리고 스캔본을 분류한다."""
    import pdf_inspector as pi
    cls = pi.classify_pdf(path)
    pdf_type = getattr(cls, "pdf_type", "unknown")
    n_pages = int(getattr(cls, "pages", 0) or 0)
    conf = float(getattr(cls, "confidence", 0.0) or 0.0)

    res = pi.extract_pages_markdown(path)
    pages = getattr(res, "pages", res)
    parts = []
    for p in pages:
        md = getattr(p, "markdown", None)
        if md is None:
            md = str(p)
        if md.strip():
            parts.append(md)
    text = "\n\n".join(parts)

    needs_ocr = (pdf_type == "scanned") or (
        n_pages > 0 and len(text) / n_pages < SCAN_CHARS_PER_PAGE)
    return {"text": text, "n_pages": n_pages, "method": "pdf_inspector",
            "pdf_type": pdf_type, "classify_confidence": round(conf, 3),
            "needs_ocr": bool(needs_ocr), "has_table": "|---" in text}


def extract_hwp(path):
    """rhwp-python. 표 내용이 보존된다."""
    import rhwp
    doc = rhwp.parse(path)
    text = doc.extract_text()
    return {"text": text, "n_pages": int(doc.page_count or 0),
            "method": "rhwp", "pdf_type": "", "classify_confidence": None,
            "needs_ocr": False, "has_table": False}


def extract_hwpx_zip(path):
    """HWPX = ZIP. Contents/section*.xml 의 <hp:t> 를 순서대로 잇는다."""
    with zipfile.ZipFile(path) as z:
        secs = sorted(n for n in z.namelist()
                      if re.match(r"Contents/section\d+\.xml$", n))
        if not secs:
            raise ValueError("no section xml")
        parts = []
        for s in secs:
            xml = z.read(s).decode("utf-8", errors="replace")
            for m in HP_T.finditer(xml):
                t = _unesc(XML_TAG.sub("", m.group(1)))
                if t.strip():
                    parts.append(t)
            parts.append("\n")
    return {"text": "\n".join(parts), "n_pages": 0, "method": "hwpx_xml",
            "pdf_type": "", "classify_confidence": None,
            "needs_ocr": False, "has_table": False}


def process(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    base = {"ext": ext, "n_pages": 0, "n_chars": 0, "method": "",
            "pdf_type": "", "classify_confidence": None,
            "needs_ocr": False, "has_table": False, "text": "", "error": ""}
    try:
        if ext == "pdf":
            r = extract_pdf(path)
        elif ext in ("hwp",):
            r = extract_hwp(path)
        elif ext == "hwpx":
            try:
                r = extract_hwpx_zip(path)
            except Exception:
                r = extract_hwp(path)       # rhwp 도 hwpx 를 읽는다
        else:
            base["error"] = "unsupported_ext"
            return base
        base.update(r)
        base["n_chars"] = len(base["text"])
    except Exception as e:
        base["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["api", "list", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=REPORTS + "/e02_documents.jsonl")
    args = ap.parse_args()

    roots = ["api", "list"] if args.source == "all" else [args.source]
    files = []
    for r in roots:
        base = os.path.join(ATT, r)
        for dirpath, _, names in os.walk(base):
            for nm in names:
                files.append((r, os.path.basename(dirpath), os.path.join(dirpath, nm)))
    files.sort()
    if args.limit:
        files = files[: args.limit]
    print("대상 파일 %d개" % len(files), flush=True)

    stats = {"ok": 0, "fail": 0, "needs_ocr": 0, "with_table": 0}
    by_ext, by_method, errs = {}, {}, {}
    with open(args.out, "w", encoding="utf-8") as fo:
        for i, (src, pid, path) in enumerate(files, 1):
            rec = process(path)
            rec.update(source=src, announcement_id=pid,
                       filename=os.path.basename(path))
            by_ext[rec["ext"]] = by_ext.get(rec["ext"], 0) + 1
            m = rec["method"] or "none"
            by_method[m] = by_method.get(m, 0) + 1
            if rec["n_chars"] > 0:
                stats["ok"] += 1
            else:
                stats["fail"] += 1
                if rec["error"]:
                    k = rec["error"].split(":")[0]
                    errs[k] = errs.get(k, 0) + 1
            if rec["needs_ocr"]:
                stats["needs_ocr"] += 1
            if rec["has_table"]:
                stats["with_table"] += 1
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if i % 250 == 0 or i == len(files):
                print("  %d/%d  성공 %d 실패 %d 표보존 %d OCR대기 %d"
                      % (i, len(files), stats["ok"], stats["fail"],
                         stats["with_table"], stats["needs_ocr"]), flush=True)

    save_report("e02_extract.json", {
        "files": len(files), **stats,
        "success_rate": round(stats["ok"] / max(len(files), 1), 4),
        "table_preserved_rate": round(stats["with_table"] / max(stats["ok"], 1), 4),
        "by_ext": by_ext, "by_method": by_method, "errors": errs,
        "output": args.out,
        "tools": {"pdf": "pdf-inspector (firecrawl)",
                  "hwp": "rhwp-python (edwardkim/rhwp)",
                  "hwpx": "zip+xml, rhwp fallback"},
    })
    print("\n완료: 성공 %d / 실패 %d / 표보존 %d / OCR대기 %d"
          % (stats["ok"], stats["fail"], stats["with_table"], stats["needs_ocr"]))
    if errs:
        print("오류 유형:", errs)


if __name__ == "__main__":
    main()
