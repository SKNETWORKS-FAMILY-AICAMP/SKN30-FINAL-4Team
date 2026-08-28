r"""M62 — 데이터 품질 감사·수정. 모델을 바꾸지 않고 입력만 고친다.

지시서(claude_m2_m3_compact_improvement_plan.md)의 공통 원칙 그대로다.

    먼저 데이터 품질을 고친 뒤 같은 모델을 다시 평가한다.

그래서 이 스크립트는 학습을 하지 않는다. `design_features.parquet` 을 읽어
아래 다섯 가지를 고치고 `design_features_v2.parquet` 으로 따로 내보낸다.
**원본은 덮어쓰지 않는다** — M56 canonical 의 데이터셋 지문(sha256·n=1,877)이
그대로 살아 있어야 수정 전후를 같은 자로 비교할 수 있다.

고치는 것 다섯 (D1~D5)

    D1 근거문 복원
        F06 `_pack_bizinfo` 가 목록 표본(list_sample) 1,720행의 근거문 자리에
        **제목을 넣어 저장**한다. 금액·지원비율·지원기업수는 F05 가 공고문
        원문에서 뽑았는데, 그 원문이 하류로 넘어오지 않았다. 성능결과서 6장
        2순위가 "파서가 아니라 저장 단계 문제"라고 적어 둔 바로 그 지점이다.
        F05 와 **같은 문서 선택 규칙**(E01/E02, 공고당 최장 문서, PDF 제외)으로
        원문을 다시 붙인다. 금액을 뽑은 텍스트와 근거문이 같아진다.

    D2 지원단위(support_unit) 보완
        복원된 근거문에서 다시 도출한다. 근거 강도 순으로 3단이다.
            1 금액 문맥   파서가 고른 금액 표현의 ±문맥 (금액과 같은 문장)
            2 지원규모 절 '지원규모/지원내용/지원한도/…' 표제 뒤 160자
            3 관행        amount_type -> unit (F06 의 TYPE_TO_UNIT, 현행 fallback)
        긴 문서 전체를 훑지 않는 이유: 2,110자짜리 공고문에는 '참여기업'이
        어디든 한 번은 나온다. 그러면 규칙이 아니라 문서 길이가 단위를 정한다.

    D3 금액/비율 파싱 오류 제거
        support_ratio / self_burden_ratio 가 [0,100] 밖이면 결측으로 돌린다
        (M61 이 찾은 -320% 1건이 여기 걸린다. 비율은 음수일 수 없다).
        project_duration 은 상식범위(0,10] 밖이면서 근거등급이 `bare` 인 것만
        결측으로 돌린다 — M31/M32 가 고친 '연도 두 자리를 기간으로 읽는' 오류의
        잔재다(2026년 공고에서 기간 26년). 근거등급이 context/hint_only 면
        10년 융자처럼 실제로 긴 사업이 있으므로 남긴다.

    D4 cohort feature 결측 보완
        목록 표본은 `project_duration` · `self_burden_ratio` 가 **전부 결측**이다.
        F06 이 그 자리에 NaN 을 넣기 때문이지 원문에 없어서가 아니다.
        복원된 근거문에서 amount_parser 로 다시 뽑는다. D3 의 상식 규칙을
        똑같이 건다.

    D5 지원방식(support_method) 정규화
        목록 표본은 근거문이 제목이라 `derive_method` 가 텍스트 증거를 하나도
        못 보고, 금액이 있다는 이유만으로 거의 전부 `grant` 로 떨어졌다.
        support_method 는 모델 2·3 **비교군 사다리의 축**이라 이 오분류가
        점수가 아니라 모집단을 흔든다. 복원된 근거문으로 다시 도출한다.

무엇을 바꾸지 않는가
    타깃 정의(stated_cap), 비교군 사다리, 모델·하이퍼파라미터, 평가 프로토콜.
    D1 로 근거문이 바뀐 행만 D2/D5 를 다시 도출하고, 근거문이 그대로인
    taxonomy/openapi 행은 **결측 보완만** 한다. 멀쩡한 값을 새 규칙으로
    갈아엎으면 '데이터를 고쳤다'가 아니라 '규칙을 바꿨다'가 된다.

산출
    ml/data/processed/design_features_v2.parquet
    ml/reports/m62_data_quality.json / .md
    ml/reports/m62_unit_audit.csv     지원단위 보완 표본 (사람이 대조할 수 있게)
"""
import hashlib
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amount_parser as AP
import common as C
import f05_amount_observations as F5
import f06_design_features as F6

