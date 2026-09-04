"""Pre-Review ML/DL 공통 모듈 — 원천 경로, 분류체계, 텍스트 정규화."""
import os
import re
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

def _find_root(start):
    """`ml/` 를 위로 거슬러 찾는다 — common.py 가 하위 디렉터리로 옮겨져도 동작한다."""
    p = os.path.abspath(start)
    while True:
        p = os.path.dirname(p)
        if os.path.isdir(os.path.join(p, "pipelines")) and os.path.isdir(os.path.join(p, "data")):
            return p
        if p == os.path.dirname(p):
            raise RuntimeError("ml root not found from %s" % start)


ROOT = _find_root(__file__)   # ml/
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
PROC = os.path.join(DATA, "processed")
ATT = os.path.join(RAW, "attachments")
REPORTS = os.path.join(ROOT, "reports")
FIGURES = os.path.join(ROOT, "figures")
# 채택 모델의 serving artifact. 데이터가 아니라 모델이라 data/ 밖에 둔다.
# `_archive/` 에는 직전 세대를 남긴다 (m56 = 모델2 v1 세대).
MODELS = os.path.join(ROOT, "models")
for _d in (RAW, PROC, ATT, REPORTS, FIGURES, MODELS):
    os.makedirs(_d, exist_ok=True)

# 사용자가 지정한 원천 4종
TEMP = r"C:\Users\playdata2\AppData\Local\Temp"
DOWNLOADS = r"C:\Users\playdata2\Downloads"
SRC_API = os.path.join(TEMP, "1. 기업마당 중소기업 지원사업 공고 Open API (5).csv")
SRC_LIST = os.path.join(TEMP, "2. 기업마당 중소기업 지원사업 목록 파일 (5).csv")
SRC_EXCEL = os.path.join(
    DOWNLOADS, "2023년 상반기 중앙부처 중소기업지원사업 공고정보(2023년 기준자료).xlsx")
SRC_EXCEL_2022 = os.path.join(
    TEMP, "2022년 중앙부처 중소기업지원사업 공고정보 검색.xlsx")

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


def read_excel_2022():
    """2022 중앙부처 엑셀. 본문 구조(【공고이름】/【사업개요】/①~④)는 2023과 같지만
    컬럼명이 다르다(사업내용/대유형/중유형). 여기서 2023 이름으로 맞춰 내보낸다.
    2022 에만 있는 '정책목적'·'누리집링크'는 쓰지 않아 그대로 둔다."""
    import pandas as pd
    df = pd.read_excel(SRC_EXCEL_2022)
    return df.rename(columns={"사업내용": "사업개요", "대유형": "대분류", "중유형": "중분류"})


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


# 리포트를 모델별 하위 폴더로 자동 분류한다. 호출부를 고치지 않아도 새 리포트가
# 구조 안에 떨어지게 하려는 것 — 루트에 쌓이기 시작하면 정리한 구조가 곧 무너진다.
# 접두어 -> 하위 경로. 더 긴 접두어가 먼저 걸리도록 길이 내림차순으로 본다.
REPORT_ROUTES = {
    # experiments/ 의 core · validation · archive 와 같은 칸을 쓴다.
    #   core        성능/구조를 실제로 바꾼 것
    #   validation  그 결과를 믿을 수 있는지 검증한 것
    #   archive     해봤지만 최종 승격되지 않은 것
    # 접두사가 여럿 걸리면 **가장 긴 것**이 이긴다 (m2 보다 m24_m1 이 우선).
    "model1/core":       ("dl01", "dl02", "dl06", "dl12_m1", "dl15", "dl20_m1",
                          "m01_support"),
    "model1/validation": ("dl03", "dl07_m1", "dl16", "m02_apply", "m08_apply",
                          "m28_m1", "m29_m1", "m54_m1", "m57_m1"),
    "model1/archive":    ("dl04", "dl05", "m24_m1", "m25_m1", "m27_m1"),

    "model2/core":       ("m65", "m69", "m73", "m82_m2_proximity"),
    "model2/validation": ("m71", "m74", "m78", "m79", "m81", "m82b", "m82c",
                          "m55_m2_leakage"),
    "model2/archive":    ("m67", "m68", "m70", "m72", "m75", "m77", "m80", "m83",
                          "m84", "m85", "m11_m2", "m15_m2", "m18_m2", "m21_m2",
                          "m26_m2", "m45_m2", "m52_m2", "m52_top", "m53_m2",
                          "m56_m2", "m63_m2", "dl08_m2", "dl14_m2", "dl19_m2"),

    "model3/core":       ("m12_m3", "m33_m3", "m38_m3", "m44_m3", "m51_m3"),
    "model3/validation": ("m30_m3", "m31_m3", "m34_m3", "m37_m3", "m47_m3",
                          "m48_m3", "m49_m3", "m64_m3", "m66_m3"),
    # m13/m16/m20/m23/dl10 = 옛 '모델 4'(OneClassSVM·AE 1세대 이상탐지).
    # 모델 4 는 별도 모델이 아니라 모델 3 으로 흡수됐고, 현행 거리기반 구조에
    # 승격되지 않아 archive 다. 파일명도 m4 -> m3 로 맞춰 두었다.
    "model3/archive":    ("m13_m3", "m16_m3", "m17_m3", "m19_m3", "m20_m3",
                          "m22_m3", "m23_m3", "m36_m3", "m50_m3", "m58_m3",
                          "m59_m3", "m60_m3", "m61_m3", "dl09_m3", "dl10_m3",
                          "dl13_m3", "dl17_m3", "dl18_m3"),

    # 데이터 파이프라인 산출 — 수집 -> 추출 -> 기준테이블 -> EDA
    "pipeline":          ("a01", "a02", "a03", "d01", "d02", "d03", "d04",
                          "e01", "f01", "f02", "f03", "f04", "f05", "f06"),
}


