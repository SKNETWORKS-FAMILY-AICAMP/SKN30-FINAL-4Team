"""M27 — 모델 1: LinearSVM + margin 기반 판단보류 (제품정렬 계획서 1순위).

M24 가 남긴 문제
    LinearSVM 은 그룹CV macroF1 0.7953 으로 LR(0.7834)보다 높은데 predict_proba
    가 없어 판단보류를 못 걸었다. 확률로 보정해봤더니 보정기 학습에 데이터의
    30% 를 떼는 비용 때문에 0.7293 으로 떨어져 오히려 LR 에 졌다.

계획서가 짚은 것
    "기획서에는 사용자-facing 확률 예측 요구가 없다."

    맞는 지적이다. 화면에 '확신도 82%'를 띄울 게 아니라면 확률이 필요 없다.
    판단보류는 **순서만 매길 수 있으면** 걸린다. decision_function 의 margin 이
    바로 그 순서다. 확률로 바꾸는 단계를 건너뛰면 보정 비용이 사라지고
    LinearSVM 은 0.7953 을 그대로 유지한다.

두 가지 margin 을 비교한다 (계획서 35~38행)
    max_score   가장 높은 클래스의 decision score. "이 클래스라고 얼마나 세게
                주장하는가"
    top2_gap    1등과 2등의 차. "1등이 2등을 얼마나 확실히 눌렀는가"

    둘은 다른 실패를 잡는다. max_score 는 낮은데 gap 이 크면 "약하게 주장하지만
    헷갈리진 않음"이고, max_score 는 높은데 gap 이 작으면 "세게 주장하지만 두
    클래스 사이에서 흔들림"이다. 후자가 오분류의 전형이라 gap 이 더 나을 수 있다.

임계값은 검증에서 정한다 (계획서 38행)
    그룹CV OOF 로 커버리지-정확도 곡선을 그리고, LR 의 운영 커버리지(70.7%)에
    맞춘 임계값을 고른다. 그 임계값을 외부 정답셋(M07 41건)에 그대로 적용한다.
    외부셋에서 임계값을 고르면 그건 검증이 아니라 튜닝이다.

평가 (계획서 40~44행)
    Macro F1 / 외부 정확도 / Coverage / class별 Recall
"""
import argparse
import json
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, recall_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m01_support_type import MIN_SUPPORT, coarsen, tfidf

warnings.filterwarnings("ignore")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DETAIL = os.path.join(C.PROC, "announcement_detail.parquet")
DOCS_API = os.path.join(C.REPORTS, "e01_documents_api.jsonl")
LABELS = os.path.join(C.PROC, "..", "labels", "openapi_manual_50.csv")
OUT = os.path.join(C.PROC, "m1_margin_abstention.parquet")
SEED = 42
# LR 의 실적용 운영점 — 이 커버리지에 맞춰 임계값을 고른다
TARGET_COVERAGE = 0.707
LR_EXTERNAL_ACCURACY = 0.7931      # M07, 판단보류 제외 29/41


def prepare(full):
    t = full.copy()
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)
    X = np.asarray(sub["text_for_model"].fillna("").astype(str).values, dtype=object)
    y = np.asarray(sub["support_type"].values, dtype=object)
    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    groups = np.asarray(np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str)))
    return X, y, groups, sub


def margins(scores):
    """decision_function 행렬에서 두 가지 margin 을 뽑는다.

    이진(2클래스)이면 sklearn 이 1차원을 주므로 2열로 되돌린다.
    """
    if scores.ndim == 1:
        scores = np.column_stack([-scores, scores])
    order = np.sort(scores, axis=1)
    return {
        "max_score": scores.max(axis=1),
        "top2_gap": order[:, -1] - order[:, -2],
    }, scores.argmax(axis=1)


def oof_margins(X, y, groups, folds=5, seed=SEED):
    """그룹CV OOF 예측과 margin. 모든 행이 정확히 한 번씩 검증에 들어간다."""
    classes = np.array(sorted(set(y)))
    cls_idx = {c: i for i, c in enumerate(classes)}
    y_idx = np.array([cls_idx[v] for v in y])
    pred = np.empty(len(y), dtype=object)
    mar = {k: np.zeros(len(y)) for k in ("max_score", "top2_gap")}

    skf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    for tr, te in skf.split(X, y_idx, groups):
        pipe = Pipeline([("t", tfidf()), ("m", LinearSVC(
            C=1.0, class_weight="balanced", random_state=seed))])
        pipe.fit(X[tr], y[tr])
        d = pipe.decision_function(X[te])
        m, arg = margins(d)
        fold_classes = pipe.named_steps["m"].classes_
        pred[te] = fold_classes[arg]
        for k in mar:
            mar[k][te] = m[k]
    return pred, mar, classes


