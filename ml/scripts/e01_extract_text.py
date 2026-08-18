"""E01 — 첨부 공고문에서 본문 텍스트 추출.

형식별 3트랙:
  PDF   → PyMuPDF 직접 추출. 텍스트가 거의 없으면 스캔본으로 판정해 OCR 대기로 표시
  HWPX  → ZIP+XML 구조라 Contents/section*.xml의 <hp:t>를 직접 파싱 (외부 도구 불필요)
  HWP   → 구형 OLE 바이너리. LibreOffice(+H2Orestart)로 PDF 변환 후 PDF 트랙에 태움

출력: reports/e01_documents.jsonl  (공고 1건 = 1줄)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ATT, REPORTS, save_report

OUT = os.path.join(REPORTS, "e01_documents.jsonl")
SCAN_CHARS_PER_PAGE = 50      # 페이지당 이 미만이면 스캔본으로 간주
HP_T = re.compile(r"<hp:t>(.*?)</hp:t>", re.S)
XML_TAG = re.compile(r"<[^>]+>")


def _unesc(s):
    for a, b in [("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                 ("&quot;", '"'), ("&apos;", "'")]:
        s = s.replace(a, b)
    return s


def extract_hwpx(path):
    """HWPX = ZIP. Contents/section*.xml 안의 <hp:t> 텍스트를 순서대로 잇는다."""
    try:
        with zipfile.ZipFile(path) as z:
            secs = sorted(n for n in z.namelist()
                          if re.match(r"Contents/section\d+\.xml$", n))
            if not secs:
                return None, "hwpx_no_section"
            parts = []
            for s in secs:
                xml = z.read(s).decode("utf-8", errors="replace")
                for m in HP_T.finditer(xml):
                    t = _unesc(XML_TAG.sub("", m.group(1)))
                    if t.strip():
                        parts.append(t)
                parts.append("\n")
            return "\n".join(parts), "hwpx_xml"
    except Exception as e:
        return None, f"hwpx_error:{type(e).__name__}"


def extract_pdf(path):
    try:
        import fitz
    except ImportError:
        return None, 0, "pymupdf_missing"
    try:
        d = fitz.open(path)
        pages = [d[i].get_text() for i in range(d.page_count)]
        n = d.page_count
        d.close()
        return "\n".join(pages), n, "pymupdf"
    except Exception as e:
        return None, 0, f"pdf_error:{type(e).__name__}"


def soffice_to_pdf(path, outdir):
    """LibreOffice headless 변환. 성공 시 생성된 PDF 경로 반환."""
    for exe in ("soffice", "libreoffice"):
        try:
            subprocess.run(
                [exe, "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", outdir, path],
                check=True, capture_output=True, timeout=180)
            got = glob.glob(os.path.join(outdir, "*.pdf"))
            if got:
                return got[0]
        except FileNotFoundError:
            continue
        except Exception:
            return None
    return None


def process(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    rec = {"ext": ext, "n_pages": 0, "n_chars": 0, "method": "",
           "needs_ocr": False, "text": "", "error": ""}

    if ext == "hwpx":
        txt, method = extract_hwpx(path)
        if txt is not None:
            rec.update(text=txt, method=method, n_chars=len(txt))
            return rec
        rec["error"] = method                      # 실패 시 LibreOffice로 폴백

    if ext == "pdf":
        txt, n, method = extract_pdf(path)
        if txt is not None:
            rec.update(text=txt, method=method, n_pages=n, n_chars=len(txt))
            rec["needs_ocr"] = n > 0 and len(txt) / n < SCAN_CHARS_PER_PAGE
            return rec
        rec["error"] = method
        return rec

    # hwp / doc / docx / hwpx 폴백 → LibreOffice 변환
    with tempfile.TemporaryDirectory() as td:
        pdf = soffice_to_pdf(path, td)
        if not pdf:
            rec["error"] = rec["error"] or "soffice_failed"
            rec["method"] = "soffice_unavailable"
            return rec
        txt, n, method = extract_pdf(pdf)
        if txt is None:
            rec["error"] = method
            return rec
        rec.update(text=txt, method="soffice+" + method, n_pages=n, n_chars=len(txt))
        rec["needs_ocr"] = n > 0 and len(txt) / n < SCAN_CHARS_PER_PAGE
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source", choices=["api", "list", "all"], default="all")
    ap.add_argument("--out", default=OUT)
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
    print(f"대상 파일 {len(files):,}개", flush=True)

    stats = {"ok": 0, "fail": 0, "needs_ocr": 0}
    by_method, by_ext = {}, {}
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
            if rec["needs_ocr"]:
                stats["needs_ocr"] += 1
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if i % 200 == 0 or i == len(files):
                print(f"  {i:,}/{len(files):,}  성공 {stats['ok']:,} "
                      f"실패 {stats['fail']:,} OCR대기 {stats['needs_ocr']:,}", flush=True)

    save_report("e01_extract.json", {
        "files": len(files), **stats,
        "success_rate": round(stats["ok"] / max(len(files), 1), 4),
        "by_ext": by_ext, "by_method": by_method, "output": args.out,
        "scan_threshold_chars_per_page": SCAN_CHARS_PER_PAGE,
    })
    print(f"\n완료: 성공 {stats['ok']:,} / 실패 {stats['fail']:,} / OCR대기 {stats['needs_ocr']:,}")


if __name__ == "__main__":
    main()
