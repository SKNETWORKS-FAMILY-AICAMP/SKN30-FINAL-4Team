r"""지원규모 정보추출 파서 (설계서 6장).

금액 표현을 (값, 단위, 의미) 3요소로 구조화한다.
설계서 지시대로 '금액'보다 '금액의 의미'를 먼저 확정한다 —
per_company와 total_budget을 섞어 하나의 회귀 타깃으로 쓰면 잘못된 모델이 된다.

M32 (2026-08-26) — 사업기간·지원기업수 패턴 교체
    M31 이 hold-out 50건에서 감사하고 전체 코퍼스 규모까지 확인한 4개 버그를
    여기로 승격시킨다. 감사(M31)와 수정(M32)을 따로 커밋한 이유는
    "고치고 나서 고쳤다고 확인"하는 자기증명을 피하기 위해서다.

        1 연도 뒤 두 자리를 기간으로   PERIOD_RE 에 왼쪽 경계 없음  2026년 -> 26년
        2 축약 연도를 기간으로         같은 곳                      '21년 -> 21년
        3 단위어를 기업수로            COUNT_RE 끝의 맨 `개`        12개소·6개월 -> 12·6
        4 천단위 콤마 절단             COUNT_RE 의 (\d{1,4})        1,500개사 -> 500

    3번이 per_recipient = total_budget / support_count 의 분모를 망가뜨려
    기업당 지원액을 166억·91.7억으로 폭발시켰다.

    바꾸지 않은 것 — 선택 규칙. 후보가 여럿일 때 첫 매치가 이긴다는 규칙은
    그대로다. 어느 후보가 '그' 값인지는 파서 버그가 아니라 정보검색 문제이고,
    50건에 맞춰 손보면 hold-out 을 튜닝셋으로 쓰는 것과 같다.

    버리지 않고 등급을 남긴다. 기간에는 근거 등급(context/hint_only/bare)을,
    상식 범위 밖 값에는 review 플래그를 붙인다. 값이 사라지면 하류는
    '근거 없음'과 '평범함'을 구별할 수 없다.
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
# ---------------------------------------------------------------- 지원기업수
# 천단위 콤마를 읽고, 왼쪽 경계로 숫자 중간 시작을 막는다.
# 맨 `개` 뒤에 한글이 오면 그것은 단위어다 — (?![가-힣]) 로 잘라낸다.
#   실측: '12개소'(금융회사 수)·'6개월'(상환기간)·'1개의 파일' 이 지원기업수로
#   들어갔다. M31 이 값이 바뀐 13건을 전수 확인했더니 구 파서가 잡은 값 중
#   진짜 지원기업수는 한 건도 없었다.
COUNT_RE = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{1,5})\s*"
    r"(?:개사내외|개내외|개사|개\s*기업|개\s*과제|개\s*팀|개(?![가-힣]))")
COUNT_SANE = (0, 5000)          # 초과는 버리지 않고 flag (구 코드는 조용히 버렸다)

# ---------------------------------------------------------------- 사업기간
# 연도 표기 2종. 기간 후보에서 걸러낼 때 쓴다.
#   '2026년'  네 자리 그대로
#   "'21년"   아포스트로피 축약 — 실측: "'19~'21년 평균 매출액" 이 21년으로 읽혔다
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*년|['’]\d{2}\s*년")
# 왼쪽 경계를 넣어 '2026년' 의 '26' 에서 시작하지 못하게 한다.
# 소수 기간('0.5년')도 실제로 쓰이므로 받아준다.
PERIOD_RE = re.compile(
    r"(?<![\d,.'’])(\d{1,2}(?:\.\d)?)\s*(?P<u>년|개월)\s*(?P<hint>이내|간|동안|이하|미만)?")
# '기간' 문맥. 매치 앞 window 안에 이게 있으면 사업기간으로 본다.
DURATION_CTX_RE = re.compile(
    r"(?:사업|지원|협약|수행|보증|대출|융자|거치|상환|교육|훈련|파견|근무|계약|"
    r"임차|입주|약정|공급|운영|고용)\s*기간|기간\s*[:：]")
# 자격요건·통계기간에 붙는 '년'. 사업기간이 아니다. 앞 문맥으로 본다.
DISQUALIFY_CTX_RE = re.compile(
    r"업력|창업\s*[후내]|설립\s*[후내]|경력|재직|거주|매출액|이상\s*경과|"
    r"최근\s*\d|연속\s*\d")
# 뒤 문맥으로 드러나는 자격요건. '3년 미만 초기창업기업' 처럼 기간 뒤에 대상이
# 붙으면 그건 사업기간이 아니라 신청자격이다.
DISQUALIFY_AFTER_RE = re.compile(
    r"^\s*(?:미만|이내|이상|이하)?\s*(?:초기|예비|신생)?\s*"
    r"(?:창업\s*)?(?:기업|업체|법인|사업자|소상공인|자|스타트업)")
DURATION_SANE = (0.0, 10.0)     # 벗어나면 삭제가 아니라 review flag

_PRIORITY = ["per_company", "per_project", "periodic", "total_budget", "unknown"]


def _to_won(num_str, unit):
    try:
        return float(num_str.replace(",", "")) * UNIT_MULT[unit]
    except ValueError:
        return None


def parse_count(text):
    """지원 기업/과제 수와 상한초과 플래그. 선택 규칙(첫 매치)은 구 파서와 같다."""
    if not isinstance(text, str) or not text.strip():
        return None, False
    for m in COUNT_RE.finditer(text):
        c = int(m.group(1).replace(",", ""))
        if c > 0:
            return c, c > COUNT_SANE[1]
    return None, False


def parse_duration(text):
    """사업기간(년) 과 근거 등급. 못 찾으면 (None, 사유).

    근거가 약한 값도 버리지 않고 등급만 낮춰 남긴다 — 하류가 등급을 보고 정한다.

        context     앞에 '사업기간/지원기간/…' 문맥이 있다        (높음)
        hint_only   '이내/간/동안' 힌트만 있다                    (보통)
        bare        맨 숫자+년/개월                               (낮음)

    반대로 **연도 표기와 자격요건은 등급이 아니라 제거**다. 기간이 아닌 것을
    낮은 등급으로 남기면 이상탐지가 그대로 '드문 설계'로 읽는다.
    """
    if not isinstance(text, str) or not text.strip():
        return None, "no_text"
    years = [m.span() for m in YEAR_RE.finditer(text)]
    hits = {}
    for m in PERIOD_RE.finditer(text):
        if any(m.start() >= a and m.end() <= b for a, b in years):
            continue                                    # 연도 표기 자체
        before = text[max(0, m.start() - 20):m.start()]
        if DISQUALIFY_CTX_RE.search(before):
            continue                                    # 업력 3년 이상 등
        has_ctx = bool(DURATION_CTX_RE.search(before))
        # 기간 문맥이 앞에 있으면 뒤에 뭐가 오든 사업기간이다. 문맥이 없을 때만
        # 뒤 문맥으로 자격요건인지 판정한다.
        if not has_ctx and DISQUALIFY_AFTER_RE.match(text[m.end():m.end() + 20]):
            continue                                    # 3년 미만 초기창업기업 등
        v = float(m.group(1))
        v = v if m.group("u") == "년" else round(v / 12, 2)
        tier = ("context" if has_ctx
                else "hint_only" if m.group("hint") else "bare")
        hits.setdefault(tier, v)
    for tier in ("context", "hint_only", "bare"):
        if tier in hits:
            return hits[tier], tier
    return None, "no_duration_evidence"


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
           "extraction_confidence": 0.0,
           "support_period_basis": "no_text", "support_period_review": False,
           "support_count_over_cap": False}
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
    cnt, over = parse_count(text)
    res["support_count"], res["support_count_over_cap"] = cnt, over

    dur, basis = parse_duration(text)
    res["support_period_year"], res["support_period_basis"] = dur, basis
    if dur is not None and not (DURATION_SANE[0] < dur <= DURATION_SANE[1]):
        res["support_period_review"] = True

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
    # 앞 6개는 회귀 검사, 뒤 6개는 M31 이 hold-out 에서 실제로 건진 문장이다.
    for s in ["기업당 최대 5천만원", "총사업비 100억원",
              "6개, 60~100백만원, 자부담 20%", "과제당 2억원 이내, 지원비율 75%",
              "월 최대 200만원", "5,000만원",
              "2026년 창업 성공패키지 참여기업 모집",      # 26년 x
              "'19~'21년 평균 매출액 기준",                 # 21년 x
              "협약금융회사 12개소를 통해",                  # 12개사 x
              "상환기간 6개월 거치",                        # 6개사 x
              "지원규모 1,500개사",                         # 500 -> 1500
              "사업기간: 3년 이내, 3년 미만 초기창업기업"]:  # 3년 O (자격요건 아님)
        print(s, "→", {k: v for k, v in parse_support(s).items()
                       if v not in (None, False, 0, 0.0)})
