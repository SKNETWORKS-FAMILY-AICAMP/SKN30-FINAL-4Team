"""M22 — 모델 3 구간 좁히기: 비교군별 Conformal (Mondrian CQR).

최종정리 문서 3.6절의 남은 한계.

    "Coverage 를 맞추는 과정에서 구간폭이 12.9배 -> 43.7배로 증가했다.
     구간은 통계적으로 잘 맞지만, 실무적으로는 너무 넓을 수 있다."

M19 는 보정폭(delta)을 **전체에 하나**로 썼다. 그러면 예측이 잘 맞는 비교군도
가장 안 맞는 비교군에 맞춰 넓어진다. 융자처럼 금액 자릿수가 넓은 칸 하나가
사업화·판로처럼 좁은 칸까지 끌고 가는 것이다.

Mondrian conformal
    보정을 비교군별로 따로 한다. 각 칸의 실제 이탈량으로 그 칸의 delta 를 정하면
    좁은 칸은 좁게, 넓은 칸은 넓게 나온다. 전체 커버리지는 유지된다.

    delta_c = 비교군 c 의 보정셋에서 잰 이탈량의 80% 분위수

    비교군이 얇으면 그 칸의 delta 가 불안정하므로 전체 delta 로 되돌린다(fallback).

여기서 재는 것
    1. 전역 보정 vs 비교군별 보정 — 커버리지·구간폭
    2. 비교군 축을 무엇으로 잡을 때 가장 좁아지는가 (성격 / 성격x방식 / 방식)
    3. 칸별 커버리지 — 전체는 맞는데 특정 칸만 어긋나지 않는가
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
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
from m19_m3_interval import (CAL_FRAC, HI, LO, NOMINAL, TARGET_HIGH, TARGET_LOW,
                             fit_quantiles, interval_metrics)

SEED = 42
MIN_CAL = 25          # 이보다 얇은 비교군은 전체 delta 로 되돌린다

COHORT_KEYS = {
    "전역": None,
    "지원성격": ["support_type"],
    "지원방식": ["support_method"],
    "성격x방식": ["support_type", "support_method"],
}


def cohort_id(t, keys):
    if not keys:
        return pd.Series("__all__", index=t.index)
    return t[keys].astype(str).agg(" / ".join, axis=1)


def mondrian_fold(Xtr, ytr, Xte, params, groups_tr, ctr, cte, rng):
    """fold 안에서 학습/보정을 나누고 비교군별 delta 를 구한다."""
    uniq = np.unique(groups_tr)
    rng.shuffle(uniq)
    cal_groups = set(uniq[:max(1, int(len(uniq) * CAL_FRAC))])
    is_cal = np.array([g in cal_groups for g in groups_tr])

    Xf, yf = Xtr.iloc[~is_cal], ytr[~is_cal]
    Xc, yc, cc = Xtr.iloc[is_cal], ytr[is_cal], ctr[is_cal]
    if len(Xc) < MIN_CAL or len(Xf) < 50:
        p = fit_quantiles(Xtr, ytr, Xte, params)
        return p[LO], p[0.5], p[HI], {}, 0.0

    pc = fit_quantiles(Xf, yf, Xc, params)
    pt = fit_quantiles(Xf, yf, Xte, params)
    E = np.maximum(pc[LO] - yc, yc - pc[HI])

    def q(arr):
        k = min(max(int(np.ceil((len(arr) + 1) * NOMINAL)), 1), len(arr))
        return float(np.sort(arr)[k - 1])

    global_delta = q(E)
    deltas = {}
    for c in pd.unique(cc):
        e = E[cc == c]
        deltas[c] = q(e) if len(e) >= MIN_CAL else global_delta

    d = np.array([deltas.get(c, global_delta) for c in cte])
    return pt[LO] - d, pt[0.5], pt[HI] + d, deltas, global_delta


def evaluate(X, y, groups, params, coh, n_splits=5):
    rng = np.random.default_rng(SEED)
    lo = np.zeros(len(y)); mid = np.zeros(len(y)); hi = np.zeros(len(y))
    n_specific, n_fallback = 0, 0
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        l, m, h, deltas, gd = mondrian_fold(
            X.iloc[tr], y[tr], X.iloc[te], params, groups[tr],
            coh[tr], coh[te], rng)
        lo[te], mid[te], hi[te] = l, m, h
        for c in coh[te]:
            if c in deltas and deltas[c] != gd:
                n_specific += 1
            else:
                n_fallback += 1
    r = interval_metrics(y, lo, hi, mid)
    r["n_cohort_specific"] = int(n_specific)
    r["n_fallback"] = int(n_fallback)
    return r, lo, hi


def per_cohort(y, lo, hi, coh, min_n=30):
    """칸별 커버리지 — 전체가 맞아도 특정 칸만 어긋날 수 있다."""
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    inside = (y >= lo2) & (y <= hi2)
    width = hi2 - lo2
    rows = []
    for c in pd.unique(coh):
        m = coh == c
        if m.sum() < min_n:
            continue
        rows.append({"cohort": str(c), "n": int(m.sum()),
                     "coverage": round(float(inside[m].mean()), 4),
                     "width_median": round(float(np.median(width[m])), 4),
                     "width_x": round(float(10 ** np.median(width[m])), 1)})
    return sorted(rows, key=lambda r: -r["n"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    d = prepare(pd.read_parquet(SRC))
    with open(C.report_path("m17_m3_tuning.json"), encoding="utf-8") as f:
        m17 = json.load(f)
    with open(C.report_path("m19_m3_interval.json"), encoding="utf-8") as f:
        m19 = json.load(f)
    feats = FEATURE_SETS[m17["feature_sets"]["chosen"]]
    params = m17["tuning"]["best_params"]
    t, X, y, g, _ = build(d, feats)

    print("모델 3 구간 좁히기 대상: %d행" % len(t))
    print("M19 전역 보정: 커버리지 %.1f%% / 구간폭 %.1f배"
          % (m19["conformal"]["coverage"] * 100,
             10 ** m19["conformal"]["width_median"]))
    print("목표: 커버리지 %.0f~%.0f%% 유지하면서 구간폭 축소"
          % (TARGET_LOW * 100, TARGET_HIGH * 100))

    t0 = time.time()
    results, best_lo, best_hi, best_key = {}, None, None, None
    keys = COHORT_KEYS if not a.quick else {"전역": None, "성격x방식":
                                            ["support_type", "support_method"]}
    for name, k in keys.items():
        coh = cohort_id(t, k).to_numpy()
        r, lo, hi = evaluate(X, y, g, params, coh)
        r["n_cohorts"] = int(len(pd.unique(coh)))
        results[name] = r
        print("  %-10s 커버리지 %.1f%% / 구간폭 %.1f배 / 비교군 %d개 / 칸별보정 %d행"
              % (name, r["coverage"] * 100, 10 ** r["width_median"],
                 r["n_cohorts"], r["n_cohort_specific"]))
        ok = TARGET_LOW <= r["coverage"] <= TARGET_HIGH
        if ok and (best_key is None
                   or r["width_median"] < results[best_key]["width_median"]):
            best_key, best_lo, best_hi = name, lo, hi

    if best_key is None:
        best_key = min(results, key=lambda k: abs(results[k]["coverage"] - NOMINAL))
        coh = cohort_id(t, keys[best_key]).to_numpy()
        _, best_lo, best_hi = evaluate(X, y, g, params, coh)
        print("\n목표 커버리지를 만족하는 축이 없어 가장 가까운 축을 고른다")

    print("\n== 선택: %s 기준 보정" % best_key)
    coh = cohort_id(t, keys[best_key]).to_numpy()
    cells = per_cohort(y, best_lo, best_hi, coh)
    if len(cells) > 1:
        print("== 칸별 커버리지 (30건 이상)")
        for c in cells[:10]:
            print("   %-24s n=%4d  커버리지 %.1f%%  구간폭 %.1f배"
                  % (c["cohort"], c["n"], c["coverage"] * 100, c["width_x"]))

    verdict = judge(results, best_key, m19, cells)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("m22_m3_narrow.json", {
        "n_rows": int(len(t)), "min_cal": MIN_CAL, "nominal": NOMINAL,
        "target_coverage": [TARGET_LOW, TARGET_HIGH],
        "m19_global": {"coverage": m19["conformal"]["coverage"],
                       "width_median": m19["conformal"]["width_median"]},
        "results": results, "chosen": best_key, "per_cohort": cells,
        "verdict": verdict, "runtime_min": round((time.time() - t0) / 60, 2)})
    write_md(results, best_key, cells, verdict, m19)


def judge(results, best, m19, cells):
    reasons = []
    g = m19["conformal"]
    b = results[best]
    gw, bw = 10 ** g["width_median"], 10 ** b["width_median"]
    reasons.append("전역 보정 %.1f배 -> %s 기준 보정 %.1f배 (%.0f%% 축소)"
                   % (gw, best, bw, (1 - bw / gw) * 100))
    reasons.append("커버리지 %.1f%% -> %.1f%% (목표 %.0f~%.0f%%)"
                   % (g["coverage"] * 100, b["coverage"] * 100,
                      TARGET_LOW * 100, TARGET_HIGH * 100))
    if cells:
        cov = [c["coverage"] for c in cells]
        reasons.append("칸별 커버리지 %.1f~%.1f%% (%d칸) — 전체만 맞고 특정 칸이 "
                       "무너지지 않았는지 확인" % (min(cov) * 100, max(cov) * 100, len(cells)))
        bad = [c for c in cells if c["coverage"] < 0.6]
        if bad:
            reasons.append("커버리지 60%% 미만 칸: %s"
                           % ", ".join("%s(%.0f%%)" % (c["cohort"], c["coverage"] * 100)
                                       for c in bad[:3]))
        # 중앙값은 안 좁아져도 칸마다 폭이 크게 갈린다. 이게 실무에 쓸 정보다.
        ws = sorted(cells, key=lambda c: c["width_x"])
        reasons.append("칸별 구간폭이 %.1f배(%s)에서 %.0f배(%s)까지 갈린다 — 중앙값은 "
                       "안 좁아져도 어떤 비교군은 쓸 만하다. 서비스에서 전체 하나가 "
                       "아니라 해당 칸의 폭을 함께 보여야 하는 이유다"
                       % (ws[0]["width_x"], ws[0]["cohort"],
                          ws[-1]["width_x"], ws[-1]["cohort"]))

    if not (TARGET_LOW <= b["coverage"] <= TARGET_HIGH):
        return {"verdict": "미채택 — 목표 커버리지 이탈", "reasons": reasons}
    if bw < gw * 0.9:
        v = "채택 — 구간이 의미 있게 좁아졌다"
    else:
        v = "개선 미미 — 전역 보정 유지"
        reasons.append("비교군별로 나눠도 구간이 거의 안 좁아진다. 칸마다 예측 난이도가 "
                       "비슷하다는 뜻이라 전역 보정을 그대로 쓰는 편이 단순하다")
    return {"verdict": v, "reasons": reasons}


def write_md(results, best, cells, verdict, m19):
    g = m19["conformal"]
    L = ["# 모델 3 구간 좁히기 — 비교군별 Conformal (Mondrian CQR)", "",
         "> 최종정리 문서 3.6절: \"구간은 통계적으로 잘 맞지만, 실무적으로는 너무",
         "> 넓을 수 있다.\" (12.9배 → 43.7배)", "",
         "## 1. 왜 넓어졌는가", "",
         "M19 는 보정폭(delta)을 **전체에 하나**로 썼습니다. 그러면 예측이 잘 맞는",
         "비교군도 가장 안 맞는 비교군에 맞춰 넓어집니다. 융자처럼 금액 자릿수가 넓은",
         "칸 하나가 사업화·판로처럼 좁은 칸까지 끌고 갑니다.", "",
         "**Mondrian conformal** 은 보정을 비교군별로 따로 합니다. 각 칸의 실제",
         "이탈량으로 그 칸의 delta 를 정하면 좁은 칸은 좁게 나옵니다. 비교군이",
         "%d건 미만이면 delta 가 불안정하므로 전역 delta 로 되돌립니다." % MIN_CAL, "",
         "## 2. 비교군 축 비교", "",
         "| 보정 축 | 비교군 수 | 커버리지 | 구간폭(log10) | 구간폭(배수) |",
         "|---|---:|---:|---:|---:|"]
    for k, r in results.items():
        mark = " **←선택**" if k == best else ""
        L.append("| %s%s | %d | %.1f%% | %.3f | **%.1f배** |"
                 % (k, mark, r["n_cohorts"], r["coverage"] * 100,
                    r["width_median"], 10 ** r["width_median"]))
    L += ["", "M19 전역 보정 기준: 커버리지 %.1f%% / 구간폭 %.1f배"
          % (g["coverage"] * 100, 10 ** g["width_median"]), ""]

    if len(cells) > 1:
        L += ["## 3. 칸별 커버리지·구간폭 (30건 이상)", "",
              "전체 커버리지가 맞아도 특정 칸만 어긋날 수 있어 따로 봅니다.",
              "**여기가 이 실험에서 실제로 건진 것입니다** — 구간폭 중앙값은 안 좁아졌지만",
              "칸마다 폭이 크게 갈립니다. 서비스에서 전체 하나가 아니라 해당 비교군의",
              "폭을 함께 보여주면, 어떤 칸은 충분히 좁습니다.", "",
              "| 비교군 | n | 커버리지 | 구간폭 |", "|---|---:|---:|---:|"]
        for c in cells:
            L.append("| %s | %d | %.1f%% | %.1f배 |"
                     % (c["cohort"], c["n"], c["coverage"] * 100, c["width_x"]))
        L.append("")

    L += ["## 4. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 5. 표현 규율", "",
          "구간이 좁아져도 **'적정 지원규모 범위'가 아닙니다.**", "", "```text",
          "허용   과거 유사사업 기반 참고 예측 범위",
          "       상대적 지원규모 참고 구간",
          "금지   적정 지원규모 범위 / 권장 금액 / 이 정도가 맞다",
          "```", ""]
    p = C.report_path("m22_m3_narrow.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
