r"""M82-B — P3 canonical 확정 전 재현성 3-run + OOF 보존.

지시서(사용자, `m82_final_result_and_next_experiments.md` 7장 A):

    OOF prediction exact match
    MAE exact match
    CI exact match
    feature dimension exact match
    feature ordering exact match

M82 본 실험은 P0~P3 네 후보를 한 fold 루프에서 쟀지만 **OOF 를 저장하지 않았다**.
다음 실험(M83/M84)의 baseline 이 M73 이 아니라 P3 가 되므로, 그 근거 OOF 를
파일로 남기고 같은 숫자가 세 번 나오는지부터 확인한다.

    P0  M73 재현 (M69 G 단계 feature + soft/ordinal_xgb)
    P3  P0 + explicit proximity + masked proximity TF-IDF/SVD

비교는 M82 와 같은 fold 분할(GroupKFold(5), program_stem)에서만 한다. 재현성은
'같은 파일을 다시 읽어 같은 값이 나온다'가 아니라 **세 번의 독립 학습이 같은
값을 낸다**로 정의한다 — 캐시를 쓰지 않는다.

산출
    ml/data/processed/m82_p3_oof.parquet      (run1 의 P0/P3 OOF — 다음 실험 baseline)
    ml/reports/m82b_p3_reproducibility.json / .md
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
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m82_m2_proximity_features as M82

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m82_p3_oof.parquet")
MD = C.report_path("m82b_p3_reproducibility.md")

N_RUNS = 3
PUBLISHED = {"P0": 0.3563, "P3": 0.3518, "delta": -0.0044,
             "ci95": [-0.0083, -0.0003], "fold_wins": 5}


def fold_p0_p3(Xs, y, titles, body, NB, P, tr, te):
    """P0 / P3 예측 + 두 설계행렬의 컬럼 목록(차원·순서 확인용)."""
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                M82.STEP, [None])
    ytr = y[tr]

    p_num_tr = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_num_te = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[te].reset_index(drop=True)
    X1_tr, X1_te = M82.augment(Xb_tr, Xb_te, p_num_tr, p_num_te)

    svd_tr, svd_te = M82.fit_prox_svd(P["prox_context_text"].to_numpy()[tr],
                                      P["prox_context_text"].to_numpy()[te])
    names = ["proxsvd%02d" % j for j in range(svd_tr.shape[1])]
    X3_tr, X3_te = M82.augment(X1_tr, X1_te,
                               pd.DataFrame(svd_tr, columns=names),
                               pd.DataFrame(svd_te, columns=names))
    return {"P0": M82.m73_block(Xb_tr, ytr, Xb_te),
            "P3": M82.m73_block(X3_tr, ytr, X3_te),
            "cols_P0": list(map(str, Xb_tr.columns)),
            "cols_P3": list(map(str, X3_tr.columns))}


def run_once(Xs, y, groups, titles, body, NB, P, tag):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = {"P0": np.zeros(n), "P3": np.zeros(n)}
    fold_id = np.zeros(n, dtype=int)
    cols = {}
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        t0 = time.time()
        fo = fold_p0_p3(Xs, y, titles, body, NB, P, tr, te)
        fold_id[te] = i
        for k in ("P0", "P3"):
            pred[k][te] = fo[k]
        cols["fold%d" % i] = {"P0": fo["cols_P0"], "P3": fo["cols_P3"]}
        print("   [%s] fold %d  P0 %.4f  P3 %.4f  (dim %d -> %d, %.0fs)"
              % (tag, i, float(np.abs(pred["P0"][te] - y[te]).mean()),
                 float(np.abs(pred["P3"][te] - y[te]).mean()),
                 len(fo["cols_P0"]), len(fo["cols_P3"]), time.time() - t0))
    return {"pred": pred, "fold_id": fold_id, "cols": cols}


def summarize(y, R):
    p0, p3 = R["pred"]["P0"], R["pred"]["P3"]
    return {"P0_MAE": round(float(np.abs(p0 - y).mean()), 6),
            "P3_MAE": round(float(np.abs(p3 - y).mean()), 6),
            "paired": M82.paired_test(y, p3, p0),
            "fold_wins": M82.fold_wins(y, p3, p0, R["fold_id"]),
            "within_2x": round(M82.within_x(y, p3, 2), 6),
            "within_3x": round(M82.within_x(y, p3, 3), 6)}


def main():
    t0 = time.time()
    print("== 데이터 — M82 와 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = F.group_key(d, "program_stem")
    fp = F.dataset_fingerprint(SRC)
    NB, body, src = SF.build(d)
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)
    print("   %s / sha %s… / 행 %d / proximity window %d"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"], M82.WINDOW_PRIMARY))

    runs, sums = [], []
    for r in range(N_RUNS):
        print("\n== run %d / %d (독립 학습 — 캐시 없음)" % (r + 1, N_RUNS))
        R = run_once(Xs, y, groups, titles, body, NB, P, "run%d" % (r + 1))
        runs.append(R)
        sums.append(summarize(y, R))
        print("   run%d  P0 %.6f  P3 %.6f  Δ%+0.6f  fold승 %d/5"
              % (r + 1, sums[-1]["P0_MAE"], sums[-1]["P3_MAE"],
                 sums[-1]["paired"]["delta_MAE"], sums[-1]["fold_wins"]))

    # ---------------------------------------------- exact match 판정
    base = runs[0]
    checks = {}
    checks["OOF prediction exact match"] = all(
        np.array_equal(runs[i]["pred"][k], base["pred"][k])
        for i in range(1, N_RUNS) for k in ("P0", "P3"))
    checks["MAE exact match"] = all(
        sums[i]["P0_MAE"] == sums[0]["P0_MAE"] and sums[i]["P3_MAE"] == sums[0]["P3_MAE"]
        for i in range(1, N_RUNS))
    checks["CI exact match"] = all(
        sums[i]["paired"]["ci95"] == sums[0]["paired"]["ci95"] for i in range(1, N_RUNS))
    checks["feature dimension exact match"] = all(
        len(runs[i]["cols"][f][k]) == len(base["cols"][f][k])
        for i in range(1, N_RUNS) for f in base["cols"] for k in ("P0", "P3"))
    checks["feature ordering exact match"] = all(
        runs[i]["cols"][f][k] == base["cols"][f][k]
        for i in range(1, N_RUNS) for f in base["cols"] for k in ("P0", "P3"))
    checks["M82 공표치 재현 (P0 0.3563 / P3 0.3518)"] = (
        abs(sums[0]["P0_MAE"] - PUBLISHED["P0"]) < 5e-4
        and abs(sums[0]["P3_MAE"] - PUBLISHED["P3"]) < 5e-4)
    checks["fold 분할이 run 간 동일"] = all(
        np.array_equal(runs[i]["fold_id"], base["fold_id"]) for i in range(1, N_RUNS))

    print("\n== 재현성 점검표 (%d-run)" % N_RUNS)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    verdict = "PASS — P3 canonical 후보 확정 가능" if all(checks.values()) else "FAIL"
    print("   판정: %s" % verdict)

    # ---------------------------------------------- OOF 보존 (다음 실험 baseline)
    out = pd.DataFrame({
        "row_id": d["row_id"].to_numpy(), "y": y, "fold": base["fold_id"],
        "cohort": d["cohort"].to_numpy(),
        "evidence_source": d["evidence_source"].to_numpy(),
        "pred_P0_m73": base["pred"]["P0"], "pred_P3": base["pred"]["P3"],
    })
    out.to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    dims = {f: {"P0": len(base["cols"][f]["P0"]), "P3": len(base["cols"][f]["P3"])}
            for f in base["cols"]}
    payload = {
        "purpose": "M82/P3 를 canonical 후보로 확정하기 전 재현성 3-run",
        "dataset": {"path": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"]},
        "n_runs": N_RUNS,
        "runs": sums,
        "feature_dims_per_fold": dims,
        "new_proximity_cols": len(M82.NUMERIC_COLS + M82.FLAG_COLS) + M82.PROX_SVD,
        "checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict,
        "published_m82": PUBLISHED,
        "oof": os.path.relpath(OUT_OOF, C.ROOT),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m82b_p3_reproducibility.json", payload)

    L = ["# M82-B — P3 재현성 3-run\n",
         "> 질문: **P3 를 Model 2 canonical 후보로 확정해도 되는가 — 세 번의 독립 "
         "학습이 같은 숫자를 내는가?**\n", "## run 별 결과\n",
         "| run | P0 MAE | P3 MAE | Δ | 95% CI | fold승 |",
         "|---|---:|---:|---:|---|---:|"]
    for i, s in enumerate(sums):
        L.append("| run%d | %.6f | %.6f | %+0.6f | [%+0.4f, %+0.4f] | %d/5 |"
                 % (i + 1, s["P0_MAE"], s["P3_MAE"], s["paired"]["delta_MAE"],
                    s["paired"]["ci95"][0], s["paired"]["ci95"][1], s["fold_wins"]))
    L.append("\n## 점검표\n")
    for k, ok in checks.items():
        L.append("- [%s] %s" % ("x" if ok else " ", k))
    L.append("\n## 판정\n\n```text\n%s\n```\n" % verdict)
    L.append("\nfold 별 설계행렬 차원 (P0 -> P3): `%s`\n" % dims)
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


if __name__ == "__main__":
    main()
