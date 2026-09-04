"""M19 — 모델 3 예측구간 신뢰도: Conformal Calibration · Pinball · 연도 hold-out.

추가개선계획서 6절. 핵심 문장을 그대로 옮긴다.

    "MAE를 0.50에서 조금 더 낮추는 것보다, 예측구간이 실제로 신뢰 가능한지가
     더 중요하다."

M17 이 남긴 문제
    LGBM quantile 의 P10~P90 구간이 실제로는 61.0% 만 덮는다(명목 80%).
    MLP quantile 은 79.0% 로 훨씬 낫다. 점추정은 LGBM, 구간은 MLP 가 낫다는
    엇갈린 상태다.

여기서 하는 것
    1. 역할 분리 (6.1절)   점추정 LGBM / 구간 별도 — 한 모델이 다 잘할 필요 없다
    2. Conformal (6.2절)   분위 예측을 보정해 명목 80%를 실제 80%에 맞춘다
    3. 평가 지표 확장 (6.3절) Pinball / Coverage / Width / MedAE / Fold Variance
    4. 연도 hold-out (6.4절)  과거 -> 최근 으로 갈라 일반화 확인

Conformalized Quantile Regression (CQR)
    분위 모델을 학습셋에서 적합한 뒤, 따로 떼어둔 보정셋에서 실제 오차를 재
    구간을 그만큼 넓히거나 좁힌다. 분포 가정이 필요 없고, 보정셋이 교환가능하면
    목표 커버리지를 유한표본에서 보장한다.
        E_i = max(q_lo(x_i) - y_i,  y_i - q_hi(x_i))
        구간 = [q_lo - Q_{1-a}(E),  q_hi + Q_{1-a}(E)]

주의 (계획서 7절)
    이 구간을 '적정 지원규모'라고 부르지 않는다.
    "과거 유사사업 조건을 기반으로 한 상대적 예측 범위"로만 쓴다.
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
from lightgbm import LGBMRegressor
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import SRC, prepare
from m17_m3_tuning import FEATURE_SETS, build

SEED = 42
NOMINAL = 0.80          # 명목 구간 (P10~P90)
LO, HI = 0.1, 0.9
TARGET_LOW, TARGET_HIGH = 0.78, 0.82   # 계획서 6.2절 목표 실제 커버리지
CAL_FRAC = 0.3          # 학습셋에서 보정용으로 떼어낼 비율


def pinball(y, pred, q):
    e = y - pred
    return float(np.maximum(q * e, (q - 1) * e).mean())


def interval_metrics(y, lo, hi, mid=None):
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    out = {
        "coverage": round(float(((y >= lo2) & (y <= hi2)).mean()), 4),
        "width_median": round(float(np.median(hi2 - lo2)), 4),
        "width_mean": round(float((hi2 - lo2).mean()), 4),
        "crossing_rate": round(float((lo > hi).mean()), 4),
        "pinball_lo": round(pinball(y, lo, LO), 5),
        "pinball_hi": round(pinball(y, hi, HI), 5),
    }
    if mid is not None:
        err = np.abs(mid - y)
        out.update(MAE_log10=round(float(err.mean()), 4),
                   MedAE_log10=round(float(np.median(err)), 4),
                   pinball_mid=round(pinball(y, mid, 0.5), 5))
    return out


def fit_quantiles(Xtr, ytr, Xte, params):
    """세 분위를 각각 학습한다."""
    out = {}
    for q in (LO, 0.5, HI):
        m = LGBMRegressor(objective="quantile", alpha=q, random_state=SEED,
                          verbose=-1, **params).fit(Xtr, ytr)
        out[q] = m.predict(Xte)
    return out


def cqr_fold(Xtr, ytr, Xte, params, groups_tr, rng):
    """한 fold 안에서 학습셋을 다시 학습/보정으로 나눠 CQR 을 적용한다.

    보정셋도 그룹 단위로 떼어낸다. 같은 사업의 재공고가 학습과 보정에 갈라지면
    보정 오차가 낙관적으로 잡혀 구간이 실제보다 좁아진다.
    """
    uniq = np.unique(groups_tr)
    rng.shuffle(uniq)
    n_cal = max(1, int(len(uniq) * CAL_FRAC))
    cal_groups = set(uniq[:n_cal])
    is_cal = np.array([g in cal_groups for g in groups_tr])

    Xf, yf = Xtr.iloc[~is_cal], ytr[~is_cal]
    Xc, yc = Xtr.iloc[is_cal], ytr[is_cal]
    if len(Xc) < 20 or len(Xf) < 50:
        p = fit_quantiles(Xtr, ytr, Xte, params)
        return p[LO], p[0.5], p[HI], 0.0

    pc = fit_quantiles(Xf, yf, Xc, params)
    pt = fit_quantiles(Xf, yf, Xte, params)
    # 보정셋에서 구간 밖으로 얼마나 벗어났는지 (conformity score)
    E = np.maximum(pc[LO] - yc, yc - pc[HI])
    k = int(np.ceil((len(E) + 1) * NOMINAL))
    k = min(max(k, 1), len(E))
    delta = float(np.sort(E)[k - 1])
    return pt[LO] - delta, pt[0.5], pt[HI] + delta, delta


def evaluate(X, y, groups, params, n_splits=5, conformal=False):
    rng = np.random.default_rng(SEED)
    lo = np.zeros(len(y))
    mid = np.zeros(len(y))
    hi = np.zeros(len(y))
    deltas = []
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        if conformal:
            l, m, h, d = cqr_fold(X.iloc[tr], y[tr], X.iloc[te], params,
                                  groups[tr], rng)
            deltas.append(d)
        else:
            p = fit_quantiles(X.iloc[tr], y[tr], X.iloc[te], params)
            l, m, h = p[LO], p[0.5], p[HI]
        lo[te], mid[te], hi[te] = l, m, h
    r = interval_metrics(y, lo, hi, mid)
    if deltas:
        r["conformal_delta_mean"] = round(float(np.mean(deltas)), 4)
    return r


def year_holdout(d, feats, params, cutoff=None):
    """계획서 6.4절 — 과거로 학습해 최근을 맞힌다.

    모델 3 은 시계열 모델이 아니지만, 연도별 정책·금액 분포가 바뀌면
    과거로 학습한 모델이 최근 사업에서 무너질 수 있다. 그것만 확인한다.
    """
    t, X, y, g, _ = build(d, feats)
    yr = t["year"].astype(float)
    ok = yr.notna()
    if ok.sum() < 200:
        return {"status": "연도 결측이 많아 불가", "n_with_year": int(ok.sum())}
    years = sorted(yr[ok].unique())
    if cutoff is None:
        # 최근 연도가 전체의 15% 이상이 되도록 자른다
        for c in reversed(years):
            if (yr >= c).sum() >= max(150, 0.15 * ok.sum()):
                cutoff = c
                break
    if cutoff is None:
        return {"status": "테스트 연도 표본 부족", "years": [float(v) for v in years]}

    tr = (yr < cutoff).to_numpy() & ok.to_numpy()
    te = (yr >= cutoff).to_numpy() & ok.to_numpy()
    if tr.sum() < 200 or te.sum() < 100:
        return {"status": "분할 표본 부족", "train": int(tr.sum()), "test": int(te.sum())}

    p = fit_quantiles(X.iloc[tr], y[tr], X.iloc[te], params)
    r = interval_metrics(y[te], p[LO], p[HI], p[0.5])
    # 연도 분할이 곧 출처 분할이 되는지 확인한다. 2026 은 Open API 전량이라
    # 연도가 바뀐 것이 아니라 모집단이 바뀐 것일 수 있다.
    r["train_cohort"] = {str(k): int(v) for k, v
                         in t.loc[tr, "cohort"].value_counts().items()}
    r["test_cohort"] = {str(k): int(v) for k, v
                        in t.loc[te, "cohort"].value_counts().items()}
    # 같은 테스트 구간에 대한 코호트 중앙값 기준선
    base = np.median(y[tr])
    r.update(cutoff_year=float(cutoff), n_train=int(tr.sum()), n_test=int(te.sum()),
             baseline_MAE=round(float(np.abs(base - y[te]).mean()), 4))
    r["vs_baseline"] = round(float((r["baseline_MAE"] - r["MAE_log10"])
                                   / r["baseline_MAE"]), 4)
    return r


def load_mlp():
    """딥러닝 MLP quantile 결과가 있으면 비교표에 함께 싣는다."""
    for n in ("dl09_m3_cohort.json", "m17_m3_tuning.json"):
        p = C.report_path(n)
        if n.startswith("dl09") and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            t = d.get("tuned_result", {})
            if "p10_p90_coverage" in t:
                return {"MAE_log10": t.get("MAE_log10"),
                        "coverage": t.get("p10_p90_coverage")}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    d = prepare(pd.read_parquet(SRC))
    with open(C.report_path("m17_m3_tuning.json"), encoding="utf-8") as f:
        m17 = json.load(f)
    fs_name = m17["feature_sets"]["chosen"]
    params = m17["tuning"]["best_params"]
    feats = FEATURE_SETS[fs_name]
    t, X, y, g, _ = build(d, feats)

    print("모델 3 예측구간 대상: %d행 / feature set '%s'" % (len(t), fs_name))
    print("M17: MAE %.4f / P10~P90 포함률 61.0%% (명목 80%%)"
          % m17["tuning"]["best_result"]["MAE_log10"])
    print("계획서 목표: 명목 80%% 구간의 실제 커버리지 78~82%%")

    t0 = time.time()
    out = {}

    print("\n== 1. 보정 전 (M17 그대로)")
    raw = evaluate(X, y, g, params)
    out["raw"] = raw
    show(raw)

    print("\n== 2. Conformal 보정 후 (계획서 6.2절)")
    cqr = evaluate(X, y, g, params, conformal=True)
    out["conformal"] = cqr
    show(cqr)
    print("   보정폭(delta) 평균 %.4f log10 = %.2f배"
          % (cqr.get("conformal_delta_mean", 0),
             10 ** cqr.get("conformal_delta_mean", 0)))

    print("\n== 3. 연도 hold-out (계획서 6.4절)")
    yh = year_holdout(d, feats, params)
    out["year_holdout"] = yh
    if "MAE_log10" in yh:
        print("   %d년 이전 %d행 학습 -> %d년 이후 %d행 테스트"
              % (yh["cutoff_year"], yh["n_train"], yh["cutoff_year"], yh["n_test"]))
        print("   MAE %.4f (같은 구간 중앙값 기준선 %.4f, %+.1f%%)"
              % (yh["MAE_log10"], yh["baseline_MAE"], yh["vs_baseline"] * 100))
        print("   구간 커버리지 %.1f%%" % (yh["coverage"] * 100))
    else:
        print("   %s" % yh.get("status"))

    mlp = load_mlp()
    out["mlp_reference"] = mlp
    verdict = judge(raw, cqr, yh, mlp)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    out.update({"feature_set": fs_name, "params": params,
                "nominal": NOMINAL, "target_coverage": [TARGET_LOW, TARGET_HIGH],
                "cal_frac": CAL_FRAC, "verdict": verdict,
                "runtime_min": round((time.time() - t0) / 60, 2)})
    C.save_report("m19_m3_interval.json", out)
    write_md(out, verdict, m17)


def show(r):
    print("   MAE %.4f / MedAE %.4f / 커버리지 %.1f%% / 구간폭 %.3f / pinball(mid) %.5f"
          % (r["MAE_log10"], r["MedAE_log10"], r["coverage"] * 100,
             r["width_median"], r["pinball_mid"]))


def judge(raw, cqr, yh, mlp):
    reasons, v = [], "부분 개선"
    reasons.append("보정 전 커버리지 %.1f%% -> 보정 후 %.1f%% (명목 %.0f%%)"
                   % (raw["coverage"] * 100, cqr["coverage"] * 100, NOMINAL * 100))
    if TARGET_LOW <= cqr["coverage"] <= TARGET_HIGH:
        v = "채택"
        reasons.append("계획서 목표 구간(%.0f~%.0f%%) 안에 들어왔다"
                       % (TARGET_LOW * 100, TARGET_HIGH * 100))
    elif cqr["coverage"] > TARGET_HIGH:
        reasons.append("목표를 넘겼다 — 구간이 필요 이상으로 넓다는 뜻이라 "
                       "보수적이지만 쓸 수는 있다")
        v = "채택(보수적)"
    else:
        reasons.append("아직 목표에 못 미친다")
    w = 10 ** cqr["width_median"]
    reasons.append("구간폭 %.3f -> %.3f log10 (%.1f배 -> %.1f배)"
                   % (raw["width_median"], cqr["width_median"],
                      10 ** raw["width_median"], w))
    if w > 20:
        reasons.append("다만 %.0f배 구간은 실무에서 좁다고 말하기 어렵다. 커버리지를 "
                       "맞춘 대가이며, 좁히려면 비교군을 더 잘게 나누거나 feature 를 "
                       "늘려야 한다" % w)
    reasons.append("점추정은 보정과 무관하다 — MAE %.4f 그대로" % cqr["MAE_log10"])
    if mlp:
        reasons.append("참고: 딥러닝 MLP quantile 커버리지 %.1f%% / MAE %.4f"
                       % (mlp["coverage"] * 100, mlp["MAE_log10"]))
    if "MAE_log10" in yh:
        reasons.append("연도 hold-out(%d년 기준) MAE %.4f, 같은 구간 기준선 대비 %+.1f%%"
                       % (yh["cutoff_year"], yh["MAE_log10"], yh["vs_baseline"] * 100))
        tc, rc = yh.get("train_cohort", {}), yh.get("test_cohort", {})
        if set(tc) != set(rc):
            reasons.append("주의: 연도 분할이 출처 분할과 겹친다 (학습 %s / 테스트 %s). "
                           "연도 일반화가 아니라 모집단 차이를 잰 것에 가깝다"
                           % (tc, rc))
    return {"verdict": v, "reasons": reasons}


def write_md(out, verdict, m17):
    raw, cqr, yh = out["raw"], out["conformal"], out["year_holdout"]
    L = ["# 모델 3 예측구간 신뢰도 — Conformal · Pinball · 연도 hold-out", "",
         "> 계획서 6.2절: \"MAE를 0.50에서 조금 더 낮추는 것보다, 예측구간이 실제로",
         "> 신뢰 가능한지가 더 중요하다.\"", "",
         "## 1. 문제", "",
         "M17 의 LGBM quantile 은 명목 80% 구간(P10~P90)이 실제로 **61.0%** 만",
         "덮었습니다. 구간을 그대로 내면 5건 중 2건이 범위 밖으로 떨어집니다.", "",
         "## 2. Conformal 보정 (계획서 6.2절)", "",
         "분위 모델을 학습셋에서 적합한 뒤, **따로 떼어둔 보정셋**에서 실제로 얼마나",
         "벗어났는지 재어 구간을 그만큼 넓힙니다. 분포 가정이 필요 없습니다.", "",
         "```text",
         "E_i   = max(q_lo(x_i) - y_i,  y_i - q_hi(x_i))     보정셋 이탈량",
         "delta = E 의 %.0f%% 분위수" % (NOMINAL * 100),
         "구간  = [q_lo - delta,  q_hi + delta]",
         "```", "",
         "보정셋도 **그룹 단위**로 뗐습니다. 같은 사업의 재공고가 학습과 보정에",
         "갈라지면 이탈량이 낙관적으로 잡혀 구간이 실제보다 좁아집니다.", "",
         "| 지표 | 보정 전 | 보정 후 |", "|---|---:|---:|",
         "| **커버리지** (명목 80%%) | **%.1f%%** | **%.1f%%** |"
         % (raw["coverage"] * 100, cqr["coverage"] * 100),
         "| 구간폭 중앙값 (log10) | %.3f | %.3f |"
         % (raw["width_median"], cqr["width_median"]),
         "| 구간폭 (배수) | %.1f배 | %.1f배 |"
         % (10 ** raw["width_median"], 10 ** cqr["width_median"]),
         "| MAE(log10) | %.4f | %.4f |" % (raw["MAE_log10"], cqr["MAE_log10"]),
         "| MedAE(log10) | %.4f | %.4f |" % (raw["MedAE_log10"], cqr["MedAE_log10"]),
         "| Pinball (P10) | %.5f | %.5f |" % (raw["pinball_lo"], cqr["pinball_lo"]),
         "| Pinball (P50) | %.5f | %.5f |" % (raw["pinball_mid"], cqr["pinball_mid"]),
         "| Pinball (P90) | %.5f | %.5f |" % (raw["pinball_hi"], cqr["pinball_hi"]),
         "| 분위 교차율 | %.2f%% | %.2f%% |"
         % (raw["crossing_rate"] * 100, cqr["crossing_rate"] * 100), "",
         "보정폭 delta 평균 **%.4f log10 (%.2f배)**."
         % (cqr.get("conformal_delta_mean", 0),
            10 ** cqr.get("conformal_delta_mean", 0)), "",
         "> **점추정은 보정의 영향을 받지 않습니다.** 구간 하한·상한만 옮기므로",
         "> MAE 는 그대로입니다. 계획서 6.1절의 역할 분리가 여기서 성립합니다 —",
         "> 점추정은 LGBM, 구간은 보정된 LGBM 이 담당합니다.", ""]

    mlp = out.get("mlp_reference")
    if mlp:
        L += ["### 딥러닝 MLP quantile 과 비교", "",
              "| 모델 | MAE | 커버리지 |", "|---|---:|---:|",
              "| LGBM (보정 전) | %.4f | %.1f%% |" % (raw["MAE_log10"], raw["coverage"] * 100),
              "| **LGBM + Conformal** | **%.4f** | **%.1f%%** |"
              % (cqr["MAE_log10"], cqr["coverage"] * 100),
              "| MLP quantile (DL) | %.4f | %.1f%% |"
              % (mlp["MAE_log10"], mlp["coverage"] * 100), ""]

    L += ["## 3. 연도 hold-out (계획서 6.4절)", ""]
    if "MAE_log10" in yh:
        L += ["과거로 학습해 최근을 맞힙니다. 모델 3 은 시계열 모델이 아니지만",
              "연도별 정책·금액 분포가 바뀌면 무너질 수 있어 그것만 확인합니다.", "",
              "```text",
              "%d년 이전 %d행 학습 -> %d년 이후 %d행 테스트"
              % (yh["cutoff_year"], yh["n_train"], yh["cutoff_year"], yh["n_test"]),
              "```", "",
              "| 지표 | 값 |", "|---|---:|",
              "| MAE(log10) | %.4f |" % yh["MAE_log10"],
              "| 같은 구간 중앙값 기준선 | %.4f |" % yh["baseline_MAE"],
              "| 기준선 대비 | %+.1f%% |" % (yh["vs_baseline"] * 100),
              "| 구간 커버리지 | %.1f%% |" % (yh["coverage"] * 100), ""]
        tc, rc = yh.get("train_cohort", {}), yh.get("test_cohort", {})
        if tc and rc:
            L += ["학습 코호트 %s / 테스트 코호트 %s" % (tc, rc), ""]
            if set(tc) != set(rc):
                L += ["> **이 결과는 연도 일반화로 읽으면 안 됩니다.** 분할선이 출처",
                      "> 경계와 겹칩니다 — 2026 은 Open API 전량이라 연도가 바뀐 것이",
                      "> 아니라 모집단이 통째로 바뀐 것입니다. taxonomy(2023 중앙부처)로",
                      "> 학습해 bizinfo(공고 원문)를 맞히는 셈이고, 두 모집단의 중앙값이",
                      "> 칸에 따라 최대 40배 갈린다는 것은 M12 에서 이미 확인했습니다.",
                      "> 연도 축만 따로 보려면 같은 출처 안에서 갈라야 합니다.", ""]
    else:
        L += ["수행 불가: %s" % yh.get("status"), ""]

    L += ["## 4. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 5. 표현 규율 (계획서 7절)", "",
          "이 구간을 **'적정 지원규모'라고 부르지 않습니다.**", "",
          "```text",
          "허용   과거 유사사업 조건을 기반으로 한 상대적 예측 범위",
          "금지   적정 지원규모 / 권장 금액 / 이 정도가 맞다",
          "```", ""]
    p = C.report_path("m19_m3_interval.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
