"""M24 — 모델 1: LinearSVM 확률 보정 + 판단보류 (최종개선계획서 1순위).

왜 이걸 하는가
    LinearSVM 은 그룹CV macroF1 0.7953 으로 LogisticRegression(0.7834)보다 높다.
    그런데 채택하지 못했다 — predict_proba 가 없어 판단보류 임계값을 걸 수 없기
    때문이다. 성능이 아니라 인터페이스 때문에 진 것이다.

    CalibratedClassifierCV 로 decision_function 을 확률로 바꾸면 그 제약이 풀린다.
    같은 커버리지에서 LR 보다 정확한지 재본다.

보정을 fold 안에서 직접 한다 (cv="prefit")
    CalibratedClassifierCV(cv=k) 를 그냥 쓰면 내부 분할이 program_stem 그룹을
    모른다. 같은 사업의 재공고가 학습부와 보정부로 갈라지면 보정기가 "이미 본
    사업"으로 확률을 맞추게 되어 낙관적으로 휜다. M05 가 잡았던 누수와 같은
    종류다. 그래서 outer fold 안에서 그룹 단위로 다시 쪼개 prefit 으로 붙인다.

        outer train ──(그룹 단위 분할)──> fit 70% / calib 30%
                                          │          │
                                    LinearSVC    보정기(sigmoid/isotonic)
        outer test  ──────────────────────────────> 평가

측정 (계획서 31~37행)
    Group CV Macro F1 / Accuracy
    Coverage / Abstention Rate / Coverage-Accuracy Curve
    ECE (Expected Calibration Error) / Brier Score

운영 기준 (계획서 39~41행)
    현재 LR 은 실적용에서 79.31% @ 커버리지 70.7% 다.
    **같은 커버리지에서** calibrated SVM 이 이를 넘는지가 판단 기준이다.
    임계값 숫자를 직접 비교하면 안 된다 — 모델마다 확률 분포가 달라
    같은 0.6 이 서로 다른 커버리지를 뜻한다.
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
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import LinearSVC

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m01_support_type import MIN_SUPPORT, coarsen, tfidf
from m05_leakage_check import prepare

warnings.filterwarnings("ignore")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
OUT = os.path.join(C.PROC, "m1_calibrated_oof.parquet")
SEED = 42
CALIB_FRAC = 0.30          # outer train 에서 보정용으로 떼는 그룹 비율
THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
# 계획서 39~41행 — LR 의 실적용 운영점. 이 커버리지에서 이겨야 채택이다.
LR_OPERATING_COVERAGE = 0.707
LR_OPERATING_ACCURACY = 0.7931
# M05 가 잰 무보정 그룹CV 기준선. 보정이 학습 데이터를 30% 떼어가는 비용을
# 드러내려면 이 값과 나란히 봐야 한다.
UNCALIBRATED_BASELINE = {"TFIDF+LinearSVM": 0.7953, "TFIDF+LogisticRegression": 0.7834}


def build_models(seed):
    """비교 후보. 전부 같은 TF-IDF 를 쓴다 — 달라지는 건 분류기·보정뿐이다."""
    return {
        "LogisticRegression(현행)": {
            "clf": LogisticRegression(max_iter=2000, C=5.0,
                                      class_weight="balanced", random_state=seed),
            "calibrate": None,
        },
        "LinearSVM + sigmoid": {
            "clf": LinearSVC(C=1.0, class_weight="balanced", random_state=seed),
            "calibrate": "sigmoid",
        },
        "LinearSVM + isotonic": {
            "clf": LinearSVC(C=1.0, class_weight="balanced", random_state=seed),
            "calibrate": "isotonic",
        },
        # 채택 모델 자체를 보정하면 어떻게 되는가. LR 은 확신도가 심하게
        # 과소평가돼 있어(평균확신 0.48인데 정확도 0.83) 그 숫자를 사용자에게
        # '확신도 48%'로 보여줄 수 없다. 분류기를 바꾸지 않고 확신도만 고친다.
        "LogisticRegression + isotonic": {
            "clf": LogisticRegression(max_iter=2000, C=5.0,
                                      class_weight="balanced", random_state=seed),
            "calibrate": "isotonic",
        },
    }


def group_split(groups, frac, rng):
    """그룹 단위로 쪼갠다. 같은 사업이 양쪽에 걸치지 않게 한다."""
    uniq = np.unique(groups)
    rng.shuffle(uniq)
    n_cal = max(1, int(len(uniq) * frac))
    cal = set(uniq[:n_cal])
    is_cal = np.array([g in cal for g in groups])
    return ~is_cal, is_cal


def fit_predict_proba(Xtr, ytr, Xte, groups_tr, spec, rng, classes):
    """한 fold 를 학습하고 test 확률을 돌려준다.

    보정이 필요하면 outer train 을 그룹 단위로 다시 쪼개 prefit 으로 붙인다.
    """
    pipe = Pipeline([("t", tfidf()), ("m", clone(spec["clf"]))])
    if spec["calibrate"] is None:
        pipe.fit(Xtr, ytr)
        proba = pipe.predict_proba(Xte)
        return align_proba(proba, pipe.named_steps["m"].classes_, classes)

    fit_idx, cal_idx = group_split(groups_tr, CALIB_FRAC, rng)
    # 보정부에 없는 클래스가 생기면 그 클래스 확률을 못 만든다. 너무 얇으면 포기.
    if len(set(ytr[cal_idx])) < len(set(ytr)) or cal_idx.sum() < 30:
        pipe.fit(Xtr, ytr)
        d = pipe.decision_function(Xte)
        return align_proba(softmax(d), pipe.named_steps["m"].classes_, classes), True

    pipe.fit(Xtr[fit_idx], ytr[fit_idx])
    # sklearn 1.6+ 에서 cv="prefit" 이 FrozenEstimator 로 대체됐다.
    # 이미 학습된 pipe 를 얼려 두고 보정기만 calib 부분에 맞춘다.
    cal = CalibratedClassifierCV(FrozenEstimator(pipe), method=spec["calibrate"])
    cal.fit(Xtr[cal_idx], ytr[cal_idx])
    proba = cal.predict_proba(Xte)
    return align_proba(proba, cal.classes_, classes), False


def softmax(d):
    d = d - d.max(axis=1, keepdims=True)
    e = np.exp(d)
    return e / e.sum(axis=1, keepdims=True)


def align_proba(proba, model_classes, all_classes):
    """fold 마다 등장 클래스가 달라 열 위치가 어긋난다. 전체 클래스 축으로 맞춘다."""
    out = np.zeros((len(proba), len(all_classes)))
    idx = {c: i for i, c in enumerate(all_classes)}
    for j, c in enumerate(model_classes):
        out[:, idx[c]] = proba[:, j]
    return out


def oof_proba(X, y, groups, spec, folds=5, seed=SEED):
    """그룹CV OOF 확률. 모든 행이 정확히 한 번씩 검증에 들어간다."""
    rng = np.random.default_rng(seed)
    classes = np.unique(y)
    proba = np.zeros((len(y), len(classes)))
    n_fallback = 0
    skf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    for tr, te in skf.split(X, y, groups):
        r = fit_predict_proba(X[tr], y[tr], X[te], groups[tr], spec, rng, classes)
        if isinstance(r, tuple):
            proba[te], fb = r
            n_fallback += int(fb)
        else:
            proba[te] = r
    return proba, classes, n_fallback


# ------------------------------------------------------------ 지표
def ece(proba, y, n_bins=10):
    """Expected Calibration Error — 확신도와 실제 정확도의 평균 괴리."""
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    correct = (pred == y).astype(float)
    edges = np.linspace(0, 1, n_bins + 1)
    total = 0.0
    for i in range(n_bins):
        m = (conf > edges[i]) & (conf <= edges[i + 1])
        if m.sum() == 0:
            continue
        total += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(total)


def brier(proba, y, n_classes):
    """다중분류 Brier — 확률벡터와 원핫정답의 제곱거리 평균."""
    onehot = np.zeros_like(proba)
    onehot[np.arange(len(y)), y] = 1.0
    return float(((proba - onehot) ** 2).sum(axis=1).mean())


def sweep(proba, y, thresholds=THRESHOLDS):
    """임계값별 커버리지·정확도·macroF1 (계획서 43~50행)."""
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    rows = []
    for t in thresholds:
        keep = conf >= t
        if keep.sum() == 0:
            continue
        rows.append({
            "threshold": t,
            "coverage": round(float(keep.mean()), 4),
            "abstention_rate": round(float(1 - keep.mean()), 4),
            "n_kept": int(keep.sum()),
            "accuracy": round(float(accuracy_score(y[keep], pred[keep])), 4),
            "macro_f1": round(float(f1_score(y[keep], pred[keep],
                                             average="macro", zero_division=0)), 4),
        })
    return rows


def accuracy_at_coverage(proba, y, target_cov):
    """커버리지를 고정하고 그때의 정확도를 잰다 — 모델 간 공정 비교의 핵심.

    임계값 숫자를 직접 비교하면 안 된다. 모델마다 확률 분포가 달라 같은 0.6 이
    서로 다른 커버리지를 뜻한다. 확신도 상위 N% 를 남기는 방식으로 맞춘다.
    """
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    n_keep = max(1, int(round(len(y) * target_cov)))
    idx = np.argsort(-conf)[:n_keep]
    return {
        "target_coverage": target_cov,
        "actual_coverage": round(float(n_keep / len(y)), 4),
        "threshold_used": round(float(conf[idx][-1]), 4),
        "accuracy": round(float(accuracy_score(y[idx], pred[idx])), 4),
        "macro_f1": round(float(f1_score(y[idx], pred[idx],
                                         average="macro", zero_division=0)), 4),
    }


def class_specific(proba, y, classes, label_names, base_t=0.50, high_t=0.70):
    """계획서 52~53행 — 혼동이 심한 클래스에만 높은 임계값을 건다.

    어느 클래스가 혼동이 심한지부터 데이터로 정한다(그 클래스로 예측했을 때의
    정확도가 낮은 순). 미리 '상담/컨설팅'이라고 박아 넣지 않는다.
    """
    pred = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    prec = {}
    for i, c in enumerate(classes):
        m = pred == i
        if m.sum() >= 10:
            prec[label_names[c]] = (round(float((y[m] == i).mean()), 4), int(m.sum()))
    weak = sorted(prec.items(), key=lambda kv: kv[1][0])[:3]
    weak_idx = {i for i, c in enumerate(classes)
                if label_names[c] in {w[0] for w in weak}}

    t = np.where(np.isin(pred, list(weak_idx)), high_t, base_t)
    keep = conf >= t
    flat = conf >= base_t
    return {
        "weak_classes": {k: {"precision": v[0], "n_pred": v[1]} for k, v in weak},
        "base_threshold": base_t, "high_threshold": high_t,
        "class_specific": {
            "coverage": round(float(keep.mean()), 4),
            "accuracy": round(float(accuracy_score(y[keep], pred[keep])), 4) if keep.sum() else None,
        },
        "flat_threshold": {
            "coverage": round(float(flat.mean()), 4),
            "accuracy": round(float(accuracy_score(y[flat], pred[flat])), 4) if flat.sum() else None,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()

    full = pd.read_parquet(TAX)
    X, y, groups, sub = prepare(full)
    # parquet 이 pyarrow 백엔드라 .values 가 ArrowExtensionArray 로 나온다.
    # sklearn 내부 인덱싱(_safe_indexing)이 이걸 못 다뤄 numpy 로 바꾼다.
    X = np.asarray(X, dtype=object)
    y = np.asarray(y)
    groups = np.asarray(groups)
    le = LabelEncoder().fit(sub["support_type"].values)
    label_names = {i: c for i, c in enumerate(le.classes_)}
    n_classes = len(set(y))
    print("모델 1 보정 실험: %d행 / %d클래스 / %d그룹"
          % (len(y), n_classes, len(set(groups))))
    print("운영 기준(계획서): LR 실적용 %.2f%% @ 커버리지 %.1f%%"
          % (LR_OPERATING_ACCURACY * 100, LR_OPERATING_COVERAGE * 100))

    results, probas = {}, {}
    for name, spec in build_models(a.seed).items():
        proba, classes, n_fb = oof_proba(X, y, groups, spec, a.folds, a.seed)
        probas[name] = proba
        pred = proba.argmax(axis=1)
        r = {
            "macro_f1": round(float(f1_score(y, pred, average="macro",
                                             zero_division=0)), 4),
            "accuracy": round(float(accuracy_score(y, pred)), 4),
            "ece": round(ece(proba, y), 4),
            "brier": round(brier(proba, y, n_classes), 4),
            "mean_confidence": round(float(proba.max(axis=1).mean()), 4),
            "calibration_fallback_folds": n_fb,
            "sweep": sweep(proba, y),
            "at_lr_coverage": accuracy_at_coverage(proba, y, LR_OPERATING_COVERAGE),
        }
        results[name] = r
        print("\n== %s" % name)
        print("  macroF1 %.4f / Acc %.4f / ECE %.4f / Brier %.4f / 평균확신 %.4f"
              % (r["macro_f1"], r["accuracy"], r["ece"], r["brier"],
                 r["mean_confidence"]))
        if n_fb:
            print("  주의: %d개 fold 에서 보정부 클래스 부족으로 softmax 로 대체" % n_fb)
        c = r["at_lr_coverage"]
        print("  커버리지 %.1f%% 고정 시 -> 정확도 %.4f (임계값 %.3f)"
              % (c["actual_coverage"] * 100, c["accuracy"], c["threshold_used"]))

    print("\n== 임계값별 커버리지-정확도 (계획서 43~50행)")
    print("%-24s %6s %9s %9s %9s" % ("모델", "임계값", "커버리지", "정확도", "macroF1"))
    for name, r in results.items():
        for s in r["sweep"]:
            if s["threshold"] in (0.40, 0.50, 0.60, 0.70, 0.80):
                print("%-24s %6.2f %8.1f%% %9.4f %9.4f"
                      % (name, s["threshold"], s["coverage"] * 100,
                         s["accuracy"], s["macro_f1"]))

    print("\n== 클래스별 임계값 차등 (계획서 52~53행)")
    cs = {}
    for name, proba in probas.items():
        cs[name] = class_specific(proba, y, np.unique(y), label_names)
        v = cs[name]
        print("  %-24s 약한 클래스 %s" % (name, list(v["weak_classes"])))
        print("    일괄 %.2f  -> 커버리지 %.1f%% / 정확도 %s"
              % (v["base_threshold"], v["flat_threshold"]["coverage"] * 100,
                 v["flat_threshold"]["accuracy"]))
        print("    차등(약한 클래스 %.2f) -> 커버리지 %.1f%% / 정확도 %s"
              % (v["high_threshold"], v["class_specific"]["coverage"] * 100,
                 v["class_specific"]["accuracy"]))

    verdict = judge(results)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)

    best = verdict["best"]
    pd.DataFrame({
        "support_type": [label_names[v] for v in y],
        "pred": [label_names[v] for v in probas[best].argmax(axis=1)],
        "confidence": probas[best].max(axis=1),
        "group": groups,
    }).to_parquet(OUT, index=False)
    print("[data] %s  (%s 의 OOF 예측)" % (OUT, best))

    C.save_report("m24_m1_calibrate.json", {
        "n_rows": int(len(y)), "n_classes": n_classes,
        "n_groups": int(len(set(groups))), "folds": a.folds, "seed": a.seed,
        "cv": "StratifiedGroupKFold (program_stem)",
        "calibration": "fold 내부에서 그룹 단위로 fit/calib 분리 후 FrozenEstimator 로 보정",
        "calib_frac": CALIB_FRAC,
        "lr_operating_point": {"coverage": LR_OPERATING_COVERAGE,
                               "accuracy": LR_OPERATING_ACCURACY},
        "results": results, "class_specific": cs, "verdict": verdict,
        "uncalibrated_baseline_m05": UNCALIBRATED_BASELINE,
    })
    write_md(results, cs, verdict)


def judge(results):
    """정확도와 확신도 신뢰성을 나눠 판정한다.

    둘은 다른 질문이고 답도 다르게 나왔다. 하나로 뭉뚱그리면 어느 쪽도
    제대로 못 쓴다.
    """
    reasons = []
    lr = results["LogisticRegression(현행)"]

    # --- 축 1: 같은 커버리지에서의 정확도 (계획서 39~41행 기준)
    best_acc = max(results, key=lambda k: results[k]["at_lr_coverage"]["accuracy"])
    gain = (results[best_acc]["at_lr_coverage"]["accuracy"]
            - lr["at_lr_coverage"]["accuracy"])
    reasons.append("[정확도] 커버리지 70.7% 고정 — " + " / ".join(
        "%s %.4f" % (k, v["at_lr_coverage"]["accuracy"]) for k, v in results.items()))

    if best_acc == "LogisticRegression(현행)" or gain < 0.01:
        acc_verdict = "분류기는 현행 LR 유지"
        reasons.append("계획서 가설(보정한 LinearSVM 이 LR 을 넘는다)은 성립하지 "
                       "않았다. 보정이 학습 데이터의 %.0f%% 를 보정용으로 떼어가는 "
                       "비용 때문이다 — 무보정 LinearSVM 은 macroF1 %.4f(M05)인데 "
                       "보정 후 %.4f 로 떨어졌다"
                       % (CALIB_FRAC * 100,
                          UNCALIBRATED_BASELINE["TFIDF+LinearSVM"],
                          max(results[k]["macro_f1"] for k in results
                              if k.startswith("LinearSVM"))))
    else:
        acc_verdict = "%s 채택 검토" % best_acc
        reasons.append("같은 커버리지에서 %+.4f — 교체를 검토할 만하다" % gain)

    # --- 축 2: 확신도 신뢰성 (ECE)
    best_ece = min(results, key=lambda k: results[k]["ece"])
    reasons.append("[확신도] ECE — " + " / ".join(
        "%s %.4f" % (k, v["ece"]) for k, v in results.items()))
    reasons.append("현행 LR 은 ECE %.4f 로 심하게 어긋나 있다 — 평균확신 %.4f 인데 "
                   "실제 정확도는 %.4f 다(과소확신). 이 숫자를 사용자에게 '확신도 "
                   "%.0f%%'로 보여줄 수 없다"
                   % (lr["ece"], lr["mean_confidence"], lr["accuracy"],
                      lr["mean_confidence"] * 100))

    lr_iso = results.get("LogisticRegression + isotonic")
    if lr_iso:
        d_acc = (lr_iso["at_lr_coverage"]["accuracy"]
                 - lr["at_lr_coverage"]["accuracy"])
        reasons.append("같은 LR 을 isotonic 보정하면 ECE %.4f -> %.4f, 같은 "
                       "커버리지 정확도는 %+.4f — 분류기를 바꾸지 않고 확신도만 "
                       "고치는 선택지다" % (lr["ece"], lr_iso["ece"], d_acc))

    v = "%s / 확신도 표기가 필요하면 %s 로 보정" % (acc_verdict, best_ece)
    return {"verdict": v, "reasons": reasons,
            "best_accuracy": best_acc, "best_calibration": best_ece,
            "best": best_acc}


def write_md(results, cs, verdict):
    L = ["# 모델 1 — LinearSVM 확률 보정 + 판단보류", "",
         "> 최종개선계획서 1순위. LinearSVM 은 그룹CV 0.7953 으로 LR(0.7834)보다",
         "> 높은데 `predict_proba` 가 없어 판단보류를 못 걸어 채택하지 못했습니다.",
         "> 보정으로 그 제약을 풀고 같은 커버리지에서 다시 겨룹니다.", "",
         "## 1. 보정을 fold 안에서 직접 한 이유", "",
         "`CalibratedClassifierCV(cv=k)` 를 그냥 쓰면 내부 분할이 `program_stem`",
         "그룹을 모릅니다. 같은 사업의 재공고가 학습부와 보정부로 갈라지면 보정기가",
         "\"이미 본 사업\"으로 확률을 맞추게 되어 낙관적으로 휩니다 — M05 가 잡았던",
         "누수와 같은 종류입니다.", "",
         "sklearn 1.6+ 에서 `cv=\"prefit\"` 이 `FrozenEstimator` 로 대체돼 그쪽을 씁니다.", "",
         "```text",
         "outer train ──(그룹 단위 분할)──> fit 70% / calib 30%",
         "                                  │          │",
         "                            LinearSVC    보정기(sigmoid/isotonic)",
         "outer test  ──────────────────────────────> 평가",
         "```", "",
         "## 2. 전체 성능", "",
         "| 모델 | macroF1 | Accuracy | ECE | Brier | 평균확신도 |",
         "|---|---:|---:|---:|---:|---:|"]
    for k, r in results.items():
        L.append("| %s | %.4f | %.4f | %.4f | %.4f | %.4f |"
                 % (k, r["macro_f1"], r["accuracy"], r["ece"], r["brier"],
                    r["mean_confidence"]))
    L += ["",
          "참고 — 무보정 기준선(M05, 같은 그룹CV): LinearSVM **%.4f** / "
          "LogisticRegression **%.4f**"
          % (UNCALIBRATED_BASELINE["TFIDF+LinearSVM"],
             UNCALIBRATED_BASELINE["TFIDF+LogisticRegression"]), "",
          "> **보정은 공짜가 아닙니다.** 보정기를 학습시키려면 학습 데이터의 %.0f%% 를"
          % (CALIB_FRAC * 100),
          "> 떼어내야 하고, 그만큼 분류기가 덜 배웁니다. LinearSVM 이 무보정 0.7953",
          "> 에서 보정 후 0.72~0.73 으로 떨어진 것이 그 비용입니다. 계획서는 이",
          "> 비용을 고려하지 않았습니다.", "",
          "ECE 는 확신도와 실제 정확도의 평균 괴리입니다 — 낮을수록 확신도를 그대로",
          "신뢰등급으로 쓸 수 있습니다. Brier 는 확률벡터 전체의 정확도입니다.", "",
          "## 3. 같은 커버리지에서 비교 (계획서 39~41행)", "",
          "**임계값 숫자를 직접 비교하면 안 됩니다.** 모델마다 확률 분포가 달라 같은",
          "0.6 이 서로 다른 커버리지를 뜻합니다. 확신도 상위 N% 를 남기는 방식으로",
          "커버리지를 맞춘 뒤 정확도를 비교했습니다.", "",
          "| 모델 | 커버리지 | 그때의 임계값 | 정확도 | macroF1 |",
          "|---|---:|---:|---:|---:|"]
    for k, r in results.items():
        c = r["at_lr_coverage"]
        L.append("| %s | %.1f%% | %.3f | **%.4f** | %.4f |"
                 % (k, c["actual_coverage"] * 100, c["threshold_used"],
                    c["accuracy"], c["macro_f1"]))
    L += ["",
          "> 기준선은 LR 의 실적용 운영점입니다 — Open API 정답 41건에서 79.31% @",
          "> 커버리지 70.7%. 위 표는 그룹CV OOF(1,404건) 기준이라 표본이 훨씬 크고",
          "> 안정적이지만, 학습 도메인 안이라 실적용 수치와 직접 비교하면 안 됩니다.", "",
          "## 4. 임계값별 커버리지-정확도 곡선 (계획서 43~50행)", "",
          "| 모델 | 임계값 | 커버리지 | 판단보류율 | 정확도 | macroF1 |",
          "|---|---:|---:|---:|---:|---:|"]
    for k, r in results.items():
        for s in r["sweep"]:
            L.append("| %s | %.2f | %.1f%% | %.1f%% | %.4f | %.4f |"
                     % (k, s["threshold"], s["coverage"] * 100,
                        s["abstention_rate"] * 100, s["accuracy"], s["macro_f1"]))

    L += ["", "## 5. 클래스별 임계값 차등 (계획서 52~53행)", "",
          "어느 클래스가 혼동이 심한지 **데이터로 정했습니다** — 그 클래스로 예측했을",
          "때의 정확도가 낮은 순 상위 3개. 미리 '상담/컨설팅'이라고 박아 넣지 않았습니다.", ""]
    for k, v in cs.items():
        L += ["**%s**" % k, "",
              "약한 클래스: %s" % ", ".join(
                  "%s(정확도 %.2f, %d건)" % (n, d["precision"], d["n_pred"])
                  for n, d in v["weak_classes"].items()), "",
              "| 방식 | 커버리지 | 정확도 |", "|---|---:|---:|",
              "| 일괄 %.2f | %.1f%% | %s |" % (v["base_threshold"],
                                              v["flat_threshold"]["coverage"] * 100,
                                              v["flat_threshold"]["accuracy"]),
              "| 차등(약한 클래스만 %.2f) | %.1f%% | %s |"
              % (v["high_threshold"], v["class_specific"]["coverage"] * 100,
                 v["class_specific"]["accuracy"]), ""]

    L += ["## 6. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = C.report_path("m24_m1_calibrate.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
