"""M17 — 모델 3 성능 개선: Feature 확장 · LightGBM 튜닝 · Quantile 확장.

개선계획서 Step 3 을 실행한다.
    Parser 고도화 -> Feature 확장 -> LightGBM 튜닝 -> Quantile 확장 -> Baseline 재비교

계획서가 이미 반영된 것 두 가지는 다시 하지 않는다.
    3절 지원방식 분리 — M12 에서 비교군 축으로 이미 갈랐다(grant/loan/…)
    4절 지원단위 분리 — M12 에서 후퇴 불가 축으로 고정했다(기업/과제/인/팀)
                        갈랐더니 company 5,000만원 vs project 2,750만원으로 벌어졌다

여기서 새로 하는 것
    1. Parser 품질 평가 (계획서 8절) — 정답 없이 할 수 있는 만큼
       수동 정답 200~300건은 사람 손이 필요하다. 대신 파서가 스스로
       확신한 정도(extraction_confidence)와 산출 경로별 분포를 대조해
       어느 경로가 미덥지 않은지 지목한다.
    2. Feature 확장 (계획서 5절) — support_target·industry·year 등 추가
    3. LightGBM 튜닝 (계획서 6절) — Optuna 가 없으면 랜덤 서치로 대체
    4. Quantile 확장 (계획서 7절) — Q10/Q50/Q90 을 함께 학습해 구간 포함률 측정

기준선 (M12 Phase B, GroupKFold(5) by program_stem, n=2,205)
    전체중앙값                0.8600
    코호트중앙값(baseline)     0.6790
    LGBM-quantile50           0.5160   <- 이걸 넘어야 개선이다
계획서 목표: MAE 0.48~0.50 / baseline 대비 20% 이상 유지
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
import os
import sys
import time

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import MIN_IMPROVEMENT, SRC, prepare

SEED = 42
BASELINE_COHORT = 0.6790
BASELINE_ML = 0.5160
TARGET_MAE = 0.50
QUANTILES = [0.1, 0.5, 0.9]

# 계획서 5절 — 단계적으로 넓힌다. 넓힐 때마다 기준선 대비 개선을 확인한다.
FEATURE_SETS = {
    "현재(M12)": {
        "cat": ["support_type", "support_method", "support_unit", "category_large",
                "industry_grp", "agency_type", "amount_type"],
        "num": ["support_count", "support_ratio", "project_duration"],
    },
    "+연도": {
        "cat": ["support_type", "support_method", "support_unit", "category_large",
                "industry_grp", "agency_type", "amount_type"],
        "num": ["support_count", "support_ratio", "project_duration", "year"],
    },
    "+자부담·추출신뢰도": {
        "cat": ["support_type", "support_method", "support_unit", "category_large",
                "industry_grp", "agency_type", "amount_type", "per_recipient_basis"],
        "num": ["support_count", "support_ratio", "project_duration", "year",
                "self_burden_ratio", "extraction_confidence"],
    },
    "+지원대상길이": {
        "cat": ["support_type", "support_method", "support_unit", "category_large",
                "industry_grp", "agency_type", "amount_type", "per_recipient_basis"],
        "num": ["support_count", "support_ratio", "project_duration", "year",
                "self_burden_ratio", "extraction_confidence", "target_len"],
    },
}

# 계획서 6절 튜닝 대상. Optuna 미설치 환경이라 시드 고정 랜덤 서치로 같은 공간을 훑는다.
SPACE = {
    "num_leaves": [7, 15, 31, 63],
    "max_depth": [-1, 4, 6, 8],
    "min_child_samples": [5, 10, 20, 40],
    "learning_rate": [0.02, 0.05, 0.1],
    "n_estimators": [200, 400, 800],
    "colsample_bytree": [0.6, 0.8, 1.0],
    "subsample": [0.6, 0.8, 1.0],
    "reg_alpha": [0.0, 0.1, 1.0],
    "reg_lambda": [0.0, 0.1, 1.0],
}


def build(d, feats):
    t = d[d["per_recipient"].notna() & (d["per_recipient"] > 0)].copy()
    t["y"] = np.log10(t["per_recipient"])
    t["target_len"] = t["support_target"].fillna("").str.len()
    cats, nums = feats["cat"], feats["num"]
    for c in cats:
        t[c] = t[c].fillna("미기재").astype("category")
    X = t[cats + nums]
    groups = t["program_stem"].fillna(t["title"]).astype(str).to_numpy()
    return t, X, t["y"].to_numpy(), groups, cats


def cv(X, y, groups, params, quantile=None, n_splits=5):
    """fold 별 MAE 를 함께 돌려준다 — 계획서 6절의 Fold Variance."""
    pred = np.zeros(len(y))
    fold_mae = []
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        kw = dict(params)
        if quantile is not None:
            kw.update(objective="quantile", alpha=quantile)
        m = LGBMRegressor(random_state=SEED, verbose=-1, **kw).fit(X.iloc[tr], y[tr])
        pred[te] = m.predict(X.iloc[te])
        fold_mae.append(float(np.abs(pred[te] - y[te]).mean()))
    err = np.abs(pred - y)
    return {
        "MAE_log10": round(float(err.mean()), 4),
        "MedAE_log10": round(float(np.median(err)), 4),
        "RMSE_log10": round(float(np.sqrt(((pred - y) ** 2).mean())), 4),
        "geo_mean_error_x": round(float(10 ** err.mean()), 3),
        "within_2x": round(float((err <= np.log10(2)).mean()), 4),
        "fold_mae": [round(v, 4) for v in fold_mae],
        "fold_std": round(float(np.std(fold_mae)), 4),
    }, pred


def quantile_band(X, y, groups, params, n_splits=5):
    """Q10/Q50/Q90 을 각각 학습해 구간 포함률을 잰다 (계획서 7절).

    모델 3의 출력은 점추정이 아니라 분포다. 구간이 실제로 80%를 덮는지 재지 않으면
    "과거 유사사업 조건을 학습한 예측 분포"라고 말할 근거가 없다.
    """
    P = {q: np.zeros(len(y)) for q in QUANTILES}
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        for q in QUANTILES:
            m = LGBMRegressor(objective="quantile", alpha=q, random_state=SEED,
                              verbose=-1, **params).fit(X.iloc[tr], y[tr])
            P[q][te] = m.predict(X.iloc[te])
    lo, mid, hi = P[0.1], P[0.5], P[0.9]
    # 분위가 서로 넘나들면 구간이 뒤집힌다. 정렬해 하한<=상한을 보장한다.
    lo2, hi2 = np.minimum(lo, hi), np.maximum(lo, hi)
    return {
        "coverage_p10_p90": round(float(((y >= lo2) & (y <= hi2)).mean()), 4),
        "band_width_log10_median": round(float(np.median(hi2 - lo2)), 4),
        "crossing_rate": round(float((lo > hi).mean()), 4),
        "MAE_q50": round(float(np.abs(mid - y).mean()), 4),
    }


def parser_quality(d):
    """계획서 8절 — 수동 정답 없이 잴 수 있는 파서 신뢰도.

    200~300건 수동 라벨링은 사람 손이 필요해 여기서 못 한다. 대신 파서가
    스스로 매긴 확신도와 산출 경로별 분포를 대조해 '어느 경로가 미덥지 않은가'를
    지목한다. 사람이 검수할 때 어디부터 볼지 정하는 데 쓴다.
    """
    t = d[d["per_recipient"].notna() & (d["per_recipient"] > 0)].copy()
    out = {"n": int(len(t))}
    for key in ("amount_type_source", "per_recipient_basis", "cohort"):
        if key not in t:
            continue
        g = t.groupby(key)["per_recipient"]
        out[key] = {str(k): {"n": int(v), "median": float(g.median()[k])}
                    for k, v in g.size().items()}
    if "extraction_confidence" in t:
        c = t["extraction_confidence"].dropna()
        out["extraction_confidence"] = {
            "n": int(len(c)), "mean": round(float(c.mean()), 3),
            "low_below_0.5": int((c < 0.5).sum()),
            "share_low": round(float((c < 0.5).mean()), 4)}
    # 상식 범위 밖으로 플래그된 금액 — 파서가 틀린 것이 확실한 표본
    out["amount_outlier_flagged"] = int(d["amount_outlier"].sum())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    trials = 8 if a.quick else a.trials

    d = prepare(pd.read_parquet(SRC))
    print("모델 3 튜닝 대상")
    print("기준선(M12): 코호트중앙값 %.4f / LGBM-quantile50 %.4f"
          % (BASELINE_COHORT, BASELINE_ML))

    t0 = time.time()
    out = {"parser_quality": parser_quality(d)}
    pq = out["parser_quality"]
    print("\n== 1. Parser 품질 (계획서 8절 — 수동 정답 없이 가능한 범위)")
    print("  금액 확보 %d행 / 상식범위 밖 플래그 %d건"
          % (pq["n"], pq["amount_outlier_flagged"]))
    if "extraction_confidence" in pq:
        e = pq["extraction_confidence"]
        print("  추출 확신도 평균 %.2f / 0.5 미만 %d건 (%.1f%%)"
              % (e["mean"], e["low_below_0.5"], e["share_low"] * 100))
    for k in ("amount_type_source", "per_recipient_basis"):
        if k in pq:
            print("  %-22s %s" % (k, {kk: v["n"] for kk, v in pq[k].items()}))

    # ---- 2. Feature 확장 -------------------------------------------------
    print("\n== 2. Feature 확장 (계획서 5절)")
    base_params = {"num_leaves": 15, "learning_rate": 0.05, "n_estimators": 400,
                   "min_child_samples": 10}
    fsets = {}
    for name, feats in FEATURE_SETS.items():
        t, X, y, g, _ = build(d, feats)
        r, _ = cv(X, y, g, base_params, quantile=0.5)
        fsets[name] = {"n": int(len(t)), "n_features": int(X.shape[1]), **r}
        print("  %-20s MAE %.4f (fold σ %.4f) / 2배이내 %.1f%%"
              % (name, r["MAE_log10"], r["fold_std"], r["within_2x"] * 100))
    best_fs = min(fsets, key=lambda k: fsets[k]["MAE_log10"])
    print("  -> 선택: %s" % best_fs)
    out["feature_sets"] = {"results": fsets, "chosen": best_fs}

    # ---- 3. LightGBM 튜닝 -------------------------------------------------
    print("\n== 3. LightGBM 튜닝 (계획서 6절, 랜덤 서치 %d회)" % trials)
    t, X, y, g, _ = build(d, FEATURE_SETS[best_fs])
    rng = np.random.default_rng(SEED)
    tried = []
    best = {"params": base_params, "result": fsets[best_fs]}
    for i in range(trials):
        p = {k: v[int(rng.integers(len(v)))] for k, v in SPACE.items()}
        r, _ = cv(X, y, g, p, quantile=0.5)
        tried.append({"params": p, "MAE_log10": r["MAE_log10"],
                      "fold_std": r["fold_std"]})
        if r["MAE_log10"] < best["result"]["MAE_log10"]:
            best = {"params": p, "result": r}
            print("  [%2d] MAE %.4f (fold σ %.4f)  <- 갱신"
                  % (i + 1, r["MAE_log10"], r["fold_std"]))
    print("  최종 MAE %.4f" % best["result"]["MAE_log10"])
    out["tuning"] = {"n_trials": trials, "trials": tried,
                     "best_params": best["params"], "best_result": best["result"]}

    # ---- 4. Quantile 확장 -------------------------------------------------
    print("\n== 4. Quantile 확장 (계획서 7절)")
    qb = quantile_band(X, y, g, best["params"])
    print("  P10~P90 포함률 %.1f%% / 구간폭 중앙값 %.3f(log10) / 분위 교차 %.2f%%"
          % (qb["coverage_p10_p90"] * 100, qb["band_width_log10_median"],
             qb["crossing_rate"] * 100))
    out["quantile_band"] = qb

    # ---- 5. 판정 ----------------------------------------------------------
    mae = best["result"]["MAE_log10"]
    imp_cohort = (BASELINE_COHORT - mae) / BASELINE_COHORT
    imp_ml = (BASELINE_ML - mae) / BASELINE_ML
    verdict = judge(mae, imp_cohort, imp_ml, best["result"], qb)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    out.update({"baseline": {"cohort_median": BASELINE_COHORT, "lgbm_q50": BASELINE_ML},
                "target_mae": TARGET_MAE,
                "improvement_vs_cohort_median": round(float(imp_cohort), 4),
                "improvement_vs_m12": round(float(imp_ml), 4),
                "verdict": verdict,
                "runtime_min": round((time.time() - t0) / 60, 2)})
    C.save_report("m17_m3_tuning.json", out)
    write_md(out, fsets, best, qb, verdict, imp_cohort, imp_ml)


def judge(mae, imp_cohort, imp_ml, res, qb):
    reasons = []
    reasons.append("MAE %.4f (계획서 목표 %.2f 이하)" % (mae, TARGET_MAE))
    reasons.append("코호트중앙값 대비 %+.1f%% (계획서: 20%% 이상 유지)" % (imp_cohort * 100))
    reasons.append("M12 대비 %+.1f%%" % (imp_ml * 100))
    reasons.append("fold 간 MAE 표준편차 %.4f — 폴드마다 성능이 흔들리는 정도"
                   % res["fold_std"])
    reasons.append("P10~P90 포함률 %.1f%% (분포 출력의 신뢰도)"
                   % (qb["coverage_p10_p90"] * 100))
    if imp_ml >= MIN_IMPROVEMENT:
        v = "개선 — 채택"
    elif imp_ml > 0 and imp_cohort >= 0.20:
        v = "소폭 개선 — 채택 (기준선 대비 20% 유지)"
    else:
        v = "개선 없음 — M12 설정 유지"
        reasons.append("튜닝으로 M12 를 의미 있게 넘지 못했다")
    return {"verdict": v, "reasons": reasons}


def write_md(out, fsets, best, qb, verdict, imp_cohort, imp_ml):
    pq = out["parser_quality"]
    L = ["# 모델 3 성능 개선 — Feature 확장 · LightGBM 튜닝 · Quantile 확장", "",
         "## 0. 계획서에서 이미 반영된 항목", "",
         "| 계획서 | 상태 |", "|---|---|",
         "| 3절 지원방식 분리 (grant/loan/…) | M12 에서 비교군 축으로 이미 분리 |",
         "| 4절 지원단위 분리 (기업/과제/인/팀) | M12 에서 **후퇴 불가 축**으로 고정 |", "",
         "지원단위를 가른 실측 근거: company 1,757건 중앙값 5,000만원 vs",
         "project 148건 7,750만원 vs person 28건 2,325만원.", "",
         "## 1. Parser 품질 (계획서 8절)", "",
         "수동 정답 200~300건은 사람 손이 필요해 여기서는 못 합니다. 대신 파서가",
         "스스로 매긴 확신도와 산출 경로별 분포를 대조해 **어느 경로가 미덥지 않은지**",
         "를 지목합니다 — 사람이 검수할 때 어디부터 볼지 정하는 용도입니다.", "",
         "| 항목 | 값 |", "|---|---|",
         "| 금액 확보 행 | %d |" % pq["n"],
         "| 상식범위 밖 플래그 | %d건 |" % pq["amount_outlier_flagged"]]
    if "extraction_confidence" in pq:
        e = pq["extraction_confidence"]
        L.append("| 추출 확신도 평균 | %.2f |" % e["mean"])
        L.append("| 확신도 0.5 미만 | %d건 (%.1f%%) |"
                 % (e["low_below_0.5"], e["share_low"] * 100))
    for k in ("amount_type_source", "per_recipient_basis"):
        if k in pq:
            L += ["", "**%s 별 분포**" % k, "", "| 값 | n | 중앙값(원) |", "|---|---:|---:|"]
            for kk, v in pq[k].items():
                L.append("| %s | %d | %,.0f |".replace("%,", "%") % (kk, v["n"], v["median"]))

    L += ["", "## 2. Feature 확장 (계획서 5절)", "",
          "| feature set | 축 수 | MAE | fold σ | 2배 이내 |",
          "|---|---:|---:|---:|---:|"]
    for k, r in fsets.items():
        mark = " ⭐" if k == out["feature_sets"]["chosen"] else ""
        L.append("| %s%s | %d | %.4f | %.4f | %.1f%% |"
                 % (k, mark, r["n_features"], r["MAE_log10"], r["fold_std"],
                    r["within_2x"] * 100))

    L += ["", "## 3. LightGBM 튜닝 (계획서 6절)", "",
          "Optuna 미설치 환경이라 시드 고정 랜덤 서치 %d회로 같은 공간을 훑었습니다."
          % out["tuning"]["n_trials"], "",
          "```text"]
    for k, v in best["params"].items():
        L.append("%-20s %s" % (k, v))
    L += ["```", "",
          "| 지표 | 값 |", "|---|---:|",
          "| MAE(log10) | %.4f |" % best["result"]["MAE_log10"],
          "| MedAE(log10) | %.4f |" % best["result"]["MedAE_log10"],
          "| 배수 오차 | %.2fx |" % best["result"]["geo_mean_error_x"],
          "| 2배 이내 | %.1f%% |" % (best["result"]["within_2x"] * 100),
          "| fold 간 σ | %.4f |" % best["result"]["fold_std"],
          "| fold별 MAE | %s |" % ", ".join("%.4f" % v for v in best["result"]["fold_mae"]),
          "",
          "## 4. Quantile 확장 (계획서 7절)", "",
          "Q10 / Q50 / Q90 을 각각 학습했습니다.", "",
          "| 지표 | 값 |", "|---|---:|",
          "| P10~P90 포함률 | %.1f%% |" % (qb["coverage_p10_p90"] * 100),
          "| 구간폭 중앙값 (log10) | %.3f |" % qb["band_width_log10_median"],
          "| 분위 교차율 | %.2f%% |" % (qb["crossing_rate"] * 100),
          "", "> 이 구간은 \"적정 범위\"가 아닙니다. **과거 유사사업 조건을 학습한**",
          "> **예측 분포**입니다. 계획서 7절의 표현 규율을 그대로 지킵니다.", "",
          "## 5. 기준선 대비", "",
          "| 모델 | MAE(log10) |", "|---|---:|",
          "| 전체중앙값 | 0.8600 |",
          "| 코호트중앙값 (baseline) | %.4f |" % BASELINE_COHORT,
          "| M12 LGBM-quantile50 | %.4f |" % BASELINE_ML,
          "| **M17 튜닝 후** | **%.4f** |" % best["result"]["MAE_log10"], "",
          "코호트중앙값 대비 **%+.1f%%** / M12 대비 **%+.1f%%**"
          % (imp_cohort * 100, imp_ml * 100), "",
          "## 6. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = C.report_path("m17_m3_tuning.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