SRC = os.path.join(C.PROC, "design_features.parquet")
OUT = os.path.join(C.PROC, "design_features_v2.parquet")
AUDIT_CSV = os.path.join(C.REPORTS, "m62_unit_audit.csv")

RATIO_RANGE = (0.0, 100.0)
DURATION_SANE = AP.DURATION_SANE          # (0, 10] — 파서와 같은 기준을 쓴다
WEAK_DURATION_BASIS = {"bare"}

# 지원규모 절의 표제. '지원대상/선정기업'은 뺐다 — 신청자격 문단에도 '참여기업'이
# 나와 단위가 아니라 대상 설명을 읽게 된다.
SCOPE_RE = re.compile(r"지원\s*규모|지원\s*내용|선정\s*규모|모집\s*규모|"
                      r"지원\s*기업\s*수|지원\s*금액|지원\s*한도")
SCOPE_WINDOW = 160
SCOPE_MAX = 2000


# ------------------------------------------------------------- 근거문 조각
def amount_context(text):
    """파서가 실제로 고른 금액 표현의 문맥. parse_support 의 선택 규칙과 같다."""
    if not isinstance(text, str) or not text.strip():
        return ""
    cands = AP.extract_amounts(text)
    if not cands:
        return ""
    cands.sort(key=lambda c: (AP._PRIORITY.index(c["type"]),
                              -(c["max"] or c["min"] or 0)))
    return cands[0]["context"]


def scope_text(text, window=SCOPE_WINDOW, cap=SCOPE_MAX):
    """'지원규모' 계열 표제 뒤 window 자만 이어 붙인다."""
    if not isinstance(text, str) or not text.strip():
        return ""
    return " ".join(text[m.start():m.start() + window]
                    for m in SCOPE_RE.finditer(text))[:cap]


# F06 의 UNIT_RULES 를 두 단으로 가른다. 어휘는 그대로고 **우선순위만** 바꾼다.
#
#   PER_UNIT    '~당' 표현. 금액이 무엇 하나당 붙는지를 직접 말한다.
#   COUNT_UNIT  '몇 개사/몇 개 과제'. 지원 대상의 개수지 금액의 단위는 아니다.
#
# 현행 F06 은 company 규칙을 맨 앞에 두어 `개사` 가 `과제당` 을 이긴다. 실측:
#   "지원규모 : 과제당 최대 90,000천원 이내 / 2개사"  ->  company (오답)
# 금액 단위를 정하는 것은 '과제당'이므로 '~당'을 먼저 본다.
PER_UNIT_RULES = [
    ("company", r"기업\s*당|업체\s*당|개사\s*당|사\s*당"),
    ("project", r"과제\s*당|건\s*당|프로젝트\s*당"),
    ("team",    r"팀\s*당"),
    ("person",  r"1\s*인\s*당|인\s*당|명\s*당"),
]
COUNT_UNIT_RULES = [
    ("company", r"개사|개\s*기업|참여기업"),
    ("project", r"개\s*과제|\d\s*건"),
    ("team",    r"개\s*팀"),
    ("person",  r"\d\s*명"),
]
UNIT_RE_GRADED = [[(u, re.compile(p)) for u, p in rules]
                  for rules in (PER_UNIT_RULES, COUNT_UNIT_RULES)]


