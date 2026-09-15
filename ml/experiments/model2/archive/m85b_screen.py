r"""M85-B — 사전 screening. program_stem fold 0~2 · T0/T2/T3/T4/T5 만.

지시(사용자):

    90분짜리 full sweep 을 바로 돌리지 말고 fold 0~2 로 먼저 screening.
    2/3 fold 이상 개선 + 평균 ΔMAE <= -0.002 인 후보만 full validation
    (5-fold · strict · CI · 재현성) 으로 넘기고, 없으면 M82/P3 유지로 종료.

full sweep 은 6후보 × (5 fold × 2 split + 재현성 5 fold) = 90 fold-fit 이다.
screening 은 5후보 × 3 fold = 15 fold-fit 이라 대략 1/6 비용이다.

## 무엇을 바꾸지 않았는가

feature 블록은 `m85_table_layout_features.build_blocks()` 를 그대로 부른다 —
screening 과 full validation 이 다른 feature 를 보면 screening 이 의미가 없다.
fold 분할도 같은 `GroupKFold(5)` 의 앞 3개를 쓴다(3-fold 로 다시 나누지 않는다).
따라서 여기서 나온 fold 0~2 숫자는 full validation 의 같은 fold 와 일치한다.

## 판정

    통과   fold 개선 >= 2/3  AND  평균 ΔMAE <= -0.002
    실패   -> M82/P3 유지로 종료 (full validation 을 돌리지 않는다)

산출
    ml/reports/m85b_screen.json / .md
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
import m85_table_layout_features as M85

MD = C.report_path("m85b_screen.md")

SCREEN_FOLDS = 3                       # program_stem GroupKFold(5) 의 앞 3개
VARIANTS = ("T0", "T2", "T3", "T4", "T5")
MIN_FOLD_WINS = 2                      # 2/3 이상
MAX_MEAN_DELTA = -0.002                # 평균 ΔMAE 문턱
P3_BASELINE = {"primary_MAE": 0.3518, "strict_MAE": 0.3751}


def fold_screen(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i):
    """`_fold` 와 같은 설계행렬을 만들되 T1 변형만 학습을 건너뛴다."""
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

    X = {"T0": (T0_tr, T0_te)}
    X["T2"] = M82.augment(T0_tr, T0_te, *blk(T2))
    X["T3"] = M82.augment(T0_tr, T0_te, *blk(T3))
    X["T4"] = M82.augment(T0_tr, T0_te, pd.DataFrame(ta, columns=tn),
                          pd.DataFrame(tb, columns=tn))
    all_tr = pd.concat([blk(T1)[0], blk(T2)[0], blk(T3)[0],
                        pd.DataFrame(ta, columns=tn)], axis=1)
    all_te = pd.concat([blk(T1)[1], blk(T2)[1], blk(T3)[1],
                        pd.DataFrame(tb, columns=tn)], axis=1)
    X["T5"] = M82.augment(T0_tr, T0_te, all_tr, all_te)

    ytr, yte = y[tr], y[te]
    pred, dims = {}, {}
    for k in VARIANTS:
        aa, bb = X[k]
        pred[k] = M82.m73_block(aa, ytr, bb)
        dims[k] = int(aa.shape[1])
    rec = {"fold": i, "n_test": int(len(te)), "dims": dims,
           "MAE": {k: round(float(np.abs(pred[k] - yte).mean()), 4) for k in VARIANTS},
           "seconds": round(time.time() - t0, 1)}
    return {"te": np.asarray(te), "pred": pred, "rec": rec}


def main():
    t_all = time.time()
    print("== 데이터")
    raw = pd.read_parquet(M85.SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = F.group_key(d, "program_stem")
    NB, body, src = SF.build(d)
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)
    print("   행 %d" % len(d))

    print("\n== Exp0 + feature 블록 (full validation 과 같은 함수)")
    BK = M85.build_blocks(d)
    if not BK["go"]:
        print("   Exp0 진행 기준 미달 — 종료")
        return
    T1, T2, T3, T4txt = BK["T1"], BK["T2"], BK["T3"], BK["T4txt"]

    # ---------------------------------------------- screening folds
    from sklearn.model_selection import GroupKFold

    print("\n== screening — program_stem fold 0~%d · %s"
          % (SCREEN_FOLDS - 1, "/".join(VARIANTS)))
    per_fold, te_all, preds = [], [], {k: [] for k in VARIANTS}
    ys = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        if i >= SCREEN_FOLDS:
            break
        fo = fold_screen(Xs, y, titles, body, NB, P, T1, T2, T3, T4txt, tr, te, i)
        per_fold.append(fo["rec"])
        te_all.append(fo["te"])
        ys.append(y[fo["te"]])
        for k in VARIANTS:
            preds[k].append(fo["pred"][k])
        print("   fold %d  %s  (%.0fs)" % (i, fo["rec"]["MAE"], fo["rec"]["seconds"]))

    y_scr = np.concatenate(ys)
    pooled = {k: np.concatenate(preds[k]) for k in VARIANTS}

    # ---------------------------------------------- 게이트
    print("\n== screening 결과 (fold 0~%d, n=%d)" % (SCREEN_FOLDS - 1, len(y_scr)))
    res, passed = {}, []
    base_folds = [r["MAE"]["T0"] for r in per_fold]
    for k in VARIANTS:
        if k == "T0":
            continue
        f_mae = [r["MAE"][k] for r in per_fold]
        deltas = [round(a - b, 4) for a, b in zip(f_mae, base_folds)]
        wins = int(sum(1 for x in deltas if x < 0))
        mean_delta = round(float(np.mean(deltas)), 4)
        pooled_delta = round(float(np.abs(pooled[k] - y_scr).mean()
                                   - np.abs(pooled["T0"] - y_scr).mean()), 4)
        ok = (wins >= MIN_FOLD_WINS) and (mean_delta <= MAX_MEAN_DELTA)
        res[k] = {"fold_MAE": f_mae, "fold_delta": deltas, "fold_wins": wins,
                  "mean_delta": mean_delta, "pooled_MAE": round(
                      float(np.abs(pooled[k] - y_scr).mean()), 4),
                  "pooled_delta": pooled_delta, "pass": bool(ok)}
        if ok:
            passed.append(k)
        print("   %-3s fold MAE %s  Δ %s  개선 %d/%d  평균Δ %+0.4f  pooled %.4f (%+0.4f)  %s"
              % (k, f_mae, deltas, wins, SCREEN_FOLDS, mean_delta,
                 res[k]["pooled_MAE"], pooled_delta, "PASS" if ok else "fail"))
    print("   T0  fold MAE %s  pooled %.4f"
          % (base_folds, float(np.abs(pooled["T0"] - y_scr).mean())))

    print("\n== 게이트: 개선 >= %d/%d AND 평균 ΔMAE <= %+0.3f"
          % (MIN_FOLD_WINS, SCREEN_FOLDS, MAX_MEAN_DELTA))
    if passed:
        verdict = "full validation 진행 — 통과 후보: %s" % ", ".join(passed)
    else:
        verdict = "종료 — 통과 후보 없음. M82/P3 유지"
    print("   %s" % verdict)

    payload = {
        "purpose": "M85 full sweep 전 사전 screening (fold 0~2, T0/T2/T3/T4/T5)",
        "gate": {"min_fold_wins": MIN_FOLD_WINS, "n_folds": SCREEN_FOLDS,
                 "max_mean_delta": MAX_MEAN_DELTA},
        "baseline": "T0 = M82/P3", "p3_baseline": P3_BASELINE,
        "exp0_audit": BK["audit"], "leakage": BK["leak"],
        "screen_n": int(len(y_scr)),
        "fold_records": per_fold,
        "results": res,
        "passed": passed,
        "verdict": verdict,
        "seconds": round(time.time() - t_all, 1),
    }
    C.save_report("m85b_screen.json", payload)

    a = BK["audit"]
    L = ["# M85-B — 사전 screening (fold 0~2 · T0/T2/T3/T4/T5)\n",
         "> full sweep(90 fold-fit) 전에 1/6 비용으로 살아남을 후보가 있는지부터 본다.\n",
         "## 게이트\n",
         "```text\n개선 >= %d/%d fold   AND   평균 ΔMAE <= %+0.3f\n```\n"
         % (MIN_FOLD_WINS, SCREEN_FOLDS, MAX_MEAN_DELTA),
         "## Exp0 — 표 복원 (참고)\n",
         "```text\ndocument 행 %d (%s)\n표 있는 행 %d — document 기준 %.1f%%\n"
         "평균 표 %.1f개 · 셀 %d · header 복원 %.1f%% · row label 복원 %.1f%%\n"
         "leakage  digit %d · amount %d · target %d\n```\n"
         % (a["document_rows"], a["ext_mix"], a["docs_with_tables"],
            100 * a["table_detect_coverage_document_rows"], a["avg_tables_per_doc"],
            a["cell_count"], 100 * a["header_detect_rate"],
            100 * a["row_label_detect_rate"], BK["leak"]["digit_residue_rows"],
            BK["leak"]["amount_regex_residue_rows"], BK["leak"]["target_exact_string_rows"]),
         "## screening 결과 (n=%d)\n" % len(y_scr),
         "| 변형 | fold0 | fold1 | fold2 | 평균 Δ | 개선 | pooled MAE | pooled Δ | 판정 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---|",
         "| T0 (M82/P3) | %.4f | %.4f | %.4f | — | — | %.4f | — | baseline |"
         % (base_folds[0], base_folds[1], base_folds[2],
            float(np.abs(pooled["T0"] - y_scr).mean()))]
    for k in VARIANTS:
        if k == "T0":
            continue
        r = res[k]
        L.append("| %s | %.4f | %.4f | %.4f | %+0.4f | %d/%d | %.4f | %+0.4f | %s |"
                 % (k, r["fold_MAE"][0], r["fold_MAE"][1], r["fold_MAE"][2],
                    r["mean_delta"], r["fold_wins"], SCREEN_FOLDS, r["pooled_MAE"],
                    r["pooled_delta"], "PASS" if r["pass"] else "fail"))
    L += ["\n## 판정\n\n```text\n%s\n```\n" % verdict]
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)
    print("\n총 %.0f초" % (time.time() - t_all))
    return payload


if __name__ == "__main__":
    main()
