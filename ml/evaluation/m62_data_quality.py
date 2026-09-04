r"""M62 — 데이터 품질 감사. 수정 전후를 같은 자로 세어 원장을 남긴다.

지시서 공통 원칙 그대로다.

    먼저 데이터 품질을 고친 뒤 같은 모델을 다시 평가한다.

**이 스크립트는 더 이상 데이터를 만들지 않는다.** 처음 판(2026-08-28)은 F06 의
산출물 위에서 고치는 **사후 보정 계층**이었는데, M65 가 그 수정을 원천인
`f06_design_features.py` 로 승격했다. 같은 일을 두 곳에서 하면 어느 쪽이
진짜인지 알 수 없으므로 여기서는 뺐다. 지금 이 파일이 하는 일은 하나다 —

    수정 전  ml/data/processed/design_features.parquet      (얼어 있는 v1)
    수정 후  ml/data/processed/design_features_v2.parquet   (F06 이 만든 v2)

두 파일을 읽어 **무엇이 몇 건 달라졌는지** 세고, 지원단위 보완의 근거를
사람이 대조할 수 있게 표본으로 남긴다.

원장(D1~D5)은 F06 이 고친 다섯 가지와 같은 이름을 쓴다.

    D1 근거문 복원        목록 표본은 제목이, Open API 는 CSV 요약이 근거문 자리에
                        들어가 있었다. 금액을 뽑은 그 문서로 바꾼다
    D2 지원단위 보완      복원된 근거문에서 근거등급 3단으로 재도출
    D3 금액/비율 파싱오류  지원비율/자부담률 [0,100] 밖, 연도 오파싱 사업기간
    D4 설계축 결측 보완    목록 표본의 사업기간·자부담률 (원문에 없어서가 아니라
                        F06 이 NaN 을 넣었기 때문에 비어 있었다)
    D5 지원방식 정규화     근거문이 제목이라 증거를 못 보고 grant 로 떨어지던 것

산출
    ml/reports/m62_data_quality.json / .md
    ml/reports/m62_unit_audit.csv     지원단위 보완 표본 (근거창 포함)
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import hashlib
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6

V1 = F6.OUT
V2 = F6.OUT_V2
AUDIT_CSV = os.path.join(C.REPORTS, "m62_unit_audit.csv")
MD = os.path.join(C.REPORTS, "m62_data_quality.md")


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _changed(a, b, col):
    """결측끼리는 같은 것으로 본다 — NaN != NaN 이라 그냥 비교하면 부풀려진다."""
    x, y = a[col], b[col]
    return ~((x.isna() & y.isna()) | (x == y))


def ledger(a, b):
    """v1 -> v2 원장. 두 프레임은 같은 순서·같은 row_id 여야 한다."""
    if not (a["row_id"].to_numpy() == b["row_id"].to_numpy()).all():
        raise RuntimeError("행 순서가 다르다 — F06 을 두 모드로 다시 돌려야 한다")

    ev_a = a["evidence_text"].fillna("").astype(str).str.strip()
    ti = a["title"].fillna("").astype(str).str.strip()
    title_only = (ev_a == "") | (ev_a == ti)
    src = b["evidence_source"]
    ev_changed = _changed(a, b, "evidence_text")

    out = {}
    out["D1_근거문_복원"] = {
        "제목만_있던_행": int(title_only.sum()),
        "근거문_교체": int(ev_changed.sum()),
        "출처_후": {k: int(v) for k, v in src.value_counts().items()},
        "교체_후_중앙값_길이": int(b.loc[ev_changed, "evidence_text"].str.len().median())
        if ev_changed.any() else 0,
        "복원_불가(제목만_남음)": int((src == "title_only").sum()),
        "복원_불가(API_요약만)": int((src == "api_summary").sum()),
    }

    mu = _changed(a, b, "support_method")
    out["D5_지원방식_재도출"] = {
        "대상(근거문_교체)": int(ev_changed.sum()),
        "변경": int(mu.sum()),
        "이동": {"%s->%s" % (x, y): int(n) for (x, y), n in
               pd.Series(list(zip(a.loc[mu, "support_method"],
                                  b.loc[mu, "support_method"]))).value_counts().items()},
        "전": {str(k): int(v) for k, v in a["support_method"].value_counts().items()},
        "후": {str(k): int(v) for k, v in b["support_method"].value_counts().items()},
    }

    uu = _changed(a, b, "support_unit")
    out["D2_지원단위_보완"] = {
        "결측_전": int(a["support_unit"].isna().sum()),
        "결측_후": int(b["support_unit"].isna().sum()),
        "결측률_전": round(float(a["support_unit"].isna().mean()), 4),
        "결측률_후": round(float(b["support_unit"].isna().mean()), 4),
        "신규_충원": int((a["support_unit"].isna() & b["support_unit"].notna()).sum()),
        "결측으로_되돌림": int((a["support_unit"].notna()
                        & b["support_unit"].isna()).sum()),
        "값_변경": int((a["support_unit"].notna() & b["support_unit"].notna()
                    & uu).sum()),
        "근거등급": {str(k): int(v) for k, v in
                 b["support_unit_basis"].value_counts().items()},
        "전": {str(k): int(v) for k, v in
              a["support_unit"].value_counts(dropna=False).items()},
        "후": {str(k): int(v) for k, v in
              b["support_unit"].value_counts(dropna=False).items()},
    }

    out["D4_설계축_결측보완"] = {
        "사업기간_신규": int((a["project_duration"].isna()
                       & b["project_duration"].notna()).sum()),
        "자부담률_신규": int((a["self_burden_ratio"].isna()
                       & b["self_burden_ratio"].notna()).sum()),
        "사업기간_결측률_전": round(float(a["project_duration"].isna().mean()), 4),
        "사업기간_결측률_후": round(float(b["project_duration"].isna().mean()), 4),
        "자부담률_결측률_전": round(float(a["self_burden_ratio"].isna().mean()), 4),
        "자부담률_결측률_후": round(float(b["self_burden_ratio"].isna().mean()), 4),
    }

    out["D3_파싱오류_제거"] = {
        "지원비율_결측으로": int((a["support_ratio"].notna()
                          & b["support_ratio"].isna()).sum()),
        "자부담률_결측으로": int((a["self_burden_ratio"].notna()
                          & b["self_burden_ratio"].isna()).sum()),
        "사업기간_결측으로": int((a["project_duration"].notna()
                          & b["project_duration"].isna()).sum()),
        "지원비율_최소_전": (None if a["support_ratio"].isna().all()
                      else float(a["support_ratio"].min())),
        "지원비율_최소_후": (None if b["support_ratio"].isna().all()
                      else float(b["support_ratio"].min())),
        "사업기간_최대_전": (None if a["project_duration"].isna().all()
                      else float(a["project_duration"].max())),
        "사업기간_최대_후": (None if b["project_duration"].isna().all()
                      else float(b["project_duration"].max())),
    }

    # 근거문 수정과 무관하게 상류에서 이미 고쳐져 있던 칸. 섞어 세면 이번 수정의
    # 크기를 부풀리게 되므로 따로 뺀다.
    out["상류_드리프트(이번_수정과_무관)"] = {
        c: int(_changed(a, b, c).sum())
        for c in ("support_count", "extraction_confidence")}
    return out


def audit_sample(a, b, per_tier=25, seed=42):
    """지원단위가 새로 채워지거나 바뀐 행에서 **근거등급별로** 표본을 뽑는다.

    규칙으로 채운 값은 '맞는 값'이 아니라 '뽑힌 값'이다(M52 의 근거 등급과
    같은 규율). 등급별로 뽑는 이유는 등급마다 정확도가 다를 수 있기 때문이고,
    한 덩이로 뽑으면 그 차이가 평균에 묻힌다.
    """
    m = ((a["support_unit"].isna() & b["support_unit"].notna())
         | (a["support_unit"].notna() & b["support_unit"].notna()
            & a["support_unit"].ne(b["support_unit"])))
    idx = pd.Index(sorted(pd.concat([
        g.sample(min(per_tier, len(g)), random_state=seed).index.to_series()
        for _, g in b[m].groupby("support_unit_basis")])))
    out = pd.DataFrame({
        "row_id": b.loc[idx, "row_id"],
        "title": b.loc[idx, "title"].astype(str).str[:80],
        "unit_before": a.loc[idx, "support_unit"].fillna("(결측)"),
        "unit_after": b.loc[idx, "support_unit"],
        "basis": b.loc[idx, "support_unit_basis"],
        "amount_type": b.loc[idx, "amount_type"],
        "matched": b.loc[idx, "support_unit_hit"],
        "evidence_window": b.loc[idx, "support_unit_window"],
    })
    out.to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
    return len(out)


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


def write_md(r):
    g = r["ledger"]
    d1, d2, d3, d4, d5 = (g["D1_근거문_복원"], g["D2_지원단위_보완"],
                          g["D3_파싱오류_제거"], g["D4_설계축_결측보완"],
                          g["D5_지원방식_재도출"])
    p1, p2 = r["model3_pool"]["before"], r["model3_pool"]["after"]
    L = []
    A = L.append
    A("# M62 — 데이터 품질 감사 (수정 전후 원장)\n")
    A("> 지시서 공통 원칙: **먼저 데이터 품질을 고친 뒤 같은 모델을 다시 평가한다.**\n")
    A("**수정은 F06 이 원천에서 한다.** 이 문서의 처음 판은 F06 산출물 위에서")
    A("고치는 사후 보정 계층이었는데, M65 가 그 수정을 `f06_design_features.py`")
    A("로 승격했습니다. 여기서는 두 파일을 읽어 **무엇이 몇 건 달라졌는지**만")
    A("셉니다.\n")
    A("| | sha256 (앞 16) | 행 | 만든 것 |")
    A("|---|---|---:|---|")
    A("| 수정 전 `design_features.parquet` | `%s` | %d | `f06 --legacy` |"
      % (r["sha256_before"][:16], r["rows"]))
    A("| 수정 후 `design_features_v2.parquet` | `%s` | %d | `f06` (기본) |\n"
      % (r["sha256_after"][:16], r["rows"]))

    A("## 0. 무엇이 잘못돼 있었는가\n")
    A("가장 큰 것 하나가 나머지를 전부 끌고 있었습니다.\n")
    A("**F06 `_pack_bizinfo` 가 근거문 자리에 금액을 뽑지 않은 텍스트를 넣어**")
    A("**저장했습니다** — 목록 표본에는 제목을, Open API 행에는 CSV 요약을.")
    A("금액·지원비율·지원기업수는 F05 가 공고문 원문에서 뽑았는데 **그 원문이**")
    A("**하류로 넘어오지 않았습니다.** 그래서 F06 이 근거문에서 도출하는 두")
    A("파생값이 엉뚱한 텍스트를 보고 만들어졌습니다 — 그 둘이 하필 **모델 2·3**")
    A("**비교군 사다리의 축**인 `support_method` 와 `support_unit` 입니다.\n")
    A("> 성능결과서 6장 2순위가 \"파서가 아니라 저장 단계 문제\"라고 적어 둔 바로 그")
    A("> 지점입니다. 파서를 고칠 일이 아니라 **원문을 다시 붙이면 되는 일**이었습니다.\n")

    A("## 1. 고친 것 다섯\n")
    A("| | 무엇 | 규모 |")
    A("|---|---|---|")
    A("| **D1** | 근거문을 금액이 뽑힌 문서로 교체 (F05 와 같은 문서 선택 규칙) | %d행 · 중앙값 %d자 |"
      % (d1["근거문_교체"], d1["교체_후_중앙값_길이"]))
    A("| **D2** | 복원된 근거문에서 `support_unit` 재도출 | 결측 %.1f%% → **%.1f%%** · 신규 %d · 정정 %d |"
      % (100 * d2["결측률_전"], 100 * d2["결측률_후"], d2["신규_충원"], d2["값_변경"]))
    A("| **D3** | 금액/비율 파싱 오류 제거 | 지원비율 %d · 자부담률 %d · 사업기간 %d |"
      % (d3["지원비율_결측으로"], d3["자부담률_결측으로"], d3["사업기간_결측으로"]))
    A("| **D4** | `project_duration`·`self_burden_ratio` 복원 | 사업기간 +%d (결측 %.1f%% → **%.1f%%**) · 자부담률 +%d |"
      % (d4["사업기간_신규"], 100 * d4["사업기간_결측률_전"],
         100 * d4["사업기간_결측률_후"], d4["자부담률_신규"]))
    A("| **D5** | 근거문에서 `support_method` 재도출 | **%d행 변경** |" % d5["변경"])
    A("")
    A("근거문 출처는 이렇게 갈립니다.\n")
    A("| 출처 | 행 | 뜻 |")
    A("|---|---:|---|")
    lbl = {"document": "공고문 원문 (금액을 뽑은 그 문서)",
           "scale_text": "taxonomy 의 지원규모 원문 (원래 결함 없음)",
           "api_summary": "문서가 없어 CSV 요약으로 후퇴",
           "title_only": "문서도 요약도 없어 제목만 남음"}
    for k, v in d1["출처_후"].items():
        A("| `%s` | %d | %s |" % (k, v, lbl.get(k, "")))
    A("")

    A("### D5 가 가장 큽니다 — 비교군 축이 틀려 있었습니다\n")
    A("근거문이 제목이면 `derive_method` 가 텍스트 증거를 하나도 못 보고,")
    A("**금액이 있다는 이유만으로 `grant` 로 떨어집니다.** 원문을 붙이자")
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
    A("점수가 아니라 **모집단**이 바뀌는 오류였습니다. 이 재도출이 실제로 나아진")
    A("것인지는 표본을 눈으로 고르지 않고 **라벨로** 쟀습니다 — `m65_m2_canonical_v2.md`")
    A("1.2절. 표본이 가장 두꺼운 축(`융자` n=388)에서 `loan` 판정의 F1 이")
    A("**0.735 → 0.808** 로 오릅니다.\n")

    A("### D2 — 지원단위는 '뽑힌 값'이라 근거를 남깁니다\n")
    A("근거 강도 순으로 3단입니다. 긴 문서 전체를 훑지 않습니다 — 2,700자짜리")
    A("공고문에는 '참여기업'이 어디든 한 번은 나오므로, 그러면 규칙이 아니라")
    A("**문서 길이가 단위를 정하게** 됩니다.\n")
    A("| 근거등급 | 무엇을 봤는가 | 행수 |")
    A("|---|---|---:|")
    tier = {"amount_context": "파서가 고른 금액 표현의 문맥 (금액과 같은 문장)",
            "scope_section": "'지원규모/지원내용/지원한도/…' 표제 뒤 160자",
            "amount_type_convention": "`amount_type` → 단위 (기존 F06 fallback)",
            "none": "근거 없음 — 결측으로 남깁니다",
            "unchanged": "근거문이 그대로라 손대지 않음 (taxonomy)"}
    for k, v in d2["근거등급"].items():
        A("| `%s` | %s | %d |" % (k, tier.get(k, ""), v))
    A("")
    A("우선순위도 하나 고쳤습니다. 기존 `UNIT_RULES` 는 `company` 를 맨 앞에 두어")
    A("`개사` 가 `과제당` 을 이깁니다 — 실측: `지원규모 : 과제당 최대 90,000천원 /")
    A("2개사` 가 `company` 로 떨어집니다. 금액의 단위를 정하는 것은 '과제당'이므로")
    A("**'~당' 표현을 개수 표현보다 먼저** 봅니다. 어휘는 기존 것 그대로입니다.\n")
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
    A("사업기간 최대값도 **%s년 → %s년** 으로 내려갑니다. 상식범위(0,10] 밖이면서"
      % (d3["사업기간_최대_전"], d3["사업기간_최대_후"]))
    A("근거등급이 `bare` 인 것만 지웠습니다 — 근거등급이 `context`/`hint_only` 면")
    A("10년 융자처럼 실제로 긴 사업이 있어 남깁니다. `bare` 쪽의 정체는 M31/M32 가")
    A("고친 '연도 두 자리를 기간으로 읽는' 오류의 잔재입니다(2026년 공고에서 기간 26년).\n")

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
    for a_ in ["log_per_recipient", "log_support_count", "support_ratio",
               "project_duration"]:
        A("| `%s` 결측률 | %.1f%% | %.1f%% |"
          % (a_, 100 * p1["축_결측률"][a_], 100 * p2["축_결측률"][a_]))
    A("")
    A("D4 가 목록 표본의 사업기간을 복원하면서 수치축 2개 조건을 넘긴 행이 생겨")
    A("pool 이 %d → %d 로 늘었습니다. **결측률만 보면 `지원단위`가 나빠진 것처럼**"
      % (p1["pool_rows"], p2["pool_rows"]))
    A("**보이는데, 분모가 달라진 것**입니다. 같은 행에서 재면 M64 가 보여줍니다.\n")

    drift = r["ledger"]["상류_드리프트(이번_수정과_무관)"]
    A("## 3. 이번 수정과 무관하게 달라진 칸\n")
    if not any(drift.values()):
        A("**없습니다.** 근거문 수정이 닿지 않는 컬럼은 v1 과 완전히 같습니다.\n")
        A("| 컬럼 | 달라진 행 |")
        A("|---|---:|")
        for k, v in drift.items():
            A("| `%s` | %d |" % (k, v))
        A("")
        A("이 줄이 0 이라는 것이 중요합니다. 여기 값이 있으면 **근거문 수정의**")
        A("**크기와 상류 갱신분이 섞여** 이번 작업이 한 일을 가릴 수 있습니다.")
        A("실제로 한 번 그렇게 됐다가 잡았습니다 — 자세한 것은")
        A("`m65_m2_canonical_v2.md` 1.1절입니다.\n")
    else:
        A("아래 컬럼은 근거문 수정과 무관한데도 달라졌습니다. 상류 산출물이")
        A("v1 시점과 어긋나 있다는 뜻이므로, 섞어 세지 않고 따로 뺍니다.\n")
        A("| 컬럼 | 달라진 행 |")
        A("|---|---:|")
        for k, v in drift.items():
            A("| `%s` | %d |" % (k, v))
        A("")
    A("## 4. 바꾸지 않은 것\n")
    A("```text")
    for u in r["unchanged"]:
        A("%s" % u)
    A("```\n")
    A("데이터를 고쳤다고 말하려면 모델·프로토콜은 그대로여야 합니다. 재평가는")
    A("M63(모델 2) · M64(모델 3) 가, 승격 점검은 M65 가 합니다.")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[report] %s" % MD)


def main():
    for p in (V1, V2):
        if not os.path.exists(p):
            raise FileNotFoundError(
                "%s 가 없다. `python f06_design_features.py%s` 를 먼저 돌린다."
                % (p, "" if p == V2 else " --legacy"))
    a = pd.read_parquet(V1).reset_index(drop=True)
    b = pd.read_parquet(V2).reset_index(drop=True)

    led = ledger(a, b)
    n_audit = audit_sample(a, b)
    rep = {
        "before": os.path.relpath(V1, C.ROOT),
        "after": os.path.relpath(V2, C.ROOT),
        "producer": "ml/scripts/f06_design_features.py (M65 에서 근본 수정)",
        "rows": int(len(a)),
        "sha256_before": file_sha256(V1),
        "sha256_after": file_sha256(V2),
        "ledger": led,
        "model2_filter_trace": {"before": _m2_trace(a), "after": _m2_trace(b)},
        "model3_pool": {"before": _m3_pool_stats(a), "after": _m3_pool_stats(b)},
        "audit_sample_rows": n_audit,
        "audit_csv": os.path.relpath(AUDIT_CSV, C.ROOT),
        "unchanged": ["타깃 정의(stated_cap)", "비교군 사다리", "모델·하이퍼파라미터",
                      "평가 프로토콜", "design_features.parquet 원본(얼어 있음)"],
    }
    C.save_report("m62_data_quality.json", rep)
    write_md(rep)

    for k in ("D1_근거문_복원", "D2_지원단위_보완", "D3_파싱오류_제거",
              "D4_설계축_결측보완"):
        print("== %s" % k)
        print("  ", {kk: vv for kk, vv in led[k].items() if kk not in ("전", "후")})
    print("== D5_지원방식_재도출")
    print("  ", led["D5_지원방식_재도출"]["이동"])
    print("\n모델2 필터: 전 %s\n           후 %s"
          % (rep["model2_filter_trace"]["before"], rep["model2_filter_trace"]["after"]))
    print("모델3 pool: 전 %s\n           후 %s"
          % (rep["model3_pool"]["before"], rep["model3_pool"]["after"]))


if __name__ == "__main__":
    main()
