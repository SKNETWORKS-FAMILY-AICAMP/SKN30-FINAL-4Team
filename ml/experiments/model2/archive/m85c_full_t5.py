r"""M85-C — screening 을 통과한 T5 만 full validation.

M85-B screening(program_stem fold 0~2, T0/T2/T3/T4/T5) 결과:

    T2  개선 1/3  평균Δ +0.0018   fail
    T3  개선 2/3  평균Δ -0.0007   fail (fold 조건은 통과, 폭 미달)
    T4  개선 1/3  평균Δ +0.0000   fail
    T5  개선 3/3  평균Δ -0.0020   PASS   <- 여기만 넘어온다

T5 = M82/P3 + (표 presence/role + header-cell relation + row×col pair
                + masked table-context TF-IDF/SVD)

full validation 은 지시서 M85 평가·승격 기준 그대로다.

    5-fold program_stem · normalized_title strict · paired CI · 재현성
    cohort(taxonomy/bizinfo) · format(hwp/hwpx) · 표 유무별 MAE 분리

feature 블록은 screening 과 같은 `M85.build_blocks()` 를 부른다.

산출
    ml/data/processed/m85_t5_oof.parquet
    ml/reports/m85c_full_t5.json / .md
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
import m2_features as F
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m82_m2_proximity_features as M82
import m85_table_layout_features as M85

OUT_OOF = os.path.join(C.PROC, "m85_t5_oof.parquet")
MD = C.report_path("m85c_full_t5.md")

P3 = {"primary_MAE": 0.3518, "strict_MAE": 0.3751,
      "within_2x": 0.5717, "within_3x": 0.7437}
M73 = {"primary_MAE": 0.3563, "strict_MAE": 0.3756}
PRACTICAL_DELTA = -0.002
SCREEN = {"T5": {"fold_delta": [-0.0008, -0.0027, -0.0026], "wins": 3,
                 "mean_delta": -0.0020}}


def fold_t0_t5(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i):
    """T0(M82/P3) 과 T5(전체 구조 feature) 둘만."""
    t0 = time.time()
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                M82.STEP, [None])
    p_tr = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_te = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[te].reset_index(drop=True)
    a, b = M82.augment(Xb_tr, Xb_te, p_tr, p_te)
    sv_tr, sv_te = M82.fit_prox_svd(P["prox_context_text"].to_numpy()[tr],
                                    P["prox_context_text"].to_numpy()[te])
    nm = ["proxsvd%02d" % j for j in range(sv_tr.shape[1])]
    T0_tr, T0_te = M82.augment(a, b, pd.DataFrame(sv_tr, columns=nm),
                               pd.DataFrame(sv_te, columns=nm))

    def blk(df):
        return df.iloc[tr].reset_index(drop=True), df.iloc[te].reset_index(drop=True)

    ta, tb = M82.fit_prox_svd(T4txt[tr], T4txt[te], n_components=M85.TBL_SVD)
    tn = ["tblsvd%02d" % j for j in range(ta.shape[1])]
    all_tr = pd.concat([blk(T1)[0], blk(T2)[0], blk(T3)[0],
                        pd.DataFrame(ta, columns=tn)], axis=1)
    all_te = pd.concat([blk(T1)[1], blk(T2)[1], blk(T3)[1],
                        pd.DataFrame(tb, columns=tn)], axis=1)
    T5_tr, T5_te = M82.augment(T0_tr, T0_te, all_tr, all_te)

    ytr, yte = y[tr], y[te]
    pred = {"T0": M82.m73_block(T0_tr, ytr, T0_te),
            "T5": M82.m73_block(T5_tr, ytr, T5_te)}
    rec = {"fold": i, "dims": {"T0": int(T0_tr.shape[1]), "T5": int(T5_tr.shape[1])},
           "MAE": {k: round(float(np.abs(p - yte).mean()), 4) for k, p in pred.items()},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "pred": pred, "rec": rec}


def run_split(Xs, y, groups, titles, body, NB, P, T1, T2, T3, T4txt, verbose=True):
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = {"T0": np.zeros(n), "T5": np.zeros(n)}
    fold_id = np.zeros(n, dtype=int)
    folds = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fo = fold_t0_t5(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i)
        fold_id[fo["te"]] = i
        for k in pred:
            pred[k][fo["te"]] = fo["pred"][k]
        folds.append(fo["rec"])
        if verbose:
            print("   fold %d  T0 %.4f  T5 %.4f  (Δ%+0.4f, %.0fs)"
                  % (i, fo["rec"]["MAE"]["T0"], fo["rec"]["MAE"]["T5"],
                     fo["rec"]["MAE"]["T5"] - fo["rec"]["MAE"]["T0"],
                     fo["rec"]["seconds"]))
    return {"fold_id": fold_id, "pred": pred, "folds": folds}


def main():
    t_all = time.time()
    print("== 데이터")
    raw = pd.read_parquet(M85.SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(M85.SRC)
    NB, body, src = SF.build(d)
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)

    print("\n== Exp0 + feature 블록 (screening 과 같은 함수)")
    BK = M85.build_blocks(d, verbose=False)
    T1, T2, T3, T4txt = BK["T1"], BK["T2"], BK["T3"], BK["T4txt"]
    has_t = BK["has_t"]
    ext_arr = BK["TB"]["_ext"].to_numpy()

    print("\n== T0 vs T5 — 5-fold [program_stem]")
    Rp = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, T1, T2, T3, T4txt)
    print("\n== 5-fold [normalized_title] (strict)")
    Rn = run_split(Xs, y, groups["normalized_title"], titles, body, NB, P, T1, T2, T3,
                   T4txt, verbose=False)
    print("   T0 %.4f  T5 %.4f"
          % (float(np.abs(Rn["pred"]["T0"] - y).mean()),
             float(np.abs(Rn["pred"]["T5"] - y).mean())))

    coh = d["cohort"].to_numpy()

    def summarize(R):
        out = {}
        for k, p in R["pred"].items():
            e = {"MAE_log10": round(float(np.abs(p - y).mean()), 4),
                 "within_2x": round(M82.within_x(y, p, 2), 4),
                 "within_3x": round(M82.within_x(y, p, 3), 4),
                 "per_fold_MAE": [round(float(np.abs(p[R["fold_id"] == i]
                                                     - y[R["fold_id"] == i]).mean()), 4)
                                  for i in range(F.N_SPLITS)]}
            for c in ("taxonomy", "bizinfo"):
                m = coh == c
                e["MAE_%s" % c] = round(float(np.abs(p[m] - y[m]).mean()), 4)
            for nm, m in (("table_present", has_t), ("table_absent", ~has_t)):
                if m.sum():
                    e["MAE_%s" % nm] = round(float(np.abs(p[m] - y[m]).mean()), 4)
                    e["n_%s" % nm] = int(m.sum())
            for nm in ("hwp", "hwpx"):
                m = ext_arr == nm
                if m.sum():
                    e["MAE_%s" % nm] = round(float(np.abs(p[m] - y[m]).mean()), 4)
                    e["n_%s" % nm] = int(m.sum())
            if k != "T0":
                e["vs_T0"] = M82.paired_test(y, p, R["pred"]["T0"])
                e["fold_wins_vs_T0"] = M82.fold_wins(y, p, R["pred"]["T0"], R["fold_id"])
            out[k] = e
        return out

    sp, sn = summarize(Rp), summarize(Rn)
    B = sp["T5"]
    print("\n== 결과 [program_stem]")
    print("   T0  MAE %.4f  2x %.1f%%  3x %.1f%%"
          % (sp["T0"]["MAE_log10"], 100 * sp["T0"]["within_2x"], 100 * sp["T0"]["within_3x"]))
    print("   T5  MAE %.4f  2x %.1f%%  3x %.1f%%  Δ%+0.4f CI[%+0.4f,%+0.4f] p=%s fold승%d/5"
          % (B["MAE_log10"], 100 * B["within_2x"], 100 * B["within_3x"],
             B["vs_T0"]["delta_MAE"], B["vs_T0"]["ci95"][0], B["vs_T0"]["ci95"][1],
             B["vs_T0"]["wilcoxon_p"], B["fold_wins_vs_T0"]))
    print("   fold별 T0 %s" % sp["T0"]["per_fold_MAE"])
    print("   fold별 T5 %s" % B["per_fold_MAE"])
    print("   코호트  taxonomy %.4f -> %.4f · bizinfo %.4f -> %.4f"
          % (sp["T0"]["MAE_taxonomy"], B["MAE_taxonomy"],
             sp["T0"]["MAE_bizinfo"], B["MAE_bizinfo"]))
    print("   표유무  있음(n=%d) %.4f -> %.4f · 없음(n=%d) %.4f -> %.4f"
          % (sp["T0"]["n_table_present"], sp["T0"]["MAE_table_present"],
             B["MAE_table_present"], sp["T0"]["n_table_absent"],
             sp["T0"]["MAE_table_absent"], B["MAE_table_absent"]))
    print("   포맷    hwp(n=%d) %.4f -> %.4f · hwpx(n=%d) %.4f -> %.4f"
          % (sp["T0"]["n_hwp"], sp["T0"]["MAE_hwp"], B["MAE_hwp"],
             sp["T0"]["n_hwpx"], sp["T0"]["MAE_hwpx"], B["MAE_hwpx"]))
    print("\n== [normalized_title] strict")
    print("   T0 %.4f  T5 %.4f  (Δ%+0.4f)"
          % (sn["T0"]["MAE_log10"], sn["T5"]["MAE_log10"],
             sn["T5"]["MAE_log10"] - sn["T0"]["MAE_log10"]))

    print("\n== 재현성 — program_stem 한 번 더 (독립 학습)")
    Rp2 = run_split(Xs, y, groups["program_stem"], titles, body, NB, P, T1, T2, T3,
                    T4txt, verbose=False)
    repro = {k: bool(np.array_equal(Rp2["pred"][k], Rp["pred"][k])) for k in Rp["pred"]}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    gain_hwp = sp["T0"]["MAE_hwp"] - B["MAE_hwp"]
    gain_hwpx = sp["T0"]["MAE_hwpx"] - B["MAE_hwpx"]
    a = BK["audit"]
    leak = BK["leak"]
    checks = {
        "1. Primary MAE < 0.3518": B["MAE_log10"] < P3["primary_MAE"],
        "2. strict MAE <= 0.3751": sn["T5"]["MAE_log10"] <= P3["strict_MAE"],
        "3. 최소 4/5 fold 개선": B["fold_wins_vs_T0"] >= 4,
        "4. paired 95% CI < 0": B["vs_T0"]["ci95"][1] < 0,
        "5. target leakage audit PASS": all(v == 0 for v in leak.values()),
        "6. table coverage 충분": a["table_detect_coverage_document_rows"] >= 0.30,
        "7. 특정 포맷에만 이득이 몰리지 않음": bool(gain_hwp > 0 and gain_hwpx > 0),
        "8. reproducibility PASS": all(repro.values()),
        "9. 실질 기준 ΔMAE <= -0.002": B["vs_T0"]["delta_MAE"] <= PRACTICAL_DELTA,
    }
    verdict = ("승격 후보 (T5)" if all(checks.values())
               else "현행 유지 — M82/P3")
    print("\n== 승격 점검표 — 대상 T5")
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)
    print("   설계행렬 %d열 -> %d열 (+%d)"
          % (Rp["folds"][0]["dims"]["T0"], Rp["folds"][0]["dims"]["T5"],
             Rp["folds"][0]["dims"]["T5"] - Rp["folds"][0]["dims"]["T0"]))

    pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "fold": Rp["fold_id"],
                  "cohort": coh, "has_table": has_t.astype(int), "ext": ext_arr,
                  "pred_T0": Rp["pred"]["T0"], "pred_T5": Rp["pred"]["T5"]}
                 ).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "screening 통과 후보 T5 의 full validation",
        "screening": SCREEN,
        "dataset": {"path": fp["path"], "sha256": fp["sha256"],
                    "rows": fp["rows_after_filters"]},
        "p3_baseline": P3, "m73_baseline": M73,
        "exp0_audit": a, "leakage": leak,
        "results": {"program_stem": sp, "normalized_title": sn},
        "dims": Rp["folds"][0]["dims"],
        "format_gain": {"hwp": round(float(gain_hwp), 4),
                        "hwpx": round(float(gain_hwpx), 4)},
        "reproducibility": repro,
        "promotion_checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict,
        "seconds": round(time.time() - t_all, 1),
    }
    C.save_report("m85c_full_t5.json", payload)

    L = ["# M85-C — T5 full validation\n",
         "> screening(fold 0~2)에서 유일하게 게이트를 통과한 **T5** 만 5-fold · strict · "
         "CI · 재현성으로 확인한다.\n",
         "## screening 통과 근거\n",
         "```text\nT5  fold Δ [-0.0008, -0.0027, -0.0026]  개선 3/3  평균Δ -0.0020\n"
         "T2/T3/T4  전부 게이트 미달\n```\n",
         "## 결과 [program_stem]\n",
         "| 변형 | MAE | 2x | 3x | Δ vs T0 | 95% CI | p | fold승 |",
         "|---|---:|---:|---:|---:|---|---:|---:|",
         "| T0 (M82/P3) | %.4f | %.1f%% | %.1f%% | — | — | — | — |"
         % (sp["T0"]["MAE_log10"], 100 * sp["T0"]["within_2x"], 100 * sp["T0"]["within_3x"]),
         "| **T5** | **%.4f** | %.1f%% | %.1f%% | %+0.4f | [%+0.4f, %+0.4f] | %s | %d/5 |"
         % (B["MAE_log10"], 100 * B["within_2x"], 100 * B["within_3x"],
            B["vs_T0"]["delta_MAE"], B["vs_T0"]["ci95"][0], B["vs_T0"]["ci95"][1],
            B["vs_T0"]["wilcoxon_p"], B["fold_wins_vs_T0"]),
         "\nfold별 T0 `%s` / T5 `%s`\n" % (sp["T0"]["per_fold_MAE"], B["per_fold_MAE"]),
         "## 분해\n",
         "| 구분 | n | T0 | T5 |\n|---|---:|---:|---:|",
         "| taxonomy | — | %.4f | %.4f |" % (sp["T0"]["MAE_taxonomy"], B["MAE_taxonomy"]),
         "| bizinfo | — | %.4f | %.4f |" % (sp["T0"]["MAE_bizinfo"], B["MAE_bizinfo"]),
         "| 표 있음 | %d | %.4f | %.4f |" % (sp["T0"]["n_table_present"],
                                          sp["T0"]["MAE_table_present"],
                                          B["MAE_table_present"]),
         "| 표 없음 | %d | %.4f | %.4f |" % (sp["T0"]["n_table_absent"],
                                          sp["T0"]["MAE_table_absent"],
                                          B["MAE_table_absent"]),
         "| hwp | %d | %.4f | %.4f |" % (sp["T0"]["n_hwp"], sp["T0"]["MAE_hwp"], B["MAE_hwp"]),
         "| hwpx | %d | %.4f | %.4f |" % (sp["T0"]["n_hwpx"], sp["T0"]["MAE_hwpx"], B["MAE_hwpx"]),
         "\n## strict [normalized_title]\n",
         "```text\nT0 %.4f\nT5 %.4f  (Δ%+0.4f)\n```\n"
         % (sn["T0"]["MAE_log10"], sn["T5"]["MAE_log10"],
            sn["T5"]["MAE_log10"] - sn["T0"]["MAE_log10"]),
         "## 승격 점검표\n"]
    for k, ok in checks.items():
        L.append("- [%s] %s" % ("x" if ok else " ", k))
    L += ["\n## 판정\n\n```text\n%s\n```\n" % verdict]
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)
    print("\n총 %.0f초" % (time.time() - t_all))
    return payload


if __name__ == "__main__":
    main()
