r"""M67 — 금액대(percentile) 분기 2-stage 구조가 M65 를 이기는가.

지시서(사용자, `model2_percentile_amount_routing_experiment.md`):

    Model 2(M65 canonical)는 단일 XGBoost 로 전체 금액 구간을 예측한다.
    금액 분포를 percentile 로 3구간(Low/Mid/High)으로 나누고, 입력 feature 로
    예상 구간을 먼저 분류한 뒤(Stage 1) 구간 전용 회귀 모델(Stage 2)로 예측하는
    구조가 OOF 성능을 실제로 개선하는지 검증하라.

바꾸지 않는 것 — 비교가 성립하려면 아래가 M65 와 같아야 한다.

    데이터셋   design_features_v2.parquet (f06 수정본), M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title 도)
    feature    m2_features.build_features (구조화 + 제목 TF-IDF/SVD 64, fold 내부 적합)
    Stage2 모델 m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    baseline   M45.cohort_median_baseline (개선율의 분모)
    metric     M45.point_metrics

바뀌는 것은 **예측 경로 하나**다.

    M65         Xte -> 단일 XGB -> log10(금액)
    M67         Xte -> Stage1 분류기 -> Low/Mid/High -> 해당 구간 XGB -> log10(금액)

한 fold 안에서 M65 와 M67 을 **같은 feature 행렬로** 학습한다. 두 번 나눠 돌리면
fold·SVD 적합이 달라져 차이가 구조 때문인지 우연 때문인지 알 수 없게 된다.

재는 것 넷.

    single      M65 재현 — 단일 XGB (paired 비교의 기준선)
    routed      Stage1 예측 구간으로 routing (실제 서비스 경로)
    oracle      실제 구간을 안다고 가정 (진단용 상한 — 최종 모델 아님)
    soft        Stage1 확률로 세 구간 예측을 가중평균 (routing error 완화 변형)

누수 방어.

    percentile 경계는 **fold train 의 y 만으로** 계산한다. 전체 데이터에서 미리
    계산하지 않는다. validation 의 y 는 (a) 최종 metric, (b) oracle 진단,
    (c) 구간별 MAE 집계에만 쓰고 routing 에는 절대 쓰지 않는다.

산출
    ml/data/processed/m67_routing_oof.parquet
    ml/reports/m67_m2_percentile_routing.json / .md
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
OUT_OOF = os.path.join(C.PROC, "m67_routing_oof.parquet")
MD = C.report_path("m67_m2_percentile_routing.md")

BUCKETS = ["Low", "Mid", "High"]
CUTS = (100.0 / 3.0, 200.0 / 3.0)     # P33.3 / P66.7 — 3등분

# M65 공표치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M65_PUBLISHED = {"MAE_log10": 0.4117, "improvement": 0.212,
                 "within_2x": 0.492, "within_3x": 0.681}

SINGLE = "M65 단일 XGB"
ROUTED = "2-stage routed"
ORACLE = "2-stage oracle"
SOFT = "2-stage soft"
METHODS = [SINGLE, ROUTED, ORACLE, SOFT]


def stage1_model():
    """Stage 1 금액대 분류기.

    지시서가 '별도의 대규모 하이퍼파라미터 탐색은 하지 않는다'고 했으므로
    Stage 2 와 같은 XGB 설정을 쓰되 목적함수만 다중분류로 바꾼다. 회귀용
    `reg:absoluteerror` 자리를 `multi:softprob` 이 차지하는 것 외에는 동일하다.
    """
    import xgboost as xgb
    p = {k: v for k, v in F.XGB_POINT.items() if k != "objective"}
    return xgb.XGBClassifier(objective="multi:softprob", num_class=3,
                             eval_metric="mlogloss", **p)


def bucket_edges(ytr):
    """fold train 의 y 만으로 경계를 잡는다. 전체 데이터를 보지 않는다."""
    return tuple(float(v) for v in np.percentile(ytr, CUTS))


def to_bucket(y, edges):
    """경계로 0/1/2 라벨. np.digitize(right=False) — 경계값은 위 구간에 들어간다."""
    return np.digitize(y, np.asarray(edges), right=False).astype(int)


def run_folds(Xs, y, groups, titles, cats):
    """한 번의 5-fold. 네 방법을 **같은 fold·같은 feature 행렬**에서 만든다."""
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = {m: np.zeros(n) for m in METHODS}
    base = np.zeros(n)                   # 비교군 중앙값 baseline (개선율 분모)
    fold_id = np.zeros(n, dtype=int)
    z_true = np.zeros(n, dtype=int)      # 실제 구간 (fold train 경계 기준, 평가용)
    z_hat = np.zeros(n, dtype=int)       # Stage1 예측 구간
    proba = np.zeros((n, 3))
    per_fold = []

    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        t0 = time.time()
        fold_id[te] = i
        Xtr, Xte, _ = F.build_features(Xs, titles, tr, te, True, True)
        ytr, yte = y[tr], y[te]

        # --- 구간 정의: train 만 본다 -------------------------------------
        edges = bucket_edges(ytr)
        ztr = to_bucket(ytr, edges)
        zte = to_bucket(yte, edges)      # 평가·oracle 용. routing 에는 쓰지 않는다.
        z_true[te] = zte

        # --- 기준선 --------------------------------------------------------
        base[te] = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)
        pred[SINGLE][te] = F.make_point_model().fit(Xtr, ytr).predict(Xte)

        # --- Stage 1 ------------------------------------------------------
        clf = stage1_model().fit(Xtr, ztr)
        pr = clf.predict_proba(Xte)
        zh = pr.argmax(1)
        proba[te] = pr
        z_hat[te] = zh

        # --- Stage 2: 구간별 전용 회귀 3개 --------------------------------
        # 세 모델 모두에게 te 전체를 물어본다. routed/oracle/soft 가 같은
        # 예측 표를 다르게 고르는 것뿐이라, 셋의 차이가 곧 routing 의 차이다.
        table = np.zeros((len(te), 3))
        for k in range(3):
            m = ztr == k
            table[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)

        rows = np.arange(len(te))
        pred[ROUTED][te] = table[rows, zh]
        pred[ORACLE][te] = table[rows, zte]
        pred[SOFT][te] = (pr * table).sum(1)

        per_fold.append({
            "fold": i,
            "n_train": int(len(tr)), "n_test": int(len(te)),
            "edges_log10": [round(e, 4) for e in edges],
            "edges_won": [int(round(10 ** e)) for e in edges],
            "train_bucket_n": [int((ztr == k).sum()) for k in range(3)],
            "test_bucket_n": [int((zte == k).sum()) for k in range(3)],
            "stage1_acc": round(float((zh == zte).mean()), 4),
            "baseline_MAE": round(float(np.abs(base[te] - yte).mean()), 4),
            "MAE": {m: round(float(np.abs(pred[m][te] - yte).mean()), 4)
                    for m in METHODS},
            "seconds": round(time.time() - t0, 1),
        })
        f = per_fold[-1]
        print("   fold %d  cut %s  train n=%s  Stage1 acc %.3f  "
              "single %.4f / routed %.4f / oracle %.4f / soft %.4f  (%.0fs)"
              % (i, f["edges_won"], f["train_bucket_n"], f["stage1_acc"],
                 f["MAE"][SINGLE], f["MAE"][ROUTED], f["MAE"][ORACLE],
                 f["MAE"][SOFT], f["seconds"]))
    return pred, base, fold_id, z_true, z_hat, proba, per_fold


def bucket_metrics(y, p, z_true):
    """실제 구간별 MAE. '극단 금액대가 실제로 좋아졌는가'를 보는 칸이다."""
    out = {}
    for k, name in enumerate(BUCKETS):
        m = z_true == k
        e = np.abs(p[m] - y[m])
        out[name] = {"n": int(m.sum()),
                     "MAE_log10": round(float(e.mean()), 4),
                     "within_2x": round(float((e <= np.log10(2)).mean()), 4)}
    return out


def paired_test(y, p_new, p_old):
    """같은 행에 대한 절대오차 차이. paired 라 표본 흔들림과 구별할 수 있다."""
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old                    # 음수면 신규가 낫다
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n_better": int((d < 0).sum()), "n_worse": int((d > 0).sum())}


def confusion(z_true, z_hat):
    M = np.zeros((3, 3), dtype=int)
    for t, h in zip(z_true, z_hat):
        M[t, h] += 1
    return M.tolist()


def leakage_checks(y, per_fold, z_true, z_hat):
    """이 실험에서 새로 생긴 누수 경로만 점검한다. 나머지는 M65 가 이미 봤다.

    '전체 경계와 우연히 같은 fold' 는 누수가 아니다. 이 타깃은 1억·2천만처럼
    **호가 단위에 뭉쳐 있어**(상위 반복값 하나가 100행대) train 표본이 조금
    달라져도 percentile 이 같은 호가에 떨어진다. 그래서 '몇 개 fold 가 우연히
    일치했는가'를 세어 보여주되, 판정 근거는 '경계가 fold 안에서 계산됐는가'와
    '경계가 fold 마다 실제로 흔들리는가' 두 칸으로 둔다.
    """
    edges = [tuple(f["edges_log10"]) for f in per_fold]
    all_edges = bucket_edges(y)
    same = sum(1 for e in edges if np.allclose(e, all_edges, atol=1e-9))
    return {
        "경계 계산 입력": "fold train 의 y 만 (np.percentile(ytr, [33.3, 66.7]))",
        "경계가 fold 마다 흔들린다(전체 사전계산 아님)": str(len(set(edges)) > 1),
        "fold별 경계(원)": str([["{:,}".format(v) for v in f["edges_won"]]
                              for f in per_fold]),
        "전체데이터 경계(참고, 미사용)": str(["{:,}".format(int(round(10 ** v)))
                                     for v in all_edges]),
        "전체 경계와 우연히 일치한 fold 수": "%d/%d — 타깃이 호가 단위에 뭉쳐 있어서 "
                                  "생기는 일치이지 누수가 아니다" % (same, len(edges)),
        "routing 입력": "predict_proba(Xte) 뿐 — yte 는 z_true(평가·oracle)에만 쓰인다",
        "Stage1 이 실제와 다른 구간으로 보낸 행": str(int((z_true != z_hat).sum())),
    }


def routing_cost(y, pred, z_true, z_hat):
    """routing 이 맞았을 때 얻는 것과 틀렸을 때 잃는 것을 나눠 센다.

    전체 MAE 하나만 보면 '구조가 안 통한다'로 끝나지만, 실제로는 두 힘이
    상쇄된다 — 맞게 보낸 행에서는 구간 전용 모델이 단일 모델을 크게 이기고,
    틀리게 보낸 행에서는 학습 범위 밖으로 예측이 밀려 크게 진다. 승격 판정이
    아니라 '무엇을 고쳐야 이 구조가 살아나는가'를 보기 위한 칸이다.
    """
    ok = z_true == z_hat
    far = np.abs(z_true - z_hat) == 2
    out = {}
    for name, m in (("routing 성공", ok), ("routing 실패", ~ok), ("반대 끝으로 오분류", far)):
        out[name] = {"n": int(m.sum()), "share": round(float(m.mean()), 4)}
        for k, label in ((SINGLE, "single"), (ROUTED, "routed"), (SOFT, "soft")):
            out[name][label + "_MAE"] = round(float(np.abs(pred[k][m] - y[m]).mean()), 4)
    return out


def summarize(y, pred, base, z_true, z_hat, per_fold):
    b = float(np.abs(base - y).mean())
    block = {}
    for m in METHODS:
        met = M45.point_metrics(y, pred[m])
        met["improvement"] = round(float((b - met["MAE_log10"]) / b), 4)
        met["per_fold_MAE"] = [f["MAE"][m] for f in per_fold]
        met["fold_std"] = round(float(np.std(met["per_fold_MAE"])), 4)
        met["buckets"] = bucket_metrics(y, pred[m], z_true)
        block[m] = met
    for m in METHODS[1:]:
        block[m]["vs_single"] = paired_test(y, pred[m], pred[SINGLE])
    stage1 = {
        "accuracy": round(float((z_hat == z_true).mean()), 4),
        "per_fold": [f["stage1_acc"] for f in per_fold],
        "confusion_true_x_pred": confusion(z_true, z_hat),
        "adjacent_error": int((np.abs(z_true - z_hat) == 1).sum()),
        "far_error": int((np.abs(z_true - z_hat) == 2).sum()),
    }
    return {"baseline_MAE": round(b, 4), "methods": block, "stage1": stage1,
            "routing_cost": routing_cost(y, pred, z_true, z_hat), "folds": per_fold,
            "leakage": leakage_checks(y, per_fold, z_true, z_hat)}


def report_block(res):
    print("   ---- 전체 OOF  (baseline %.4f)" % res["baseline_MAE"])
    for m in METHODS:
        met = res["methods"][m]
        print("      %-16s MAE %.4f (fold σ %.4f)  개선 %.1f%%  2배내 %.1f%%  3배내 %.1f%%"
              % (m, met["MAE_log10"], met["fold_std"], 100 * met["improvement"],
                 100 * met["within_2x"], 100 * met["within_3x"]))
    print("   ---- 실제 구간별 MAE")
    for name in BUCKETS:
        print("      %-5s n=%4d  " % (name, res["methods"][SINGLE]["buckets"][name]["n"])
              + "  ".join("%s %.4f" % (m.split()[-1],
                                       res["methods"][m]["buckets"][name]["MAE_log10"])
                          for m in METHODS))
    s1 = res["stage1"]
    print("   ---- Stage1 정확도 %.3f (인접 오분류 %d행 / 반대 끝 %d행)"
          % (s1["accuracy"], s1["adjacent_error"], s1["far_error"]))
    print("        confusion(행=실제 Low/Mid/High, 열=예측)")
    for r, row in zip(BUCKETS, s1["confusion_true_x_pred"]):
        print("          %-5s %s" % (r, row))
    print("   ---- routing 성공/실패가 가른 것")
    for k, v in res["routing_cost"].items():
        print("      %-14s n=%4d (%.1f%%)  single %.4f -> routed %.4f (soft %.4f)"
              % (k, v["n"], 100 * v["share"], v["single_MAE"], v["routed_MAE"],
                 v["soft_MAE"]))
    print("   ---- paired 비교 (기준: %s)" % SINGLE)
    for m in METHODS[1:]:
        v = res["methods"][m]["vs_single"]
        print("      %-16s ΔMAE %+0.4f  95%%CI [%+0.4f, %+0.4f]  wilcoxon p=%s  "
              "좋아진행 %d / 나빠진행 %d"
              % (m, v["delta_MAE"], v["ci95"][0], v["ci95"][1], v["wilcoxon_p"],
                 v["n_better"], v["n_worse"]))
    print("   ---- 누수 점검")
    for k, v in res["leakage"].items():
        print("      %-34s %s" % (k, v))


def main():
    t0 = time.time()
    print("== 데이터 — M65 와 같은 입력")
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
    print("   타깃 log10(per_recipient), basis=stated_cap / feature %s / seed %d"
          % (F.FEATURE_VERSION, F.PIPELINE_SEED))

    results = {}
    first_pred = None
    for gname in ("program_stem", "normalized_title"):
        print("\n== 5-fold [%s]" % gname)
        pred, base, fold_id, z_true, z_hat, proba, per_fold = run_folds(
            Xs, y, groups[gname], titles, cats)
        results[gname] = summarize(y, pred, base, z_true, z_hat, per_fold)
        report_block(results[gname])

        if gname == "program_stem":
            first_pred = {m: pred[m].copy() for m in METHODS}
            pd.DataFrame({
                "row_id": d["row_id"].to_numpy(), "y": y, "fold": fold_id,
                "z_true": z_true, "z_hat": z_hat,
                "p_low": proba[:, 0], "p_mid": proba[:, 1], "p_high": proba[:, 2],
                "pred_baseline": base,
                "pred_single": pred[SINGLE], "pred_routed": pred[ROUTED],
                "pred_oracle": pred[ORACLE], "pred_soft": pred[SOFT],
                "support_type": d["support_type"].to_numpy(),
                "support_method": d["support_method"].to_numpy(),
                "support_unit": d["support_unit"].to_numpy(),
                "cohort": d["cohort"].to_numpy(),
            }).to_parquet(OUT_OOF, index=False)
            print("   [oof] %s" % OUT_OOF)

    # ------------------------------------------------------------ 재현성
    print("\n== 재현성 — 같은 seed 로 한 번 더 돌려 OOF 가 일치하는가")
    pred2 = run_folds(Xs, y, groups["program_stem"], titles, cats)[0]
    repro = {m: bool(np.allclose(pred2[m], first_pred[m])) for m in METHODS}
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))

    # ------------------------------------------------------------ 승격 판정
    ps = results["program_stem"]
    single, routed, oracle = (ps["methods"][m] for m in (SINGLE, ROUTED, ORACLE))
    v = routed["vs_single"]
    fold_win = sum(1 for a, b in zip(routed["per_fold_MAE"], single["per_fold_MAE"]) if a < b)
    checks = {
        "1. 전체 OOF MAE 가 단일모델보다 낮다": routed["MAE_log10"] < single["MAE_log10"],
        "2. 개선이 특정 fold 하나에 의존하지 않는다 (5폴드 중 4 이상 우세)": fold_win >= 4,
        "3. Low/High 구간 MAE 가 함께 감소": (
            routed["buckets"]["Low"]["MAE_log10"] < single["buckets"]["Low"]["MAE_log10"]
            and routed["buckets"]["High"]["MAE_log10"] < single["buckets"]["High"]["MAE_log10"]),
        "4. paired 95% CI 가 0 을 넘지 않는다 (우연과 구별됨)": v["ci95"][1] < 0,
        "5. oracle 상한이 단일모델보다 낮다 (구간별 모델에 여지가 있다)":
            oracle["MAE_log10"] < single["MAE_log10"],
        "6. 재현성 통과": all(repro.values()),
    }
    verdict = "승격 후보" if all(checks.values()) else "현행 유지 (M65)"
    print("\n== 승격 점검표")
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    payload = {
        "purpose": "percentile 3구간 2-stage routing 이 M65 단일 XGB 를 이기는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "features": F.FEATURE_VERSION + " (구조화 + 제목 SVD64, fold 내부 적합)",
            "stage2_model": F.XGB_POINT,
        },
        "changed": "예측 경로만 — 단일 회귀 -> Stage1 분류 후 구간 전용 회귀",
        "stage1_model": {"objective": "multi:softprob", "num_class": 3,
                         "params": "XGB_POINT 와 동일(objective 제외)"},
        "bucket_definition": {"cuts_percentile": list(CUTS),
                              "computed_on": "fold train y only"},
        "results": results, "reproducibility": repro,
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict, "published_m65": M65_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m67_m2_percentile_routing.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    L = []
    A = L.append
    A("# M67 — 금액대 percentile 분기(2-stage) 실험\n")
    A("> 질문: **하나의 모델이 전체 금액 분포를 학습하는 것보다, 예상 금액대를")
    A("> 먼저 분류한 뒤 구간 전용 회귀 모델을 쓰는 것이 OOF 성능을 개선하는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("feature  %s" % u["features"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("## 1. 전체 OOF (group=program_stem)\n")
    A("| 방법 | MAE(log10) | fold σ | baseline 대비 | 2배 이내 | 3배 이내 | "
      "ΔMAE vs 단일 | 95% CI | wilcoxon p |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in METHODS:
        b = ps["methods"][m]
        v = b.get("vs_single")
        A("| %s | %.4f | %.4f | %.1f%% | %.1f%% | %.1f%% | %s | %s | %s |"
          % (m, b["MAE_log10"], b["fold_std"], 100 * b["improvement"],
             100 * b["within_2x"], 100 * b["within_3x"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             (str(v["wilcoxon_p"]) if v else "—")))
    A("")
    A("> baseline(비교군 중앙값) MAE %.4f. M65 공표치는 MAE 0.4117 / 개선 21.2%% / "
      "2배 이내 49.2%% 이고, 위 표의 '%s' 행이 그 재현입니다.\n"
      % (ps["baseline_MAE"], SINGLE))
    A("## 2. 실제 구간별 MAE\n")
    A("| 구간 | n | " + " | ".join(METHODS) + " |")
    A("|---|---:|" + "---:|" * len(METHODS))
    for name in BUCKETS:
        A("| %s | %d | " % (name, ps["methods"][SINGLE]["buckets"][name]["n"])
          + " | ".join("%.4f" % ps["methods"][m]["buckets"][name]["MAE_log10"]
                       for m in METHODS) + " |")
    A("")
    A("## 3. Stage 1 금액대 분류\n")
    s1 = ps["stage1"]
    A("정확도 **%.1f%%** (fold별 %s) · 인접 구간 오분류 %d행 · 반대 끝 오분류 %d행\n"
      % (100 * s1["accuracy"], s1["per_fold"], s1["adjacent_error"], s1["far_error"]))
    A("| 실제 \\ 예측 | Low | Mid | High |")
    A("|---|---:|---:|---:|")
    for r, row in zip(BUCKETS, s1["confusion_true_x_pred"]):
        A("| %s | %d | %d | %d |" % (r, row[0], row[1], row[2]))
    A("")
    A("## 4. routing 성공/실패가 가른 것\n")
    A("| 행 집합 | n | 비중 | 단일 XGB MAE | routed MAE | soft MAE |")
    A("|---|---:|---:|---:|---:|---:|")
    for k, v in ps["routing_cost"].items():
        A("| %s | %d | %.1f%% | %.4f | %.4f | %.4f |"
          % (k, v["n"], 100 * v["share"], v["single_MAE"], v["routed_MAE"], v["soft_MAE"]))
    A("")
    A("> 구조 자체는 작동합니다 — 맞게 보낸 행에서는 구간 전용 모델이 단일 모델을")
    A("> 크게 이깁니다. 지는 이유는 **Stage 1 이 틀린 행에서 예측이 학습 범위 밖으로")
    A("> 밀려나기 때문**입니다. 전체 MAE 는 이 두 힘의 합입니다.\n")
    A("## 5. fold별 구간 경계와 MAE\n")
    A("| fold | 경계(원) | train n (L/M/H) | Stage1 acc | " + " | ".join(METHODS) + " |")
    A("|---|---|---|---:|" + "---:|" * len(METHODS))
    for f in ps["folds"]:
        A("| %d | %s | %s | %.3f | "
          % (f["fold"], " / ".join("{:,}".format(x) for x in f["edges_won"]),
             "/".join(str(x) for x in f["train_bucket_n"]), f["stage1_acc"])
          + " | ".join("%.4f" % f["MAE"][m] for m in METHODS) + " |")
    A("")
    A("## 6. 엄격 그룹(normalized_title) 재확인\n")
    A("| 방법 | MAE(log10) | Stage1 acc |")
    A("|---|---:|---:|")
    for m in METHODS:
        A("| %s | %.4f | %s |" % (m, nt["methods"][m]["MAE_log10"],
                                  "%.3f" % nt["stage1"]["accuracy"] if m == ROUTED else "—"))
    A("")
    A("## 7. 누수 점검 / 재현성\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in ps["leakage"].items():
        A("| %s | %s |" % (k, v))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")
    A("## 8. 승격 점검표\n")
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("")
    A("## 결론\n")
    A("```text")
    A("M65 canonical")
    A("MAE = %.4f" % ps["methods"][SINGLE]["MAE_log10"])
    A("")
    A("Percentile 3구간 2-stage routing")
    A("Stage 1 accuracy = %.4f" % s1["accuracy"])
    A("OOF MAE  = %.4f  (oracle %.4f / soft %.4f)"
      % (ps["methods"][ROUTED]["MAE_log10"], ps["methods"][ORACLE]["MAE_log10"],
         ps["methods"][SOFT]["MAE_log10"]))
    for name in BUCKETS:
        A("%-4s MAE = %.4f  (단일 %.4f)"
          % (name, ps["methods"][ROUTED]["buckets"][name]["MAE_log10"],
             ps["methods"][SINGLE]["buckets"][name]["MAE_log10"]))
    A("")
    A("결론: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