def oof_lr(X, y, groups, folds=5, seed=SEED):
    """비교용 LR OOF 확신도."""
    pred = np.empty(len(y), dtype=object)
    conf = np.zeros(len(y))
    skf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    y_idx = pd.factorize(y)[0]
    for tr, te in skf.split(X, y_idx, groups):
        pipe = Pipeline([("t", tfidf()), ("m", LogisticRegression(
            max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))])
        pipe.fit(X[tr], y[tr])
        p = pipe.predict_proba(X[te])
        cls = pipe.named_steps["m"].classes_
        conf[te] = p.max(axis=1)
        pred[te] = cls[p.argmax(axis=1)]
    return pred, conf


def sweep(score, pred, y, n_points=25):
    """커버리지-정확도 곡선. 임계값 대신 커버리지를 축으로 훑는다.

    margin 은 확률이 아니라 스케일이 모델마다 다르다. 커버리지를 축으로
    잡아야 서로 다른 신호를 나란히 비교할 수 있다.
    """
    order = np.argsort(-score)
    rows = []
    for cov in np.linspace(0.2, 1.0, n_points):
        k = max(1, int(round(len(y) * cov)))
        idx = order[:k]
        rows.append({
            "coverage": round(float(k / len(y)), 4),
            "threshold": round(float(score[idx][-1]), 4),
            "accuracy": round(float(accuracy_score(y[idx], pred[idx])), 4),
            "macro_f1": round(float(f1_score(y[idx], pred[idx],
                                             average="macro", zero_division=0)), 4),
        })
    return rows


def at_coverage(score, pred, y, cov):
    order = np.argsort(-score)
    k = max(1, int(round(len(y) * cov)))
    idx = order[:k]
    return {
        "coverage": round(float(k / len(y)), 4),
        "threshold": round(float(score[idx][-1]), 4),
        "accuracy": round(float(accuracy_score(y[idx], pred[idx])), 4),
        "macro_f1": round(float(f1_score(y[idx], pred[idx],
                                         average="macro", zero_division=0)), 4),
    }


def class_recall(pred, y, classes, keep=None):
    """클래스별 Recall (계획서 44행). keep 을 주면 그 부분집합에서만 잰다."""
    yy, pp = (y, pred) if keep is None else (y[keep], pred[keep])
    out = {}
    for c in classes:
        m = yy == c
        if m.sum() == 0:
            out[str(c)] = None
            continue
        out[str(c)] = {"n": int(m.sum()),
                       "recall": round(float((pp[m] == c).mean()), 4)}
    return out


# ------------------------------------------------------- 외부 정답셋 (M07)
def load_docs(path, limit=4000):
    if not os.path.exists(path):
        return {}
    best = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            pid = str(r.get("announcement_id", ""))
            if not pid:
                continue
            if pid not in best or r.get("n_chars", 0) > best[pid].get("n_chars", 0):
                best[pid] = r
    return {k: v.get("text", "")[:limit] for k, v in best.items()}


