"""모델 2 전용 원천 feature 층 (M69).

지시서(사용자, `model2_source_feature_enhancement_expert_retry_plan.md`)는
구조 실험(M67 routing · M68 residual/MoE)이 전부 기각된 뒤의 다음 수를
**"입력을 보강하고 Stage 1 금액구간 분류력이 실제로 올라가는지 먼저 본다"**
로 잡았다. 이 파일은 그 보강층 하나만 담는다 — 공통 F06 스키마는 건드리지
않고, 모델 2 가 쓰는 자리에서만 얹는 층이다.

## 왜 별도 파일인가

`m2_features` 는 M53/M56/M65 가 실측한 조건을 **못 박아 둔 파일**이다. 서빙과
학습이 같은 곳을 import 하게 만든 것이 그 파일의 존재 이유라, 검증되지 않은
feature 를 거기에 섞으면 그 규율이 깨진다. 여기서 만들고, 실험이 이기면
그때 승격한다.

## 원천 텍스트가 무엇인가

    taxonomy (976행)   business_taxonomy 의 purpose / content / target_text
                       (사업목적·사업내용·지원대상 원문) + scale_text
    bizinfo  (901행)   공고문 원문 전체 (F06 이 evidence_text 에 담아 둔 것).
                       문서가 없으면 API 요약문.

## 타깃 누수를 어디서 끊는가 (지시서 4장)

타깃은 `per_recipient` = `amount_max` 이고, 그 값은 **원천 텍스트에서 파서가
고른 금액 후보 하나**다(실측: 1,877행 전부 일치). 그래서 누수 차단을 값 비교로
하지 않는다 — 값으로 거르면 "총사업비가 결측이다"라는 사실 자체가 타깃을
가리키게 된다. 대신 **구조로** 끊는다.

    1. `AP.extract_amounts` 로 후보를 다시 뽑고 `AP.parse_support` 와 같은
       우선순위로 정렬해 **파서가 골랐을 후보(=타깃)를 지목**한다.
    2. 그 후보를 빼고 남은 것에서만 금액 feature 를 만든다.
    3. 텍스트 feature 는 숫자를 전부 지운 뒤에만 쓴다 —
       금액 표현은 [AMOUNT] 로, 남은 모든 숫자는 `#` 로.

쓰지 않기로 한 것(지시서 4장 '사용 금지'에 걸리는 것). 이유를 남겨 두지
않으면 다음 사람이 같은 걸 다시 넣는다.

    amount_max / per_recipient        타깃 그 자체
    amount_min                        같은 금액 표현의 하한. 타깃과 같은 문장에서
                                      나온 값이라 사실상 타깃의 복사본
    amount_unit_raw                   타깃 표현의 단위어(백만원/억원). 자릿수를
                                      그대로 읽는 값
    per_recipient_basis               타깃 정제 조건(전부 stated_cap 이라 상수)
    evidence_text 원문(비마스킹)       타깃이 적힌 문장

## support_cap / loan_limit / support_per_recipient 는 왜 없는가

지시서 3장의 후보 중 이 셋은 **이 데이터셋에서 타깃과 같은 값**이다.
`per_recipient(basis=stated_cap)` 이 곧 원문에 적힌 기업당 한도이고, 융자
사업의 한도도 같은 자리에서 파싱된다(융자는 `support_method=loan` 으로만
갈린다 — `m2_features.TARGET_SEMANTICS` 참조). 지시서 4장이 지목한
"target 과 사실상 동일한 의미의 금액 컬럼"이라 feature 로 넣지 않고
누수 후보로 분리한다.

지시서 3장의 나머지 중 `support_rate` · `self_burden_rate` · `selected_count` ·
`project_duration` · `support_unit` · `support_method` 는 **이미 M65 feature 에
들어 있다**(`M45.CATS` / `M45.NUMS`). 여기서는 값을 새로 만드는 게 아니라
결측 여부·근거 등급·일관성 같은 **표현을 보강**한다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
import os as _os
import sys as _sys

def _find_ml_root(_start):
    """`ml/` 를 위로 거슬러 찾는다. 파일이 몇 단계 아래로 옮겨져도 동작한다."""
    _p = _os.path.abspath(_start)
    while True:
        _p = _os.path.dirname(_p)
        if (_os.path.isdir(_os.path.join(_p, "pipelines"))
                and _os.path.isdir(_os.path.join(_p, "data"))):
            return _p
        if _p == _os.path.dirname(_p):
            raise RuntimeError("ml root not found from %s" % _start)


_ML = _find_ml_root(__file__)
for _d in ("pipelines", "evaluation", "experiments"):
    _base = _os.path.join(_ML, _d)
    if not _os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in _os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in _sys.path:
            _sys.path.insert(0, _dp)
# -------------------------------------------------------------------------

import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amount_parser as AP
import common as C
import m2_features as F

LAYER_VERSION = "m2-source-v1"
TAXONOMY = os.path.join(C.PROC, "business_taxonomy.parquet")

# 지시서 3장 후보 중 타깃과 같은 값이라 feature 로 쓰지 않는 것.
LEAKAGE_EXCLUDED = {
    "support_cap": "타깃 그 자체 — per_recipient(basis=stated_cap)이 원문의 기업당 한도다",
    "support_per_recipient": "타깃 그 자체 (컬럼 이름만 다르다)",
    "support_per_project": "amount_type=per_project 행의 타깃. support_unit 으로만 갈린다",
    "loan_limit": "융자 사업의 타깃. 별도 컬럼이 아니라 같은 자리에서 파싱된다",
    "amount_min": "타깃과 같은 금액 표현의 하한 — 사실상 복사본",
    "amount_unit_raw": "타깃 표현의 단위어. 자릿수를 그대로 읽는다",
}

# 이미 M65 feature 에 있어서 '새로 넣을' 것이 아닌 것.
ALREADY_IN_M65 = ["support_rate(=support_ratio)", "self_burden_rate(=self_burden_ratio)",
                  "selected_count(=support_count)", "project_duration",
                  "support_unit", "support_method"]

# ------------------------------------------------------------ 텍스트 마스킹
_DIGIT = re.compile(r"\d")
_BODY_CAP = 4000          # 문서 앞 4,000자. 공고문 뒤쪽은 서식·법령이라 신호가 없다

# 지급 주기. 금액 표현의 문맥에서만 찾는다 — 문서 전체를 훑으면 '월 매출'·
# '연 2회 접수' 같은 무관한 표현이 걸린다.
CADENCE_RULES = [
    ("daily",     r"1?\s*일\s*(?:당|최대|한도)|일당|1일\s*\d"),
    ("monthly",   r"월\s*(?:당|최대|한도|정액)|매월|월\s*\d|개월간\s*월"),
    ("quarterly", r"분기\s*(?:당|별|최대|한도)"),
    ("yearly",    r"연\s*(?:간|당|최대|한도)|연간|\d\s*년간"),
]
CADENCE_RE = [(k, re.compile(p)) for k, p in CADENCE_RULES]

# 지원 항목(무엇에 쓰는 돈인가). 금액 규모를 가르는 설계 정보이고 금액 표현과
# 독립이다 — 시설·설비 사업은 수십억, 전시회 참가는 수백만원이다.
SCOPE_KEYWORDS = [
    ("facility",  r"시설|설비|장비\s*구축|공장|건축|증설|생산라인"),
    ("rnd",       r"연구\s*개발|R&D|기술\s*개발|과제\s*수행|시제품|실증"),
    ("labor",     r"인건비|채용|고용\s*장려|임금|근로자\s*지원"),
    ("overseas",  r"해외\s*진출|수출|전시회|박람회|바이어|통관|물류비"),
    ("marketing", r"마케팅|홍보|브랜드|디자인\s*개발|판로"),
    ("consult",   r"컨설팅|멘토링|자문|진단|교육|훈련"),
    ("cert",      r"인증|특허|지식재산|규격|시험\s*분석"),
    ("finance",   r"융자|보증|이차보전|정책자금|운전자금"),
]
SCOPE_RE = [(k, re.compile(p)) for k, p in SCOPE_KEYWORDS]


def mask_text(text, cap=_BODY_CAP):
    """텍스트 feature 로 넘기기 전에 숫자를 전부 없앤다.

    `m2_features.mask_amount_expressions` 는 '1,000만원' 처럼 **단위가 붙은**
    표현만 지운다. 본문에는 '60~100백만원' 의 앞쪽 '60' 처럼 단위 없이 남는
    숫자가 있고, 그것만으로도 자릿수가 새어 나간다. 그래서 마스킹 뒤 남은
    숫자를 전부 `#` 로 덮는다 — 자릿수 정보를 0으로 만드는 것이 목적이다.
    """
    return _DIGIT.sub("#", F.mask_amount_expressions(str(text or "")))[:cap]


# ------------------------------------------------------------ 금액 후보
def sorted_candidates(text):
    """`AP.parse_support` 와 **같은 우선순위**로 정렬한 금액 후보.

    같은 규칙을 두 번 쓰지 않도록 정렬 키를 파서에서 그대로 가져온다. 여기가
    어긋나면 '파서가 고른 후보(=타깃)'를 잘못 지목해 타깃이 feature 로 샌다.
    """
    c = AP.extract_amounts(text)
    c.sort(key=lambda x: (AP._PRIORITY.index(x["type"]), -(x["max"] or x["min"] or 0)))
    return c


def _val(c):
    v = c["max"] if c["max"] is not None else c["min"]
    return float(v) if v else np.nan


def _log10(v):
    return float(np.log10(v)) if (v is not None and np.isfinite(v) and v > 0) else np.nan


def _cadence(fragment):
    for k, rx in CADENCE_RE:
        if rx.search(fragment or ""):
            return k
    return "none"


# ------------------------------------------------------------ 행 단위 추출
def extract_row(source_text, body_text, row):
    """한 행의 원천 feature. `row` 는 design_features_v2 의 한 줄(Series)."""
    src = str(source_text or "")
    cands = sorted_candidates(src)
    target_cand = cands[0] if cands else None       # 파서가 골랐을 후보 = 타깃
    others = cands[1:]                              # 여기서만 금액 feature 를 만든다

    tb = [_val(c) for c in others if c["type"] == "total_budget"]
    tb = [v for v in tb if np.isfinite(v) and v > 0]
    total_budget = max(tb) if tb else np.nan

    count = row.get("support_count")
    count = float(count) if pd.notna(count) else np.nan
    ratio = row.get("support_ratio")
    burden = row.get("self_burden_ratio")
    dur = row.get("project_duration")

    ctx = target_cand["context"] if target_cand else src[:200]
    cadence = _cadence(ctx)
    scope = {("nb_scope_" + k): int(bool(rx.search(src))) for k, rx in SCOPE_RE}

    return {
        # --- B. budget / cap -------------------------------------------
        "nb_total_budget_log10": _log10(total_budget),
        "nb_has_total_budget": int(np.isfinite(total_budget)),
        "nb_n_amounts": len(cands),
        "nb_n_amounts_other": len(others),
        # 건수가 1이면 '총예산÷건수'는 정의상 기업당 한도와 같은 숫자다 —
        # 다른 경로로 만든 타깃 복사본이 된다. 그래서 2건 이상에서만 만든다.
        "nb_budget_per_count_log10": _log10(total_budget / count)
        if (np.isfinite(total_budget) and np.isfinite(count) and count >= 2) else np.nan,
        # --- C. selected_count -----------------------------------------
        "nb_count_log10": _log10(count),
        "nb_has_count": int(np.isfinite(count)),
        "nb_count_over_cap": int(np.isfinite(count) and count > AP.COUNT_SANE[1]),
        # --- D. support_rate / self_burden ------------------------------
        "nb_has_ratio": int(pd.notna(ratio)),
        "nb_has_burden": int(pd.notna(burden)),
        "nb_rate_sum": float(ratio) + float(burden)
        if (pd.notna(ratio) and pd.notna(burden)) else np.nan,
        # --- E. duration / 지급주기 --------------------------------------
        "nb_duration_basis": str(row.get("duration_basis") or "none"),
        "nb_has_duration": int(pd.notna(dur)),
        "nb_duration_months": float(dur) * 12 if pd.notna(dur) else np.nan,
        "nb_cadence": cadence,
        "nb_is_periodic": int(cadence != "none"),
        # --- F. unit / method 보강 ---------------------------------------
        "nb_unit_basis": str(row.get("support_unit_basis") or "none"),
        "nb_method_hits_n": len([h for h in str(row.get("method_hits") or "").split("|") if h]),
        "nb_amount_type_source": str(row.get("amount_type_source") or "none"),
        "nb_evidence_source": str(row.get("evidence_source") or "none"),
        "nb_src_len_log10": _log10(max(len(src), 1)),
        "nb_body_len_log10": _log10(max(len(str(body_text or "")), 1)),
        **scope,
    }


# feature 를 지시서 9장의 ablation 단계에 배정한다. 이름이 아니라 이 표가 기준이다.
LAYERS = {
    "B": ["nb_total_budget_log10", "nb_has_total_budget", "nb_n_amounts",
          "nb_n_amounts_other", "nb_budget_per_count_log10"],
    "C": ["nb_count_log10", "nb_has_count", "nb_count_over_cap"],
    "D": ["nb_has_ratio", "nb_has_burden", "nb_rate_sum"],
    "E": ["nb_duration_basis", "nb_has_duration", "nb_duration_months",
          "nb_cadence", "nb_is_periodic"],
    "F": ["nb_unit_basis", "nb_method_hits_n", "nb_amount_type_source",
          "nb_evidence_source", "nb_src_len_log10", "nb_body_len_log10"]
         + ["nb_scope_" + k for k, _ in SCOPE_KEYWORDS],
}
CAT_FEATURES = ["nb_duration_basis", "nb_cadence", "nb_unit_basis",
                "nb_amount_type_source", "nb_evidence_source"]


def raw_bodies(d):
    """마스킹 **전** 원천 텍스트 두 갈래.

        src   금액 후보를 뽑는 자리 (taxonomy=scale_text, bizinfo=공고문 원문)
        raw   본문 텍스트 feature 의 재료. taxonomy 는 사업목적·사업내용·
              지원대상 원문을 앞에 붙인다 — 그쪽 `scale_text` 는 146자 남짓이라
              그것만으로는 '사업 내용'이라 부를 것이 없다.

    M70(본문 표현 튜닝)이 `head`·`section`·`dedup` 같은 변형을 만들려면 마스킹
    전 원문이 필요하다. `build` 가 쓰던 조립을 여기로 꺼내 두 곳이 같은 문자열을
    보게 한다 — 갈라지면 M69 baseline 이 재현되지 않는다.
    """
    tax = pd.read_parquet(TAXONOMY).set_index("row_id")
    src, raw = [], []
    for rid, ev in zip(d["row_id"].astype(str), d["evidence_text"].fillna("")):
        if rid in tax.index:
            t = tax.loc[rid]
            desc = " ".join(str(t.get(c) or "") for c in ("purpose", "content", "target_text"))
            src.append(str(ev))
            raw.append(desc + " " + str(ev))
        else:
            src.append(str(ev))
            raw.append(str(ev))
    return np.array(src), np.array(raw)


def build(d):
    """모델링 행(`M45.prepare` 를 통과한 프레임)에 원천 feature 층을 만든다.

    반환
        NB      새 feature 프레임 (행 순서는 `d` 그대로)
        body    마스킹된 본문 텍스트 (텍스트 feature 용, G 단계)
        src     마스킹 **전** 원천 텍스트 (감사용 — 모델에 넣지 않는다)
    """
    src, raw = raw_bodies(d)
    body = [mask_text(t) for t in raw]
    rows = [extract_row(s, b, d.iloc[i]) for i, (s, b) in enumerate(zip(src, body))]
    NB = pd.DataFrame(rows)
    for c in CAT_FEATURES:
        NB[c] = NB[c].fillna("none").astype("category")
    return NB, np.array(body), np.array(src)


def columns_upto(step):
    """ablation 단계까지 누적된 새 feature 이름. 'A' 면 빈 목록(M65 그대로)."""
    order = ["B", "C", "D", "E", "F"]
    if step in ("A", "G"):
        return sum((LAYERS[k] for k in order), []) if step == "G" else []
    return sum((LAYERS[k] for k in order[:order.index(step) + 1]), [])


def manifest():
    return {
        "layer_version": LAYER_VERSION,
        "source_text": {
            "taxonomy": "business_taxonomy.purpose + content + target_text (+ scale_text)",
            "bizinfo": "design_features_v2.evidence_text (공고문 원문 또는 API 요약)",
        },
        "target_exclusion": "AP.extract_amounts 를 AP._PRIORITY 로 정렬한 뒤 "
                            "첫 후보(=파서가 고른 타깃)를 제외하고 나머지에서만 금액 feature 생성",
        "text_masking": "mask_amount_expressions -> 남은 모든 숫자를 '#' 로 치환, 앞 %d자"
                        % _BODY_CAP,
        "layers": {k: list(v) for k, v in LAYERS.items()},
        "categorical": list(CAT_FEATURES),
        "leakage_excluded": dict(LEAKAGE_EXCLUDED),
        "already_in_m65": list(ALREADY_IN_M65),
    }
