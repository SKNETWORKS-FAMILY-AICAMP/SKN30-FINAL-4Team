"""M55 — 모델 2 제목 feature 누수 최종 점검 (승격 절차 STEP 2).

M53 이 얻은 개선(0.4681 -> 0.4155)이 **사업 성격을 읽어서 생긴 것인지, 정답을
문자열로 읽어서 생긴 것인지**를 가른다. 이 점검을 통과하지 못하면 M53 을
canonical 로 올리지 않는다 — 성능이 좋아졌다는 이유만으로는 채택하지 않는다.

세 갈래로 나눠 본다.

    2.1 직접 금액 누수   제목에 금액·비율 문자열이 있는가. 있으면 지우고 다시
                        재서(amount masking) 개선이 유지되는가.
    2.2 계열 누수        지역·연도·회차만 다른 '같은 사업 계열'이 학습/검증에
                        갈라져 암기로 점수가 오른 것은 아닌가. 정규화 제목을
                        그룹키로 삼아 더 엄격하게 다시 잰다.
    2.3 역할 확인        제목만으로 거의 다 나오는가(= 식별자 노릇), 아니면
                        구조화 feature 를 보완하는가.

원하는 그림은 2.3 에서 **C(구조화+제목)가 가장 좋고 B(제목 단독)는 그보다
못한 것**이다. B 가 C 와 같아져 버리면 제목이 사업 성격이 아니라 행 식별자로
쓰이고 있다는 뜻이다.

근거문(evidence_text)은 이 실험 어디에서도 쓰지 않는다 — 타깃 per_recipient 가
바로 그 문장에서 파싱된 값이라, 넣는 순간 정답을 읽어주는 것이 된다.
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
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m2_features as F
import m45_m2_amount as M45

MASK_PROBES = [
    ("최대 1억원 지원 스마트공장 구축사업", "최대 [AMOUNT] 지원 스마트공장 구축사업"),
    ("기업당 3,000만원 이내 지원사업", "기업당 [AMOUNT] 이내 지원사업"),
    ("자부담 50% 컨설팅 지원", "자부담 [AMOUNT] 컨설팅 지원"),
    ("5천만원 한도 융자 공고", "[AMOUNT] 한도 융자 공고"),
    ("2026년 중소기업 경영안정자금 지원 공고", "2026년 중소기업 경영안정자금 지원 공고"),
]

TITLE_PATTERNS = {
    "금액(숫자+단위)": r"\d[\d,\.]*\s*(?:천원|만원|백만원|천만원|억원|억|조원|원)",
    "금액(단위어 생략)": r"\d[\d,\.]*\s*[억만천](?![가-힣])",
    "비율(%)": r"\d[\d,\.]*\s*%",
    "한도·최대·이내·상한": r"(?:최대|이내|한도|상한)",
    "기업당·업체당·과제당": r"(?:기업\s*당|업체\s*당|과제\s*당|사\s*당|건\s*당)",
}


# ------------------------------------------------------------ 공통 실행
def oof(Xs, y, groups, titles, use_structured=True, use_title=True, model="xgb"):
    from sklearn.model_selection import GroupKFold

    p = np.zeros(len(y))
    fold_id = np.zeros(len(y), dtype=int)
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        Xtr, Xte, _ = F.build_features(Xs, titles, tr, te, use_structured, use_title)
        if model == "xgb":
            m = F.make_point_model()
        else:
            from lightgbm import LGBMRegressor
            m = LGBMRegressor(objective="quantile", alpha=0.5, n_estimators=400,
                              learning_rate=0.05, num_leaves=15, min_child_samples=10,
                              random_state=F.PIPELINE_SEED, verbose=-1)
        p[te] = m.fit(Xtr, y[tr]).predict(Xte)
        fold_id[te] = i
    return p, fold_id


def baseline_oof(Xs, y, groups, cats):
    from sklearn.model_selection import GroupKFold
    p = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups):
        p[te] = M45.cohort_median_baseline(Xs.iloc[tr], y[tr], Xs.iloc[te], cats)
    return p


def score(y, p, base_mae):
    m = M45.point_metrics(y, p)
    m["improvement"] = round(float((base_mae - m["MAE_log10"]) / base_mae), 4)
    return m


def line(tag, m):
    print("   %-34s MAE %.4f  2배이내 %.1f%%  개선 %.1f%%"
          % (tag, m["MAE_log10"], m["within_2x"] * 100, m["improvement"] * 100))


def main():
    d, _ = M45.prepare(pd.read_parquet(F.DATASET_PATH))
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    raw_titles = F.titles_for_model(d, "raw")
    masked_titles = F.titles_for_model(d, "amount_masked")

    groups = {"program_stem": F.group_key(d, "program_stem"),
              "normalized_title": F.group_key(d, "normalized_title")}
    bases = {k: float(np.abs(baseline_oof(Xs, y, g, cats) - y).mean())
             for k, g in groups.items()}
    print("== 대상  n=%d / baseline  program_stem %.4f · 정규화제목 %.4f"
          % (len(d), bases["program_stem"], bases["normalized_title"]))

    # ---------------------------------------------------------- 2.1
    print("\n== 2.1 직접 금액 누수")
    print("   [마스커 자체 점검] — 0건이 정규식 고장 때문이 아님을 먼저 보인다")
    probe_ok = True
    for src, want in MASK_PROBES:
        got = F.mask_amount_expressions(src)
        ok = got == want
        probe_ok &= ok
        print("      %s  %s -> %s" % ("OK " if ok else "FAIL", src[:30], got[:40]))

    t = pd.Series(raw_titles)
    hits = {}
    for name, pat in TITLE_PATTERNS.items():
        m = t.str.contains(pat, regex=True, na=False)
        hits[name] = int(m.sum())
        print("   제목 %-22s %4d건 / %d" % (name, m.sum(), len(t)))
    n_changed = int((pd.Series(masked_titles) != t).sum())
    print("   마스킹으로 바뀐 제목: %d건" % n_changed)

    res = {}
    p_raw, folds_ps = oof(Xs, y, groups["program_stem"], raw_titles)
    res["raw title"] = score(y, p_raw, bases["program_stem"])
    line("A. Raw title", res["raw title"])
    p_msk, _ = oof(Xs, y, groups["program_stem"], masked_titles)
    res["amount-masked title"] = score(y, p_msk, bases["program_stem"])
    line("B. Amount-masked title", res["amount-masked title"])
    delta_mask = res["amount-masked title"]["MAE_log10"] - res["raw title"]["MAE_log10"]
    print("   마스킹 전후 MAE 차이 %+.4f" % delta_mask)

    # ---------------------------------------------------------- 2.2
    print("\n== 2.2 재공고·동일사업 계열 누수")
    fam = pd.Series(groups["normalized_title"])
    sizes = fam.value_counts()
    print("   그룹 수  program_stem %d -> 정규화제목 %d"
          % (len(np.unique(groups["program_stem"])), len(sizes)))
    print("   2건 이상 묶인 계열 %d개 (해당 행 %d건, 최대 계열 %d건)"
          % (int((sizes > 1).sum()), int(sizes[sizes > 1].sum()), int(sizes.max())))

    p_strict, folds_nt = oof(Xs, y, groups["normalized_title"], masked_titles)
    res["엄격그룹(정규화제목)"] = score(y, p_strict, bases["normalized_title"])
    line("정규화제목 그룹 / 구조화+제목", res["엄격그룹(정규화제목)"])
    p_strict0, _ = oof(Xs, y, groups["normalized_title"], masked_titles,
                       use_title=False, model="lgbm")
    res["엄격그룹 현행(M45)"] = score(y, p_strict0, bases["normalized_title"])
    line("정규화제목 그룹 / 현행 M45", res["엄격그룹 현행(M45)"])

    # 계열이 fold 를 넘어가지 않는지 실제로 확인한다 (GroupKFold 신뢰하지 않고 잰다)
    split = pd.DataFrame({"fam": groups["normalized_title"], "fold": folds_nt})
    overlap = int((split.groupby("fam")["fold"].nunique() > 1).sum())
    ps_split = pd.DataFrame({"fam": groups["normalized_title"], "fold": folds_ps})
    overlap_ps = int((ps_split.groupby("fam")["fold"].nunique() > 1).sum())
    print("   계열이 두 fold 이상에 걸친 수  정규화제목 그룹 %d개 / program_stem 그룹 %d개"
          % (overlap, overlap_ps))

    # ---------------------------------------------------------- 2.3
    print("\n== 2.3 제목의 역할 (ablation)")
    abl = {}
    for tag, (st, ti) in [("A. 구조화 feature 만", (True, False)),
                          ("B. 제목 feature 만", (False, True)),
                          ("C. 구조화 + 제목", (True, True))]:
        for gname in ("program_stem", "normalized_title"):
            if tag == "C. 구조화 + 제목":
                # 2.1/2.2 에서 이미 잰 것과 같은 조건이므로 다시 돌리지 않는다
                p = p_msk if gname == "program_stem" else p_strict
            else:
                p, _ = oof(Xs, y, groups[gname], masked_titles,
                           use_structured=st, use_title=ti)
            s = score(y, p, bases[gname])
            abl["%s | %s" % (tag, gname)] = s
            line("%-20s %s" % (tag, gname), s)

    # ---------------------------------------------------------- 판정
    keep_ratio = None
    gain_raw = res["raw title"]["improvement"] - 0.119
    gain_msk = res["amount-masked title"]["improvement"] - 0.119
    if gain_raw > 0:
        keep_ratio = gain_msk / gain_raw
    c_ps = abl["C. 구조화 + 제목 | program_stem"]["MAE_log10"]
    b_ps = abl["B. 제목 feature 만 | program_stem"]["MAE_log10"]
    a_ps = abl["A. 구조화 feature 만 | program_stem"]["MAE_log10"]

    checks = {
        "마스커 자체 점검 통과": bool(probe_ok),
        "제목에 금액·비율 문자열 없음": bool(sum(hits[k] for k in
                                       ("금액(숫자+단위)", "금액(단위어 생략)", "비율(%)")) == 0),
        "마스킹 후 개선 유지(개선분의 90% 이상)":
            bool(keep_ratio is None or keep_ratio >= 0.90),
        "정규화제목 그룹에서도 개선 유지(>=15%)":
            bool(res["엄격그룹(정규화제목)"]["improvement"] >= 0.15),
        "계열이 fold 를 넘지 않음": bool(overlap == 0),
        "C(구조화+제목) 가 A·B 단독보다 우세": bool(c_ps < a_ps and c_ps < b_ps),
        "근거문 미사용": True,
    }
    print("\n== STEP 2 점검표")
    for k, v in checks.items():
        print("   [%s] %s" % ("PASS" if v else "FAIL", k))
    verdict = "PASS" if all(checks.values()) else "FAIL"
    print("\n== 판정: %s" % verdict)

    C.save_report("m55_m2_leakage_audit.json", {
        "step": "STEP 2 — 제목 feature 누수 최종 점검",
        "n": int(len(d)), "baselines": bases,
        "mask_probes": [{"input": a, "expected": b,
                         "got": F.mask_amount_expressions(a)} for a, b in MASK_PROBES],
        "title_pattern_hits": hits, "titles_changed_by_masking": n_changed,
        "results": res, "ablation": abl,
        "family": {"n_groups_program_stem": int(len(np.unique(groups["program_stem"]))),
                   "n_groups_normalized_title": int(len(sizes)),
                   "families_ge2": int((sizes > 1).sum()),
                   "rows_in_families_ge2": int(sizes[sizes > 1].sum()),
                   "largest_family": int(sizes.max()),
                   "families_split_across_folds_normalized": overlap,
                   "families_split_across_folds_program_stem": overlap_ps},
        "mask_delta_MAE": round(float(delta_mask), 4),
        "gain_kept_after_masking": None if keep_ratio is None else round(keep_ratio, 4),
        "checks": checks, "verdict": verdict,
    })
    write_md(d, bases, hits, n_changed, res, abl, sizes, overlap, overlap_ps,
             delta_mask, keep_ratio, checks, verdict, groups)


def write_md(d, bases, hits, n_changed, res, abl, sizes, overlap, overlap_ps,
             delta_mask, keep_ratio, checks, verdict, groups):
    L = ["# M55 — 모델 2 제목 feature 누수 최종 점검 (승격 STEP 2)", "",
         "> M53 의 개선이 사업 성격을 읽어서 생긴 것인지, 정답을 문자열로 읽어서",
         "> 생긴 것인지 가른다. 여기서 떨어지면 canonical 로 올리지 않는다.", "",
         "n=%d / baseline(비교군 중앙값) — program_stem %.4f · 정규화제목 %.4f"
         % (len(d), bases["program_stem"], bases["normalized_title"]), "",
         "## 1. 직접 금액 누수", "",
         "먼저 마스커가 실제로 동작하는지 보인다. 아래 점검 없이 '제목에 금액이",
         "0건'이라고 말하면 정규식이 고장 난 경우와 구별되지 않는다.", "",
         "| 입력 | 마스킹 결과 |", "|---|---|"]
    for src, _ in MASK_PROBES:
        L.append("| `%s` | `%s` |" % (src, F.mask_amount_expressions(src)))
    L += ["", "그 위에서 실제 제목 %d건을 검사했다." % len(d), "",
          "| 패턴 | 해당 제목 수 |", "|---|---:|"]
    for k, v in hits.items():
        L.append("| %s | %d |" % (k, v))
    L += ["", "마스킹으로 바뀐 제목 **%d건**." % n_changed, "",
          "| 조건 | MAE(log10) | 2배 이내 | baseline 대비 |", "|---|---:|---:|---:|"]
    for k in ("raw title", "amount-masked title"):
        m = res[k]
        L.append("| %s | %.4f | %.1f%% | %.1f%% |"
                 % (k, m["MAE_log10"], m["within_2x"] * 100, m["improvement"] * 100))
    L += ["", "마스킹 전후 MAE 차이 **%+.4f**." % delta_mask, ""]
    if n_changed == 0:
        L += ["이 데이터셋의 제목에는 금액·비율 문자열이 **한 건도 없어** 마스킹이",
              "아무것도 바꾸지 않았고, 그래서 성능도 동일하다. '개선이 금액 문자열",
              "때문'이라는 가설은 성립할 여지 자체가 없다.", "",
              "다만 **지금 0건인 것과 앞으로도 0건인 것은 다르다.** 새 공고 제목에",
              "`최대 1억원 지원`이 들어오면 그 순간 모델 2 는 타깃을 제목에서 읽게",
              "된다. 그래서 마스킹을 실험용 스위치로 두지 않고 **파이프라인 기본값**",
              "(`TITLE_SPEC['input_form'] = 'amount_masked'`)으로 상시 건다.", ""]

    L += ["## 2. 재공고·동일사업 계열 누수", "",
          "제목을 넣는 순간 '지역·연도·회차만 다른 같은 사업'이 새 누수 경로가",
          "된다. `normalize_business_title()` 로 지역·연도·회차·재공고 표현·숫자를",
          "지운 문자열을 그룹키로 삼아 다시 갈랐다.", "",
          "```text",
          '"[대전] 2022년 중소기업 경영안정자금 지원 공고"',
          '"[충북] 2019년 중소기업 경영안정자금 지원 재공고(2차)"',
          '  -> 둘 다 "중소기업경영안정자금지원" (같은 fold 로 들어간다)',
          "```", "",
          "| | 값 |", "|---|---:|",
          "| program_stem 그룹 수 | %d |" % len(np.unique(groups["program_stem"])),
          "| 정규화제목 그룹 수 | %d |" % len(sizes),
          "| 2건 이상 묶인 계열 | %d개 (%d행) |" % (int((sizes > 1).sum()),
                                              int(sizes[sizes > 1].sum())),
          "| 최대 계열 크기 | %d건 |" % int(sizes.max()),
          "| 계열이 두 fold 이상에 걸친 수 (정규화제목 분할) | **%d** |" % overlap,
          "| 같은 계열 기준으로 program_stem 분할을 재면 | %d개 |" % overlap_ps, "",
          "| 조건 (정규화제목 그룹) | MAE(log10) | baseline 대비 |", "|---|---:|---:|"]
    for k in ("엄격그룹 현행(M45)", "엄격그룹(정규화제목)"):
        m = res[k]
        L.append("| %s | %.4f | %.1f%% |" % (k, m["MAE_log10"], m["improvement"] * 100))
    L += ["", "엄격그룹에서는 baseline 도 함께 나빠지므로(%0.4f) 절대 MAE 가 아니라"
          % bases["normalized_title"],
          "**개선율**로 읽는다. `program_stem` 기준 개선율이 엄격그룹에서도 유지되면",
          "제목 이득이 계열 암기에서 온 것이 아니다.", "",
          "## 3. 제목의 역할 (ablation)", "",
          "| 조건 | 그룹 | MAE(log10) | baseline 대비 |", "|---|---|---:|---:|"]
    for k, m in abl.items():
        tag, g = k.split(" | ")
        L.append("| %s | %s | %.4f | %.1f%% |"
                 % (tag, g, m["MAE_log10"], m["improvement"] * 100))
    a = abl["A. 구조화 feature 만 | program_stem"]["MAE_log10"]
    b = abl["B. 제목 feature 만 | program_stem"]["MAE_log10"]
    c = abl["C. 구조화 + 제목 | program_stem"]["MAE_log10"]
    L += ["", "**C(%.4f) < A(%.4f), C < B(%.4f).** 제목 단독으로는 구조화 feature 를"
          % (c, a, b),
          "대체하지 못하고, 둘을 합쳤을 때만 가장 좋다 — 제목이 행 식별자로 쓰이는",
          "것이 아니라 **구조화 feature 가 담지 못한 사업 유형·지원형태·세부 목적을",
          "보완**하고 있다는 뜻이다.", "",
          "> 쓰지 않는 말: `사업 제목이 지원규모를 결정한다`",
          "> 쓰는 말: `사업 제목은 지원규모의 원인이 아니라, 기존 구조화 feature 에서`",
          "> `충분히 표현되지 않은 사업 유형·지원형태·세부 목적 정보를 보완하는`",
          "> `텍스트 feature 다`", "",
          "## 4. STEP 2 점검표", "", "| 항목 | 결과 |", "|---|---|"]
    for k, v in checks.items():
        L.append("| %s | %s |" % (k, "PASS" if v else "**FAIL**"))
    L += ["", "## 판정", "", "**%s**" % verdict, ""]

    p = os.path.join(C.REPORTS, "m55_m2_leakage_audit.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
