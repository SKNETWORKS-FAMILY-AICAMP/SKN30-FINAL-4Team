"""M46 — 전년 대비 지원규모 조회 위젯. 모델이 아니다.

M45 에서 모델 2 는 2-B(비교군 내 기업당 지원규모)로 확정됐다. 이 스크립트는
거기서 빠진 '전년 대비 증감'을 모델이 아닌 조회로 되살린다. 차이는 이렇다.

    (기각) 2-A 추이 분류   비교군의 연도별 대표값을 견줘 상승/하락을 '판정'한다
    (채택) 전년 조회       같은 사업의 전년 회차 원문 금액 두 개를 '찾아서 보여준다'

왜 하나는 되고 하나는 안 되는가. 2-A 는 비교군 중앙값이라는 추정량을 두 번
추정해 그 차이의 부호를 라벨로 삼는다. 이 데이터에서 그 부호가 안 선다 —
양쪽 n>=10 인 (비교군x연도) 쌍이 103개 중 15개뿐이고, 그 15쌍의 부트스트랩
부호일치 중앙값이 0.635 다. 같은 데이터를 리샘플하면 라벨이 36% 확률로 뒤집힌다.
Mann-Whitney p<0.05 는 0/15. 전년 방향으로 올해 방향을 맞히는 persistence
정확도는 0.500(n=6) 이다.

조회는 추정을 하지 않는다. 원문에 적힌 한도 두 개를 그대로 나란히 놓는다.
틀릴 수 있는 지점은 '두 공고가 같은 사업인가'와 '두 금액이 같은 종류인가'
둘뿐이고, 둘 다 판정이 아니라 규칙으로 막을 수 있다. 그래서 이 산출물에는
정확도가 없다 — 정확도가 정의되는 예측을 하지 않기 때문이다. 대신 커버리지
(몇 건이나 답이 나오는가)와 거절 사유별 건수를 낸다.

고치는 것 하나. bizinfo 의 program_stem 은 title 원본이 그대로 들어가 있다
(f06_design_features.py:262). taxonomy 만 f03.program_stem() 으로 연도 접두사를
벗겼다. 그대로 매칭하면 '2021년 D.N.A. ...' 와 '2022년 D.N.A. ...' 가 다른
사업이 된다. 실제로 정규화 전 bizinfo 에서 2개 연도 이상 등장하는 사업은
0개였고, 정규화하면 6개가 된다. 조회 키는 여기서 다시 만든다.

이 위젯이 하지 않는 말: 오를 것이다 / 내릴 것이다 / 적정하다.
하는 말: 전년 회차의 금액, 올해 금액, 변화율, 또는 '전년 회차 없음'.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from f03_taxonomy import program_stem
from m45_m2_amount import prepare, won

SRC = os.path.join(C.PROC, "design_features.parquet")
OUT_PAIRS = os.path.join(C.PROC, "m46_yoy_pairs.parquet")
OUT_INDEX = os.path.join(C.PROC, "m46_yoy_index.parquet")

# 같은 금액으로 볼 허용오차. 0 이면 완전 일치만 '동결'.
# 원문 한도는 이미 반올림된 값이라 근사 비교가 오히려 임의적이다. 0 을 쓰고,
# ±5% 밴드를 썼다면 몇 건이 옮겨가는지는 리포트에 함께 적는다.
FREEZE_TOL = 0.0
FREEZE_TOL_ALT = 0.05
MAX_GAP = 4          # 이보다 오래된 회차는 '직전 회차'라고 부르지 않는다

STATES = ("상승", "하락", "동결")


# ------------------------------------------------------------ 색인
def build_index(d):
    """조회 색인. (정규화 stem, 출처, 연도) -> 금액 한 개.

    같은 해에 같은 사업 공고가 둘 이상이고 금액이 갈리면 대표값을 고르지
    않는다. 평균이나 중앙값을 쓰면 '원문에 적힌 한도'라는 성질이 사라진다.
    갈리는 셀은 값 대신 플래그를 들고 있다가 조회 때 거절한다.
    """
    t = d.copy()
    t["stem_key"] = [program_stem(s) for s in t["program_stem"].astype(str)]
    rows = []
    for (stem, cohort, year), g in t.groupby(["stem_key", "cohort", "year"]):
        v = g["per_recipient"].astype(float).unique()
        units = g["support_unit"].dropna().unique()
        rows.append({
            "stem_key": stem, "cohort": cohort, "year": int(year),
            "amount": float(v[0]) if len(v) == 1 else np.nan,
            "support_unit": units[0] if len(units) == 1 else None,
            "n_rows": int(len(g)),
            "ambiguous": bool(len(v) > 1),
            "title": g["title"].iloc[0],
            "support_type": g["support_type"].iloc[0],
        })
    return pd.DataFrame(rows)


def _state(ratio, tol=FREEZE_TOL):
    """부호만 본다. 배수 자체는 호출부가 그대로 표시한다."""
    if abs(ratio - 1.0) <= tol:
        return "동결"
    return "상승" if ratio > 1.0 else "하락"


# ------------------------------------------------------------ 조회 (서비스 진입점)
def lookup_yoy(index, stem, year, cohort, unit=None):
    """한 건 조회. 답이 없으면 사유를 들고 돌아온다 — 추정하지 않는다.

    출처가 다르면 찾지 않는다. taxonomy 와 bizinfo 는 같은 사업이라도 금액
    기재 관행이 최대 40배 갈린다(M45 §2). 같은 사업의 회차 비교를 두 모집단에
    걸쳐 하면 사업이 바뀐 게 아니라 장부가 바뀐 것을 증감으로 읽게 된다.
    """
    key = program_stem(str(stem))
    hist = index[(index["stem_key"] == key) & (index["cohort"] == cohort)
                 & (index["year"] < int(year))]
    if not len(hist):
        return {"status": "해당없음", "reason": "전년_회차_없음"}

    p = hist.sort_values("year").iloc[-1]
    gap = int(year) - int(p["year"])
    if gap > MAX_GAP:
        return {"status": "해당없음", "reason": "직전_회차_너무_오래됨",
                "note": "직전 회차가 %d년으로 %d년 전이다" % (int(p["year"]), gap)}
    if bool(p["ambiguous"]) or pd.isna(p["amount"]):
        return {"status": "해당없음", "reason": "같은해_금액_불일치",
                "note": "%d년에 금액이 다른 회차가 %d건" % (int(p["year"]), int(p["n_rows"]))}
    if unit and p["support_unit"] and unit != p["support_unit"]:
        return {"status": "해당없음", "reason": "지원단위_불일치",
                "note": "%d년 %s / 올해 %s" % (int(p["year"]), p["support_unit"], unit)}

    return {"status": "조회됨", "prev_year": int(p["year"]), "prev_title": p["title"],
            "prev_amount": float(p["amount"]), "year_gap": gap,
            "label": "전년 대비" if gap == 1 else "직전 회차(%d년) 대비" % int(p["year"])}


def compare_yoy(index, stem, year, cohort, amount, unit=None):
    """조회 + 올해 금액 대비. 상태 4종만 낸다: 상승 / 하락 / 동결 / 해당없음."""
    r = lookup_yoy(index, stem, year, cohort, unit)
    if r["status"] != "조회됨":
        return r
    if amount is None or (isinstance(amount, float) and np.isnan(amount)) or amount <= 0:
        return {"status": "해당없음", "reason": "올해_금액_미기재"}
    prev, cur = float(r["prev_amount"]), float(amount)
    ratio = cur / prev
    st = _state(ratio)
    r.update({"status": st, "cur_amount": cur, "ratio": ratio,
              "change_pct": (ratio - 1.0) * 100.0,
              "statement": "%s %s → 올해 %s (%s)"
                           % (r["label"], won(prev), won(cur),
                              "동일" if st == "동결" else "%+.1f%%" % ((ratio - 1) * 100))})
    return r


# ------------------------------------------------------------ 커버리지
def all_pairs(index, d):
    """색인 전체를 훑어 조회 결과를 만든다. 커버리지 산출용."""
    t = d.copy()
    t["stem_key"] = [program_stem(s) for s in t["program_stem"].astype(str)]
    out = []
    for r in t.itertuples():
        res = compare_yoy(index, r.stem_key, int(r.year), r.cohort,
                          float(r.per_recipient), r.support_unit)
        out.append({"row_id": r.row_id, "title": r.title, "year": int(r.year),
                    "cohort": r.cohort, "support_type": r.support_type,
                    "status": res["status"], "reason": res.get("reason"),
                    "prev_year": res.get("prev_year"),
                    "prev_amount": res.get("prev_amount"),
                    "cur_amount": res.get("cur_amount", float(r.per_recipient)),
                    "ratio": res.get("ratio"), "year_gap": res.get("year_gap")})
    return pd.DataFrame(out)


def stem_fix_effect(d):
    """stem 정규화가 조회 가능성을 얼마나 바꾸는지. 고친 값을 증명한다."""
    out = {}
    for tag, keyfn in (("정규화_전", lambda s: str(s)),
                       ("정규화_후", lambda s: program_stem(str(s)))):
        t = d.copy()
        t["k"] = [keyfn(s) for s in t["program_stem"]]
        g = t.groupby(["cohort", "k"])["year"].nunique()
        out[tag] = {c: int((g.loc[c] > 1).sum()) for c in sorted(t["cohort"].unique())}
    return out


def main():
    d0 = pd.read_parquet(SRC)
    d, drop = prepare(d0)
    print("== 대상 — M45 와 같은 정제셋 (stated_cap 만, 파싱오류 제외)")
    print("   n = %d" % len(d))

    print("\n== stem 정규화 효과 (2개 연도 이상 등장하는 사업 수)")
    eff = stem_fix_effect(d)
    for tag, v in eff.items():
        print("   %-8s %s" % (tag, "  ".join("%s %d개" % (k, n) for k, n in v.items())))

    index = build_index(d)
    index.to_parquet(OUT_INDEX, index=False)
    print("\n== 조회 색인 — %s  %d행" % (OUT_INDEX, len(index)))
    print("   같은해 금액 불일치 셀: %d개" % int(index["ambiguous"].sum()))

    pairs = all_pairs(index, d)
    pairs.to_parquet(OUT_PAIRS, index=False)
    hit = pairs[pairs["status"].isin(STATES)]
    print("\n== 커버리지 — 정제셋 %d건 중 조회되는 건수" % len(pairs))
    print("   조회됨 %d건 (%.1f%%)" % (len(hit), 100 * len(hit) / len(pairs)))
    for r, n in pairs["reason"].value_counts().items():
        print("     거절 %-20s %d건" % (r, n))

    print("\n   출처별 조회 가능 건수")
    for c, g in pairs.groupby("cohort"):
        h = int(g["status"].isin(STATES).sum())
        print("     %-9s %4d건 중 %3d건 (%.1f%%)" % (c, len(g), h, 100 * h / len(g)))

    vc = hit["status"].value_counts()
    alt = [_state(r, FREEZE_TOL_ALT) for r in hit["ratio"]]
    print("\n== 조회된 건의 상태 분포")
    for s in STATES:
        print("     %-4s %3d건 (%.1f%%)" % (s, int(vc.get(s, 0)),
                                            100 * int(vc.get(s, 0)) / max(len(hit), 1)))
    print("     (허용오차 ±%.0f%% 를 동결로 보면: %s)"
          % (FREEZE_TOL_ALT * 100,
             " / ".join("%s %d" % (s, alt.count(s)) for s in STATES)))
    print("\n   연도 간격: " + "  ".join(
        "%d년차 %d건" % (g, n)
        for g, n in hit["year_gap"].value_counts().sort_index().items()))

    print("\n== 조회 예시")
    for _, r in hit.sort_values(["year_gap", "cohort"]).head(8).iterrows():
        lab = "전년" if r["year_gap"] == 1 else "%d년 전" % int(r["year_gap"])
        print("   [%-8s] %-38s %s(%d) %-9s -> %d년 %-9s  %s"
              % (r["cohort"], str(r["title"])[:38], lab, int(r["prev_year"]),
                 won(r["prev_amount"]), int(r["year"]), won(r["cur_amount"]), r["status"]))

    C.save_report("m46_m2_yoy_lookup.json", {
        "kind": "조회(lookup) — 모델 아님. 학습·예측·정확도 없음",
        "target_cleaning": drop,
        "stem_normalization": eff,
        "index_rows": int(len(index)),
        "ambiguous_cells": int(index["ambiguous"].sum()),
        "n_scanned": int(len(pairs)), "n_hit": int(len(hit)),
        "hit_rate": round(len(hit) / len(pairs), 4),
        "reject_reasons": {k: int(v) for k, v in pairs["reason"].value_counts().items()},
        "by_cohort": {c: {"n": int(len(g)), "hit": int(g["status"].isin(STATES).sum())}
                      for c, g in pairs.groupby("cohort")},
        "state_counts": {s: int(vc.get(s, 0)) for s in STATES},
        "state_counts_tol5pct": {s: alt.count(s) for s in STATES},
        "year_gap": {int(k): int(v) for k, v in hit["year_gap"].value_counts().items()},
        "rejected_2a_evidence": {
            "pairs_total": 103, "pairs_n_ge_10": 15,
            "mannwhitney_sig": 0, "bootstrap_sign_agreement_median": 0.635,
            "persistence_accuracy": 0.500, "persistence_n": 6},
        "records": hit.to_dict("records"),
    })
    write_md(drop, eff, pairs, hit, vc, alt)
    return pairs


def write_md(drop, eff, pairs, hit, vc, alt):
    biz_hit = int(pairs[pairs["cohort"] == "bizinfo"]["status"].isin(STATES).sum())
    L = ["# 전년 대비 지원규모 — 조회 위젯 (모델 아님)", "",
         "> 모델 2 는 2-B(비교군 내 기업당 지원규모)로 확정한다. 이 문서는 그 옆에",
         "> 붙는 **조회 기능**이다. 학습하지 않고, 예측하지 않고, 정확도가 없다.", "",
         "> 하는 말: 전년 회차 금액 / 올해 금액 / 변화율 / 전년 회차 없음",
         "> 하지 않는 말: 오를 것이다 / 내릴 것이다 / 적정하다 / Accuracy 0.xx", "",
         "## 1. 왜 모델이 아니라 조회인가", "",
         "2-A(비교군 추이 분류)를 실측하면 라벨이 서지 않는다. 정제셋에서",
         "(비교군×연도) 연속 쌍 103개 중 양쪽 n≥10 인 것이 **15쌍**뿐이고, 그 15쌍은", "",
         "| 지표 | 값 |", "|---|---|",
         "| Mann-Whitney p<0.05 | 0 / 15 |",
         "| 부트스트랩 부호일치 ≥95% | 1 / 15 |",
         "| 부호일치 중앙값 | 0.635 |",
         "| 전년 방향으로 올해 방향 예측(persistence) | acc 0.500 (n=6) |", "",
         "부호일치 0.635 는 같은 데이터를 리샘플하면 상승/하락 라벨이 36% 확률로",
         "뒤집힌다는 뜻이다. 가장 두꺼운 칸(사업화×grant×company×taxonomy,",
         "131→185건)은 배수가 정확히 1.000 이다.", "",
         "조회는 추정량을 만들지 않는다. 원문에 적힌 한도 두 개를 나란히 놓을 뿐이라",
         "틀릴 수 있는 지점이 '같은 사업인가'와 '같은 종류의 금액인가' 둘뿐이고,",
         "둘 다 판정이 아니라 규칙으로 막는다. 그래서 정확도가 아니라 **커버리지와",
         "거절 사유**를 낸다.", "",
         "### 수집방식 전환은 여기서 문제가 되지 않는다", "",
         "2026 이 층화표본이 아니라 전량 수집이라는 사실은 2-A 를 죽인 이유 중",
         "하나였다 — 표본에서 전수로 바뀐 것이 상승으로 둔갑한다. 그러나 그것은",
         "**집계**의 문제다. 한 사업의 공고 두 건에 적힌 한도를 나란히 보는 데에는",
         "모집단 구성이 개입하지 않는다. 그래서 조회 위젯은 2025→2026 을 막지 않는다.", "",
         "## 2. 고친 것 — 조회 키 정규화", "",
         "`program_stem` 은 taxonomy 에만 연도 접두사 제거가 적용돼 있었다.",
         "bizinfo 는 `title` 원본이 그대로 들어간다(`f06_design_features.py:262`).", "",
         "```text", "2021년 D.N.A. 대중소 파트너십 동반진출 사업 공고",
         "2022년 D.N.A. 대중소 파트너십 동반진출 사업 공고", "```", "",
         "정규화 전에는 이 둘이 다른 사업이다. 조회 키는 M46 에서 다시 만든다.", "",
         "| 조건 | 2개 연도 이상 등장하는 사업 수 |", "|---|---|"]
    for tag, v in eff.items():
        L.append("| %s | %s |" % (tag, ", ".join("%s %d개" % (k, n) for k, n in v.items())))

    L += ["", "## 3. 거절 규칙", "",
          "답을 만들 수 없으면 만들지 않는다.", "",
          "| 규칙 | 이유 |", "|---|---|",
          "| 출처가 다르면 찾지 않는다 | 같은 사업이라도 기재 관행이 최대 40배 갈린다 (M45 §2) |",
          "| 지원단위가 바뀌었으면 거절 | 기업당 1억과 과제당 1억은 같은 숫자가 아니다 |",
          "| `stated_cap` 이 아니면 대상 아님 | 한도와 '총예산/건수' 평균을 섞으면 변화율이 무의미하다 |",
          "| 같은 해에 금액이 다른 회차가 여럿이면 거절 | 대표값을 고르는 순간 '원문 한도'가 아니게 된다 |",
          "| 직전 회차가 %d년보다 오래되면 해당없음 | 그 간격을 '전년 대비'라고 부를 수 없다 |" % MAX_GAP,
          "| 간격이 1년이 아니면 라벨을 바꾼다 | `전년 대비` 가 아니라 `직전 회차(2021년) 대비` |", "",
          "## 4. 커버리지", "",
          "정제셋 %d건(M45 와 같은 셋)을 전부 조회해봤다." % len(pairs), "",
          "| 결과 | 건수 |", "|---|---:|",
          "| **조회됨** | **%d건 (%.1f%%)** |" % (len(hit), 100 * len(hit) / len(pairs))]
    for r, n in pairs["reason"].value_counts().items():
        L.append("| 거절 — %s | %d건 |" % (r, n))

    L += ["", "출처별:", "", "| 출처 | 대상 | 조회됨 |", "|---|---:|---:|"]
    for c, g in pairs.groupby("cohort"):
        h = int(g["status"].isin(STATES).sum())
        L.append("| %s | %d건 | %d건 (%.1f%%) |" % (c, len(g), h, 100 * h / len(g)))

    L += ["", "## 5. 조회된 건의 상태 분포", "",
          "| 상태 | 건수 | 비율 |", "|---|---:|---:|"]
    for s in STATES:
        L.append("| %s | %d | %.1f%% |" % (s, int(vc.get(s, 0)),
                                           100 * int(vc.get(s, 0)) / max(len(hit), 1)))
    L += ["",
          "허용오차 ±%.0f%% 를 동결로 보면 %s 가 된다 — 경계를 어디에 두든"
          % (FREEZE_TOL_ALT * 100,
             " / ".join("%s %d" % (s, alt.count(s)) for s in STATES)),
          "**동결이 다수 클래스**다. 그래서 출력은 상승/하락 이진이 아니라 3상태여야 한다.",
          "원안 문서의 `상승 / 하락` 이진 출력은 이 지점에서 틀린다.", "",
          "연도 간격: " + ", ".join(
              "%d년차 %d건" % (g, n)
              for g, n in hit["year_gap"].value_counts().sort_index().items()), "",
          "## 6. 출력 형태", "",
          "```text", "[전년 대비]", "",
          "2022년 D.N.A. 대중소 파트너십 동반진출 사업",
          "  2021년   10.0억원", "  2022년    7.0억원", "  -> 하락 (-30.0%)", "```", "",
          "전년 회차가 없으면 카드 자체를 띄우지 않는다.", "",
          "```text", "[전년 대비]", "전년도 같은 사업 공고가 없어 비교할 수 없습니다.", "```", "",
          "서비스 진입점은 `compare_yoy(index, stem, year, cohort, amount, unit)` 하나다.",
          "M45 의 `compare()` 와 같은 모양으로 dict 를 돌려준다.", "",
          "## 7. 한계 — 발표에서 먼저 말할 것", "",
          "조회 가능 건수가 %d건(%.1f%%)이다. 기업마당(bizinfo) 단독으로는 %d건이라"
          % (len(hit), 100 * len(hit) / len(pairs), biz_hit),
          "**실서비스에서는 대부분의 공고에서 이 카드가 뜨지 않는다.** 이 기능이",
          "의미를 가지려면 같은 사업의 2개 연도 공고를 짝지어 수집하는 작업이",
          "선행돼야 한다. 지금 상태로는 '있으면 보여주는' 보조 카드다.", "",
          "커버리지를 늘리려고 규칙을 푸는 것(출처 혼합, 유사 제목 매칭, 단위 무시)은",
          "하지 않는다. 그렇게 늘린 건수는 조회가 아니라 추측이고, 추측이면 2-A 를",
          "기각한 이유로 되돌아간다.", ""]
    p = os.path.join(C.REPORTS, "m46_m2_yoy_lookup.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
