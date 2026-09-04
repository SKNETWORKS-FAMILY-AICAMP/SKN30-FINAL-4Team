r"""M68b — "보정량을 줄여서 쓰면 되지 않는가" 를 닫는 사후 점검.

M68 에서 residual 보정 넷이 전부 M65 를 못 이겼다. 보정량과 실제 잔차의 상관이
+0.04 ~ +0.10 으로 약하기 때문인데, 상관이 0 은 아니므로 **보정을 그대로 더하지
말고 λ 만큼 줄여서 더하면** 이득이 남을 수 있다는 반론이 가능하다.

        final = base + λ · correction        (M68 은 λ=1 을 썼다)

여기서는 M68 이 저장한 OOF 표만 다시 읽어 λ 를 훑는다. 모델을 새로 학습하지
않으므로 몇 초면 끝난다.

    oracle λ    평가 대상 OOF 자체에서 MAE 를 최소화하는 λ. **낙관 상한**이다 —
                평가에 쓰는 데이터로 λ 를 고른 것이므로 승격 근거가 될 수 없다.
    honest λ    fold f 의 λ 를 **나머지 4 fold 에서만** 고르고 fold f 에 적용.
                실제로 쓸 수 있는 값은 이쪽이다. paired 검정도 이 값으로 한다.

두 grouping(program_stem · normalized_title) 모두에서 잰다. 지시서 승격 기준 4
("엄격 split 에서도 개선이 유지되는가")가 이 사후 보정에도 그대로 걸리기 때문이다.

산출: ml/reports/m68b_shrinkage_check.json
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
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

import os

import numpy as np
import pandas as pd

import common as C

SOURCES = {"program_stem": os.path.join(C.PROC, "m68_residual_oof.parquet"),
           "normalized_title": os.path.join(C.PROC, "m68_residual_oof_strict.parquet")}
LAMBDAS = np.round(np.arange(0.0, 1.51, 0.05), 2)
COLS = {"corr_e1": "E1 전역 residual", "corr_e2": "E2 예측구간 residual(33/67)",
        "corr_e2b": "E2b 예측구간 residual(25/75)", "corr_e3": "E3 residual MoE",
        "corr_e4": "E4 극단구간만 보정(20/80)"}
SEED = 42


def mae(y, p):
    return float(np.abs(p - y).mean())


def paired(y, p_new, p_old):
    """같은 행의 절대오차 차이. 음수면 신규가 낫다."""
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old
    rng = np.random.default_rng(SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": float("%.3g" % stats.wilcoxon(e_new, e_old).pvalue)}


def analyze(path):
    d = pd.read_parquet(path)
    y = d["y"].to_numpy()
    base = d["pred_base"].to_numpy()
    fold = d["fold"].to_numpy()
    base_mae = mae(y, base)
    print("   M65 base OOF MAE %.4f" % base_mae)

    out = {}
    for col, name in COLS.items():
        c = d[col].to_numpy()
        grid = {float(l): mae(y, base + l * c) for l in LAMBDAS}
        ol = min(grid, key=grid.get)

        honest = np.zeros(len(y))
        picked = []
        for f in np.unique(fold):
            te = fold == f
            tr = ~te
            l = min(LAMBDAS, key=lambda v: mae(y[tr], base[tr] + v * c[tr]))
            picked.append(float(l))
            honest[te] = base[te] + l * c[te]
        fold_win = sum(1 for f in np.unique(fold)
                       if mae(y[fold == f], honest[fold == f])
                       < mae(y[fold == f], base[fold == f]))

        out[name] = {
            "corr_with_residual": round(float(np.corrcoef(c, y - base)[0, 1]), 4),
            "abs_correction_mean": round(float(np.abs(c).mean()), 4),
            "lambda1_MAE": round(grid[1.0], 4),
            "oracle_lambda": ol, "oracle_lambda_MAE": round(grid[ol], 4),
            "oracle_gain_vs_M65": round(base_mae - grid[ol], 4),
            "honest_lambda_per_fold": picked,
            "honest_MAE": round(mae(y, honest), 4),
            "honest_gain_vs_M65": round(base_mae - mae(y, honest), 4),
            "honest_within_2x": round(float(
                (np.abs(honest - y) <= np.log10(2)).mean()), 4),
            "honest_fold_win": fold_win,
            "honest_paired": paired(y, honest, base),
        }
        v = out[name]
        print("      %-26s 상관 %+0.3f | λ=1 %.4f | oracle λ %.2f -> %.4f "
              "| honest %.4f (Δ %+0.4f, CI [%+0.4f, %+0.4f], fold승 %d/%d)"
              % (name, v["corr_with_residual"], v["lambda1_MAE"], ol,
                 v["oracle_lambda_MAE"], v["honest_MAE"],
                 v["honest_paired"]["delta_MAE"], v["honest_paired"]["ci95"][0],
                 v["honest_paired"]["ci95"][1], fold_win, len(picked)))
    return {"M65_base_MAE": round(base_mae, 4), "methods": out}


def main():
    results = {}
    for gname, path in SOURCES.items():
        if not os.path.exists(path):
            print("== %s — OOF 없음 (%s). M68 을 먼저 돌린다" % (gname, path))
            continue
        print("== %s" % gname)
        results[gname] = analyze(path)

    payload = {
        "purpose": "residual 보정을 λ 로 줄여 더하면 M65 를 이길 수 있는가",
        "source_oof": {k: os.path.relpath(v, C.ROOT) for k, v in SOURCES.items()},
        "note": "모델을 새로 학습하지 않는다. M68 이 저장한 OOF 예측·보정량만 다시 잰다. "
                "oracle λ 는 평가 데이터에서 고른 값이라 낙관 상한이며 승격 근거가 아니다. "
                "honest λ 는 fold 밖에서 고른 값이라 실제로 쓸 수 있다.",
        "lambda_grid": [float(v) for v in LAMBDAS],
        "results": results,
        "run_timestamp": pd.Timestamp.now().isoformat(),
    }
    C.save_report("m68b_shrinkage_check.json", payload)
    return payload


if __name__ == "__main__":
    main()
