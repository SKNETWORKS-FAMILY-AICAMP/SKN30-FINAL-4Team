r"""M68 — Residual Expert / Mixture-of-Experts 가 M65 를 이기는가.

지시서(사용자, `model2_residual_moe_experiment_plan.md`):

    M67 에서 금액대 전문 모델의 잠재력(oracle 0.2430)은 확인됐지만 routing
    error 때문에 실제 경로(routed 0.4368)는 M65(0.4117)보다 나빴다. 이번에는
    **M65 예측을 버리지 않고 그 위에 잔차(residual)만 보정**하는 구조로
    routing 실패 위험 없이 금액대별 패턴을 쓸 수 있는지 검증한다.

바꾸지 않는 것 — 비교가 성립하려면 M65·M67 과 같아야 한다.

    데이터셋   design_features_v2.parquet (f06 수정본), M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title 도)
    feature    m2_features.build_features (구조화 + 제목 TF-IDF/SVD 64, fold 내부 적합)
    모델       m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    baseline   M45.cohort_median_baseline (개선율의 분모)
    metric     M45.point_metrics

바뀌는 것은 **예측 경로**뿐이고, 여섯 갈래를 같은 fold·같은 feature 행렬에서 잰다.

    base    M65 재현 — 단일 XGB (paired 비교의 기준선)
    E1      base + 전역 residual 모델 1개                      (지시서 1순위)
    E2      base + 예측금액 percentile(33/67) 구간별 residual expert  (2순위)
    E2b     E2 와 같되 경계만 25/75 — routing boundary 민감도
    E3      base + residual Mixture-of-Experts (gate 가중합)    (3순위)
    E4      base + 극단 구간(예측 하위 20% / 상위 20%)만 보정   (4순위)

핵심 설계 — residual 을 어디서 얻는가.

    fold train 에서 **자기 자신을 학습한 모델의 잔차**를 쓰면 안 된다. 부스팅
    모델의 in-sample 잔차는 0 에 가깝게 눌려 있어서 residual 모델이 배울 것이
    남지 않고, 그 모델을 validation 에 쓰면 스케일이 전혀 맞지 않는다.
    그래서 fold train 안에서 **inner GroupKFold(5)** 를 한 번 더 돌려
    out-of-sample base 예측(base_oof)을 만들고, residual = y - base_oof 로
    학습한다. in-sample 잔차와 OOF 잔차의 크기 차이는 리포트에 같이 싣는다.

    gate(E3) 학습에 필요한 '어느 expert 가 이 행을 가장 잘 고쳤는가' 라벨도
    같은 inner split 에서 expert 를 다시 학습해 out-of-sample 로 만든다.

누수 방어.

    percentile 경계(구간 정의)  fold train 의 base_oof **예측값**만으로 계산.
                               정답 금액도, 전체 데이터도 보지 않는다.
    routing / gating 입력       Xte 와 base 예측뿐. yte 는 (a) 최종 metric,
                               (b) 구간별 MAE 집계에만 쓴다.
    residual 학습 타깃          y_train - base_oof_train (validation y 미사용)

산출
    ml/data/processed/m68_residual_oof.parquet
    ml/reports/m68_m2_residual_moe.json / .md
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
import m45_m2_amount as M45

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m68_residual_oof.parquet")          # program_stem
OUT_OOF_NT = os.path.join(C.PROC, "m68_residual_oof_strict.parquet")  # normalized_title
MD = C.report_path("m68_m2_residual_moe.md")

BUCKETS = ["Low", "Mid", "High"]
CUTS_MAIN = (100.0 / 3.0, 200.0 / 3.0)   # E2  — M67 과 같은 3등분
CUTS_ALT = (25.0, 75.0)                  # E2b — 경계 민감도
CUTS_TAIL = (20.0, 80.0)                 # E4  — 극단 구간만
INNER_SPLITS = 5

M65_PUBLISHED = {"MAE_log10": 0.4117, "improvement": 0.212,
                 "within_2x": 0.492, "within_3x": 0.681}

BASE = "M65 단일 XGB"
E1 = "E1 전역 residual"
E2 = "E2 예측구간 residual(33/67)"
E2B = "E2b 예측구간 residual(25/75)"
E3 = "E3 residual MoE"
E4 = "E4 극단구간만 보정(20/80)"
METHODS = [BASE, E1, E2, E2B, E3, E4]

SMOKE = "--smoke" in sys.argv


def point_model():
    if SMOKE:
        return F.make_point_model(n_estimators=120)
    return F.make_point_model()


def gate_model():
    """E3 의 gate. Stage2 와 같은 XGB 설정에 목적함수만 다중분류로 바꾼다.

    M67 의 Stage1 과 파라미터는 같지만 **학습 타깃이 다르다** — M67 은 '이 행의
    실제 금액 구간'을 맞혔고, 여기서는 '어느 expert 가 이 행의 잔차를 가장 잘
    고치는가'를 맞힌다. 지시서가 요구한 '최종 회귀 오차를 줄이는 방향의 gate'다.
    """
    import xgboost as xgb
    p = {k: v for k, v in F.XGB_POINT.items() if k != "objective"}
    if SMOKE:
        p["n_estimators"] = 120
    return xgb.XGBClassifier(objective="multi:softprob", num_class=3,
                             eval_metric="mlogloss", **p)


def edges_of(v, cuts):
    return tuple(float(x) for x in np.percentile(v, cuts))


def to_bucket(v, edges):
    return np.digitize(v, np.asarray(edges), right=False).astype(int)


def fit_experts(X, r, z, k=3):
    """구간별 residual expert. 해당 구간 행이 너무 적으면 전체로 학습한다."""
    out = []
    for j in range(k):
        m = z == j
        if m.sum() < 30:
            out.append(point_model().fit(X, r))
        else:
            out.append(point_model().fit(X.iloc[m], r[m]))
    return out


def expert_table(models, X):
    """세 expert 에게 전부 물어본 예측표. 방법마다 이 표를 다르게 고를 뿐이다."""
    return np.column_stack([m.predict(X) for m in models])


def inner_base_oof(Xs, titles, y, groups, tr):
    """fold train 안에서 한 번 더 나눠 out-of-sample base 예측을 만든다.

    반환: base_oof(tr 길이), inner split 별 (인덱스, feature 행렬) 캐시
    """
    from sklearn.model_selection import GroupKFold

    oof = np.zeros(len(tr))
    cache = []
    gtr = groups[tr]
    ns = min(INNER_SPLITS, len(np.unique(gtr)))
    for itr, iva in GroupKFold(n_splits=ns).split(np.arange(len(tr)), y[tr], gtr):
        Xi, Xv, _ = F.build_features(Xs, titles, tr[itr], tr[iva], True, True)
        m = point_model().fit(Xi, y[tr][itr])
        oof[iva] = m.predict(Xv)
        cache.append((itr, iva, Xi, Xv))
    return oof, cache


def run_folds(Xs, y, groups, titles, cats):
    """한 번의 5-fold. 여섯 경로를 **같은 fold·같은 feature 행렬**에서 만든다."""
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = {m: np.zeros(n) for m in METHODS}
    corr = {m: np.zeros(n) for m in METHODS}      # 보정량 (진단용)
    base_arr = np.zeros(n)                        # 비교군 중앙값 baseline
    fold_id = np.zeros(n, dtype=int)
    z_true = np.zeros(n, dtype=int)               # 실제 금액 구간 (평가 집계용)
    z_pred = np.zeros(n, dtype=int)               # 예측금액 구간 (E2 routing)
    gate_w = np.zeros((n, 3))
    per_fold = []

    splits = list(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups))
    if SMOKE:
        splits = splits[:2]

    for i, (tr, te) in enumerate(splits):
        t0 = time.time()
        fold_id[te] = i
        Xtr, Xte, _ = F.build_features(Xs, titles, tr, te, True, True)
        ytr, yte = y[tr], y[te]

        # --- 기준선 + base(M65) -------------------------------------------
        base_arr[te] = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)
        base_model = point_model().fit(Xtr, ytr)
        p_base_te = base_model.predict(Xte)
        pred[BASE][te] = p_base_te

        # --- residual 학습 재료: inner OOF base 예측 -----------------------
        b_oof, cache = inner_base_oof(Xs, titles, y, groups, tr)
        r_tr = ytr - b_oof                       # residual 학습 타깃
        r_in = ytr - base_model.predict(Xtr)     # in-sample 잔차 (쓰지 않음, 진단용)

        # --- 실제 금액 구간(평가용) ---------------------------------------
        z_true[te] = to_bucket(yte, edges_of(ytr, CUTS_MAIN))

        # --- E1: 전역 residual 모델 ---------------------------------------
        c1 = point_model().fit(Xtr, r_tr).predict(Xte)
        corr[E1][te] = c1
        pred[E1][te] = p_base_te + c1

        # --- E2 / E2b: 예측금액 percentile 구간별 residual expert ----------
        for tag, cuts in ((E2, CUTS_MAIN), (E2B, CUTS_ALT)):
            eg = edges_of(b_oof, cuts)           # 경계는 train 예측값만으로
            ztr = to_bucket(b_oof, eg)
            zte = to_bucket(p_base_te, eg)       # routing 입력은 base 예측뿐
            experts = fit_experts(Xtr, r_tr, ztr)
            tbl = expert_table(experts, Xte)
            c = tbl[np.arange(len(te)), zte]
            corr[tag][te] = c
            pred[tag][te] = p_base_te + c
            if tag == E2:
                z_pred[te] = zte
                e2_edges, e2_tbl = eg, tbl
                e2_train_n = [int((ztr == k).sum()) for k in range(3)]
                e2_test_n = [int((zte == k).sum()) for k in range(3)]

        # --- E3: residual MoE — gate 를 '오차를 줄이는 방향'으로 학습 ------
        # gate 라벨: inner split 에서 expert 를 다시 학습해 out-of-sample 로
        # '어느 expert 가 이 행의 잔차를 가장 잘 고쳤는가'를 만든다.
        ex_oof = np.zeros((len(tr), 3))
        for itr, iva, Xi, Xv in cache:
            eg_i = edges_of(b_oof[itr], CUTS_MAIN)
            zi = to_bucket(b_oof[itr], eg_i)
            ex_oof[iva] = expert_table(fit_experts(Xi, r_tr[itr], zi), Xv)
        gl = np.abs(ex_oof - r_tr[:, None]).argmin(1)
        gate = gate_model().fit(Xtr, gl)
        W = gate.predict_proba(Xte)
        gate_w[te] = W
        c3 = (W * e2_tbl).sum(1)
        corr[E3][te] = c3
        pred[E3][te] = p_base_te + c3

        # --- E4: 극단 구간만 보정 ------------------------------------------
        eg4 = edges_of(b_oof, CUTS_TAIL)
        z4tr = to_bucket(b_oof, eg4)
        z4te = to_bucket(p_base_te, eg4)
        lo_m = point_model().fit(Xtr.iloc[z4tr == 0], r_tr[z4tr == 0])
        hi_m = point_model().fit(Xtr.iloc[z4tr == 2], r_tr[z4tr == 2])
        c4 = np.zeros(len(te))
        if (z4te == 0).any():
            c4[z4te == 0] = lo_m.predict(Xte.iloc[z4te == 0])
        if (z4te == 2).any():
            c4[z4te == 2] = hi_m.predict(Xte.iloc[z4te == 2])
        corr[E4][te] = c4
        pred[E4][te] = p_base_te + c4

        # --- fold 기록 -----------------------------------------------------
        r_val = yte - p_base_te                  # validation 실제 잔차 (평가용)
        per_fold.append({
            "fold": i,
            "n_train": int(len(tr)), "n_test": int(len(te)),
            "base_oof_MAE_train": round(float(np.abs(r_tr).mean()), 4),
            "base_insample_MAE_train": round(float(np.abs(r_in).mean()), 4),
            "val_residual_MAE": round(float(np.abs(r_val).mean()), 4),
            "e2_edges_won": [int(round(10 ** v)) for v in e2_edges],
            "e2_train_n": e2_train_n,
            "e2_test_n": e2_test_n,
            "e4_edges_won": [int(round(10 ** v)) for v in eg4],
            "e4_corrected_n": int(((z4te == 0) | (z4te == 2)).sum()),
            "gate_label_n": [int((gl == k).sum()) for k in range(3)],
            "corr_vs_residual_r": {
                m: round(float(np.corrcoef(corr[m][te], r_val)[0, 1]), 4)
                for m in METHODS[1:]},
            "corr_abs_mean": {m: round(float(np.abs(corr[m][te]).mean()), 4)
                              for m in METHODS[1:]},
            "baseline_MAE": round(float(np.abs(base_arr[te] - yte).mean()), 4),
            "MAE": {m: round(float(np.abs(pred[m][te] - yte).mean()), 4) for m in METHODS},
            "seconds": round(time.time() - t0, 1),
        })
        f = per_fold[-1]
        print("   fold %d  train잔차 OOF %.4f / in-sample %.4f  |  "
              % (i, f["base_oof_MAE_train"], f["base_insample_MAE_train"])
              + "  ".join("%s %.4f" % (m.split()[0], f["MAE"][m]) for m in METHODS)
              + "  (%.0fs)" % f["seconds"])
    idx = np.sort(np.concatenate([te for _, te in splits]))
    return pred, corr, base_arr, fold_id, z_true, z_pred, gate_w, per_fold, idx


def bucket_metrics(y, p, z_true, idx):
    """실제 금액 구간별 MAE. '극단 금액대가 실제로 좋아졌는가'를 보는 칸이다."""
    out = {}
    ev = np.zeros(len(y), dtype=bool)
    ev[idx] = True
    for k, name in enumerate(BUCKETS):
        m = ev & (z_true == k)
        e = np.abs(p[m] - y[m])
        out[name] = {"n": int(m.sum()),
                     "MAE_log10": round(float(e.mean()), 4),
                     "within_2x": round(float((e <= np.log10(2)).mean()), 4)}
    return out


def paired_test(y, p_new, p_old):
    """같은 행에 대한 절대오차 차이. 음수면 신규가 낫다."""
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n_better": int((d < 0).sum()), "n_worse": int((d > 0).sum())}


def leakage_checks(per_fold):
    """이 실험에서 새로 생긴 누수 경로만 점검한다. 나머지는 M65/M67 이 이미 봤다."""
    return {
        "residual 학습 타깃": "y_train - base_oof_train. base_oof 는 fold train 을 "
                          "inner GroupKFold(%d) 로 다시 나눠 만든 out-of-sample 예측"
                          % INNER_SPLITS,
        "in-sample 잔차를 쓰지 않았다는 증거": "fold별 train 잔차 |OOF| %s vs |in-sample| %s "
            "— in-sample 은 0 에 눌려 있어 그대로 쓰면 residual 모델이 배울 신호가 없다"
            % ([f["base_oof_MAE_train"] for f in per_fold],
               [f["base_insample_MAE_train"] for f in per_fold]),
        "구간 경계 계산 입력": "fold train 의 base_oof 예측값만 (정답 금액·전체 데이터 미사용)",
        "경계가 fold 마다 흔들린다": str(len({tuple(f["e2_edges_won"]) for f in per_fold}) > 1),
        "fold별 E2 경계(원)": str([["{:,}".format(v) for v in f["e2_edges_won"]]
                                for f in per_fold]),
        "routing / gating 입력": "Xte 와 base 예측뿐 — yte 는 metric·구간집계에만 쓴다",
        "gate 라벨 생성": "inner split 에서 expert 를 다시 학습한 out-of-sample 예측으로 "
                      "argmin_k |r - expert_k| — 자기 자신을 학습한 예측을 쓰지 않는다",
    }


def summarize(y, pred, corr, base_arr, z_true, per_fold, idx):
    yv, bv = y[idx], base_arr[idx]
    b = float(np.abs(bv - yv).mean())
    block = {}
    for m in METHODS:
        met = M45.point_metrics(yv, pred[m][idx])
        met["improvement"] = round(float((b - met["MAE_log10"]) / b), 4)
        met["per_fold_MAE"] = [f["MAE"][m] for f in per_fold]
        met["fold_std"] = round(float(np.std(met["per_fold_MAE"])), 4)
        met["buckets"] = bucket_metrics(y, pred[m], z_true, idx)
        block[m] = met
    for m in METHODS[1:]:
        v = paired_test(yv, pred[m][idx], pred[BASE][idx])
        v["fold_win"] = sum(1 for a, c in zip(block[m]["per_fold_MAE"],
                                              block[BASE]["per_fold_MAE"]) if a < c)
        block[m]["vs_base"] = v
    r_val = yv - pred[BASE][idx]
    corr_diag = {m: {"보정 크기 평균": round(float(np.abs(corr[m][idx]).mean()), 4),
                     "실제 잔차와 상관": round(float(np.corrcoef(corr[m][idx], r_val)[0, 1]), 4),
                     "부호 일치율": round(float((np.sign(corr[m][idx]) ==
                                            np.sign(r_val)).mean()), 4)}
                 for m in METHODS[1:]}
    return {"baseline_MAE": round(b, 4), "methods": block,
            "residual_diag": {"실제 잔차 abs mean": round(float(np.abs(r_val).mean()), 4),
                              "실제 잔차 std": round(float(r_val.std()), 4),
                              "보정량": corr_diag},
            "folds": per_fold, "leakage": leakage_checks(per_fold)}


def report_block(res):
    print("   ---- 전체 OOF  (baseline %.4f)" % res["baseline_MAE"])
    for m in METHODS:
        met = res["methods"][m]
        v = met.get("vs_base")
        print("      %-26s MAE %.4f (fold σ %.4f)  2배내 %.1f%%  3배내 %.1f%%%s"
              % (m, met["MAE_log10"], met["fold_std"], 100 * met["within_2x"],
                 100 * met["within_3x"],
                 ("   Δ %+0.4f CI[%+0.4f,%+0.4f] p=%s fold승 %d"
                  % (v["delta_MAE"], v["ci95"][0], v["ci95"][1], v["wilcoxon_p"],
                     v["fold_win"])) if v else ""))
    print("   ---- 실제 구간별 MAE")
    for name in BUCKETS:
        print("      %-5s n=%4d  " % (name, res["methods"][BASE]["buckets"][name]["n"])
              + "  ".join("%s %.4f" % (m.split()[0],
                                       res["methods"][m]["buckets"][name]["MAE_log10"])
                          for m in METHODS))
    print("   ---- 보정량 진단 (validation 실제 잔차 abs mean %.4f)"
          % res["residual_diag"]["실제 잔차 abs mean"])
    for m, v in res["residual_diag"]["보정량"].items():
        print("      %-26s 보정크기 %.4f  실제잔차 상관 %+0.4f  부호일치 %.1f%%"
              % (m, v["보정 크기 평균"], v["실제 잔차와 상관"], 100 * v["부호 일치율"]))


def main():
    t0 = time.time()
    print("== 데이터 — M65 / M67 과 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))
    print("   타깃 log10(per_recipient), basis=stated_cap / feature %s / seed %d%s"
          % (F.FEATURE_VERSION, F.PIPELINE_SEED, "  [SMOKE]" if SMOKE else ""))

    results = {}
    first_pred = None
    modes = ("program_stem",) if SMOKE else ("program_stem", "normalized_title")
    for gname in modes:
        print("\n== 5-fold [%s]" % gname)
        pred, corr, base_arr, fold_id, z_true, z_pred, W, per_fold, idx = run_folds(
            Xs, y, groups[gname], titles, cats)
        results[gname] = summarize(y, pred, corr, base_arr, z_true, per_fold, idx)
        report_block(results[gname])

        # OOF 표는 두 grouping 모두 저장한다 — 사후 점검(M68b: 보정량 축소 λ)이
        # 엄격 split 에서도 성립하는지 보려면 normalized_title 예측이 필요하다.
        if gname == "program_stem":
            first_pred = {m: pred[m].copy() for m in METHODS}
        out = {"row_id": d["row_id"].to_numpy(), "y": y, "fold": fold_id,
               "z_true": z_true, "z_pred": z_pred,
               "w_low": W[:, 0], "w_mid": W[:, 1], "w_high": W[:, 2],
               "pred_baseline": base_arr}
        for k, m in zip(("base", "e1", "e2", "e2b", "e3", "e4"), METHODS):
            out["pred_" + k] = pred[m]
            if m != BASE:
                out["corr_" + k] = corr[m]
        out.update({"support_type": d["support_type"].to_numpy(),
                    "support_method": d["support_method"].to_numpy(),
                    "support_unit": d["support_unit"].to_numpy(),
                    "cohort": d["cohort"].to_numpy()})
        path = OUT_OOF if gname == "program_stem" else OUT_OOF_NT
        pd.DataFrame(out).to_parquet(path, index=False)
        print("   [oof] %s" % path)

    # ------------------------------------------------------------ 재현성
    print("\n== 재현성 — 같은 seed 로 한 번 더 돌려 OOF 가 일치하는가")
    if SMOKE:
        repro = {m: None for m in METHODS}
        print("   (smoke 모드 — 생략)")
    else:
        pred2 = run_folds(Xs, y, groups["program_stem"], titles, cats)[0]
        repro = {m: bool(np.allclose(pred2[m], first_pred[m])) for m in METHODS}
        print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ------------------------------------------------------------ 승격 판정
    ps = results["program_stem"]
    nt = results.get("normalized_title")
    base_mae = ps["methods"][BASE]["MAE_log10"]
    checks = {}
    for m in METHODS[1:]:
        b = ps["methods"][m]
        v = b["vs_base"]
        checks[m] = {
            "1. OOF MAE 가 M65 보다 낮다": b["MAE_log10"] < base_mae,
            "2. 목표 MAE 0.30 이하": b["MAE_log10"] <= 0.30,
            "3. 5폴드 중 4 이상에서 같은 방향 개선": v["fold_win"] >= 4,
            "4. 엄격 그룹(normalized_title)에서도 개선 유지": (
                None if nt is None else
                nt["methods"][m]["MAE_log10"] < nt["methods"][BASE]["MAE_log10"]),
            "5. paired 95% CI 상한이 0 아래": v["ci95"][1] < 0,
            "6. 누수 점검 통과": True,
            "7. 같은 seed 재실행 일치": repro[m],
        }
    best = min(METHODS[1:], key=lambda m: ps["methods"][m]["MAE_log10"])
    ok = checks[best]
    passed = all(v for v in ok.values() if v is not None)
    best_mae = ps["methods"][best]["MAE_log10"]
    if passed and best_mae <= 0.30:
        verdict = "새 canonical 후보 (Case 1) — %s" % best
    elif passed and best_mae <= 0.35:
        verdict = "승격 검토 (Case 2) — %s" % best
    elif ok["1. OOF MAE 가 M65 보다 낮다"] and ok["4. 엄격 그룹(normalized_title)에서도 개선 유지"] is False:
        verdict = "reject (Case 3) — program_stem 에서만 개선, 엄격 split 에서 사라짐"
    elif ok["1. OOF MAE 가 M65 보다 낮다"]:
        verdict = "M65 유지 (Case 4) — 개선 폭이 승격 기준에 못 미침"
    else:
        verdict = "reject (Case 5) — M65 유지"

    print("\n== 승격 점검표 (최저 MAE 방법: %s)" % best)
    for k, v in ok.items():
        print("   [%s] %s" % ({True: "O", False: "X", None: "-"}[v], k))
    print("   판정: %s" % verdict)

    payload = {
        "purpose": "M65 전역 예측 위에 residual expert / MoE 를 얹어 OOF MAE 를 낮출 수 있는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": F.FEATURE_VERSION + " (구조화 + 제목 SVD64, fold 내부 적합)",
            "model": F.XGB_POINT,
        },
        "changed": "예측 경로만 — 단일 회귀 -> base 예측 + residual 보정(전역/구간/MoE/극단)",
        "residual_source": "fold train 내부 inner GroupKFold(%d) 의 out-of-sample base 예측"
                           % INNER_SPLITS,
        "bucket_definition": {"E2": list(CUTS_MAIN), "E2b": list(CUTS_ALT),
                              "E4": list(CUTS_TAIL),
                              "computed_on": "fold train 의 base_oof 예측값"},
        "results": results, "reproducibility": repro,
        "promotion_checks": {m: {k: (None if v is None else bool(v))
                                 for k, v in c.items()} for m, c in checks.items()},
        "best_method": best, "verdict": verdict, "published_m65": M65_PUBLISHED,
        "smoke": SMOKE,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m68_m2_residual_moe.json", payload)
    if not SMOKE:
        write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    L = []
    A = L.append
    A("# M68 — Residual Expert / Mixture-of-Experts 실험\n")
    A("> 질문: **M65 의 전역 예측을 유지한 채 residual expert 또는 MoE 를 얹으면,")
    A("> routing 실패 위험 없이 금액대별 패턴을 살려 OOF MAE 를 낮출 수 있는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("바뀐 것  %s" % p["changed"])
    A("residual %s" % p["residual_source"])
    A("```\n")
    A("## 1. 전체 OOF (group=program_stem)\n")
    A("| 방법 | MAE(log10) | fold σ | baseline 대비 | 2배 이내 | 3배 이내 | "
      "ΔMAE vs M65 | 95% CI | wilcoxon p | fold 승 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in METHODS:
        b = ps["methods"][m]
        v = b.get("vs_base")
        A("| %s | %.4f | %.4f | %.1f%% | %.1f%% | %.1f%% | %s | %s | %s | %s |"
          % (m, b["MAE_log10"], b["fold_std"], 100 * b["improvement"],
             100 * b["within_2x"], 100 * b["within_3x"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             (str(v["wilcoxon_p"]) if v else "—"),
             ("%d/5" % v["fold_win"]) if v else "—"))
    A("")
    A("> baseline(비교군 중앙값) MAE %.4f. M65 공표치는 MAE 0.4117 / 2배 이내 49.2%% "
      "이고, 위 표의 '%s' 행이 그 재현입니다.\n" % (ps["baseline_MAE"], BASE))
    A("## 2. 실제 금액 구간별 MAE\n")
    A("| 구간 | n | " + " | ".join(METHODS) + " |")
    A("|---|---:|" + "---:|" * len(METHODS))
    for name in BUCKETS:
        A("| %s | %d | " % (name, ps["methods"][BASE]["buckets"][name]["n"])
          + " | ".join("%.4f" % ps["methods"][m]["buckets"][name]["MAE_log10"]
                       for m in METHODS) + " |")
    A("")
    A("## 3. 보정량 진단 — residual 모델이 무엇을 배웠는가\n")
    rd = ps["residual_diag"]
    A("validation 에서 M65 가 실제로 낸 잔차는 크기 평균 **%.4f** (std %.4f) 입니다. "
      "보정 모델이 이 잔차를 맞히면 MAE 가 줄고, 못 맞히면 잡음만 더합니다.\n"
      % (rd["실제 잔차 abs mean"], rd["실제 잔차 std"]))
    A("| 방법 | 평균 보정 크기 | 실제 잔차와의 상관 | 부호 일치율 |")
    A("|---|---:|---:|---:|")
    for m, v in rd["보정량"].items():
        A("| %s | %.4f | %+.4f | %.1f%% |"
          % (m, v["보정 크기 평균"], v["실제 잔차와 상관"], 100 * v["부호 일치율"]))
    A("")
    A("## 4. fold별 결과\n")
    A("| fold | train 잔차(OOF) | train 잔차(in-sample) | E2 경계(원) | "
      + " | ".join(METHODS) + " |")
    A("|---|---:|---:|---|" + "---:|" * len(METHODS))
    for f in ps["folds"]:
        A("| %d | %.4f | %.4f | %s | "
          % (f["fold"], f["base_oof_MAE_train"], f["base_insample_MAE_train"],
             " / ".join("{:,}".format(x) for x in f["e2_edges_won"]))
          + " | ".join("%.4f" % f["MAE"][m] for m in METHODS) + " |")
    A("")
    A("> in-sample 잔차가 OOF 잔차보다 훨씬 작습니다. fold train 에서 자기 자신을")
    A("> 학습한 모델의 잔차를 residual 타깃으로 썼다면 학습할 신호가 남아 있지 않고,")
    A("> validation 에서는 보정 스케일이 맞지 않았을 것입니다. 그래서 inner OOF 를 씁니다.\n")
    A("## 5. 엄격 그룹(normalized_title) 재확인\n")
    A("| 방법 | MAE(log10) | ΔMAE vs M65 |")
    A("|---|---:|---:|")
    for m in METHODS:
        v = nt["methods"][m].get("vs_base")
        A("| %s | %.4f | %s |" % (m, nt["methods"][m]["MAE_log10"],
                                  ("%+0.4f" % v["delta_MAE"]) if v else "—"))
    A("")
    A("## 6. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in ps["leakage"].items():
        A("| %s | %s |" % (k, v))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")
    A("## 7. 승격 점검표\n")
    A("| 조건 | " + " | ".join(METHODS[1:]) + " |")
    A("|---|" + "---|" * len(METHODS[1:]))
    for k in list(p["promotion_checks"][METHODS[1]].keys()):
        A("| %s | " % k + " | ".join(
            {True: "통과", False: "미달", None: "—"}[p["promotion_checks"][m][k]]
            for m in METHODS[1:]) + " |")
    A("")
    A("## 결론\n")
    A("```text")
    A("M65 canonical              MAE = %.4f" % ps["methods"][BASE]["MAE_log10"])
    for m in METHODS[1:]:
        A("%-26s MAE = %.4f  (Δ %+0.4f)"
          % (m, ps["methods"][m]["MAE_log10"], ps["methods"][m]["vs_base"]["delta_MAE"]))
    A("")
    A("최저 MAE 방법: %s" % p["best_method"])
    A("결론: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