def report_subdir(name):
    """리포트 파일명이 속할 하위 폴더. 못 찾으면 'shared'."""
    n = os.path.basename(name).lower()
    best = (None, -1)
    for sub, prefixes in REPORT_ROUTES.items():
        for p in prefixes:
            if n.startswith(p) and len(p) > best[1]:
                best = (sub, len(p))
    return best[0] or "shared"


def report_path(name):
    """리포트 파일의 실제 경로.

    쓰기  -> 분류된 하위 폴더 (없으면 만든다)
    읽기  -> 이미 있는 파일을 찾아준다. 정리 전 루트에 있던 산출물
             (`e01_documents.jsonl` 등)을 읽는 코드가 그대로 동작해야 한다.
    """
    if os.path.dirname(name):                     # 호출부가 경로를 준 경우
        return os.path.join(REPORTS, name)
    routed = os.path.join(REPORTS, report_subdir(name), name)
    if os.path.exists(routed):
        return routed
    flat = os.path.join(REPORTS, name)
    if os.path.exists(flat):
        return flat
    # 라우팅에 없는 이름이어도 실물이 있으면 찾아 준다. 하위가 두 단계
    # (model3/archive/…) 라 한 단계만 보면 놓친다 — 재귀로 훑는다.
    for dp, dn, fn in os.walk(REPORTS):
        if name in fn:
            return os.path.join(dp, name)
    os.makedirs(os.path.dirname(routed), exist_ok=True)
    return routed


def save_report(name, obj):
    import json
    # 호출부가 이미 하위 경로를 준 경우엔 그대로 존중한다.
    sub = os.path.dirname(name) or report_subdir(name)
    d = os.path.join(REPORTS, sub)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, os.path.basename(name))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    print(f"[report] {p}")
    return p


# ---- 지원규모 상식 범위 (파싱 오류 판별) ----
# 이 범위를 벗어난 값은 금액 자체가 아니라 파싱이 틀린 것으로 본다.
# 실례: "예산규모는 1조 4,517억원"(정부 전체 예산)에서 '1조'를 놓쳐
#       4,517억원이 per_company 로 잡힌 건이 있었다.
#
# 정의를 common 에 두는 이유: 금액을 보는 모든 하류가 같은 기준을 써야 한다.
# 예전에는 참고범위 산출 스크립트에만 있어서 STL·예측이 오류값을 그대로 보고
# 있었다(그 셋은 이후 제거됐다). 지금 소비처는 s03f/s07a/s08a/s09a 다.
SANE_RANGE = {
    "per_company": (1e5, 5e9),      # 10만원 ~ 50억원
    "per_project": (1e5, 1e10),     # 10만원 ~ 100억원
    "periodic": (1e4, 1e8),         # 1만원 ~ 1억원 (월/연 단위)
    "total_budget": (1e6, 1e13),    # 100만원 ~ 10조원
}


def mark_outliers(obs, amount_col="amount_max", type_col="amount_type"):
    """SANE_RANGE 밖이면 True 인 불리언 Series 를 돌려준다.

    행을 지우지 않고 플래그만 붙인다. 몇 건을 왜 뺐는지 추적할 수 있고,
    기준을 바꿔도 원문 재파싱이 필요 없다.
    amount_type 이 SANE_RANGE 에 없는 값(unknown 등)은 판정 대상이 아니라 False.
    """
    import pandas as pd
    flag = pd.Series(False, index=obs.index)
    for t, (lo, hi) in SANE_RANGE.items():
        m = obs[type_col] == t
        flag |= m & ~obs[amount_col].between(lo, hi)
    return flag
