"""S01D — 목록 표본 5,000건의 공고문 원문 다운로드.

목록 CSV에는 파일 URL이 없다. 상세페이지를 1회 요청해
`/cmm/fms/fileDown.do?atchFileId=...&fileSn=N` 링크를 뽑은 뒤 파일을 받는다.
공고당 요청 2회(페이지 1 + 파일 1)로 억제하기 위해 문서 1개만 고른다.

우선순위: '본문출력파일' 섹션 > 첨부 중 pdf/hwp/hwpx > 없으면 건너뜀
(zip·이미지는 제외 — 압축 해제/OCR 비용 대비 이득이 낮다)
"""
import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import unquote

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ATT, BIZINFO, DETAIL_URL, PROC, REPORTS, UA, safe_name,
                    save_report)

OUT_DIR = os.path.join(ATT, "list")
MANIFEST = os.path.join(REPORTS, "s01d_manifest_list.csv")
SAMPLE = os.path.join(PROC, "list_sample.parquet")
FIELDS = ["announcement_id", "status", "section", "filename", "ext",
          "atch_url", "size", "path", "error"]

DOC_EXT = ("pdf", "hwp", "hwpx", "docx", "doc")


def load_done(manifest_path=MANIFEST):
    done = set()
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") in ("ok", "no_doc"):
                    done.add(r["announcement_id"])
    return done


def pick_file(html):
    """상세페이지에서 받을 문서 1개를 고른다. (section, name, ext, url) 또는 None."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("div.attached_file_list")
    if not box:
        return None
    section = ""
    cands = []
    for el in box.find_all(["h3", "li"]):
        if el.name == "h3":
            section = el.get_text(strip=True)
            continue
        nm = el.select_one(".file_name")
        a = None
        for link in el.find_all("a", href=True):
            if "fileDown.do" in link["href"]:
                a = link
                break
        if not nm or not a:
            continue
        name = nm.get_text(strip=True)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        cands.append({"section": section, "name": name, "ext": ext,
                      "url": BIZINFO + a["href"]})
    if not cands:
        return None
    body = [c for c in cands if "본문출력" in c["section"] and c["ext"] in DOC_EXT]
    if body:
        return body[0]
    docs = [c for c in cands if c["ext"] in DOC_EXT]
    return docs[0] if docs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--sample", default=SAMPLE,
                    help="받을 표본 parquet 경로. 기본은 S01B 의 5,000건. "
                         "S01C 겨냥 표본(list_sample_targeted.parquet)을 넘기면 "
                         "그쪽을 받는다 — 파일 자체(HWP/PDF)는 첨부함(announcement_id)에 "
                         "묶이므로 어느 표본에서 왔든 저장 위치는 같다.")
    ap.add_argument("--manifest", default=None,
                    help="매니페스트 경로. 지정하지 않으면 --sample 이 기본값(S01B 5,000건)일 "
                         "때는 기존 매니페스트를, 그 외(S01C 겨냥 표본 등)에는 자동으로 "
                         "별도 파일(s01d_manifest_<sample명>.csv)을 써서 목적별로 분리한다.")
    args = ap.parse_args()

    if args.manifest:
        manifest_path = args.manifest
    elif args.sample == SAMPLE:
        manifest_path = MANIFEST
    else:
        stem = os.path.splitext(os.path.basename(args.sample))[0]
        manifest_path = os.path.join(REPORTS, "s01d_manifest_%s.csv" % stem)

    s_df = pd.read_parquet(args.sample)
    ids = s_df["announcement_id"].astype(str).tolist()
    if args.limit:
        ids = ids[: args.limit]
    done = load_done(manifest_path)
    todo = [i for i in ids if i not in done]
    print(f"표본 {len(ids):,}건 / 완료 {len(done):,}건 / 이번 실행 {len(todo):,}건")
    print(f"공고당 2요청, 간격 {args.delay}s → 예상 {len(todo)*2*args.delay/60:.0f}분\n", flush=True)
    if not todo:
        print("받을 것이 없습니다.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    new = not os.path.exists(manifest_path)
    mf = open(manifest_path, "a", encoding="utf-8", newline="")
    w = csv.DictWriter(mf, fieldnames=FIELDS)
    if new:
        w.writeheader()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

    ok = nodoc = fail = 0
    t0 = time.time()
    for n, pid in enumerate(todo, 1):
        ref = DETAIL_URL.format(pid)
        rec = {"announcement_id": pid, "status": "fail", "section": "",
               "filename": "", "ext": "", "atch_url": "", "size": 0,
               "path": "", "error": ""}
        try:
            page = sess.get(ref, timeout=60, allow_redirects=True)
            time.sleep(args.delay)
            if page.status_code != 200:
                rec["error"] = f"page HTTP {page.status_code}"
            else:
                cand = pick_file(page.text)
                if cand is None:
                    rec["status"] = "no_doc"
                    nodoc += 1
                else:
                    rec.update(section=cand["section"], filename=cand["name"],
                               ext=cand["ext"], atch_url=cand["url"])
                    for attempt in range(args.retries):
                        try:
                            r = sess.get(cand["url"], headers={"Referer": ref}, timeout=90)
                            if r.status_code == 200 and r.content:
                                cd = r.headers.get("Content-Disposition", "")
                                nm = cand["name"]
                                if "filename=" in cd:
                                    try:
                                        nm = unquote(cd.split("filename=", 1)[1].strip().strip('"')) or nm
                                    except Exception:
                                        pass
                                d = os.path.join(OUT_DIR, pid)
                                os.makedirs(d, exist_ok=True)
                                path = os.path.join(d, safe_name(nm))
                                with open(path, "wb") as fo:
                                    fo.write(r.content)
                                rec.update(status="ok", size=len(r.content),
                                           path=os.path.relpath(path, ATT))
                                ok += 1
                                break
                            rec["error"] = f"file HTTP {r.status_code}"
                        except Exception as e:
                            rec["error"] = f"{type(e).__name__}: {e}"[:180]
                        if attempt < args.retries - 1:
                            time.sleep(2 ** attempt)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"[:180]
        if rec["status"] == "fail":
            fail += 1
        w.writerow(rec)
        mf.flush()

        if n % 50 == 0 or n == len(todo):
            el = time.time() - t0
            print(f"  {n:,}/{len(todo):,}  ok {ok:,} / 문서없음 {nodoc:,} / 실패 {fail:,}  "
                  f"경과 {el/60:.1f}분  잔여 ~{el/n*(len(todo)-n)/60:.0f}분", flush=True)
        time.sleep(args.delay)

    mf.close()
    report_tag = "targeted" if args.sample != SAMPLE else "list"
    save_report("s01d_download_%s.json" % report_tag, {
        "sample": len(ids), "attempted": len(todo), "ok": ok,
        "no_doc": nodoc, "fail": fail,
        "success_rate": round(ok / max(len(todo), 1), 4),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "delay_sec": args.delay, "out_dir": OUT_DIR, "manifest": manifest_path,
        "sample_source": args.sample,
    })
    print(f"\n완료: 성공 {ok:,} / 문서없음 {nodoc:,} / 실패 {fail:,}")


if __name__ == "__main__":
    main()
