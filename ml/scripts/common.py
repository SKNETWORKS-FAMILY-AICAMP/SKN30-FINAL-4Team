"""Pre-Review ML/DL 공통 모듈 — 원천 경로, 분류체계, 텍스트 정규화."""
import os
import re
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ml/
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
PROC = os.path.join(DATA, "processed")
ATT = os.path.join(RAW, "attachments")
REPORTS = os.path.join(ROOT, "reports")
FIGURES = os.path.join(ROOT, "figures")
for _d in (RAW, PROC, ATT, REPORTS, FIGURES):
    os.makedirs(_d, exist_ok=True)

# 사용자가 지정한 원천 3종
TEMP = r"C:\Users\playdata2\AppData\Local\Temp"
DOWNLOADS = r"C:\Users\playdata2\Downloads"
SRC_API = os.path.join(TEMP, "1. 기업마당 중소기업 지원사업 공고 Open API (5).csv")
SRC_LIST = os.path.join(TEMP, "2. 기업마당 중소기업 지원사업 목록 파일 (5).csv")
SRC_EXCEL = os.path.join(
    DOWNLOADS, "2023년 상반기 중앙부처 중소기업지원사업 공고정보(2023년 기준자료).xlsx")

BIZINFO = "https://www.bizinfo.go.kr"
DETAIL_URL = BIZINFO + "/sii/siia/selectSIIA200Detail.do?pblancId={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def read_list():
    """장기 공고 목록 97,794건. 원본에 잘못된 바이트 1개가 있어 replace 처리."""
    import pandas as pd
    return pd.read_csv(SRC_LIST, encoding="cp949", encoding_errors="replace")


def read_api():
    import pandas as pd
    return pd.read_csv(SRC_API, encoding="utf-8-sig")


def read_excel():
    import pandas as pd
    return pd.read_excel(SRC_EXCEL, sheet_name="23년 상반기")


# ---------------------------------------------------------------- 분류체계
CANON_CATEGORIES = ["경영", "기술", "수출", "내수", "창업", "인력", "금융", "소상", "기타"]
CORE8 = ["경영", "기술", "수출", "내수", "창업", "인력", "금융", "기타"]
_ALIAS = {"소상공인": "소상", "소상": "소상", "기술/내수": "기술"}


def norm_category(v):
    if not isinstance(v, str):
        return None
    v = _ALIAS.get(v.strip(), v.strip())
    return v if v in CANON_CATEGORIES else None


# ---------------------------------------------------------------- 텍스트
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
_ENT = [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'")]


def strip_html(s):
    if not isinstance(s, str):
        return ""
    for br in ("<br>", "<br/>", "<br />", "</p>", "</div>", "</li>"):
        s = s.replace(br, "\n")
    s = TAG_RE.sub(" ", s)
    for a, b in _ENT:
        s = s.replace(a, b)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = WS_RE.sub(" ", s)
    s = re.sub(r"[ ]*\n[ ]*", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


PBLANC_RE = re.compile(r"pblancId=(PBLN_\d+)")


def pblanc_id(url):
    if not isinstance(url, str):
        return None
    m = PBLANC_RE.search(url)
    return m.group(1) if m else None


def safe_name(s, maxlen=120):
    """파일시스템 안전 파일명."""
    s = re.sub(r'[<>:"/\|?*\x00-\x1f]', "_", str(s)).strip(" .")
    return (s[:maxlen] or "unnamed")


def save_report(name, obj):
    import json
    p = os.path.join(REPORTS, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    print(f"[report] {p}")
    return p