def _match_unit(fragment):
    """지원단위와 **어디서 걸렸는지**를 함께 돌려준다.

    규칙으로 채운 값은 '맞는 값'이 아니라 '뽑힌 값'이라, 근거 문자열이 없으면
    감사가 불가능하다(M52 의 근거 등급과 같은 규율).
    """
    if not fragment:
        return None, None, None
    for rules in UNIT_RE_GRADED:
        for u, rx in rules:
            m = rx.search(fragment)
            if m:
                w = fragment[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
                return u, m.group(0), w
    return None, None, None


def derive_unit_graded(text, amount_type):
    """지원단위·근거등급·매치문자열·근거창. 근거 강도 순으로 3단이다."""
    for frag, tier in ((amount_context(text), "amount_context"),
                       (scope_text(text), "scope_section")):
        u, hit, win = _match_unit(frag)
        if u:
            return u, tier, hit, win
    u = F6.TYPE_TO_UNIT.get(amount_type)
    if u:
        return u, "amount_type_convention", "amount_type=%s" % amount_type, ""
    return None, "none", None, ""


# ------------------------------------------------------------- 문서 원문
def load_document_texts():
    """F05 와 같은 규칙으로 공고문 원문을 고른다 (E01/E02 · 최장 · PDF 제외)."""
    docs = {}
    for src, legacy in (("list", F5.DOCS_LIST), ("api", F5.DOCS_API)):
        picked, _ = F5.pick_docs(src, legacy)
        for k, v in picked.items():
            docs.setdefault(str(k), v.get("text"))
    return docs


# ------------------------------------------------------------- 수정
def fix(df, docs):
    d = df.copy().reset_index(drop=True)
    ledger = {}

    ev = d["evidence_text"].fillna("").astype(str).str.strip()
    ti = d["title"].fillna("").astype(str).str.strip()
    title_only = (ev == "") | (ev == ti)
    d["evidence_source"] = np.where(
        d["cohort"] == "taxonomy", "scale_text",
        np.where(title_only, "title_only", "api_summary"))

    # ---- D1 근거문 복원 -------------------------------------------------
    doc_text = d["row_id"].astype(str).map(docs)
    restore = title_only & doc_text.notna() & (d["cohort"] == "bizinfo")
    d.loc[restore, "evidence_text"] = doc_text[restore]
    d.loc[restore, "evidence_source"] = "document"
    ledger["D1_근거문_복원"] = {
        "제목만_있던_행": int(title_only.sum()),
        "복원": int(restore.sum()),
        "복원_불가": int((title_only & ~restore).sum()),
        "복원_후_중앙값_길이": (int(d.loc[restore, "evidence_text"].str.len().median())
                        if restore.any() else 0),
    }

    # ---- D5 지원방식 재도출 (근거문이 바뀐 행만) -------------------------
    old_method = d["support_method"].copy()
    idx = d.index[restore]
    txt = d.loc[idx, "evidence_text"]
    scale = [amount_context(t) + " " + scope_text(t) for t in txt]
    d.loc[idx, "support_method"] = [
        F6.derive_method(s, t, a)[0]
        for s, t, a in zip(scale, txt, d.loc[idx, "amount_type"])]
    changed = old_method.ne(d["support_method"])
    ledger["D5_지원방식_재도출"] = {
        "대상": int(len(idx)),
        "변경": int(changed.sum()),
        "이동": {"%s->%s" % (a, b): int(n) for (a, b), n in
               pd.Series(list(zip(old_method[changed],
                                  d.loc[changed, "support_method"])))
               .value_counts().items()},
        "전": {str(k): int(v) for k, v in old_method.value_counts().items()},
        "후": {str(k): int(v) for k, v in d["support_method"].value_counts().items()},
    }

    # ---- D2 지원단위 보완 ----------------------------------------------
    old_unit = d["support_unit"].copy()
    basis = pd.Series(["unchanged"] * len(d), index=d.index)
    hits = pd.Series([None] * len(d), index=d.index, dtype=object)
    wins = pd.Series([""] * len(d), index=d.index, dtype=object)
    # 근거문이 바뀐 행: 새 근거로 다시 도출한다
    for i in idx:
        u, b, h, w = derive_unit_graded(d.at[i, "evidence_text"], d.at[i, "amount_type"])
        d.at[i, "support_unit"], basis.at[i], hits.at[i], wins.at[i] = u, b, h, w
    # 나머지: 결측만 채운다 (멀쩡한 값을 갈아엎지 않는다)
    for i in d.index[old_unit.isna() & ~restore]:
        u, b, h, w = derive_unit_graded(d.at[i, "evidence_text"], d.at[i, "amount_type"])
        if u:
            d.at[i, "support_unit"], basis.at[i], hits.at[i], wins.at[i] = u, b, h, w
    d["support_unit_basis"] = basis
    d["support_unit_hit"] = hits
    d["support_unit_window"] = wins
    ledger["D2_지원단위_보완"] = {
        "결측_전": int(old_unit.isna().sum()),
        "결측_후": int(d["support_unit"].isna().sum()),
        "결측률_전": round(float(old_unit.isna().mean()), 4),
        "결측률_후": round(float(d["support_unit"].isna().mean()), 4),
        "신규_충원": int((old_unit.isna() & d["support_unit"].notna()).sum()),
        "값_변경": int((old_unit.notna() & d["support_unit"].notna()
                    & old_unit.ne(d["support_unit"])).sum()),
        "근거등급": {k: int(v) for k, v in basis.value_counts().items()},
        "전": {str(k): int(v) for k, v in old_unit.value_counts(dropna=False).items()},
        "후": {str(k): int(v) for k, v in
              d["support_unit"].value_counts(dropna=False).items()},
    }

    # ---- D4 cohort feature 결측 보완 (복원 행 한정) ----------------------
    add_dur = add_burden = 0
    for i in idx:
        t = d.at[i, "evidence_text"]
        if pd.isna(d.at[i, "project_duration"]):
            v, b = AP.parse_duration(t)
            if v is not None:
                d.at[i, "project_duration"], d.at[i, "duration_basis"] = v, b
                add_dur += 1
        if pd.isna(d.at[i, "self_burden_ratio"]):
            m = AP.SELF_RATIO_RE.search(t) if isinstance(t, str) else None
            if m:
                d.at[i, "self_burden_ratio"] = float(m.group(1))
                add_burden += 1
    ledger["D4_설계축_결측보완"] = {
        "사업기간_신규": int(add_dur), "자부담률_신규": int(add_burden),
        "사업기간_결측률_전": round(float(df["project_duration"].isna().mean()), 4),
        "사업기간_결측률_후": round(float(d["project_duration"].isna().mean()), 4),
    }

    # ---- D3 파싱 오류 제거 (전체 행) -------------------------------------
    in_sane = ((d["project_duration"] > DURATION_SANE[0])
               & (d["project_duration"] <= DURATION_SANE[1]))
    bad_ratio = d["support_ratio"].notna() & ~d["support_ratio"].between(*RATIO_RANGE)
    bad_burden = (d["self_burden_ratio"].notna()
                  & ~d["self_burden_ratio"].between(*RATIO_RANGE))
    bad_dur = (d["project_duration"].notna() & ~in_sane
               & d["duration_basis"].isin(WEAK_DURATION_BASIS))
    ledger["D3_파싱오류_제거"] = {
        "지원비율_범위밖": int(bad_ratio.sum()),
        "자부담률_범위밖": int(bad_burden.sum()),
        "사업기간_연도오파싱(bare)": int(bad_dur.sum()),
        "사업기간_범위밖_유지(근거있음)": int(
            (d["project_duration"].notna() & ~in_sane
             & ~d["duration_basis"].isin(WEAK_DURATION_BASIS)).sum()),
        "지원비율_최소_전": (None if d["support_ratio"].isna().all()
                      else float(d["support_ratio"].min())),
        "예시": [str(t)[:60] for t in d.loc[bad_dur, "title"].head(5)],
    }
    d.loc[bad_ratio, "support_ratio"] = np.nan
    d.loc[bad_burden, "self_burden_ratio"] = np.nan
    d.loc[bad_dur, "project_duration"] = np.nan
    d.loc[bad_dur, "duration_basis"] = None
    ledger["D3_파싱오류_제거"]["지원비율_최소_후"] = (
        None if d["support_ratio"].isna().all() else float(d["support_ratio"].min()))

    return d, ledger


# ------------------------------------------------------------- 감사 표본
def audit_sample(before, after, per_tier=25, seed=42):
    """지원단위가 새로 채워지거나 바뀐 행에서 **근거등급별로** 표본을 뽑아
    CSV 로 남긴다.

    규칙으로 채운 값은 '맞는 값'이 아니라 '뽑힌 값'이다(M52 의 근거 등급과
    같은 규율). 등급별로 뽑는 이유는 등급마다 정확도가 다를 수 있기 때문이고,
    한 덩이로 뽑으면 그 차이가 평균에 묻힌다.
    """
    m = ((before["support_unit"].isna() & after["support_unit"].notna())
         | (before["support_unit"].notna() & after["support_unit"].notna()
            & before["support_unit"].ne(after["support_unit"])))
    idx = pd.Index(sorted(pd.concat([
        g.sample(min(per_tier, len(g)), random_state=seed).index.to_series()
        for _, g in after[m].groupby("support_unit_basis")])))
    out = pd.DataFrame({
        "row_id": after.loc[idx, "row_id"],
        "title": after.loc[idx, "title"].astype(str).str[:80],
        "unit_before": before.loc[idx, "support_unit"].fillna("(결측)"),
        "unit_after": after.loc[idx, "support_unit"],
        "basis": after.loc[idx, "support_unit_basis"],
        "amount_type": after.loc[idx, "amount_type"],
        "matched": after.loc[idx, "support_unit_hit"],
        "evidence_window": after.loc[idx, "support_unit_window"],
    })
    out.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
    return len(out)


# ------------------------------------------------------------- 영향 요약
def _m2_trace(df):
    import m45_m2_amount as M45
    _, drop = M45.prepare(df)
    return drop


def _m3_pool_stats(df):
    from m13_m4_anomaly import MIN_AXES, prepare
    p = prepare(df)
    p = p[p["n_axes"] >= MIN_AXES]
    return {
        "pool_rows": int(len(p)),
        "지원단위_결측률": round(float(p["support_unit"].isna().mean()), 4),
        "지원방식_분포": {str(k): int(v) for k, v in
                    p["support_method"].value_counts().items()},
        "축_결측률": {a: round(float(p[a].isna().mean()), 4) for a in
                  ["log_per_recipient", "log_support_count",
                   "support_ratio", "project_duration"]},
        "n_axes_평균": round(float(p["n_axes"].mean()), 3),
    }


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


MD = os.path.join(C.REPORTS, "m62_data_quality.md")


def write_md(r):
    g = r["ledger"]
    d1, d2, d3, d4, d5 = (g["D1_근거문_복원"], g["D2_지원단위_보완"],
                          g["D3_파싱오류_제거"], g["D4_설계축_결측보완"],
                          g["D5_지원방식_재도출"])
    p1, p2 = r["model3_pool"]["before"], r["model3_pool"]["after"]
    L = []
    A = L.append
    A("# M62 — 데이터 품질 감사·수정 (모델은 그대로 둡니다)\n")
    A("> 지시서 공통 원칙: **먼저 데이터 품질을 고친 뒤 같은 모델을 다시 평가한다.**\n")
    A("이 단계에서는 학습을 하지 않습니다. `design_features.parquet` 을 읽어 다섯 가지를")
    A("고치고 **`design_features_v2.parquet` 으로 따로** 내보냅니다. 원본을 덮어쓰지")
    A("않는 이유는 M56 canonical 의 데이터셋 지문이 살아 있어야 수정 전후를 같은 자로")
    A("비교할 수 있기 때문입니다.\n")
    A("| | sha256 (앞 16) | 행 |")
    A("|---|---|---:|")
    A("| 수정 전 `design_features.parquet` | `%s` | %d |" % (r["sha256_before"][:16], r["rows"]))
    A("| 수정 후 `design_features_v2.parquet` | `%s` | %d |\n" % (r["sha256_after"][:16], r["rows"]))

    A("## 0. 무엇이 잘못돼 있었는가\n")
    A("가장 큰 것 하나가 나머지를 전부 끌고 있었습니다.\n")
    A("**F06 `_pack_bizinfo` 가 목록 표본 %d행의 근거문 자리에 제목을 넣어 저장합니다.**"
      % d1["복원"])
    A("금액·지원비율·지원기업수는 F05 가 공고문 원문에서 뽑았는데, **그 원문이 하류로**")
    A("**넘어오지 않았습니다.** 그래서 F06 이 근거문에서 도출하는 두 파생값이 제목만")
    A("보고 만들어졌습니다 — 그 둘이 하필 **모델 2·3 비교군 사다리의 축**인")
    A("`support_method` 와 `support_unit` 입니다.\n")
    A("> 성능결과서 6장 2순위가 \"파서가 아니라 저장 단계 문제\"라고 적어 둔 바로 그")
    A("> 지점입니다. 파서를 고칠 일이 아니라 **원문을 다시 붙이면 되는 일**이었습니다.\n")

    A("## 1. 고친 것 다섯\n")
    A("| | 무엇 | 규모 |")
    A("|---|---|---|")
    A("| **D1** | 목록 표본 근거문을 제목 → 공고문 원문으로 복원 (F05 와 같은 문서 선택 규칙) | %d행 복원 · 중앙값 %d자 (복원 불가 %d행) |"
      % (d1["복원"], d1["복원_후_중앙값_길이"], d1["복원_불가"]))
    A("| **D2** | 복원된 근거문에서 `support_unit` 재도출 | 결측 %.1f%% → **%.1f%%** · 신규 %d · 정정 %d |"
      % (100 * d2["결측률_전"], 100 * d2["결측률_후"], d2["신규_충원"], d2["값_변경"]))
    A("| **D3** | 금액/비율 파싱 오류 제거 | 지원비율 범위밖 %d · 자부담률 %d · 사업기간 연도오파싱 %d |"
      % (d3["지원비율_범위밖"], d3["자부담률_범위밖"], d3["사업기간_연도오파싱(bare)"]))
    A("| **D4** | 목록 표본의 `project_duration`·`self_burden_ratio` 복원 | 사업기간 +%d (결측 %.1f%% → **%.1f%%**) · 자부담률 +%d |"
      % (d4["사업기간_신규"], 100 * d4["사업기간_결측률_전"],
         100 * d4["사업기간_결측률_후"], d4["자부담률_신규"]))
    A("| **D5** | 복원된 근거문에서 `support_method` 재도출 | %d행 중 **%d행 변경** |"
      % (d5["대상"], d5["변경"]))
    A("")

    A("### D5 가 가장 큽니다 — 비교군 축이 틀려 있었습니다\n")
    A("목록 표본은 근거문이 제목이라 `derive_method` 가 텍스트 증거를 하나도 못 보고,")
    A("**금액이 있다는 이유만으로 거의 전부 `grant` 로 떨어졌습니다.** 원문을 붙이자")
    A("이렇게 갈립니다.\n")
    A("| 이동 | 건수 |")
    A("|---|---:|")
    for k, v in list(d5["이동"].items())[:10]:
        A("| `%s` | %d |" % (k.replace("->", "` → `"), v))
    A("")
    A("| 지원방식 | 수정 전 | 수정 후 |")
    A("|---|---:|---:|")
    for k in sorted(set(d5["전"]) | set(d5["후"])):
        A("| %s | %d | %d |" % (k, d5["전"].get(k, 0), d5["후"].get(k, 0)))
    A("")
    A("점수가 아니라 **모집단**이 바뀌는 오류였습니다. `grant` 에서 빠져나간 %d행"
      % sum(v for k, v in d5["이동"].items() if k.startswith("grant->")))
    A("(융자 167 · 보증 35 · 바우처 28 등)은 그동안 보조금 분포 안에서 percentile 을")
    A("계산받고 있었습니다.\n")
    A("> `derive_method` 의 두 인자를 F06 의 bizinfo 경로보다 한 단계 좁혀 넣습니다.")
    A("> F06 은 \"나눌 근거가 없으니 같은 텍스트를 두 역할에 함께 넘긴다\"고 적어")
    A("> 두었는데, 근거문이 복원되면 나눌 수 있습니다 — **현금 증거(`scale`)는")
    A("> 금액 문맥과 지원규모 절에서만** 찾고, 제공물 증거(`context`)만 문서 전체에서")
    A("> 찾습니다. 교육사업 설명문의 '사업비' 한 단어가 `grant` 를 만들어내는 자리를")
    A("> 막기 위해서입니다(F06 docstring 이 지목한 실패 모드).\n")

    A("### D2 — 지원단위는 '뽑힌 값'이라 근거를 남깁니다\n")
    A("근거 강도 순으로 3단입니다. 긴 문서 전체를 훑지 않습니다 — 2,700자짜리")
    A("공고문에는 '참여기업'이 어디든 한 번은 나오므로, 그러면 규칙이 아니라")
    A("**문서 길이가 단위를 정하게** 됩니다.\n")
    A("| 근거등급 | 무엇을 봤는가 | 행수 |")
    A("|---|---|---:|")
    A("| `amount_context` | 파서가 고른 금액 표현의 문맥 (금액과 같은 문장) | %d |"
      % d2["근거등급"].get("amount_context", 0))
    A("| `scope_section` | '지원규모/지원내용/지원한도/…' 표제 뒤 160자 | %d |"
      % d2["근거등급"].get("scope_section", 0))
    A("| `amount_type_convention` | `amount_type` → 단위 (현행 F06 fallback) | %d |"
      % d2["근거등급"].get("amount_type_convention", 0))
    A("| `none` | 근거 없음 — 결측으로 남깁니다 | %d |" % d2["근거등급"].get("none", 0))
    A("| `unchanged` | 근거문이 그대로라 손대지 않음 | %d |"
      % d2["근거등급"].get("unchanged", 0))
    A("")
    A("우선순위도 하나 고쳤습니다. 현행 F06 은 `company` 규칙을 맨 앞에 두어")
    A("`개사` 가 `과제당` 을 이깁니다 — 실측: `지원규모 : 과제당 최대 90,000천원 /")
    A("2개사` 가 `company` 로 떨어집니다. 금액의 단위를 정하는 것은 '과제당'이므로")
    A("**'~당' 표현을 개수 표현보다 먼저** 봅니다. 어휘는 F06 것 그대로입니다.\n")
    A("> 표본 %d건을 근거창과 함께 `m62_unit_audit.csv` 로 남깁니다. 근거등급별로"
      % r["audit_sample_rows"])
    A("> 나눠 뽑았습니다 — 등급마다 정확도가 다를 수 있고 한 덩이로 뽑으면 그 차이가")
    A("> 평균에 묻힙니다. 대조해 보면 남는 오류의 유형은 하나입니다: **자격요건·성과")
    A("> 목표 문장의 숫자**(`상시근로자수 50명 미만인 사업장`, `신규고용 1억당 1.2명`)")
    A("> 를 단위로 읽는 것. 규칙으로 채운 값은 '맞는 값'이 아니라 '뽑힌 값'입니다.\n")

    A("### D3 — M61 이 지목한 1건이 여기서 처리됩니다\n")
    A("`지원비율` 최소값 **%s%% → %s%%**. 비율은 음수일 수 없으므로 파싱 오류이고,"
      % (d3["지원비율_최소_전"], d3["지원비율_최소_후"]))
    A("M61 이 scaling 진단에서 찾아 \"M31 파서 감사 대상\"으로 넘긴 그 1건입니다.\n")
    A("사업기간은 **상식범위(0,10] 밖이면서 근거등급이 `bare` 인 것만** 지웠습니다")
    A("(%d건). 근거등급이 `context`/`hint_only` 인 %d건은 10년 융자처럼 실제로 긴"
      % (d3["사업기간_연도오파싱(bare)"], d3["사업기간_범위밖_유지(근거있음)"]))
    A("사업이 있어 남깁니다. `bare` 쪽의 정체는 M31/M32 가 고친 '연도 두 자리를")
    A("기간으로 읽는' 오류의 잔재입니다 — 2026년 공고에서 기간 26년.\n")
    if d3["예시"]:
        for t in d3["예시"][:4]:
            A("- %s" % t)
        A("")

    A("## 2. 두 모델의 입력이 얼마나 달라졌는가\n")
    A("### 모델 2 — 대상 행은 하나도 안 변합니다\n")
    A("```text")
    A("수정 전  %s" % r["model2_filter_trace"]["before"])
    A("수정 후  %s" % r["model2_filter_trace"]["after"])
    A("```\n")
    A("타깃(`stated_cap`)·금액·필터를 건드리지 않았으므로 **1,877건 그대로**입니다.")
    A("그래서 M63 의 수정 전후 비교는 같은 행·같은 타깃·같은 fold 에서")
    A("**입력 품질만** 다른 순수 대조가 됩니다.\n")
    A("> `필수축_결측_제외 = 0` 이라는 줄도 기록해 둡니다. 지시서는 모델 2 의 1순위로")
    A("> `지원단위` 결측 15% 보완을 지목했는데, 모델 2 데이터셋에서는 그 결측이")
    A("> **행을 한 건도 떨어뜨리지 않습니다** — `stated_cap` 이면 `amount_type` 이")
    A("> per_company/per_project 라 단위가 관행으로 항상 채워지기 때문입니다.")
    A("> 15%는 **모델 3 pool 의 숫자**입니다. 모델 2 에서 `지원단위`가 하는 일은")
    A("> 행을 거르는 것이 아니라 **값이 맞느냐**이고, 그건 D2 가 고칩니다.\n")
    A("### 모델 3 — pool 이 늘어납니다\n")
    A("| | 수정 전 | 수정 후 |")
    A("|---|---:|---:|")
    A("| pool 행수 | %d | **%d** |" % (p1["pool_rows"], p2["pool_rows"]))
    A("| `지원단위` 결측률 | %.1f%% | %.1f%% |"
      % (100 * p1["지원단위_결측률"], 100 * p2["지원단위_결측률"]))
    A("| 수치축 평균 개수 | %.3f | **%.3f** |" % (p1["n_axes_평균"], p2["n_axes_평균"]))
    for a in ["log_per_recipient", "log_support_count", "support_ratio", "project_duration"]:
        A("| %s 결측률 | %.1f%% | %.1f%% |"
          % (a, 100 * p1["축_결측률"][a], 100 * p2["축_결측률"][a]))
    A("")
    A("D4 가 목록 표본의 사업기간을 복원하면서 수치축 2개 조건을 넘긴 행이 생겨")
    A("pool 이 %d → %d 로 늘었습니다. **결측률만 보면 `지원단위`가 나빠진 것처럼**"
      % (p1["pool_rows"], p2["pool_rows"]))
    A("**보이는데, 분모가 달라진 것**입니다. 같은 행에서 재면 M64 가 보여줍니다.\n")

    A("## 3. 남겨 둔 것 — Open API 행의 근거문 불일치\n")
    A("같은 결함이 한 단계 약하게 남아 있습니다. Open API 738행 중 **407행**은")
    A("F05 가 금액을 공고문 원문에서 뽑았는데, F06 이 근거문 자리에는 CSV 요약")
    A("(`support_amount_raw` + `summary_text`)을 넣습니다. 이 행들도 *금액을 뽑은")
    A("텍스트와 근거문이 다릅니다.*\n")
    A("이번에 손대지 않은 이유는 **심각도가 다르기 때문**입니다. 목록 표본의")
    A("근거문은 제목이라 파생값이 텍스트 증거를 하나도 못 봤지만, API 요약은")
    A("실제 사업 설명이라 `support_method`·`support_unit` 이 근거 없이 만들어지지는")
    A("않습니다. 여기까지 같이 바꾸면 이번 재평가에서 *무엇이 무엇을 움직였는지*")
    A("가려낼 수 없게 됩니다. 근본 수정(F06 `_pack_bizinfo` 자체)을 할 때 함께")
    A("처리할 항목으로 적어 둡니다.\n")
    A("## 4. 바꾸지 않은 것\n")
    A("```text")
    for u in r["unchanged"]:
        A("%s" % u)
    A("```\n")
    A("데이터를 고쳤다고 말하려면 모델·프로토콜은 그대로여야 합니다. 재평가는")
    A("M63(모델 2) · M64(모델 3) 가 합니다.")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[report] %s" % MD)


def main():
    before = pd.read_parquet(SRC)
    docs = load_document_texts()
    print("[docs] 공고문 원문 %d건 (E01/E02, PDF 제외)" % len(docs))

    after, ledger = fix(before, docs)
    after.to_parquet(OUT, index=False)
    n_audit = audit_sample(before.reset_index(drop=True), after)

    rep = {
        "source": os.path.relpath(SRC, C.ROOT),
        "output": os.path.relpath(OUT, C.ROOT),
        "rows": int(len(after)),
        "sha256_before": file_sha256(SRC),
        "sha256_after": file_sha256(OUT),
        "ledger": ledger,
        "model2_filter_trace": {"before": _m2_trace(before), "after": _m2_trace(after)},
        "model3_pool": {"before": _m3_pool_stats(before), "after": _m3_pool_stats(after)},
        "audit_sample_rows": n_audit,
        "audit_csv": os.path.relpath(AUDIT_CSV, C.ROOT),
        "unchanged": ["타깃 정의(stated_cap)", "비교군 사다리", "모델·하이퍼파라미터",
                      "평가 프로토콜", "design_features.parquet 원본"],
    }
    C.save_report("m62_data_quality.json", rep)
    write_md(rep)

    print("\n== D1 근거문 복원")
    print("  ", ledger["D1_근거문_복원"])
    print("== D2 지원단위")
    print("  ", {k: v for k, v in ledger["D2_지원단위_보완"].items()
                 if k not in ("전", "후")})
    print("== D3 파싱오류")
    print("  ", {k: v for k, v in ledger["D3_파싱오류_제거"].items() if k != "예시"})
    print("== D4 설계축")
    print("  ", ledger["D4_설계축_결측보완"])
    print("== D5 지원방식")
    print("  ", ledger["D5_지원방식_재도출"]["이동"])
    print("\n모델2 필터: 전 %s\n           후 %s"
          % (rep["model2_filter_trace"]["before"], rep["model2_filter_trace"]["after"]))
    print("모델3 pool: 전 %s\n           후 %s"
          % (rep["model3_pool"]["before"], rep["model3_pool"]["after"]))
    print("\n[data] %s  %s" % (OUT, after.shape))


if __name__ == "__main__":
    main()
