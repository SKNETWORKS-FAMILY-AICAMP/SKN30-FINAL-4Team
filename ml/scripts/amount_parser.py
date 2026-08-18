"""지원규모 정보추출 파서 (설계서 6장).

금액 표현을 (값, 단위, 의미) 3요소로 구조화한다.
설계서 지시대로 '금액'보다 '금액의 의미'를 먼저 확정한다 —
per_company와 total_budget을 섞어 하나의 회귀 타깃으로 쓰면 잘못된 모델이 된다.
"""
import re

UNIT_MULT = {
    "원": 1, "천원": 1_000, "만원": 10_000, "천만원": 10_000_000,
    "백만원": 1_000_000, "억원": 100_000_000, "억": 100_000_000,
    "조원": 1_000_000_000_000,
}
# 긴 단위를 먼저 매칭해야 '백만원'이 '만원'으로 잘리지 않는다
_UNIT_ALT = "|".join(sorted(UNIT_MULT, key=len, reverse=True))
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"

AMOUNT_RE = re.compile(
    rf"(?P<lo>{_NUM})\s*(?:(?P<sep>[~∼〜\-–]|이상|부터)\s*(?P<hi>{_NUM}))?\s*(?P<unit>{_UNIT_ALT})")

TYPE_PATTERNS = [
    ("per_company", r"(기업\s*당|업체\s*당|사\s*당|개사\s*당|1\s*개사|참여기업\s*당|1\s*개\s*기업)"),
    ("per_project", r"(과제\s*당|건\s*당|1\s*건|팀\s*당|프로젝트\s*당)"),
    ("total_budget", r"(총\s*사업비|총사업비|총\s*예산|사업\s*규모|지원\s*규모|총\s*지원|예산\s*총액|총액|출연금\s*총)"),
    ("periodic", r"(월\s*최대|연\s*최대|분기\s*당|월\s*[0-9]|연간\s*최대|1\s*인\s*당)"),
]
TYPE_RE = [(t, re.compile(p)) for t, p in TYPE_PATTERNS]

MAX_HINT = re.compile(r"(최대|이내|한도|까지|상한)")
MIN_HINT = re.compile(r"(최소|이상|부터)")
RATIO_RE = re.compile(r"(?:지원\s*비율|보조율|지원율|국비|출연금\s*비율)\s*[:：은는]?\s*(\d{1,3}(?:\.\d+)?)\s*%")
SELF_RATIO_RE = re.compile(r"(?:자부담|기업\s*부담|민간\s*부담|자기\s*부담)\s*(?:비율|금)?\s*[:：은는]?\s*(\d{1,3}(?:\.\d+)?)\s*%")
COUNT_RE = re.compile(r"(\d{1,4})\s*(?:개사|개\s*기업|개\s*과제|개\s*팀|개사내외|개내외|개)")
PERIOD_RE = re.compile(r"(\d{1,2})\s*(?:년|개월)\s*(?:이내|간|동안)?")

_PRIORITY = ["per_company", "per_project", "periodic", "total_budget", "unknown"]


def _to_won(num_str, unit):
    try:
        return float(num_str.replace(",", "")) * UNIT_MULT[unit]
    except ValueError:
        return None


def _classify_type(text, span, window=30):
    """금액 표현 주변 문맥에서 금액의 의미를 판정한다."""
    ctx = text[max(0, span[0] - window):span[1] + 10]
    for t, rx in TYPE_RE:
        if rx.search(ctx):
            return t, ctx
    return "unknown", ctx


def extract_amounts(text):
    if not isinstance(text, str) or not text.strip():
        return []
    out = []
    for m in AMOUNT_RE.finditer(text):
        unit = m.group("unit")
        lo = _to_won(m.group("lo"), unit)
        hi = _to_won(m.group("hi"), unit) if m.group("hi") else None
        if lo is None:
            continue
        t, ctx = _classify_type(text, m.span())
        if hi is not None:
            vmin, vmax = min(lo, hi), max(lo, hi)
        elif MAX_HINT.search(ctx):
            vmin, vmax = None, lo
        elif MIN_HINT.search(ctx):
            vmin, vmax = lo, None
        else:
            vmin, vmax = lo, lo
        out.append({"raw": m.group(0), "min": vmin, "max": vmax, "unit": unit,
                    "type": t, "context": ctx.strip()})
    return out


def parse_support(text):
    """announcement_detail의 지원규모 필드 묶음을 반환한다."""
    res = {"support_amount_raw": None, "support_amount_min": None,
           "support_amount_max": None, "support_amount_unit": None,
           "support_amount_type": None, "support_ratio": None,
           "support_count": None, "self_payment_ratio": None,
           "support_period_year": None, "n_amount_candidates": 0,
           "extraction_confidence": 0.0}
    if not isinstance(text, str) or not text.strip():
        return res

    cands = extract_amounts(text)
    res["n_amount_candidates"] = len(cands)
    if cands:
        cands.sort(key=lambda c: (_PRIORITY.index(c["type"]),
                                  -(c["max"] or c["min"] or 0)))
        b = cands[0]
        res.update(support_amount_raw=b["raw"], support_amount_min=b["min"],
                   support_amount_max=b["max"], support_amount_unit=b["unit"],
                   support_amount_type=b["type"])

    m = SELF_RATIO_RE.search(text)
    if m:
        res["self_payment_ratio"] = float(m.group(1))
    m = RATIO_RE.search(text)
    if m:
        res["support_ratio"] = float(m.group(1))
    elif res["self_payment_ratio"] is not None:
        res["support_ratio"] = round(100.0 - res["self_payment_ratio"], 2)
    m = COUNT_RE.search(text)
    if m:
        c = int(m.group(1))
        if 0 < c <= 5000:
            res["support_count"] = c
    m = PERIOD_RE.search(text)
    if m:
        v = int(m.group(1))
        res["support_period_year"] = v if "년" in m.group(0) else round(v / 12, 2)

    conf = 0.0
    if res["support_amount_max"] or res["support_amount_min"]:
        conf += 0.5 if res["support_amount_type"] != "unknown" else 0.2
    if res["support_ratio"] is not None:
        conf += 0.2
    if res["support_count"] is not None:
        conf += 0.15
    if res["self_payment_ratio"] is not None:
        conf += 0.15
    res["extraction_confidence"] = round(min(conf, 1.0), 2)
    return res


if __name__ == "__main__":
    for s in ["기업당 최대 5천만원", "총사업비 100억원",
              "6개, 60~100백만원, 자부담 20%", "과제당 2억원 이내, 지원비율 75%",
              "월 최대 200만원", "5,000만원", "60백만원", "1억원"]:
        print(s, "→", {k: v for k, v in parse_support(s).items() if v})
