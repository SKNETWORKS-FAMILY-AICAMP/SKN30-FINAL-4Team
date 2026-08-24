"""D01 — Open API 1,570건 공고문 원문 다운로드.

printFlpthNm에 직접 다운로드 URL(/cmm/fms/getImageFile.do)이 이미 있어
상세페이지 크롤링 없이 파일당 1회 요청으로 받는다.

인증 조건(실측): JSESSIONID 쿠키 + Referer 헤더가 없으면 403.
재실행하면 manifest를 읽어 받은 것은 건너뛴다.
"""
import argparse
import csv
import os
import sys
import time
from urllib.parse import unquote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ATT, DETAIL_URL, REPORTS, UA, read_api, safe_name, save_report)

OUT_DIR = os.path.join(ATT, "api")
MANIFEST = os.path.join(REPORTS, "d01_manifest_api.csv")
FIELDS = ["pblancId", "seq", "url", "status", "http", "content_type",
          "filename", "size", "path", "error"]


def load_done():
    done = set()
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "ok":
                    done.add((r["pblancId"], r["seq"]))
    return done


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def filename_from(resp, fallback):
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        raw = cd.split("filename=", 1)[1].strip().strip('"')
        try:
            name = unquote(raw)
            if name.strip():
                return name.strip()
        except Exception:
            pass
    return fallback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.0, help="요청 간격(초)")
    ap.add_argument("--limit", type=int, default=0, help="상위 N건만 (0=전체)")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    df = read_api()
    tasks = []
    for _, r in df.iterrows():
        pid = str(r["pblancId"])
        urls = [u.strip() for u in str(r.get("printFlpthNm") or "").split("@") if u.strip()]
        names = [n.strip() for n in str(r.get("printFileNm") or "").split("@") if n.strip()]
        for i, u in enumerate(urls):
            fb = names[i] if i < len(names) else f"{pid}_{i}"
            tasks.append((pid, str(i), u, fb))
    if args.limit:
        tasks = tasks[: args.limit]

    done = load_done()
    todo = [t for t in tasks if (t[0], t[1]) not in done]
    print(f"전체 대상 {len(tasks):,}건 / 완료 {len(done):,}건 / 이번 실행 {len(todo):,}건")
    print(f"간격 {args.delay}s → 예상 {len(todo) * args.delay / 60:.0f}분\n", flush=True)
    if not todo:
        print("받을 것이 없습니다.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    new = not os.path.exists(MANIFEST)
    mf = open(MANIFEST, "a", encoding="utf-8", newline="")
    w = csv.DictWriter(mf, fieldnames=FIELDS)
    if new:
        w.writeheader()

    s = make_session()
    # 세션 쿠키 확보 (첫 공고 상세페이지 1회)
    try:
        s.get(DETAIL_URL.format(todo[0][0]), timeout=30)
    except Exception as e:
        print(f"[warn] 세션 확보 실패: {e}")

    ok = fail = 0
    t0 = time.time()
    for n, (pid, seq, url, fb) in enumerate(todo, 1):
        ref = DETAIL_URL.format(pid)
        rec = {"pblancId": pid, "seq": seq, "url": url, "status": "fail",
               "http": "", "content_type": "", "filename": "", "size": 0,
               "path": "", "error": ""}
        for attempt in range(args.retries):
            try:
                resp = s.get(url, headers={"Referer": ref}, timeout=60)
                rec["http"] = resp.status_code
                rec["content_type"] = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and len(resp.content) > 0:
                    name = safe_name(filename_from(resp, fb))
                    d = os.path.join(OUT_DIR, pid)
                    os.makedirs(d, exist_ok=True)
                    path = os.path.join(d, f"{seq}_{name}")
                    with open(path, "wb") as fo:
                        fo.write(resp.content)
                    rec.update(status="ok", filename=name, size=len(resp.content),
                               path=os.path.relpath(path, ATT))
                    ok += 1
                    break
                rec["error"] = f"HTTP {resp.status_code}"
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:200]
            if attempt < args.retries - 1:
                time.sleep(2 ** attempt)          # 백오프
        if rec["status"] != "ok":
            fail += 1
        w.writerow(rec)
        mf.flush()

        if n % 50 == 0 or n == len(todo):
            el = time.time() - t0
            eta = el / n * (len(todo) - n) / 60
            print(f"  {n:,}/{len(todo):,}  성공 {ok:,} 실패 {fail:,}  "
                  f"경과 {el/60:.1f}분  잔여 ~{eta:.0f}분", flush=True)
        time.sleep(args.delay)

    mf.close()
    save_report("d01_download_api.json", {
        "targets": len(tasks), "attempted": len(todo), "ok": ok, "fail": fail,
        "success_rate": round(ok / max(len(todo), 1), 4),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "delay_sec": args.delay, "out_dir": OUT_DIR, "manifest": MANIFEST,
    })
    print(f"\n완료: 성공 {ok:,} / 실패 {fail:,}")


if __name__ == "__main__":
    main()
