"""M26 — 모델 2: 예측구간 실용성 판정 (최종개선계획서 3순위).

계획서 69~94행. 핵심 문장을 그대로 옮긴다.

    "MAE 소폭 개선보다 coverage 유지 + interval width 감소"

M22 에서 확인한 것
    비교군별 보정(Mondrian)으로는 구간폭이 43.7배 -> 41.1배, 6% 밖에 안 좁아진다.
    대신 칸마다 폭이 18.8배(연구개발/grant)에서 368배(설비/grant)까지 갈린다.

그래서 여기서 하는 일은 "더 좁히기"가 아니다
    좁힐 수 없다는 것은 이미 쟀다. 대신 **어느 비교군의 구간은 쓸 만하고 어느
    것은 쓰면 안 되는지**를 판정해 출력에 붙인다. 계획서 91~94행이 요구한
    세 단계 상태다.

        참고 가능           구간이 실무에서 의미를 갖는 폭
        범위 넓음           참고는 되지만 폭이 크다는 경고를 함께 낸다
        참고 범위 제시 어려움  구간 대신 "편차가 커 제시하기 어렵다"고 적는다

    368배 구간을 "P10~P90 참고 범위"라고 내보내면, 담당자가 그 숫자를 근거로
    쓸 수 없을 뿐 아니라 근거가 있다고 오해한다. 안 내보내는 것이 정직하다.

추가 지표 (계획서 76~82행)
    Interval Score (Winkler) — 커버리지와 폭을 하나로 합친 점수.
        구간을 넓히면 폭 벌점이 늘고, 벗어나면 이탈 벌점이 붙는다.
        폭만 보거나 커버리지만 보는 것보다 정직하다.
    Pinball Loss / MedAE / Coverage / Median·Mean Width
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import SRC, prepare
from m17_m3_tuning import FEATURE_SETS, build
from m19_m3_interval import HI, LO, NOMINAL, fit_quantiles, pinball
from m22_m3_narrow import cohort_id, mondrian_fold

SEED = 42
MIN_CELL = 30           # 이보다 얇은 비교군은 판정하지 않는다
# 계획서 84~94행. 구간폭(배수)을 기준으로 세 단계로 가른다.
#
# 경계를 어디에 둘 것인가 — M22 의 실측 분포에서 잡았다.
#   연구개발/grant 18.8배 / 판로/grant 24.5배 / 사업화/grant 36.0배
#   융자/loan 42.0배 / 설비/grant 368.2배
# 30배까지는 "이 정도 규모"라는 감을 주고, 100배를 넘으면 사실상 무제한이라
# 담당자가 근거로 쓸 수 없다.
USABLE_MAX = 30.0
WIDE_MAX = 100.0
STATUS_USABLE = "참고 가능"
STATUS_WIDE = "범위 넓음"
STATUS_UNUSABLE = "참고 범위 제시 어려움"


def interval_score(y, lo, hi, alpha=1 - NOMINAL):
    """Winkler interval score. 낮을수록 좋다.

        폭 + (2/alpha) * (하한 미달분 + 상한 초과분)

    구간을 넓히면 첫 항이, 벗어나면 둘째 항이 벌점을 준다. 커버리지만 보거나
    폭만 보면 한쪽으로 도망칠 수 있는데 이 지표는 둘을 같이 묶는다.
    """
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    width = hi2 - lo2
    below = np.maximum(lo2 - y, 0.0)
    above = np.maximum(y - hi2, 0.0)
    return width + (2.0 / alpha) * (below + above)


def classify(width_x):
    if width_x <= USABLE_MAX:
        return STATUS_USABLE
    if width_x <= WIDE_MAX:
        return STATUS_WIDE
    return STATUS_UNUSABLE


def evaluate(X, y, groups, params, coh, n_splits=5):
    """M22 와 같은 비교군별 conformal 로 구간을 만든다."""
    rng = np.random.default_rng(SEED)
    lo = np.zeros(len(y)); mid = np.zeros(len(y)); hi = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        l, m, h, _, _ = mondrian_fold(X.iloc[tr], y[tr], X.iloc[te], params,
                                      groups[tr], coh[tr], coh[te], rng)
        lo[te], mid[te], hi[te] = l, m, h
    return lo, mid, hi


def per_cohort_report(y, lo, mid, hi, coh, min_n=MIN_CELL):
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    inside = (y >= lo2) & (y <= hi2)
    width = hi2 - lo2
    iscore = interval_score(y, lo, hi)
    rows = []
    for c in pd.unique(coh):
        m = coh == c
        if m.sum() < min_n:
            continue
        w_med = float(np.median(width[m]))
        w_x = float(10 ** w_med)
        rows.append({
            "cohort": str(c), "n": int(m.sum()),
            "coverage": round(float(inside[m].mean()), 4),
            "width_median_log10": round(w_med, 4),
            "width_x": round(w_x, 1),
            "interval_score": round(float(np.median(iscore[m])), 4),
            "MAE_log10": round(float(np.abs(mid[m] - y[m]).mean()), 4),
            "MedAE_log10": round(float(np.median(np.abs(mid[m] - y[m]))), 4),
            "pinball_lo": round(pinball(y[m], lo[m], LO), 5),
            "pinball_hi": round(pinball(y[m], hi[m], HI), 5),
            "status": classify(w_x),
        })
    return sorted(rows, key=lambda r: r["width_x"])


def coverage_of(rows, statuses, total_rows):
    n = sum(r["n"] for r in rows if r["status"] in statuses)
    return n, round(n / total_rows, 4) if total_rows else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", default="성격x방식",
                    help="보정·판정 기준 축 (M22 에서 고른 축)")
    a = ap.parse_args()

    d = prepare(pd.read_parquet(SRC))
    with open(os.path.join(C.REPORTS, "m17_m3_tuning.json"), encoding="utf-8") as f:
        m17 = json.load(f)
    feats = FEATURE_SETS[m17["feature_sets"]["chosen"]]
    params = m17["tuning"]["best_params"]
    t, X, y, g, _ = build(d, feats)

    keys = {"전역": None, "지원성격": ["support_type"],
            "지원방식": ["support_method"],
            "성격x방식": ["support_type", "support_method"]}
    coh = cohort_id(t, keys[a.cohort]).to_numpy()

    print("모델 2 구간 실용성 판정: %d행 / 보정축 %s" % (len(t), a.cohort))
    print("판정 기준: <=%.0f배 %s / <=%.0f배 %s / 그 위 %s"
          % (USABLE_MAX, STATUS_USABLE, WIDE_MAX, STATUS_WIDE, STATUS_UNUSABLE))

    t0 = time.time()
    lo, mid, hi = evaluate(X, y, g, params, coh)

    # 전체 지표 (계획서 76~82행)
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    inside = (y >= lo2) & (y <= hi2)
    overall = {
        "coverage": round(float(inside.mean()), 4),
        "width_median_log10": round(float(np.median(hi2 - lo2)), 4),
        "width_median_x": round(float(10 ** np.median(hi2 - lo2)), 1),
        "width_mean_log10": round(float((hi2 - lo2).mean()), 4),
        "interval_score_median": round(float(np.median(interval_score(y, lo, hi))), 4),
        "interval_score_mean": round(float(interval_score(y, lo, hi).mean()), 4),
        "MAE_log10": round(float(np.abs(mid - y).mean()), 4),
        "MedAE_log10": round(float(np.median(np.abs(mid - y))), 4),
        "pinball_lo": round(pinball(y, lo, LO), 5),
        "pinball_mid": round(pinball(y, mid, 0.5), 5),
        "pinball_hi": round(pinball(y, hi, HI), 5),
    }
    print("\n== 전체 지표")
    print("  커버리지 %.1f%% / 구간폭 중앙값 %.1f배 / Interval Score(중앙) %.4f"
          % (overall["coverage"] * 100, overall["width_median_x"],
             overall["interval_score_median"]))
    print("  MAE %.4f / MedAE %.4f / Pinball(P50) %.5f"
          % (overall["MAE_log10"], overall["MedAE_log10"], overall["pinball_mid"]))

    rows = per_cohort_report(y, lo, mid, hi, coh)
    print("\n== 비교군별 실용성 판정 (%d건 이상, %d칸)" % (MIN_CELL, len(rows)))
    print("%-26s %5s %8s %9s %11s  %s"
          % ("비교군", "n", "커버리지", "구간폭", "IntScore", "판정"))
    for r in rows:
        print("%-26s %5d %7.1f%% %8.1f배 %11.4f  %s"
              % (r["cohort"], r["n"], r["coverage"] * 100, r["width_x"],
                 r["interval_score"], r["status"]))

    n_judged = sum(r["n"] for r in rows)
    n_usable, p_usable = coverage_of(rows, {STATUS_USABLE}, n_judged)
    n_ok, p_ok = coverage_of(rows, {STATUS_USABLE, STATUS_WIDE}, n_judged)
    n_bad, p_bad = coverage_of(rows, {STATUS_UNUSABLE}, n_judged)
    print("\n== 서비스 관점 집계 (판정 대상 %d행 기준)" % n_judged)
    print("  참고 가능        %4d행 (%.1f%%)" % (n_usable, p_usable * 100))
    print("  범위 넓음 포함   %4d행 (%.1f%%)" % (n_ok, p_ok * 100))
    print("  제시 어려움      %4d행 (%.1f%%)" % (n_bad, p_bad * 100))
    print("  판정 제외(%d건 미만) %d행" % (MIN_CELL, len(y) - n_judged))

    verdict = judge(overall, rows, n_judged, len(y), p_usable, p_ok, p_bad)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("m26_m2_interval_usability.json", {
        "n_rows": int(len(t)), "cohort_axis": a.cohort, "min_cell": MIN_CELL,
        "thresholds": {"usable_max_x": USABLE_MAX, "wide_max_x": WIDE_MAX},
        "overall": overall, "per_cohort": rows,
        "service_summary": {
            "n_judged": int(n_judged),
            "usable": {"n": int(n_usable), "share": p_usable},
            "usable_or_wide": {"n": int(n_ok), "share": p_ok},
            "unusable": {"n": int(n_bad), "share": p_bad},
            "not_judged": int(len(y) - n_judged),
        },
        "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })
    write_md(overall, rows, n_judged, len(y), p_usable, p_ok, p_bad, verdict, a.cohort)


def judge(overall, rows, n_judged, n_total, p_usable, p_ok, p_bad):
    reasons, v = [], "채택"
    reasons.append("전체 커버리지 %.1f%% / 구간폭 중앙값 %.1f배 — M22 에서 확인한 대로 "
                   "더 좁힐 여지는 없다"
                   % (overall["coverage"] * 100, overall["width_median_x"]))
    reasons.append("Interval Score 중앙값 %.4f — 커버리지와 폭을 한 값으로 묶은 지표다. "
                   "폭만 보거나 커버리지만 보면 한쪽으로 도망칠 수 있다"
                   % overall["interval_score_median"])
    reasons.append("판정 대상 %d행 중 '%s' %.1f%%, '%s' 포함 %.1f%%, '%s' %.1f%%"
                   % (n_judged, STATUS_USABLE, p_usable * 100, STATUS_WIDE,
                      p_ok * 100, STATUS_UNUSABLE, p_bad * 100))
    if rows:
        best, worst = rows[0], rows[-1]
        reasons.append("가장 좁은 칸 %s %.1f배 / 가장 넓은 칸 %s %.1f배 — 하나의 "
                       "폭으로 뭉뚱그려 낼 수 없는 이유다"
                       % (best["cohort"], best["width_x"],
                          worst["cohort"], worst["width_x"]))
    if p_bad > 0:
        reasons.append("'%s' 로 판정된 칸은 구간을 내보내지 않고 그 사실을 표기한다. "
                       "폭이 %.0f배를 넘으면 담당자가 근거로 쓸 수 없을 뿐 아니라 "
                       "근거가 있다고 오해한다" % (STATUS_UNUSABLE, WIDE_MAX))
    reasons.append("비교군 %d건 미만인 %d행은 애초에 판정하지 않는다(비교군 부족)"
                   % (MIN_CELL, n_total - n_judged))
    if p_usable < 0.3:
        v = "조건부 채택 — 구간을 기본 출력으로 두면 안 된다"
        reasons.append("'%s' 비중이 %.1f%% 로 낮다. 구간은 요청 시 보조 정보로 내고, "
                       "기본 출력은 percentile 위치로 두는 편이 안전하다"
                       % (STATUS_USABLE, p_usable * 100))
    return {"verdict": v, "reasons": reasons}


def write_md(overall, rows, n_judged, n_total, p_usable, p_ok, p_bad,
             verdict, axis):
    L = ["# 모델 2 — 예측구간 실용성 판정", "",
         "> 최종개선계획서 3순위(69~94행): \"MAE 소폭 개선보다 coverage 유지 +",
         "> interval width 감소\"", "",
         "## 1. 여기서 하는 일은 \"더 좁히기\"가 아닙니다", "",
         "M22 에서 이미 쟀습니다 — 비교군별 보정으로는 43.7배 → 41.1배, **6% 밖에**",
         "**안 좁아집니다.** 칸마다 예측 난이도가 비슷해서 나눠도 얻는 게 없습니다.", "",
         "대신 **어느 비교군의 구간은 쓸 만하고 어느 것은 쓰면 안 되는지**를 판정해",
         "출력에 붙입니다. 계획서 91~94행이 요구한 세 단계입니다.", "",
         "```text",
         "%-22s 구간폭 <= %.0f배    구간을 그대로 낸다" % (STATUS_USABLE, USABLE_MAX),
         "%-22s <= %.0f배           구간과 함께 '폭이 넓다'를 표기한다" % (STATUS_WIDE, WIDE_MAX),
         "%-22s 그 위             구간 대신 '편차가 커 제시하기 어렵다'" % STATUS_UNUSABLE,
         "```", "",
         "> 368배 구간을 \"P10~P90 참고 범위\"라고 내보내면, 담당자가 그 숫자를 근거로",
         "> 쓸 수 없을 뿐 아니라 **근거가 있다고 오해합니다.** 안 내보내는 것이",
         "> 정직합니다.", "",
         "경계값은 M22 의 실측 분포에서 잡았습니다 — 연구개발/grant 18.8배,",
         "판로/grant 24.5배, 사업화/grant 36.0배, 융자/loan 42.0배, 설비/grant 368.2배.", "",
         "## 2. 전체 지표 (계획서 76~82행)", "",
         "| 지표 | 값 |", "|---|---:|",
         "| Coverage (명목 %.0f%%) | %.1f%% |" % (NOMINAL * 100, overall["coverage"] * 100),
         "| Median Interval Width | %.4f log10 (**%.1f배**) |"
         % (overall["width_median_log10"], overall["width_median_x"]),
         "| Mean Interval Width | %.4f log10 |" % overall["width_mean_log10"],
         "| **Interval Score** (중앙) | **%.4f** |" % overall["interval_score_median"],
         "| Interval Score (평균) | %.4f |" % overall["interval_score_mean"],
         "| MAE(log10) | %.4f |" % overall["MAE_log10"],
         "| Median Absolute Error | %.4f |" % overall["MedAE_log10"],
         "| Pinball (P10 / P50 / P90) | %.5f / %.5f / %.5f |"
         % (overall["pinball_lo"], overall["pinball_mid"], overall["pinball_hi"]), "",
         "> **Interval Score(Winkler)** 는 커버리지와 폭을 한 값으로 묶습니다 —",
         "> `폭 + (2/α)·(하한 미달분 + 상한 초과분)`. 구간을 넓히면 첫 항이, 벗어나면",
         "> 둘째 항이 벌점을 줍니다. 폭만 보거나 커버리지만 보면 한쪽으로 도망칠 수",
         "> 있는데 이 지표는 둘을 같이 묶습니다.", "",
         "## 3. 비교군별 판정 (%s 축, %d건 이상)" % (axis, MIN_CELL), "",
         "| 비교군 | n | 커버리지 | 구간폭 | Interval Score | MedAE | 판정 |",
         "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        L.append("| %s | %d | %.1f%% | **%.1f배** | %.4f | %.4f | **%s** |"
                 % (r["cohort"], r["n"], r["coverage"] * 100, r["width_x"],
                    r["interval_score"], r["MedAE_log10"], r["status"]))

    L += ["", "## 4. 서비스 관점 집계", "",
          "| 상태 | 행 수 | 비중 |", "|---|---:|---:|",
          "| %s | %d | %.1f%% | " % (STATUS_USABLE,
                                     sum(r["n"] for r in rows if r["status"] == STATUS_USABLE),
                                     p_usable * 100),
          "| %s | %d | %.1f%% |" % (STATUS_WIDE,
                                    sum(r["n"] for r in rows if r["status"] == STATUS_WIDE),
                                    (p_ok - p_usable) * 100),
          "| %s | %d | %.1f%% |" % (STATUS_UNUSABLE,
                                    sum(r["n"] for r in rows if r["status"] == STATUS_UNUSABLE),
                                    p_bad * 100),
          "| (판정 제외 — 비교군 %d건 미만) | %d | — |" % (MIN_CELL, n_total - n_judged), "",
          "## 5. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 6. 표현 규율", "", "```text",
          "허용   과거 유사사업 기반 참고 예측 범위",
          "       상대적 지원규모 참고 구간",
          "       (넓은 칸) 비교군 편차가 커 참고 범위를 제시하기 어렵습니다",
          "금지   적정 지원규모 범위 / 권장 금액 / 이 정도가 맞다",
          "```", ""]
    p = os.path.join(C.REPORTS, "m26_m2_interval_usability.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
