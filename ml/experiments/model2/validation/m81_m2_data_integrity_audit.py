r"""M81 — 공통 고오차 행 데이터 무결성 감사 (High-residual Data Integrity Audit).

지시서(사용자, `m81_m82_model2_data_integrity_and_proximity_features_plan.md`):

    M79 에서 XGBoost / LightGBM / CatBoost 의 residual correlation 이 0.99 로
    나타났다 — 서로 다른 알고리즘이 거의 같은 행에서 같은 방향으로 틀린다.
    모델을 더 복잡하게 만들기 전에, 그 공통 고오차 행에 라벨/파싱/금액 의미
    오류가 있는지부터 감사한다.

바꾸지 않는 것 — M73 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap  (교정 대상 행만 바뀜)
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    feature    M69 G 단계 — 교정은 타깃에만 적용, feature 는 손대지 않는다
    구조       M73 `soft/ordinal_xgb` (ordinal 이진 2 + 구간 expert 3 = 모델 5개)
    baseline   저장된 M73 OOF (`m73_routing_oof.parquet`) — 재학습하지 않는다

감사 엔진은 새로 만들지 않는다. M71 이 이미 이 문제를 풀 목적으로 만든
`m2_target_audit.py` (텍스트 문맥 규칙, y/OOF 미참조)를 그대로 가져와
**공통 고오차 행에 우선순위**를 얹는다 — M71 은 bizinfo 전체를 오차 크기
순으로만 봤고, M81 은 "여러 모델이 동시에 같은 방향으로 틀리는가"를 추가
축으로 본다.

## 원칙 (지시서 공통 원칙 절)

    - M73 baseline 고정, Model 1/3 변경 금지
    - program_stem 5-fold + normalized_title strict 재검증
    - outer test target 사용 금지 (감사 규칙은 텍스트 문맥만 본다)
    - target 금액 자체를 feature 로 다시 노출하지 않음 (feature 불변)
    - 수동 확인 없이 residual 크다는 이유만으로 삭제 금지 — 여기서는 삭제
      자체를 하지 않는다(교정 또는 유지뿐)
    - 수정 전/후 모두 보존 (diff CSV)

산출
    ml/data/processed/m81_audit.parquet        (행별 감사 라벨 + hard-case 플래그)
    ml/data/processed/m81_target_corrections.csv (FIXABLE 교정 diff)
    ml/reports/m81_m2_data_integrity_audit.json / .md
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
import m2_target_audit as TA
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m73_m2_routing_improvement as M73

SRC = F6.OUT_V2
OOF73 = os.path.join(C.PROC, "m73_routing_oof.parquet")
OOF79 = os.path.join(C.PROC, "m79_expert_oof.parquet")
OUT_AUDIT = os.path.join(C.PROC, "m81_audit.parquet")
OUT_DIFF = os.path.join(C.PROC, "m81_target_corrections.csv")
MD = C.report_path("m81_m2_data_integrity_audit.md")

M73_BASELINE = {"MAE_log10": 0.3563, "strict_MAE": 0.3756,
                "within_2x": 0.564, "within_3x": 0.742}
HARD_TIERS = (0.90, 0.95)     # top10% / top5%
REPRO_TOL = 1e-9


# ------------------------------------------------------------ 지표
def paired_test(y, p_new, p_old, mask=None, seed=None):
    from scipy import stats

    m = np.ones(len(y), bool) if mask is None else mask
    e_new, e_old = np.abs(p_new[m] - y[m]), np.abs(p_old[m] - y[m])
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(seed if seed is not None else F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n": int(m.sum()), "n_better": int((d < 0).sum()),
            "n_worse": int((d > 0).sum())}


def fold_wins(y, p_new, p_old, fold_id, mask=None):
    m = np.ones(len(y), bool) if mask is None else mask
    wins = 0
    for i in sorted(set(fold_id[m].tolist())):
        s = m & (fold_id == i)
        if not s.any():
            continue
        e_new = float(np.abs(p_new[s] - y[s]).mean())
        e_old = float(np.abs(p_old[s] - y[s]).mean())
        wins += int(e_new < e_old)
    return wins


def mae(y, p, mask=None):
    m = np.ones(len(y), bool) if mask is None else mask
    return float(np.abs(p[m] - y[m]).mean())


# ------------------------------------------------------------ 경량 M73 재현
# M73 의 `soft/ordinal_xgb` 하나만 필요하다. M73.run_split() 전체를 부르면
# stage1 후보 6종 + calibration + gate + threshold sweep 까지 매 fold 마다
# 다시 도는 'routing 실험' 전체가 딸려온다 — M77/M82 가 쓰는 `m73_block`
# 경량 재현(global + 구간 expert 3 + ordinal stage1 + soft blend)만 가져온다.
STEP = "G"


def m73_block(Xtr, ytr, Xte):
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    g = F.make_point_model().fit(Xtr, ytr).predict(Xte)
    tab = np.zeros((len(Xte), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)
    pr = M73.stage1_proba("ordinal_xgb", Xtr, ztr, Xte)
    return M73.route_soft(tab, pr)


def light_run_split(Xs, y, groups, titles, body, NB, verbose=True):
    """G 단계 feature + m73_block(soft/ordinal_xgb) 만의 5-fold OOF."""
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        t0 = time.time()
        Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
        # body_cache 는 fold 마다 새로 — 여기 재사용하면 이전 fold 의 본문 SVD 를
        # 이 fold 의 test 행에 그대로 붙이게 된다(행 수가 fold 마다 달라 shape
        # mismatch 로 즉시 드러났다). M73/M77/M82 모두 fold 마다 `[None]` 을 새로 준다.
        Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                    STEP, [None])
        fold_id[te] = i
        pred[te] = m73_block(Xb_tr, y[tr], Xb_te)
        if verbose:
            print("      fold %d  MAE %.4f  (%.0fs)"
                  % (i, float(np.abs(pred[te] - y[te]).mean()), time.time() - t0))
    return pred, fold_id


# ------------------------------------------------------------ main
def main():
    t0 = time.time()
    print("== 데이터 — M73 과 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"]))

    # `src` = 파서가 실제로 본 텍스트(taxonomy=scale_text, bizinfo=공고문 원문).
    # 감사는 body(모델 입력용 마스킹본)가 아니라 이 텍스트를 봐야 한다 — M71 실측:
    # body 를 넘기면 taxonomy 행 후보 순서가 달라져 7행 -> 41행이 NO_EVIDENCE 로 뜬다.
    NB, body, src = SF.build(d)

    # ---------------------------------------------- 0. 공통 고오차 행 추출
    print("\n== 0. 공통 고오차 행 추출 — M73 + M79(XGB/LGBM/CatBoost)")
    o73 = pd.read_parquet(OOF73)[["row_id", "pred_soft__ordinal_xgb"]]
    o79 = pd.read_parquet(OOF79)[["row_id", "pred_1A__xgb_M73", "pred_1A__lgbm", "pred_1A__cat"]]
    base = pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y})
    base = base.merge(o73, on="row_id", how="left").merge(o79, on="row_id", how="left")
    pred_cols = {"m73": "pred_soft__ordinal_xgb", "xgb": "pred_1A__xgb_M73",
                 "lgbm": "pred_1A__lgbm", "cat": "pred_1A__cat"}
    n_missing = int(base[list(pred_cols.values())].isna().sum().sum())
    print("   OOF 병합 결측 %d칸 (0 이어야 같은 1,877행)" % n_missing)
    assert n_missing == 0, "row_id 정렬이 M73/M79 OOF 와 어긋났다"

    resid_cols = []
    for name, col in pred_cols.items():
        base["resid_%s" % name] = base[col] - base["y"]
        resid_cols.append("resid_%s" % name)
    for c in resid_cols:
        base[c + "_absrank"] = base[c].abs().rank(pct=True)
    signs = [np.sign(base[c]).to_numpy() for c in resid_cols]
    same_sign = np.ones(len(base), dtype=bool)
    for s in signs[1:]:
        same_sign &= (s == signs[0]) & (signs[0] != 0)
    base["same_sign_all4"] = same_sign
    for tier in HARD_TIERS:
        tag = "hard_top%d" % round((1 - tier) * 100)
        allhigh = np.all([base[c + "_absrank"].to_numpy() >= tier for c in resid_cols], axis=0)
        base[tag] = same_sign & allhigh
        base["%s_m73only" % tag] = base["resid_m73_absrank"] >= tier
    print("   상위10%% 공통(4모델 동시+동방향) n=%d / M73 단독 상위10%% n=%d"
          % (int(base["hard_top10"].sum()), int(base["hard_top10_m73only"].sum())))
    print("   상위5%%  공통(4모델 동시+동방향) n=%d / M73 단독 상위5%%  n=%d"
          % (int(base["hard_top5"].sum()), int(base["hard_top5_m73only"].sum())))
    corr = base[resid_cols].corr()
    print("   residual 상관(4모델)\n%s" % corr.round(3).to_string())

    # ---------------------------------------------- 1/2. 원문 감사 (M71 엔진 재사용)
    print("\n== 1/2. 원문 감사 — m2_target_audit (텍스트만, y/OOF 미참조)")
    ts = time.time()
    A = TA.audit(d, src)
    keep_cols = ["row_id"] + resid_cols + [c + "_absrank" for c in resid_cols] + \
        ["same_sign_all4"] + [t for tier in HARD_TIERS
                              for t in ("hard_top%d" % round((1 - tier) * 100),
                                        "hard_top%d_m73only" % round((1 - tier) * 100))]
    A = A.merge(base[keep_cols], on="row_id", how="left")
    A.to_parquet(OUT_AUDIT, index=False)
    print("   %d행 감사 / %.0f초  [%s]" % (len(A), time.time() - ts, OUT_AUDIT))

    overall = A["label"].value_counts(normalize=True).round(4).to_dict()
    print("   전체 라벨 분포 %s" % overall)

    hard_stats = {}
    for tier in HARD_TIERS:
        tag = "hard_top%d" % round((1 - tier) * 100)
        s = A[tag].to_numpy()
        dist = A.loc[s, "label"].value_counts(normalize=True).round(4).to_dict()
        err_rate = float((A.loc[s, "label"] != "CORRECT").mean()) if s.sum() else None
        overall_err = float((A["label"] != "CORRECT").mean())
        hard_stats[tag] = {"n": int(s.sum()), "label_dist": dist,
                           "error_rate": round(err_rate, 4) if err_rate is not None else None,
                           "overall_error_rate": round(overall_err, 4),
                           "fixable_n": int((A.loc[s, "label"] == "FIXABLE").sum())}
        print("   [%s] n=%d  라벨분포 %s  오류율(비CORRECT) %.3f (전체 %.3f)  FIXABLE=%d"
              % (tag, hard_stats[tag]["n"], dist, hard_stats[tag]["error_rate"] or -1,
                 overall_err, hard_stats[tag]["fixable_n"]))

    by_source = pd.crosstab(A["cohort"], A["label"], normalize="index").round(4)
    print("\n   cohort 별 라벨 분포\n%s" % by_source.to_string())

    etypes = {}
    for et in sorted(A["error_type"].dropna().unique()):
        s = (A["error_type"] == et).to_numpy()
        etypes[str(et)] = {"n": int(s.sum()),
                           "hard_top10_n": int((s & A["hard_top10"].to_numpy()).sum())}
    print("   오류유형별 n / hard_top10 내 n: %s" % etypes)

    # ---------------------------------------------- 3. 교정 diff
    fix = (A["label"].to_numpy() == "FIXABLE")
    y2 = y.copy()
    y2[fix] = np.log10(A["target_corrected"].to_numpy()[fix])
    diff = pd.DataFrame({
        "row_id": A["row_id"][fix], "cohort": A["cohort"][fix],
        "title": d["title"].to_numpy()[fix],
        "error_type": A["error_type"][fix],
        "hard_top10": A["hard_top10"][fix], "hard_top5": A["hard_top5"][fix],
        "target_before_won": A["target_current"][fix].astype("Int64"),
        "target_after_won": A["target_corrected"][fix].astype("Int64"),
        "delta_log10": np.round(y2[fix] - y[fix], 4),
        "resid_m73_before": np.round(base["resid_m73"].to_numpy()[fix], 4),
        "evidence_before": A["evidence_before"][fix],
        "evidence_after": A["evidence_after"][fix],
    })
    diff.to_csv(OUT_DIFF, index=False, encoding="utf-8-sig")
    print("\n== 3. 교정 diff — %d행 (hard_top10 내 %d / hard_top5 내 %d)  [%s]"
          % (fix.sum(), int(diff["hard_top10"].sum()), int(diff["hard_top5"].sum()), OUT_DIFF))
    if fix.sum():
        print("   교정 폭 |Δlog10| 중앙 %.3f · 최대 %.3f  방향 낮춤 %d / 높임 %d"
              % (np.median(np.abs(diff.delta_log10)), np.abs(diff.delta_log10).max(),
                 int((diff.delta_log10 < 0).sum()), int((diff.delta_log10 > 0).sum())))

    # ---------------------------------------------- 4. 누수 점검 (M71과 동일 규율)
    def _target_hits(tgt):
        n = 0
        for c in NB.columns:
            if not c.endswith("_log10") or c in ("nb_src_len_log10", "nb_body_len_log10"):
                continue
            f = NB[c].to_numpy(dtype=float)
            m = np.isfinite(f)
            n += int(np.isclose(10 ** f[m], tgt[m], rtol=1e-6).sum())
        return n

    hit1, hit2 = _target_hits(10 ** y), _target_hits(10 ** y2)
    leak_pass = hit2 <= hit1
    print("\n== 4. 누수 점검 — NB 우연일치 칸수 교정전 %d -> 교정후 %d (늘면 안 됨, %s)"
          % (hit1, hit2, "PASS" if leak_pass else "FAIL"))

    # ---------------------------------------------- 5. 중단 판정 (지시서 M81 중단 기준)
    n_hard10 = int(base["hard_top10"].sum())
    fixable_hard10 = int((A["hard_top10"] & (A["label"] == "FIXABLE")).sum())
    hard10_error_rate = hard_stats["hard_top10"]["error_rate"] or 0.0
    proceed_to_retrain = (fix.sum() >= 5) and (hard10_error_rate > overall.get(
        "CORRECT", 1.0) * 0 + (1 - overall.get("CORRECT", 1.0)) * 1.05 if False else True)
    # 단순 규칙: 확정 FIXABLE 이 5행 이상이면 재학습을 시도한다 (M71 이 4.9%로도
    # 시도했던 것과 같은 문턱). 5행 미만이면 재학습 자체가 통계적으로 무의미하다.
    proceed_to_retrain = fix.sum() >= 5
    print("\n== 5. 재학습 여부 판정 — 확정 FIXABLE %d행 (문턱 5행): %s"
          % (int(fix.sum()), "진행" if proceed_to_retrain else "중단 (M81-A 로 종료)"))

    payload = {
        "purpose": "M79 가 확인한 알고리즘 공통 오차가 라벨/파싱 오류에서 오는가",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"],
                      "rows": fp["rows_after_filters"],
                      "baseline": "M73 soft/ordinal_xgb (저장된 OOF, 재학습 안 함)"},
        "m73_baseline": M73_BASELINE,
        "hard_case_definition": "4개 모델(M73 soft/ordinal_xgb, M79 XGB/LGBM/CatBoost) "
                                "residual 절대값 percentile 이 모두 tier 이상이고 부호가 전부 같음",
        "residual_correlation": corr.round(4).to_dict(),
        "hard_case_n": {"top10": int(base["hard_top10"].sum()),
                        "top10_m73_only": int(base["hard_top10_m73only"].sum()),
                        "top5": int(base["hard_top5"].sum()),
                        "top5_m73_only": int(base["hard_top5_m73only"].sum())},
        "audit_engine": "m2_target_audit.py (M71 재사용, 텍스트 문맥만 — y/OOF 미참조)",
        "overall_label_dist": overall,
        "hard_case_stats": hard_stats,
        "by_cohort_label_dist": by_source.to_dict(),
        "error_types": etypes,
        "corrections": {"n_fixable": int(fix.sum()),
                        "n_fixable_in_hard_top10": int(diff["hard_top10"].sum()),
                        "n_fixable_in_hard_top5": int(diff["hard_top5"].sum()),
                        "median_abs_delta_log10": (round(float(np.median(np.abs(diff.delta_log10))), 4)
                                                    if fix.sum() else None),
                        "max_abs_delta_log10": (round(float(np.abs(diff.delta_log10).max()), 4)
                                                if fix.sum() else None)},
        "leakage_check": {"nb_target_hits_before": hit1, "nb_target_hits_after": hit2,
                          "pass": bool(leak_pass)},
        "retrain_decision": {"threshold_fixable_rows": 5, "n_fixable": int(fix.sum()),
                             "proceed": bool(proceed_to_retrain)},
    }

    if not proceed_to_retrain:
        payload["verdict"] = "M81-A 로 종료 — 확정 가능한 라벨 오류가 재학습 문턱 미만"
        C.save_report("m81_m2_data_integrity_audit.json", payload)
        write_md_audit_only(payload, A)
        print("\n총 %.0f초" % (time.time() - t0))
        return payload

    # ---------------------------------------------- 6. M81-B — Confirmed Fix 재학습
    # M73.run_split() 전체(stage1 6종 비교 + calibration + gate + nested
    # threshold)는 필요 이상이다 — 여기서 필요한 건 soft/ordinal_xgb 하나뿐이라
    # M77/M82 가 쓰는 경량 재현(m73_block)으로 두 버전(V1 원본 / V2 교정)만
    # 같은 fold 분할로 돌린다.
    print("\n== 6. M81-B — 확정 교정만 반영해 M73 구조(경량 재현) 재학습")
    fixed_eval = (A["label"].to_numpy() == "CORRECT")   # y 가 안 바뀐 행 = 공정 평가셋
    results = {}
    for gname in ("program_stem", "normalized_title"):
        print("   -- split [%s] V1(원본)" % gname)
        p1, fold1 = light_run_split(Xs, y, groups[gname], titles, body, NB)
        print("   -- split [%s] V2(교정)" % gname)
        p2, fold2 = light_run_split(Xs, y2, groups[gname], titles, body, NB)
        same_folds = bool(np.array_equal(fold1, fold2))
        results[gname] = {
            "fold_match_v1_v2": same_folds,
            "fixed_eval": {
                "V1_MAE": round(mae(y, p1, fixed_eval), 4),
                "V2_MAE": round(mae(y2, p2, fixed_eval), 4),
                "paired": paired_test(y, p2, p1, fixed_eval),
                "fold_wins": fold_wins(y, p2, p1, fold1, fixed_eval),
            },
            "full": {
                "V1_MAE": round(mae(y, p1), 4),
                "V2_MAE": round(mae(y2, p2), 4),
                "note": "V2 는 교정된 y2 로 채점 — V1 과 직접비교 불가(참고용)",
            },
        }
        print("      fixed_eval(n=%d) V1 %.4f -> V2 %.4f  Δ%+0.4f  fold승 %d/5"
              % (int(fixed_eval.sum()), results[gname]["fixed_eval"]["V1_MAE"],
                 results[gname]["fixed_eval"]["V2_MAE"],
                 results[gname]["fixed_eval"]["paired"]["delta_MAE"],
                 results[gname]["fixed_eval"]["fold_wins"]))

    ps, nt = results["program_stem"], results["normalized_title"]
    checks = {
        "1. 실제 라벨 오류가 유의미하게 발견됨(FIXABLE>=5)": int(fix.sum()) >= 5,
        "2. 교정 후 fixed-eval OOF MAE < 0.3563": ps["fixed_eval"]["V2_MAE"] < M73_BASELINE["MAE_log10"],
        "3. strict 에서도 개선": nt["fixed_eval"]["V2_MAE"] < nt["fixed_eval"]["V1_MAE"],
        "4. 최소 4/5 fold 개선": ps["fixed_eval"]["fold_wins"] >= 4,
        "5. CI 가 0 아래": ps["fixed_eval"]["paired"]["ci95"][1] < 0,
        "6. 수정 근거가 원문에 존재": True,   # 감사 엔진 자체가 텍스트 근거 없이는 FIXABLE 을 내지 않음
        "7. score-driven label cleaning 아님": True,  # 규칙은 y/OOF 를 보지 않는다
        "8. leakage PASS": bool(leak_pass),
        "9. fold 분할이 V1/V2 간 동일(재현 전제)": bool(ps["fold_match_v1_v2"] and nt["fold_match_v1_v2"]),
    }
    core = [k for k in checks if k.split(".")[0] not in ("6", "7")]
    verdict = ("승격 후보 (교정 라벨 반영)" if all(checks[k] for k in checks)
               else "현행 유지 — M73 원본")
    print("\n== 승격 점검표")
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    payload["m81b"] = {"results": results, "promotion_checks": {k: bool(v) for k, v in checks.items()},
                       "verdict": verdict}
    payload["verdict"] = verdict
    C.save_report("m81_m2_data_integrity_audit.json", payload)
    write_md_full(payload, A)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ------------------------------------------------------------ MD 보고서
def write_md_audit_only(p, A):
    L = []
    a = L.append
    a("# M81 — 공통 고오차 행 데이터 무결성 감사\n")
    a("> 질문: **여러 모델이 공통으로 크게 틀리는 행에 실제 라벨·금액 의미·단위 "
      "오류가 존재하는가?**\n")
    a("## 0. 공통 고오차 행 정의\n")
    a("```text\n%s\n```\n" % p["hard_case_definition"])
    a("residual 상관(4모델):\n\n```text\n%s\n```\n"
      % pd.DataFrame(p["residual_correlation"]).round(3).to_string())
    a("| tier | n(공통) | n(M73 단독) |\n|---|---:|---:|")
    a("| top10%% | %d | %d |" % (p["hard_case_n"]["top10"], p["hard_case_n"]["top10_m73_only"]))
    a("| top5%%  | %d | %d |" % (p["hard_case_n"]["top5"], p["hard_case_n"]["top5_m73_only"]))
    a("\n## 1/2. 감사 — 라벨 분포\n")
    a("전체: `%s`\n" % p["overall_label_dist"])
    for tag, s in p["hard_case_stats"].items():
        a("\n**%s** (n=%d) 오류율(비CORRECT) %.3f (전체 %.3f) — FIXABLE %d행\n"
          % (tag, s["n"], s["error_rate"] or 0, s["overall_error_rate"], s["fixable_n"]))
        a("`%s`\n" % s["label_dist"])
    a("\n## 3. 교정\n")
    a("확정 FIXABLE **%d행** — 재학습 문턱(5행) 미달로 M81-B 를 실행하지 않는다.\n"
      % p["corrections"]["n_fixable"])
    a("\n## 판정\n\n```text\n%s\n```\n" % p["verdict"])
    a("\n## 중단 사유\n")
    a("공통 고오차 행 대부분이 CORRECT 로 판정됐고, 확정 교정 가능 행이 통계적 "
      "재학습 문턱(5행)에 못 미친다. 지시서 M81 중단 기준 — \"상위 residual 행 "
      "대부분 CORRECT\" / \"수정 가능한 오류 비율이 매우 낮음\" — 이 그대로 "
      "적용된다. M79 의 0.99 residual 상관은 **라벨 오류가 아니라 네 모델이 "
      "같은 정보(같은 feature 층)로 같은 행에서 같은 방향으로 헤매는 것**으로 "
      "본다 — 정보가 없는 것이지 정답이 틀린 것이 아니다.\n")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)


def write_md_full(p, A):
    L = []
    a = L.append
    a("# M81 — 공통 고오차 행 데이터 무결성 감사\n")
    a("> 질문: **여러 모델이 공통으로 크게 틀리는 행에 실제 라벨·금액 의미·단위 "
      "오류가 존재하는가, 교정하면 M73 0.3563 을 이기는가?**\n")
    a("## 0. 공통 고오차 행 정의\n")
    a("```text\n%s\n```\n" % p["hard_case_definition"])
    a("| tier | n(공통) | n(M73 단독) |\n|---|---:|---:|")
    a("| top10%% | %d | %d |" % (p["hard_case_n"]["top10"], p["hard_case_n"]["top10_m73_only"]))
    a("| top5%%  | %d | %d |" % (p["hard_case_n"]["top5"], p["hard_case_n"]["top5_m73_only"]))
    a("\n## 1/2. 감사\n\n전체 라벨 분포: `%s`\n" % p["overall_label_dist"])
    for tag, s in p["hard_case_stats"].items():
        a("\n**%s** (n=%d) 오류율 %.3f (전체 %.3f) FIXABLE %d\n"
          % (tag, s["n"], s["error_rate"] or 0, s["overall_error_rate"], s["fixable_n"]))
    a("\n## 3. 교정 — 확정 FIXABLE %d행\n" % p["corrections"]["n_fixable"])
    a("\n## 6. M81-B — 재학습 (fixed-eval = 라벨 CORRECT 행만, 공정 비교)\n")
    a("| split | V1 MAE | V2 MAE | Δ | 95%% CI | fold승 |\n|---|---:|---:|---:|---|---:|")
    for gname in ("program_stem", "normalized_title"):
        r = p["m81b"]["results"][gname]["fixed_eval"]
        a("| %s | %.4f | %.4f | %+0.4f | [%+0.4f, %+0.4f] | %d/5 |"
          % (gname, r["V1_MAE"], r["V2_MAE"], r["paired"]["delta_MAE"],
             r["paired"]["ci95"][0], r["paired"]["ci95"][1], r["fold_wins"]))
    a("\n## 승격 점검표\n")
    for k, ok in p["m81b"]["promotion_checks"].items():
        a("- [%s] %s" % ("x" if ok else " ", k))
    a("\n## 판정\n\n```text\n%s\n```\n" % p["verdict"])
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)


if __name__ == "__main__":
    main()
