r"""M32 — M31 이 감사한 4개 파서 버그를 승격하고 전체 feature 를 재생성한다.

계획서(model3_vector_direction_and_training_diagnostics) Step 1 "parser 수정",
직전 계획서(model3_improvement_plan_clean_holdout) Task 2·3 에 해당한다.

무엇이 바뀌었나 — 바뀐 것은 `amount_parser.py` 의 패턴 두 개뿐이다.

    COUNT_RE    천단위 콤마를 읽고, 맨 `개` 뒤에 한글이 오면 단위어로 보고 버린다
    PERIOD_RE   왼쪽 경계를 넣어 연도의 뒤 두 자리에서 시작하지 못하게 하고,
                '기간' 문맥 > '이내/간' 힌트 > 맨 숫자 순으로 근거 등급을 매긴다

선택 규칙(첫 매치가 이긴다)은 건드리지 않았다. 후보가 여럿일 때 어느 것이
'그' 값인지는 파서 버그가 아니라 정보검색 문제이고, hold-out 50건에 맞춰
손보면 그 50건을 튜닝셋으로 쓰는 것과 같다.

이 스크립트가 하는 일 — **재생성 결과를 검산만 한다.**
    파이프라인 자체는 F02 -> M02 -> F03 -> F05 -> F06 를 그대로 다시 돌렸다.
    여기서는 교체 전 스냅샷(_pre_m32/)과 교체 후를 나란히 놓고,
    (1) 축별로 몇 건이 살아남고 몇 건이 바뀌었는가
    (2) 이상탐지가 쓰는 수치축의 분포가 어떻게 달라졌는가
    (3) hold-out 50건에서 M31 이 지목한 오류가 실제로 사라졌는가
    를 낸다.

가장 중요한 한 줄은 (2)에 있다. 교체 전 `project_duration` 의 중앙값은
22.0 이었다 — 사업기간이 아니라 공고연도(20xx 의 뒤 두 자리)를 재고 있었다.
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

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

PRE = os.path.join(C.PROC, "_pre_m32", "design_features.parquet")
POST = os.path.join(C.PROC, "design_features.parquet")
HOLDOUT = os.path.join(C.DATA, "labels", "m3_anomaly_holdout_50.csv")
AUDIT = os.path.join(C.REPORTS, "m31_parser_audit.csv")

AXES = ["project_duration", "support_count", "per_recipient", "support_ratio",
        "amount_max"]
LABEL_COL = "판단(정상/비전형)"


def load(path):
    return pd.read_parquet(path).drop_duplicates("row_id").set_index("row_id")


def changed_mask(o, n):
    o = pd.to_numeric(o, errors="coerce")
    n = pd.to_numeric(n, errors="coerce")
    return ~((o.isna() & n.isna()) | np.isclose(o, n, equal_nan=True))


def axis_table(pre, post, ix):
    rows = []
    for a in AXES:
        o, n = pre.loc[ix, a], post.loc[ix, a]
        ch = changed_mask(o, n)
        on, nn = pd.to_numeric(o, errors="coerce"), pd.to_numeric(n, errors="coerce")
        rows.append({
            "axis": a,
            "보유_전": int(on.notna().sum()), "보유_후": int(nn.notna().sum()),
            "변동": int(ch.sum()),
            "중앙값_전": None if on.notna().sum() == 0 else round(float(on.median()), 3),
            "중앙값_후": None if nn.notna().sum() == 0 else round(float(nn.median()), 3),
        })
    return rows


def sanity(pre, post):
    lo, hi = C.SANE_RANGE["per_company"]
    out = {}
    for tag, d in (("전", pre), ("후", post)):
        pr = pd.to_numeric(d["per_recipient"], errors="coerce")
        du = pd.to_numeric(d["project_duration"], errors="coerce")
        cnt = pd.to_numeric(d["support_count"], errors="coerce")
        out[tag] = {
            "기업당액 상식범위 밖": int((pr.notna() & ~pr.between(lo, hi)).sum()),
            "사업기간 10년 초과": int((du > 10).sum()),
            "사업기간 중앙값": None if du.notna().sum() == 0 else round(float(du.median()), 2),
            "지원기업수 중앙값": None if cnt.notna().sum() == 0 else round(float(cnt.median()), 1),
        }
    return out


def holdout_view(pre, post):
    """M31 이 오류를 지목한 행에서 실제로 값이 고쳐졌는지 한 줄씩 확인한다."""
    hold = pd.read_csv(HOLDOUT, encoding="utf-8-sig")
    audit = (pd.read_csv(AUDIT, encoding="utf-8-sig").set_index("row_id")
             if os.path.exists(AUDIT) else None)
    rows = []
    for _, h in hold.iterrows():
        rid = h["row_id"]
        if rid not in pre.index or rid not in post.index:
            continue
        errs = ""
        if audit is not None and rid in audit.index:
            errs = str(audit.loc[rid, "error_types"] or "")
            errs = "" if errs == "nan" else errs
        rows.append({
            "row_id": rid, "사업명": str(h["사업명"])[:44],
            "사람라벨": h[LABEL_COL], "하위유형": h["하위유형"],
            "M31_오류": errs,
            "기간_전": pre.loc[rid, "project_duration"],
            "기간_후": post.loc[rid, "project_duration"],
            "기간_근거": post.loc[rid, "duration_basis"],
            "기업수_전": pre.loc[rid, "support_count"],
            "기업수_후": post.loc[rid, "support_count"],
            "기업당액_전": pre.loc[rid, "per_recipient"],
            "기업당액_후": post.loc[rid, "per_recipient"],
        })
    return pd.DataFrame(rows)


def won(v):
    if v is None or pd.isna(v):
        return "—"
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def main():
    if not os.path.exists(PRE):
        sys.exit("교체 전 스냅샷이 없습니다: %s" % PRE)
    pre, post = load(PRE), load(POST)
    ix = pre.index.intersection(post.index)

    axes = axis_table(pre, post, ix)
    san = sanity(pre, post)
    hv = holdout_view(pre, post)

    bas = post["duration_basis"].value_counts(dropna=False)
    bas = {("(없음)" if pd.isna(k) else str(k)): int(v) for k, v in bas.items()}

    print("M32 — 파서 교체 후 재생성 검산")
    print("  행 수 전 %d / 후 %d / 공통 %d" % (len(pre), len(post), len(ix)))
    print("\n== 축별 보유·변동")
    print("  %-20s %8s %8s %8s %12s %12s"
          % ("축", "보유_전", "보유_후", "변동", "중앙값_전", "중앙값_후"))
    for r in axes:
        print("  %-20s %8d %8d %8d %12s %12s"
              % (r["axis"], r["보유_전"], r["보유_후"], r["변동"],
                 r["중앙값_전"], r["중앙값_후"]))

    print("\n== 상식 검산")
    for k in san["전"]:
        print("  %-22s %10s -> %10s" % (k, san["전"][k], san["후"][k]))

    print("\n== 사업기간 근거 등급 (교체 후)")
    print("  %s" % bas)

    fixed = hv[hv["M31_오류"] != ""]
    print("\n== hold-out 50건 중 M31 이 오류로 지목한 %d건" % len(fixed))
    for _, r in fixed.iterrows():
        print("  %-44s 기간 %6s->%-6s(%-20s) 기업수 %6s->%-6s 기업당액 %9s->%s"
              % (r["사업명"], r["기간_전"], r["기간_후"], r["기간_근거"],
                 r["기업수_전"], r["기업수_후"], won(r["기업당액_전"]), won(r["기업당액_후"])))

    hv.to_csv(os.path.join(C.REPORTS, "m32_holdout_refeature.csv"),
              index=False, encoding="utf-8-sig")

    rep = {
        "scope": "amount_parser.py 패턴 교체 후 F02->M02->F03->F05->F06 전체 재생성",
        "n_rows": {"pre": int(len(pre)), "post": int(len(post)), "common": int(len(ix))},
        "axes": axes,
        "sanity": san,
        "duration_basis_dist": bas,
        "holdout_rows_flagged_by_m31": int(len(fixed)),
        "holdout_csv": os.path.join(C.REPORTS, "m32_holdout_refeature.csv"),
        "unchanged_on_purpose": "후보 선택 규칙(첫 매치). 정보검색 문제라 파서에서 풀지 않는다.",
    }
    C.save_report("m32_m3_parser_fix.json", rep)
    write_md(rep, hv, fixed)


def write_md(rep, hv, fixed):
    a = {r["axis"]: r for r in rep["axes"]}
    L = ["# M32 — 파서 교체와 feature 전체 재생성", "",
         "> M31 은 감사만 했습니다. 여기서 검증된 4개 규칙을 `amount_parser.py` 로",
         "> 승격시키고 `F02 -> M02 -> F03 -> F05 -> F06` 를 전부 다시 돌렸습니다.", "",
         "## 1. 무엇을 바꿨나", "",
         "| 패턴 | 바뀐 내용 |", "|---|---|",
         "| `COUNT_RE` | 천단위 콤마를 읽는다. 맨 `개` 뒤에 한글이 오면 단위어로 보고 버린다 |",
         "| `PERIOD_RE` | 왼쪽 경계를 넣어 연도의 뒤 두 자리에서 시작하지 못하게 한다 |",
         "| `parse_duration` | 근거 등급을 매긴다 — `context` > `hint_only` > `bare` |",
         "| `parse_count` | 5,000 초과를 조용히 버리지 않고 `support_count_over_cap` 으로 표시한다 |",
         "",
         "**바꾸지 않은 것 — 선택 규칙.** 후보가 여럿일 때 첫 매치가 이긴다는 규칙은",
         "그대로입니다. 어느 후보가 '그' 값인지는 파서 버그가 아니라 정보검색 문제이고,",
         "hold-out 50건에 맞춰 손보면 그 50건을 튜닝셋으로 쓰는 것과 같습니다.", "",
         "## 2. 축별 보유·변동 (전체 %d행)" % rep["n_rows"]["common"], "",
         "| 축 | 보유 전 | 보유 후 | 값이 바뀐 행 | 중앙값 전 | 중앙값 후 |",
         "|---|---:|---:|---:|---:|---:|"]
    for r in rep["axes"]:
        L.append("| `%s` | %d | %d | %d | %s | %s |"
                 % (r["axis"], r["보유_전"], r["보유_후"], r["변동"],
                    r["중앙값_전"], r["중앙값_후"]))
    L += ["",
          "**이 표에서 읽어야 할 한 줄은 `project_duration` 의 중앙값입니다 —**",
          "**%s 에서 %s 로 바뀌었습니다.** 교체 전 이 축은 사업기간이 아니라"
          % (a["project_duration"]["중앙값_전"], a["project_duration"]["중앙값_후"]),
          "공고연도(20xx 의 뒤 두 자리)를 재고 있었습니다. M31 이 954행의 절반이",
          "연도 오파싱이라고 적은 그대로입니다.", "",
          "`support_count` 는 %d건에서 %d건으로 줄었습니다. 사라진 것은 값이 아니라"
          % (a["support_count"]["보유_전"], a["support_count"]["보유_후"]),
          "`12개소`·`6개월`·`1개의 파일` 같은 단위어 오탐입니다. M31 이 hold-out 에서",
          "전수 확인했을 때 구 파서가 잡은 값 중 진짜 지원기업수는 한 건도 없었습니다.", "",
          "## 3. 상식 검산", "",
          "| 항목 | 교체 전 | 교체 후 |", "|---|---:|---:|"]
    for k in rep["sanity"]["전"]:
        L.append("| %s | %s | %s |" % (k, rep["sanity"]["전"][k], rep["sanity"]["후"][k]))
    L += ["",
          "## 4. 사업기간 근거 등급 (교체 후)", "",
          "값을 지우는 대신 등급을 남깁니다. 하류가 등급을 보고 쓸지 말지 정합니다.", "",
          "| 등급 | 건수 | 의미 |", "|---|---:|---|"]
    meaning = {
        "context": "앞에 '사업기간/지원기간/…' 문맥이 있다 (높음)",
        "hint_only": "'이내/간/동안' 힌트만 있다 (보통)",
        "bare": "맨 숫자+년/개월 (낮음)",
        "no_duration_evidence": "기간 근거가 없어 값을 만들지 않았다",
        "no_text": "원문 자체가 없다",
        "(없음)": "해당 코호트에서 기간을 뽑지 않는다 (목록 표본)",
    }
    for k, v in sorted(rep["duration_basis_dist"].items(), key=lambda kv: -kv[1]):
        L.append("| `%s` | %d | %s |" % (k, v, meaning.get(k, "")))
    L += ["", "## 5. hold-out 50건 — M31 이 지목한 %d건이 어떻게 바뀌었나"
          % rep["holdout_rows_flagged_by_m31"], "",
          "| 사업 | 사람라벨 | M31 오류 | 기간 | 기업수 | 기업당액 |",
          "|---|---|---|---|---|---|"]
    for _, r in fixed.iterrows():
        L.append("| %s | %s | `%s` | %s → %s (%s) | %s → %s | %s → %s |"
                 % (r["사업명"], r["사람라벨"], r["M31_오류"],
                    r["기간_전"], r["기간_후"], r["기간_근거"],
                    r["기업수_전"], r["기업수_후"],
                    won(r["기업당액_전"]), won(r["기업당액_후"])))
    L += ["",
          "## 6. 여기서 멈추고 다음으로 넘기는 것", "",
          "파서를 고쳤으니 **기존 라벨을 그대로 쓸 수 없습니다.** 라벨러가",
          "'비전형'이라고 본 15건 중 8건은 고장난 값을 보고 내린 판정이었습니다.",
          "M33 에서 교정된 값으로 다시 라벨링하고 `normal / atypical_design /`",
          "`data_error / uncertain` 네 칸으로 나눕니다.", ""]
    p = os.path.join(C.REPORTS, "m32_m3_parser_fix.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
