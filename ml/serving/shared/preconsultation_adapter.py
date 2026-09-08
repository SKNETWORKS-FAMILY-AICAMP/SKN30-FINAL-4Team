"""사전협의서(사전협의 요청서) → canonical feature 변환 Adapter — serving 전용.

왜 별도 계층인가
----------------
공고문과 사전협의서는 같은 수치를 다른 의미로 적는다. 사전협의서에는
`사업기간`(프로그램 전체)과 `지원기간`(개별 과제)이 함께 있고, 모델 3 이 학습한
`project_duration` 은 **후자**다. 공용 파서(`ml/pipelines/shared/amount_parser.py`)
는 후보가 여럿일 때 첫 매치를 채택하므로 문서 앞쪽의 사업기간을 집어 온다.

공용 파서를 고치지 않는 이유는 하나다 — 그 파서는 학습 feature 를 만든 바로 그
코드다. 여기서 정규식을 하나 바꾸면 `design_features` 지문이 달라지고 모델 2
재학습·모델 3 재검증까지 끌려 나온다. 그래서 **학습 경로는 얼려 두고 서빙 입력만
이 어댑터에서 보정한다.**

실측 (사전협의서 예시 1건)
    support_period_year = 28.0   백틱 축약연도를 YEAR_RE 가 못 읽어 `28년` 이
                                 기간으로 채택됐다. 등급이 context 라
                                 f06.apply_sanity 의 bare 필터도 통과한다 —
                                 28년이 그대로 모델 3 에 들어간다.
    support_count       = 4      `4과제` 에서 온 값이 아니다. 문서 후반의
                                 `3차년도 지원과제(4개)` 라는 **다른 의미의 수**다.
                                 값이 우연히 그럴듯해 육안으로는 걸러지지 않는다.

이 어댑터는 그 둘을 바로잡고 근거를 함께 남긴다. **값을 만들어 내지는 않는다** —
문서에 없으면 None 이고, 애매하면 대표값 대신 후보를 보존한다.
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SHARED = os.path.join(_ML, "pipelines", "shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

import amount_parser as AP                      # noqa: E402
import f06_design_features as F06               # noqa: E402


# ---------------------------------------------------------------- 1 연도 표기
# YEAR_RE 는 축약 연도의 따옴표로 표준 작은따옴표와 오른쪽 작은따옴표 두 글자만
# 안다. 한글 문서는 실제로 백틱과 여는 작은따옴표를 훨씬 자주 쓴다 — 예시 문서
# 한 건에서만 세 곳이 새어 기간 후보로 잡혔다. 파서에 넣기 전에 이 글자들을
# 표준 작은따옴표로 통일한다. 숫자 두 자리 앞에 붙은 경우만 바꾸므로
# 인용부호로 쓰인 따옴표(예: 기업혁신형을 감싼 것)는 건드리지 않는다.
YEAR_QUOTES = "`´ˋ‘‛′"
_YEAR_QUOTE_RE = re.compile("[" + re.escape(YEAR_QUOTES) + "](?=\\d{2})")


def normalize_year_marks(text):
    """축약 연도의 따옴표를 표준 작은따옴표로 통일한다. 다른 문자는 두고."""
    if not isinstance(text, str):
        return ""
    return _YEAR_QUOTE_RE.sub("'", text)


# ---------------------------------------------------------------- 2 기간 의미
# 우선순위: 지원기간 > 과제기간 > 수행기간 > 사업기간.
# 앞의 셋은 개별 과제의 기간(project), 사업기간은 프로그램 전체(program)다.
DUR_ROLES = [
    ("project", 0, "지원기간", re.compile(r"지원\s*기간")),
    ("project", 1, "과제기간", re.compile(r"과제\s*기간")),
    ("project", 2, "수행기간", re.compile(r"수행\s*기간")),
    ("project", 2, "협약기간", re.compile(r"협약\s*기간")),
    ("project", 2, "개발기간", re.compile(r"개발\s*기간")),
    ("program", 3, "사업기간", re.compile(r"사업\s*기간")),
]
DUR_WINDOW = 30          # 기간 키워드를 찾아볼 앞쪽 문맥 폭


def _line_of(text, pos):
    """근거로 남길 한 줄. 표 셀이 줄바꿈으로 끊겨 있어 줄 단위가 가장 읽기 좋다."""
    a = text.rfind("\n", 0, pos) + 1
    b = text.find("\n", pos)
    return text[a:(b if b != -1 else len(text))].strip()


def _governing_role(before):
    """매치 앞 문맥에서 **가장 가까운** 기간 키워드를 찾는다.

    가장 가까운 것을 쓰는 이유: `(지원기간) 최대 3년(2년+1년)` 처럼 한 줄에 값이
    여럿 붙을 때, 앞선 키워드 하나가 그 줄 전체를 지배하기 때문이다.
    """
    best = None
    for role, prio, label, rx in DUR_ROLES:
        hit = None
        for m in rx.finditer(before):
            hit = m                                  # 마지막 = 가장 가까운 것
        if hit is None:
            continue
        if best is None or hit.start() > best[0]:
            best = (hit.start(), role, prio, label)
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]


def extract_durations(text):
    """기간 후보를 역할별로 분리해 돌려준다. 값은 년 단위.

    sanity 는 여기서 **등급과 무관하게** 건다. 공용 파서 쪽 f06.apply_sanity 는
    bare 등급만 걷어내므로 28.0 처럼 context 등급인 오파싱이 살아남는다.
    """
    t = normalize_year_marks(text)
    years = [m.span() for m in AP.YEAR_RE.finditer(t)]
    lo, hi = AP.DURATION_SANE
    cands, rejected = [], []
    for m in AP.PERIOD_RE.finditer(t):
        if any(m.start() >= a and m.end() <= b for a, b in years):
            continue                                       # 연도 표기 자체
        before = t[max(0, m.start() - DUR_WINDOW):m.start()]
        if AP.DISQUALIFY_CTX_RE.search(before):
            continue                                       # 업력 3년 이상 등
        role, prio, label = _governing_role(before)
        if role is None and AP.DISQUALIFY_AFTER_RE.match(t[m.end():m.end() + 20]):
            continue                                       # 3년 미만 초기창업기업
        v = float(m.group(1))
        v = v if m.group("u") == "년" else round(v / 12.0, 2)
        item = {"value": v, "role": role, "label": label, "priority": prio,
                "raw": m.group(0).strip(), "evidence": _line_of(t, m.start()),
                "pos": m.start()}
        if not (lo < v <= hi):
            item["reason"] = "duration_out_of_range"
            rejected.append(item)
            continue
        if role is not None:
            cands.append(item)
    return t, cands, rejected


def _pick_duration(cands, role):
    """우선순위 → 문서 순서로 대표값을 고른다."""
    pool = [c for c in cands if c["role"] == role]
    if not pool:
        return None
    pool.sort(key=lambda c: (c["priority"], c["pos"]))
    return pool[0]


# ------------------------------------------------- project_duration 정책
# 모델에 들어갈 값을 어느 규칙으로 고를 것인가. 기본은 **학습 규칙**이다.
#
#   training_rule         백틱 연도만 고쳐 놓고 학습 때와 같은 규칙으로 뽑는다.
#                         예시 문서에서 5.0(사업기간).
#   support_period_first  지원기간 > 과제기간 > 수행기간 > 사업기간 순으로 고른다.
#                         예시 문서에서 3.0.
#
# 왜 training_rule 이 기본인가 — Model 2 학습셋과 Model 3 비교군 pool 이 **이미
# 학습 규칙으로** 만들어져 있다. 그 pool 의 project_duration 은 사업기간·지원기간·
# 협약기간·융자기간이 섞인 값이다. 신규 요청만 지원기간으로 바꾸면 같은 축에서
# 서로 다른 것을 재게 된다 — 거리가 커진 이유가 설계 때문인지 의미 차이 때문인지
# 구분할 수 없다.
#
# support_period_first 가 의미상 더 정확한 것은 맞다. 그건 pool 을 다시 만들고
# 재평가한 뒤에 기본으로 올릴 일이다(그때는 기간을 축 하나로 섞지 말고
# 사업기간/지원기간/협약기간/융자기간으로 나누는 편이 낫다).
DURATION_POLICY_TRAINING = "training_rule"
DURATION_POLICY_SUPPORT_FIRST = "support_period_first"
DURATION_POLICIES = (DURATION_POLICY_TRAINING, DURATION_POLICY_SUPPORT_FIRST)


def training_rule_duration(text):
    """학습 때와 같은 규칙. 단 백틱 축약연도만 미리 고쳐서 넣는다.

    백틱 정규화는 의미 변경이 아니라 **오파싱 제거**다. `` `28년 `` 을 기간으로
    읽는 것은 어느 정책에서도 옳지 않다(실측 28.0). 그 하나만 걷어내고 나머지
    선택 규칙은 건드리지 않는다.
    """
    return AP.parse_duration(normalize_year_marks(text))


# ---------------------------------------------------------------- 3 지원건수
# 공용 COUNT_RE 는 `개` 를 요구해서 `4과제`·`6과제` 를 통째로 놓친다. 반대로
# `지원과제(4개)` 는 잡는다 — 그래서 예시 문서에서 신규과제 수 자리에 3차년도
# 선별지원 과제 수가 들어갔다. 여기서는 **둘 다 잡되 의미를 붙여 구분**한다.
COUNT_RE = re.compile(
    r"(?<![\d,])(?P<n>\d{1,3}(?:,\d{3})+|\d{1,4})\s*"
    r"(?P<unit>개사내외|개내외|개사|개\s*기업|개\s*과제|개\s*팀|개\s*소|"
    r"과제|기업(?![가-힣])|개(?![가-힣]))")

COUNT_SEMANTICS = [
    (re.compile(r"\d\s*차\s*년도?\s*지원\s*과제"), "third_year_selected_projects"),
    (re.compile(r"신규\s*과제"), "new_projects"),
    (re.compile(r"지원\s*규모|선정\s*규모|모집\s*규모|지원\s*대상"), "support_scale"),
]
COUNT_TYPES = [
    (re.compile(r"기업\s*혁신\s*형"), "company_innovation"),
    (re.compile(r"시장\s*개척\s*형"), "market_expansion"),
]
SEM_WINDOW = 140         # 의미 키워드를 찾아볼 앞쪽 문맥 폭
TYPE_WINDOW = 200        # 유형(기업혁신형/시장개척형)을 찾아볼 폭


def _nearest(before, rules):
    """앞 문맥에서 가장 가까운 규칙의 라벨."""
    best = None
    for rx, label in rules:
        hit = None
        for m in rx.finditer(before):
            hit = m
        if hit is not None and (best is None or hit.start() > best[0]):
            best = (hit.start(), label)
    return best[1] if best else None


def extract_support_counts(text):
    """지원건수 후보를 의미 라벨과 함께 전부 돌려준다. 합산하지 않는다."""
    t = normalize_year_marks(text)
    out = []
    for m in COUNT_RE.finditer(t):
        n = int(m.group("n").replace(",", ""))
        if n <= 0:
            continue
        sem = _nearest(t[max(0, m.start() - SEM_WINDOW):m.start()], COUNT_SEMANTICS)
        if sem == "new_projects":
            typ = _nearest(t[max(0, m.start() - TYPE_WINDOW):m.start()], COUNT_TYPES)
            if typ:
                sem = "new_projects_" + typ
        out.append({"value": n, "semantic": sem, "raw": m.group(0).strip(),
                    "unit": re.sub(r"\s+", "", m.group("unit")),
                    "evidence": _line_of(t, m.start()), "pos": m.start()})
    return out


def resolve_support_count(cands):
    """대표 support_count 를 정한다 — **확실할 때만.**

    `기업혁신형 4과제` 와 `시장개척형 6과제` 를 10 으로 합치지 않는다. 두 유형이
    배타적인지, 같은 연도인지, 그 둘이 전부인지 문서만으로는 확정할 수 없다.
    확정할 수 없으면 대표값은 None 이고 후보가 남는다.
    """
    new = [c for c in cands if (c["semantic"] or "").startswith("new_projects")]
    if not new:
        return None, "no_new_project_count"
    vals = sorted({c["value"] for c in new})
    if len(vals) == 1:
        return vals[0], "single_new_project_count"
    return None, "multiple_semantic_candidates"


# ---------------------------------------------------------------- 4 금액 의미
_UNIT_ALT = "|".join(sorted(AP.UNIT_MULT, key=len, reverse=True))
ANNUAL_RE = re.compile(
    r"(?:연간|연\s*평균|년간)\s*(?P<n>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>" + _UNIT_ALT + r")")


def extract_annual_amount(text):
    """`연간 6억원` 처럼 연 단위로 적힌 지원액. 과제당 총액과 섞지 않는다."""
    t = normalize_year_marks(text)
    m = ANNUAL_RE.search(t)
    if not m:
        return None, None
    val = float(m.group("n").replace(",", "")) * AP.UNIT_MULT[m.group("unit")]
    return val, _line_of(t, m.start())


# ---------------------------------------------------------------- 5 진입점
CANONICAL_FIELDS = ["support_type", "support_method", "support_unit", "amount_type",
                    "per_recipient", "support_count", "project_duration",
                    "support_ratio"]


def adapt(text, row_id=None, base=None,
          duration_policy=DURATION_POLICY_TRAINING):
    """사전협의서 원문 → canonical feature + 근거.

    base 로 문서에서 뽑을 수 없는 분류축(support_type 등)을 넘길 수 있다.
    모델 3 은 support_type 이 비면 그 행을 채점하지 않는다(원본 prepare 규칙).

    duration_policy 는 모델에 들어갈 `project_duration` 을 어느 규칙으로 고를지
    정한다. 기본은 학습 규칙이다 — 위 DURATION_POLICY_* 주석 참조. 의미를 나눈
    `program_duration_years` / `project_duration_years` 는 정책과 무관하게 항상
    함께 돌려주므로, 나중에 pool 을 다시 만들 때 그대로 쓸 수 있다.
    """
    if duration_policy not in DURATION_POLICIES:
        raise ValueError("알 수 없는 duration_policy: %r (허용: %s)"
                         % (duration_policy, list(DURATION_POLICIES)))
    norm, dur_cands, dur_rejected = extract_durations(text)
    project = _pick_duration(dur_cands, "project")
    program = _pick_duration(dur_cands, "program")

    counts = extract_support_counts(text)
    count_value, count_reason = resolve_support_count(counts)

    parsed = AP.parse_support(norm)
    annual, annual_ev = extract_annual_amount(text)

    amount_type = parsed.get("support_amount_type")
    amount_max = parsed.get("support_amount_max")
    per_recip, per_recip_basis = F06.per_recipient(
        amount_type,
        float(amount_max) if amount_max is not None else float("nan"),
        count_value)

    review = [{"field": "project_duration", "reason": r["reason"],
               "value": r["value"], "evidence": r["evidence"]}
              for r in dur_rejected]
    if count_value is None and counts:
        review.append({"field": "support_count", "reason": count_reason,
                       "candidates": [c["value"] for c in counts
                                      if (c["semantic"] or "").startswith("new_projects")]})

    # 모델에 들어갈 기간. 정책에 따라 고르되 상식 범위는 두 정책 모두에 건다 —
    # 10년을 넘는 값은 실측상 기간이 아니라 오파싱이었고(28.0), 그것을 그대로
    # 넣으면 학습 규칙과 맞추는 이득보다 축 하나를 망가뜨리는 손해가 크다.
    train_value, train_basis = training_rule_duration(text)
    lo, hi = AP.DURATION_SANE
    duration_review = None
    if train_value is not None and not (lo < train_value <= hi):
        duration_review = {"policy_value": train_value, "basis": train_basis,
                           "reason": "duration_out_of_range"}
        train_value = None

    if duration_review is not None:
        review.append({"field": "project_duration", **duration_review})

    if duration_policy == DURATION_POLICY_TRAINING:
        model_duration = train_value
        duration_basis = train_basis
    else:
        model_duration = project["value"] if project else None
        duration_basis = project["label"] if project else None

    feats = dict(base or {})
    feats.setdefault("row_id", row_id)
    feats["project_duration"] = model_duration
    feats["support_count"] = count_value
    feats["amount_type"] = amount_type
    feats["per_recipient"] = None if per_recip != per_recip else float(per_recip)
    # 문서에 없는 값은 추정하지 않는다 — 파서가 None 을 주면 None 그대로 둔다.
    feats["support_ratio"] = parsed.get("support_ratio")
    feats["self_burden_ratio"] = parsed.get("self_payment_ratio")

    missing = [f for f in CANONICAL_FIELDS if feats.get(f) is None]
    conf = ("high" if project and count_value is not None and not missing
            else "medium" if project else "low")

    return {
        "row_id": row_id,
        "features": feats,
        "program_duration_years": program["value"] if program else None,
        "project_duration_years": project["value"] if project else None,
        "duration_policy": duration_policy,
        "duration_basis": duration_basis,
        "duration_evidence": {
            "model_value": model_duration,
            "training_rule_value": train_value,
            "support_period_value": project["value"] if project else None,
            "project": project and {"value": project["value"], "basis": project["label"],
                                    "evidence": project["evidence"]},
            "program": program and {"value": program["value"], "basis": program["label"],
                                    "evidence": program["evidence"]},
            "rejected": dur_rejected,
            "review": duration_review,
        },
        "support_count_candidates": [
            {k: c[k] for k in ("value", "semantic", "evidence", "raw")} for c in counts],
        "support_count_basis": count_reason,
        "amounts": {
            "support_amount_min": parsed.get("support_amount_min"),
            "support_amount_max": parsed.get("support_amount_max"),
            "support_amount_type": amount_type,
            "support_per_project_annual": annual,
            "per_recipient": feats["per_recipient"],
            "per_recipient_basis": per_recip_basis,
            "annual_evidence": annual_ev,
        },
        "missing_features": missing,
        "review": review,
        "confidence": conf,
    }


if __name__ == "__main__":
    import json
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        raise SystemExit("사용법: python preconsultation_adapter.py <문서텍스트파일>")
    with open(src, encoding="utf-8") as fh:
        print(json.dumps(adapt(fh.read()), ensure_ascii=False, indent=2, default=str))
