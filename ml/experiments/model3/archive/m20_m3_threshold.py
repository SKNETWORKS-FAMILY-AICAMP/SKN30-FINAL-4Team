"""M20 — 모델 4 경고정책 검증: Alert Budget · Threshold Robustness.

추가개선계획서 9절. 핵심 문장을 그대로 옮긴다.

    "숫자 추가 상승보다 실제 경고정책의 안정성 검증"

계획서가 지적한 문제 두 가지를 먼저 정리한다.

    1. 지표를 한 조건으로 통일하라 (8절)
       M16 은 "recall 95.0%"(상위 k 기준)와 "recall 80.0%"(상위 2% 기준)를
       같이 보고했다. 잣대가 다른 두 숫자라 최종 보고서에 함께 올리면 안 된다.
       여기서는 **경고율(alert rate)을 축으로 고정**하고 전부 그 위에서 잰다.

    2. specificity 를 단독 제시하지 마라 (8절)
       상위 2%만 경고하면 안 건드린 98%가 전부 '정상 유지'로 계산된다.
       Recall / Precision / Alert Rate 를 항상 함께 낸다.

여기서 하는 것
    9.1 Threshold Robustness   경고율 1/2/3/5/10% 별 recall·precision·정상유지율
    9.3 Alert Budget           "전체 사업 중 몇 %까지 경고할 것인가"의 근거표
    10  평가 지표 확장          F1 / PR-AUC / 임계선

    9.2 실제 이상 사례 hold-out 은 여기서 못 한다 — 사람이 확인한 정답
        30~50건이 필요하다. 대신 그 자리를 비워 두고 무엇이 필요한지 적는다.
        합성 이상치만으로 잰 값이라는 한계를 리포트에 명시한다.

기준선 (M16 최종 설정)
    scaler standard / features A_설계핵심 / nu 0.02 / gamma 0.5
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
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m3_anomaly import MIN_AXES, SRC, inject_synthetic, prepare
from m16_m3_ocsvm_tuning import EXPERIMENTS, encode

OUT = os.path.join(C.PROC, "design_anomaly_alerts.parquet")
SEED = 42
ALERT_RATES = [0.01, 0.02, 0.03, 0.05, 0.10]
N_SYNTHETIC = 60
N_REPEAT = 5            # 합성 이상치를 시드를 바꿔 여러 번 만들어 흔들림을 본다


def score_once(train, num, cat, scaler, nu, gamma, seed):
    """합성 이상치를 섞어 점수를 낸다. 시드마다 다른 이상치가 만들어진다."""
    rng = np.random.default_rng(seed)
    syn = inject_synthetic(train, N_SYNTHETIC, rng=rng)
    mixed = pd.concat([train.assign(__synthetic=False), syn], ignore_index=True)
    Xtr, Xap, _ = encode(train, mixed, num, cat, scaler)
    s = -OneClassSVM(kernel="rbf", gamma=gamma, nu=nu).fit(Xtr).score_samples(Xap)
    return s, mixed["__synthetic"].to_numpy()


def at_alert_rate(s, is_syn, rate):
    """경고율을 고정하고 그 위에서 전부 잰다 — 잣대를 하나로 통일한다."""
    k = max(1, int(round(len(s) * rate)))
    thr = np.sort(s)[::-1][k - 1]
    flagged = s >= thr
    tp = int(flagged[is_syn].sum())
    fp = int(flagged[~is_syn].sum())
    fn = int((~flagged[is_syn]).sum())
    tn = int((~flagged[~is_syn]).sum())
    recall = tp / max(tp + fn, 1)
    prec = tp / max(tp + fp, 1)
    return {
        "alert_rate": round(float(flagged.mean()), 4),
        "n_alerts": int(flagged.sum()),
        "recall": round(float(recall), 4),
        "precision": round(float(prec), 4),
        "normal_retention": round(float(tn / max(tn + fp, 1)), 4),
        "f1": round(float(2 * prec * recall / max(prec + recall, 1e-9)), 4),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def sweep(train, num, cat, scaler, nu, gamma, n_repeat=N_REPEAT):
    """경고율별로 여러 시드에서 재고 평균±표준편차를 낸다 (robustness)."""
    rows = {r: [] for r in ALERT_RATES}
    prauc = []
    for i in range(n_repeat):
        s, is_syn = score_once(train, num, cat, scaler, nu, gamma, SEED + i)
        prauc.append(float(average_precision_score(is_syn, s)))
        for r in ALERT_RATES:
            rows[r].append(at_alert_rate(s, is_syn, r))
    out = []
    for r, rs in rows.items():
        agg = {"alert_rate_target": r, "n_repeat": len(rs)}
        for k in ("alert_rate", "n_alerts", "recall", "precision",
                  "normal_retention", "f1"):
            v = np.array([x[k] for x in rs], dtype=float)
            agg[k] = round(float(v.mean()), 4)
            agg[k + "_std"] = round(float(v.std()), 4)
        agg["fp_mean"] = round(float(np.mean([x["fp"] for x in rs])), 1)
        out.append(agg)
    return out, {"pr_auc_mean": round(float(np.mean(prauc)), 4),
                 "pr_auc_std": round(float(np.std(prauc)), 4)}


def recommend(sweep_rows, min_recall=0.75):
    """경고 예산을 고르는 근거. recall 하한을 지키는 가장 작은 경고율."""
    ok = [r for r in sweep_rows if r["recall"] >= min_recall]
    if not ok:
        return None
    return min(ok, key=lambda r: r["alert_rate_target"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=N_REPEAT)
    a = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    with open(C.report_path("m16_m3_ocsvm_tuning.json"), encoding="utf-8") as f:
        m16 = json.load(f)
    ch = m16["chosen"]
    scaler, nu = ch["scaler"], float(ch["nu"])
    gamma = ch["gamma"] if ch["gamma"] == "scale" else float(ch["gamma"])
    feats = EXPERIMENTS[ch["features"]]

    print("모델 4 경고정책 검증: %d행" % len(train))
    print("M16 설정: scaler=%s / features=%s / nu=%s / gamma=%s"
          % (scaler, ch["features"], nu, gamma))
    print("합성 이상치 %d건 x 시드 %d회" % (N_SYNTHETIC, a.repeat))

    t0 = time.time()
    rows, prauc = sweep(train, feats["num"], feats["cat"], scaler, nu, gamma,
                        a.repeat)

    print("\n== 경고율별 성능 (계획서 9.1·9.3절)")
    print("%-8s %-16s %-16s %-16s %-10s" %
          ("경고율", "recall", "precision", "정상유지율", "F1"))
    for r in rows:
        print("%-8s %.3f±%.3f     %.3f±%.3f     %.4f±%.4f   %.3f"
              % ("%.0f%%" % (r["alert_rate_target"] * 100),
                 r["recall"], r["recall_std"], r["precision"], r["precision_std"],
                 r["normal_retention"], r["normal_retention_std"], r["f1"]))
    print("\nPR-AUC %.4f ± %.4f" % (prauc["pr_auc_mean"], prauc["pr_auc_std"]))

    rec = recommend(rows)
    print("\n== 권장 경고 예산")
    if rec:
        print("  경고율 %.0f%% — recall %.1f%% / precision %.1f%% / 실제 경고 %d건 중"
              " 헛것 %.1f건"
              % (rec["alert_rate_target"] * 100, rec["recall"] * 100,
                 rec["precision"] * 100, rec["n_alerts"], rec["fp_mean"]))
    else:
        print("  recall 75%% 를 지키는 경고율이 없다")

    verdict = judge(rows, rec, prauc)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    # 실제 사업에 최종 경고 표시를 붙여 저장
    Xtr, Xap, _ = encode(train, train, feats["num"], feats["cat"], scaler)
    s = -OneClassSVM(kernel="rbf", gamma=gamma, nu=nu).fit(Xtr).score_samples(Xap)
    pct = pd.Series(s).rank(pct=True).to_numpy() * 100
    budget = rec["alert_rate_target"] if rec else 0.02
    train2 = train.assign(
        anomaly_score=np.round((s - s.min()) / max(s.max() - s.min(), 1e-9), 4),
        score_pct=pct, alerted=pct >= (100 - budget * 100))
    train2[["row_id", "cohort", "title", "support_type", "support_method",
            "n_axes", "anomaly_score", "score_pct", "alerted"]] \
        .to_parquet(OUT, index=False)
    print("[data] %s  (경고 %d건)" % (OUT, int(train2["alerted"].sum())))

    C.save_report("m20_m3_threshold.json", {
        "n_rows": int(len(train)), "n_synthetic": N_SYNTHETIC,
        "n_repeat": a.repeat, "config": {"scaler": scaler, "features": ch["features"],
                                         "nu": nu, "gamma": str(gamma)},
        "note": ("경고율(alert rate)을 축으로 고정하고 모든 지표를 그 위에서 쟀다. "
                 "M16 이 잣대가 다른 두 recall 을 함께 보고한 문제를 여기서 정리한다."),
        "limitation": ("합성 이상치로만 평가했다. 계획서 9.2절의 '사람이 확인한 실제 "
                       "이상 사례 30~50건 hold-out' 은 라벨링이 필요해 수행하지 못했다. "
                       "그 검증 전에는 '실제 정책사업에서도 작동한다'고 말할 수 없다."),
        "sweep": rows, "pr_auc": prauc, "recommended": rec, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2)})
    write_md(rows, prauc, rec, verdict, ch)


def judge(rows, rec, prauc):
    reasons, v = [], "조건부 채택"
    if rec:
        reasons.append("경고율 %.0f%% 에서 recall %.1f%% / precision %.1f%% "
                       "— recall 75%% 하한을 지키는 가장 작은 경고 예산"
                       % (rec["alert_rate_target"] * 100, rec["recall"] * 100,
                          rec["precision"] * 100))
        if rec["recall_std"] < 0.05:
            reasons.append("시드를 바꿔도 recall 표준편차 %.3f — 경고정책이 흔들리지 않는다"
                           % rec["recall_std"])
            v = "채택"
        else:
            reasons.append("시드에 따라 recall 이 ±%.3f 흔들린다 — 경고 예산을 여유 있게 "
                           "잡아야 한다" % rec["recall_std"])
    else:
        reasons.append("recall 75% 를 지키는 경고율이 없다")
        v = "미채택"

    lo, hi = rows[0], rows[-1]
    reasons.append("경고율 %.0f%% -> %.0f%% 로 늘리면 recall %.1f%% -> %.1f%%, "
                   "precision %.1f%% -> %.1f%%"
                   % (lo["alert_rate_target"] * 100, hi["alert_rate_target"] * 100,
                      lo["recall"] * 100, hi["recall"] * 100,
                      lo["precision"] * 100, hi["precision"] * 100))
    reasons.append("PR-AUC %.4f — 경고율과 무관한 순위 품질" % prauc["pr_auc_mean"])
    reasons.append("한계: 합성 이상치로만 쟀다. 사람이 확인한 실제 이상 사례 "
                   "hold-out(계획서 9.2절) 전에는 실제 사업에서 작동한다고 말할 수 없다")
    return {"verdict": v, "reasons": reasons}


def write_md(rows, prauc, rec, verdict, ch):
    L = ["# 모델 4 경고정책 검증 — Alert Budget · Threshold Robustness", "",
         "> 계획서 11절: \"숫자 추가 상승보다 실제 경고정책의 안정성 검증\"", "",
         "## 0. 잣대를 하나로 통일했습니다", "",
         "M16 은 recall 을 두 번 보고했습니다 — 상위 k 기준 **95.0%**, 상위 2% 기준",
         "**80.0%**. 잣대가 다른 두 숫자라 최종 보고서에 함께 올리면 안 됩니다.", "",
         "여기서는 **경고율(alert rate)을 축으로 고정**하고 모든 지표를 그 위에서",
         "쟀습니다. 계획서 8절의 지적을 그대로 반영한 것입니다.", "",
         "```text",
         "scaler     %s" % ch["scaler"],
         "features   %s" % ch["features"],
         "nu         %s" % ch["nu"],
         "gamma      %s" % ch["gamma"],
         "합성 이상치 %d건 x 시드 %d회" % (N_SYNTHETIC, rows[0]["n_repeat"]),
         "```", "",
         "## 1. 경고율별 성능 (계획서 9.1·9.3절)", "",
         "| 경고율 | 경고 건수 | recall | precision | 정상 유지율 | F1 | 헛경고 |",
         "|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        mark = " **←권장**" if rec and r is rec else ""
        L.append("| %.0f%%%s | %d | %.1f%% ± %.1f | %.1f%% ± %.1f | %.2f%% | %.3f | %.1f건 |"
                 % (r["alert_rate_target"] * 100, mark, r["n_alerts"],
                    r["recall"] * 100, r["recall_std"] * 100,
                    r["precision"] * 100, r["precision_std"] * 100,
                    r["normal_retention"] * 100, r["f1"], r["fp_mean"]))
    L += ["", "PR-AUC **%.4f ± %.4f** — 경고율을 어떻게 잡든 변하지 않는 순위 품질입니다."
          % (prauc["pr_auc_mean"], prauc["pr_auc_std"]), "",
          "> **정상 유지율만 따로 보면 안 됩니다.** 상위 1%만 경고하면 안 건드린 99%가",
          "> 전부 '정상 유지'로 계산돼 값이 저절로 99%대가 됩니다. 담당자가 겪는 경고",
          "> 피로는 *경고 중 헛것의 비율*(precision)입니다. 위 표는 계획서 8절대로",
          "> recall / precision / alert rate 를 항상 함께 냅니다.", "",
          "## 2. 왜 이 경고 예산인가 (계획서 9.3절)", ""]
    if rec:
        L += ["recall 75% 를 하한으로 두고, 그것을 지키는 **가장 작은** 경고율을 골랐습니다.",
              "경고를 더 내면 recall 은 오르지만 담당자가 볼 건수도 같이 늘어납니다.", "",
              "```text",
              "경고율      %.0f%%  (%d건 경고)"
              % (rec["alert_rate_target"] * 100, rec["n_alerts"]),
              "recall      %.1f%% ± %.1f" % (rec["recall"] * 100, rec["recall_std"] * 100),
              "precision   %.1f%% ± %.1f" % (rec["precision"] * 100, rec["precision_std"] * 100),
              "헛경고      평균 %.1f건" % rec["fp_mean"],
              "```", ""]
    else:
        L += ["recall 75% 를 지키는 경고율이 없습니다.", ""]

    L += ["## 3. Threshold Robustness (계획서 9.1절)", "",
          "합성 이상치를 시드를 바꿔 %d번 다시 만들고 같은 경고율에서 재측정했습니다."
          % rows[0]["n_repeat"],
          "표준편차가 작으면 경고정책이 표본에 따라 흔들리지 않는다는 뜻입니다.", "",
          "| 경고율 | recall σ | precision σ |", "|---:|---:|---:|"]
    for r in rows:
        L.append("| %.0f%% | %.3f | %.3f |"
                 % (r["alert_rate_target"] * 100, r["recall_std"], r["precision_std"]))

    L += ["", "## 4. 하지 못한 것 (계획서 9.2절)", "",
          "**사람이 확인한 실제 이상 사례 30~50건 hold-out** 은 수행하지 못했습니다.",
          "라벨링에 사람 손이 필요합니다.", "",
          "그 검증 전까지 이 수치는 **합성 이상치를 얼마나 잘 잡는가**만 말합니다.",
          "실제 정책사업에서도 작동한다고 주장하려면 다음이 필요합니다.", "",
          "```text",
          "1. 담당자가 '이건 확인이 필요하다'고 판단한 실제 사업 30~50건",
          "2. 그 목록은 threshold 튜닝에 쓰지 않고 최종 검증에만 사용",
          "3. 같은 경고율에서 recall 을 다시 측정",
          "```", "",
          "## 5. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = C.report_path("m20_m3_threshold.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