def external_eval(X, y, thresholds, seed=SEED):
    """taxonomy 전량으로 학습해 Open API 에 적용하고 M07 정답 41건과 대조.

    임계값은 CV 에서 이미 정해 넘겨받는다 — 여기서 고르면 튜닝이 된다.
    """
    if not os.path.exists(LABELS):
        return {"status": "정답 파일 없음"}
    pipe = Pipeline([("t", tfidf()), ("m", LinearSVC(
        C=1.0, class_weight="balanced", random_state=seed))])
    pipe.fit(X, y)
    classes = pipe.named_steps["m"].classes_

    d = pd.read_parquet(DETAIL)
    pids = d["announcement_id"].astype(str).tolist()
    docs = load_docs(DOCS_API)
    fallback = (d["summary_text"].fillna("") + "\n"
                + d["target_text"].fillna("")).tolist()
    texts = [docs.get(p, fb) for p, fb in zip(pids, fallback)]

    dec = pipe.decision_function(texts)
    m, arg = margins(dec)
    pred = classes[arg]
    got = pd.DataFrame({"announcement_id": pids, "pred": pred,
                        "max_score": m["max_score"], "top2_gap": m["top2_gap"]})

    lab = pd.read_csv(LABELS, encoding="utf-8-sig")
    lab["announcement_id"] = lab["announcement_id"].astype(str)
    lab["label_19class"] = lab["label_19class"].fillna("").astype(str)
    lab = lab[lab["label_19class"] != ""]
    mg = lab.merge(got, on="announcement_id", how="left")
    mg = mg[mg["pred"].notna()].copy()
    mg["correct"] = mg["pred"] == mg["label_19class"]

    out = {"n_labeled": int(len(mg)),
           "all": {"n": int(len(mg)),
                   "accuracy": round(float(mg["correct"].mean()), 4)}}
    for signal, thr in thresholds.items():
        keep = mg[signal] >= thr
        out[signal] = {
            "threshold": round(float(thr), 4),
            "coverage": round(float(keep.mean()), 4),
            "n_kept": int(keep.sum()),
            "accuracy": (round(float(mg.loc[keep, "correct"].mean()), 4)
                         if keep.sum() else None),
        }
    return out, mg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()

    full = pd.read_parquet(TAX)
    X, y, groups, sub = prepare(full)
    print("모델 1 margin 판단보류: %d행 / %d클래스 / %d그룹"
          % (len(y), len(set(y)), len(set(groups))))
    print("목표 커버리지 %.1f%% (LR 운영점) / LR 외부 정확도 %.4f"
          % (TARGET_COVERAGE * 100, LR_EXTERNAL_ACCURACY))

    t0 = time.time()
    svm_pred, mar, classes = oof_margins(X, y, groups, a.folds, a.seed)
    lr_pred, lr_conf = oof_lr(X, y, groups, a.folds, a.seed)

    base = {
        "LinearSVM (판단보류 없음)": {
            "macro_f1": round(float(f1_score(y, svm_pred, average="macro",
                                             zero_division=0)), 4),
            "accuracy": round(float(accuracy_score(y, svm_pred)), 4)},
        "LogisticRegression (판단보류 없음)": {
            "macro_f1": round(float(f1_score(y, lr_pred, average="macro",
                                             zero_division=0)), 4),
            "accuracy": round(float(accuracy_score(y, lr_pred)), 4)},
    }
    print("\n== 판단보류 없이 (그룹CV 전체)")
    for k, v in base.items():
        print("  %-32s macroF1 %.4f / Acc %.4f" % (k, v["macro_f1"], v["accuracy"]))

    signals = {
        "max_score": (mar["max_score"], svm_pred),
        "top2_gap": (mar["top2_gap"], svm_pred),
        "LR proba (비교)": (lr_conf, lr_pred),
    }
    print("\n== 커버리지 %.1f%% 고정 시" % (TARGET_COVERAGE * 100))
    at = {}
    for name, (score, pred) in signals.items():
        at[name] = at_coverage(score, pred, y, TARGET_COVERAGE)
        print("  %-18s 정확도 %.4f / macroF1 %.4f (임계값 %.4f)"
              % (name, at[name]["accuracy"], at[name]["macro_f1"],
                 at[name]["threshold"]))

    curves = {n: sweep(s, p, y) for n, (s, p) in signals.items()}
    print("\n== 커버리지-정확도 곡선 (발췌)")
    print("%-18s %s" % ("신호", "  ".join("%d%%" % c for c in (40, 60, 70, 80, 100))))
    for name, rows in curves.items():
        picks = []
        for target in (0.4, 0.6, 0.7, 0.8, 1.0):
            r = min(rows, key=lambda x: abs(x["coverage"] - target))
            picks.append("%.4f" % r["accuracy"])
        print("%-18s %s" % (name, "  ".join(picks)))

    # 클래스별 Recall — 판단보류 적용 전후
    best_signal = max(("max_score", "top2_gap"),
                      key=lambda k: at[k]["accuracy"])
    thr = at[best_signal]["threshold"]
    keep = mar[best_signal] >= thr
    recalls = {
        "전체": class_recall(svm_pred, y, classes),
        "판단보류 적용(%s)" % best_signal: class_recall(svm_pred, y, classes, keep),
    }

    print("\n== 외부 정답셋 (M07 41건) — CV 에서 정한 임계값을 그대로 적용")
    ext = external_eval(X, y, {"max_score": at["max_score"]["threshold"],
                               "top2_gap": at["top2_gap"]["threshold"]}, a.seed)
    if isinstance(ext, tuple):
        ext, mg = ext
        print("  판단보류 없이 전체 %d건: 정확도 %.4f"
              % (ext["all"]["n"], ext["all"]["accuracy"]))
        for s in ("max_score", "top2_gap"):
            e = ext[s]
            print("  %-12s 커버리지 %.1f%% (%d건) / 정확도 %s"
                  % (s, e["coverage"] * 100, e["n_kept"], e["accuracy"]))
        print("  참고 — LR 판단보류 제외 정확도 %.4f @ 커버리지 70.7%%"
              % LR_EXTERNAL_ACCURACY)
        mg[["announcement_id", "label_19class", "pred", "correct",
            "max_score", "top2_gap"]].to_parquet(OUT, index=False)
        print("[data] %s" % OUT)
    else:
        print("  %s" % ext.get("status"))

    verdict = judge(base, at, ext, best_signal)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("m27_m1_margin_abstention.json", {
        "n_rows": int(len(y)), "n_classes": int(len(set(y))),
        "n_groups": int(len(set(groups))), "folds": a.folds, "seed": a.seed,
        "cv": "StratifiedGroupKFold (program_stem)",
        "target_coverage": TARGET_COVERAGE,
        "lr_external_accuracy": LR_EXTERNAL_ACCURACY,
        "no_abstention": base, "at_target_coverage": at,
        "curves": curves, "class_recall": recalls,
        "best_signal": best_signal, "external": ext, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })
    write_md(base, at, curves, recalls, ext, best_signal, verdict)


