"""A07 — 모델 간 차이의 유의성 검정 (paired).

문서 한계 4번("Ensemble 개선폭 5.7%의 유의성 미검정")에 답한다.

같은 fold·같은 검증 표본에서 나온 오차이므로 **대응표본(paired)**이다.
두 모델의 MAE를 따로 평균 내서 비교하면 fold 간 난이도 차이가 잡음으로 섞이는데,
fold별 차이를 먼저 구하면 그 성분이 상쇄된다.

세 가지로 본다.
  1. fold별 승패      — 몇 개 fold에서 이겼나 (부호검정에 해당)
  2. Wilcoxon         — 차이의 중앙값이 0인가 (분포 가정 없음, n=17에 적합)
  3. paired bootstrap — fold를 복원추출해 평균 차이의 95% 신뢰구간

관측 단위 bootstrap이 아니라 **fold 단위**로 재표집한다. walk-forward에서는
같은 fold 안의 관측이 서로 독립이 아니므로 관측 단위로 뽑으면 신뢰구간이
실제보다 좁게 나온다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

from common import PROC, save_report
from a06_timeaxis_compare import AXES, lead_time_bounds, build_ts, quarter_folds, forecast

MASTER = f"{PROC}/announcement_master.parquet"
N_BOOT = 10000


def fold_mae(store, name):
    """모델별 fold -> MAE."""
    return {k: float(np.mean(np.abs(y - p))) for k, y, p, _ in store[name]}


def paired(store, a, b, seed=42):
    """a - b. 음수면 a가 낫다(MAE 기준)."""
    ma, mb = fold_mae(store, a), fold_mae(store, b)
    keys = [k for k in ma if k in mb]
    d = np.array([ma[k] - mb[k] for k in keys])
    n = len(d)

    wins = int((d < 0).sum())
    try:
        w_p = float(stats.wilcoxon(d).pvalue) if n >= 6 and np.any(d != 0) else float("nan")
    except Exception:
        w_p = float("nan")

    rng = np.random.default_rng(seed)
    boots = d[rng.integers(0, n, size=(N_BOOT, n))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])

    return {
        "model": a, "baseline": b, "n_folds": n,
        "mae_a": round(float(np.mean(list(ma.values()))), 3),
        "mae_b": round(float(np.mean(list(mb.values()))), 3),
        "mean_diff": round(float(d.mean()), 3),
        "improve_pct": round(float(-d.mean() / np.mean(list(mb.values())) * 100), 2),
        "ci95": [round(float(lo), 3), round(float(hi), 3)],
        "ci_excludes_zero": bool(lo < 0 and hi < 0) or bool(lo > 0 and hi > 0),
        "wins": wins, "losses": n - wins,
        "win_rate": round(wins / n, 3),
        "wilcoxon_p": None if np.isnan(w_p) else round(w_p, 4),
        "fold_diff_std": round(float(d.std(ddof=1)), 3),
        "verdict": _verdict(lo, hi, w_p),
    }


def _verdict(lo, hi, p):
    """CI와 Wilcoxon이 엇갈릴 수 있다. 둘 다 만족해야 '유의'로 본다."""
    ci_ok = (lo < 0 and hi < 0) or (lo > 0 and hi > 0)
    p_ok = (not np.isnan(p)) and p < 0.05
    if ci_ok and p_ok:
        return "유의"
    if ci_ok or p_ok:
        return "경계"          # 한쪽만 만족 — 확정하지 않는다
    return "판정불가"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--q-start", default="2020-Q1")
    args = ap.parse_args()

    m = pd.read_parquet(MASTER, columns=["registered_date", "application_end", "category_large"])
    m["registered_date"] = pd.to_datetime(m["registered_date"])
    m["application_end"] = pd.to_datetime(m["application_end"], errors="coerce")
    lo, hi, _, _ = lead_time_bounds(m)
    folds = quarter_folds(lo, hi, args.q_start)
    print("공통 기간 %s~%s / fold %d개 / bootstrap %d회\n" % (lo, hi, len(folds), N_BOOT))

    # 비교 쌍: (모델, 기준). Ensemble의 가치를 두 방향에서 본다.
    PAIRS = [("Ensemble", "Seasonal Naive"),   # 문서가 주장한 개선폭
             ("Ensemble", "CatBoost"),         # 앙상블이 최고 단일모델보다 나은가
             ("CatBoost", "Seasonal Naive"),   # 최고 단일모델은 베이스라인을 넘나
             ("Ensemble", "Last Value")]

    out = {}
    for key, (col, label) in AXES.items():
        ts, cats = build_ts(m, col, lo, hi)
        _, store = forecast(ts, cats, args.seed, args.skip_slow, folds, return_store=True)

        print("=== %s 기준 ===" % label)
        rows = []
        for a, b in PAIRS:
            if a not in store or b not in store:
                continue
            r = paired(store, a, b, args.seed)
            rows.append(r)
            sig = r["verdict"]
            print("  %-16s vs %-16s  ΔMAE %+6.2f (%+5.1f%%)  CI[%+6.2f,%+6.2f]  "
                  "승 %2d/%2d  p=%s  → %s" % (
                      a, b, r["mean_diff"], r["improve_pct"], r["ci95"][0], r["ci95"][1],
                      r["wins"], r["n_folds"],
                      ("%.4f" % r["wilcoxon_p"]) if r["wilcoxon_p"] is not None else "-", sig))
        out[key] = {"label": label, "pairs": rows}
        print()

    save_report("a07_paired_test.json", {
        "purpose": "모델 간 MAE 차이의 유의성 — fold 단위 paired 검정",
        "method": {
            "unit": "fold (walk-forward 분기)",
            "n_bootstrap": N_BOOT,
            "tests": ["fold별 승패", "Wilcoxon signed-rank", "paired bootstrap 95% CI"],
            "note": "fold 내부 관측은 독립이 아니므로 관측 단위가 아닌 fold 단위로 재표집",
        },
        "period": [str(lo), str(hi)], "n_folds": len(folds),
        "axes": out,
    })


if __name__ == "__main__":
    main()