def judge(base, at, ext, best_signal):
    reasons, v = [], "채택 검토"
    svm = base["LinearSVM (판단보류 없음)"]["macro_f1"]
    lr = base["LogisticRegression (판단보류 없음)"]["macro_f1"]
    reasons.append("판단보류 없이 — LinearSVM macroF1 %.4f vs LR %.4f. 확률로 "
                   "바꾸지 않으니 M24 에서 보정 때문에 잃었던 성능(0.7293)이 "
                   "그대로 돌아왔다" % (svm, lr))

    lr_at = at["LR proba (비교)"]["accuracy"]
    for s in ("max_score", "top2_gap"):
        d = at[s]["accuracy"] - lr_at
        reasons.append("커버리지 %.0f%% 에서 %s %.4f (LR %.4f, %+.4f)"
                       % (TARGET_COVERAGE * 100, s, at[s]["accuracy"], lr_at, d))
    reasons.append("두 margin 중 %s 가 낫다" % best_signal)

    if isinstance(ext, dict) and ext.get("all"):
        e = ext.get(best_signal, {})
        if e.get("accuracy") is not None:
            reasons.append("외부 정답셋 — %s 판단보류 적용 시 정확도 %.4f @ 커버리지 "
                           "%.1f%% (LR %.4f @ 70.7%%)"
                           % (best_signal, e["accuracy"], e["coverage"] * 100,
                              LR_EXTERNAL_ACCURACY))
            if e["accuracy"] >= LR_EXTERNAL_ACCURACY:
                v = "LinearSVM + margin 판단보류 채택"
                reasons.append("CV 와 외부셋 양쪽에서 LR 을 넘거나 같다")
            else:
                v = "조건부 — CV 는 앞서나 외부셋에서 못 넘었다"
                reasons.append("외부 41건은 표본이 작아 단정하기 어렵다. 표본을 "
                               "늘려 다시 재야 한다")
    reasons.append("확률 보정을 하지 않으므로 사용자에게 '확신도 N%' 를 보여줄 수 "
                   "없다. 기획서가 그 요구를 하지 않으므로 문제되지 않는다")
    return {"verdict": v, "reasons": reasons}


def write_md(base, at, curves, recalls, ext, best_signal, verdict):
    L = ["# 모델 1 — LinearSVM + margin 기반 판단보류", "",
         "> 제품정렬 계획서 1순위: \"기획서에는 사용자-facing 확률 예측 요구가 없으므로",
         "> predict_proba 대신 decision_function margin 을 활용한다.\"", "",
         "## 1. 왜 margin 인가", "",
         "M24 에서 확인한 것 — LinearSVM 을 확률로 보정하면 보정기 학습에 데이터의",
         "30% 를 떼야 해서 macroF1 이 0.7953 → 0.7293 으로 떨어집니다. 확률을 얻는",
         "대가로 분류력을 잃는 구조입니다.", "",
         "**그런데 판단보류에 확률이 필요한 게 아닙니다.** 어느 예측이 더 미덥지",
         "않은지 **순서만 매길 수 있으면** 걸립니다. `decision_function` 의 margin 이",
         "바로 그 순서입니다. 확률로 바꾸는 단계를 건너뛰면 보정 비용이 사라집니다.", "",
         "| 신호 | 뜻 | 잡는 실패 |", "|---|---|---|",
         "| `max_score` | 1등 클래스의 decision score | 어느 클래스도 세게 주장 못 하는 경우 |",
         "| `top2_gap` | 1등 − 2등 | 세게 주장하지만 두 클래스 사이에서 흔들리는 경우 |", "",
         "## 2. 판단보류 없이 (그룹CV 전체)", "",
         "| 모델 | macroF1 | Accuracy |", "|---|---:|---:|"]
    for k, v in base.items():
        L.append("| %s | %.4f | %.4f |" % (k, v["macro_f1"], v["accuracy"]))
    L += ["",
          "확률로 바꾸지 않으니 M24 에서 보정 때문에 잃었던 성능이 그대로 돌아왔습니다.", "",
          "## 3. 커버리지 %.1f%% 고정 시 (LR 운영점)" % (TARGET_COVERAGE * 100), "",
          "margin 은 확률이 아니라 스케일이 신호마다 다릅니다. **커버리지를 축으로**",
          "**맞춰야** 서로 다른 신호를 나란히 비교할 수 있습니다.", "",
          "| 신호 | 정확도 | macroF1 | 그때의 임계값 |", "|---|---:|---:|---:|"]
    for k, v in at.items():
        L.append("| %s | **%.4f** | %.4f | %.4f |"
                 % (k, v["accuracy"], v["macro_f1"], v["threshold"]))

    L += ["", "## 4. 커버리지-정확도 곡선", "",
          "| 신호 | 40% | 60% | 70% | 80% | 100% |", "|---|---:|---:|---:|---:|---:|"]
    for name, rows in curves.items():
        picks = []
        for target in (0.4, 0.6, 0.7, 0.8, 1.0):
            r = min(rows, key=lambda x: abs(x["coverage"] - target))
            picks.append("%.4f" % r["accuracy"])
        L.append("| %s | %s |" % (name, " | ".join(picks)))

    L += ["", "## 5. 외부 정답셋 (M07 41건)", "",
          "**임계값은 CV 에서 정해 그대로 가져왔습니다.** 외부셋에서 임계값을 고르면",
          "그건 검증이 아니라 튜닝입니다.", ""]
    if isinstance(ext, dict) and ext.get("all"):
        L += ["| 조건 | 커버리지 | 건수 | 정확도 |", "|---|---:|---:|---:|",
              "| 판단보류 없이 | 100% | %d | %.4f |"
              % (ext["all"]["n"], ext["all"]["accuracy"])]
        for s in ("max_score", "top2_gap"):
            e = ext.get(s, {})
            if e:
                L.append("| %s >= %.4f | %.1f%% | %d | %s |"
                         % (s, e["threshold"], e["coverage"] * 100, e["n_kept"],
                            e["accuracy"]))
        L += ["| **LR (현행, 참고)** | 70.7% | 29 | **%.4f** |" % LR_EXTERNAL_ACCURACY, ""]
    else:
        L += ["수행 불가: %s" % ext.get("status", "?"), ""]

    L += ["## 6. 클래스별 Recall (계획서 44행)", "",
          "판단보류를 걸면 어느 클래스가 통째로 빠지는지 확인합니다 — 커버리지만",
          "보면 특정 클래스가 전부 보류로 밀려도 드러나지 않습니다.", "",
          "| 클래스 | 전체 n | 전체 Recall | 보류 적용 후 n | 보류 적용 후 Recall |",
          "|---|---:|---:|---:|---:|"]
    full_r = recalls["전체"]
    key = [k for k in recalls if k != "전체"][0]
    kept_r = recalls[key]
    for c in sorted(full_r, key=lambda k: -(full_r[k]["n"] if full_r[k] else 0)):
        f, kp = full_r.get(c), kept_r.get(c)
        L.append("| %s | %s | %s | %s | %s |"
                 % (c,
                    f["n"] if f else "—", "%.4f" % f["recall"] if f else "—",
                    kp["n"] if kp else "0", "%.4f" % kp["recall"] if kp else "—"))

    L += ["", "## 7. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 8. 맞바꾼 것", "", "```text",
          "얻은 것   LinearSVM 의 분류력(macroF1 0.7953)을 유지한 채 판단보류를 건다",
          "잃은 것   확률이 없으므로 화면에 '확신도 82%' 를 띄울 수 없다",
          "          margin 값은 스케일이 임의라 사용자에게 보여줄 숫자가 아니다",
          "```", "",
          "기획서가 사용자-facing 확률을 요구하지 않으므로 이 교환은 성립합니다.",
          "요구가 생기면 M24 의 isotonic 보정으로 되돌아가되, 정확도 −0.035 를",
          "감수해야 합니다.", ""]
    p = os.path.join(C.REPORTS, "m27_m1_margin_abstention.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
