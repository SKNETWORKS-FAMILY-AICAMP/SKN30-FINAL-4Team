"""M16 — 모델 4 성능 개선: Scaling · 조건부 탐지 · nu/gamma · Threshold Calibration.

개선계획서 Step 2 를 실행한다.
    log1p/Scaling -> Feature Ablation -> Conditional OCSVM -> nu/gamma tuning
    -> Threshold Calibration

계획서의 지표 하나를 바로잡고 시작한다.

    계획서: "One-Class SVM  Recall 78.3% / 정상 유지율 82.7%"

    82.7% 는 정상 유지율이 아니다. M13 이 낸 그 숫자는 **표본 80% 로 재학습했을 때
    상위 30건 이상 사례가 얼마나 유지되는가**(목록 재현성)이고, 오탐과는 무관하다.

    계획서가 실제로 원하는 것은 명확하다 — "공무원 서비스에서는 경고 피로 때문에
    정상 유지율이 중요". 그건 **정상 사업을 정상으로 두는 비율**, 즉 specificity 다.
    이 스크립트에서 그 값을 처음으로 측정한다.

        recall       합성 이상치를 상위 k 안에 잡는 비율      (놓치지 않는가)
        specificity  실제 사업을 경고하지 않는 비율            (성가시지 않은가)
        재현성       표본을 바꿔도 상위 목록이 유지되는 비율   (믿을 수 있는가)

    세 값은 서로 다른 것을 재고, 셋 다 있어야 판단이 된다.

목표 (계획서 Step 2)
    recall >= 75%, specificity >= 88~90%

기준선 (M13, 같은 2,339행 · 같은 합성 이상치)
    IsolationForest 8.3% / LOF 63.3% / OneClassSVM 78.3%
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import (CAT_FEATS, MIN_AXES, NUM_FEATS, SRC, inject_synthetic,
                            prepare)

OUT = os.path.join(C.PROC, "design_anomaly_tuned.parquet")
SEED = 42
TARGET_RECALL = 0.75
TARGET_SPECIFICITY = 0.88

# 계획서 6절의 feature 조합
EXPERIMENTS = {
    "A_설계핵심": {"num": ["log_per_recipient", "log_support_count", "project_duration"],
                "cat": ["support_method", "amount_type"]},
    "B_A+비율단위": {"num": NUM_FEATS,
                  "cat": ["support_method", "amount_type", "support_unit"]},
    "C_B+성격기관": {"num": NUM_FEATS, "cat": CAT_FEATS},
}


def encode(train, apply_df, num, cat, scaler="standard"):
    """계획서 2절 — StandardScaler / RobustScaler 비교.

    금액은 이미 log10 이라 log1p 를 다시 씌우지 않는다. 두 번 로그를 취하면
    자릿수 차이가 뭉개져 '기업당 1억'과 '기업당 10억'이 거의 같은 값이 된다.
    """
    parts_tr, parts_ap, names = [], [], []
    for f in num:
        # 비교군별로 학습할 때는 한 축이 통째로 비는 일이 생긴다(그 비교군에서
        # 아무도 기재하지 않은 항목). 중앙값이 NaN 이 되어 그대로 흘러가면
        # OneClassSVM 이 NaN 입력으로 죽는다. 0 으로 두면 표준화 후 평균 자리다.
        med = train[f].median()
        med = 0.0 if pd.isna(med) else med
        parts_tr.append(train[f].fillna(med).to_numpy())
        parts_ap.append(apply_df[f].fillna(med).to_numpy())
        names.append(f)
        parts_tr.append(train[f].isna().astype(float).to_numpy())
        parts_ap.append(apply_df[f].isna().astype(float).to_numpy())
        names.append(f + "__missing")
    for f in cat:
        freq = train[f].value_counts(normalize=True)
        parts_tr.append(train[f].map(freq).fillna(0.0).to_numpy())
        parts_ap.append(apply_df[f].map(freq).fillna(0.0).to_numpy())
        names.append(f + "__freq")

    Xtr, Xap = np.column_stack(parts_tr), np.column_stack(parts_ap)
    sc = (RobustScaler() if scaler == "robust" else StandardScaler()).fit(Xtr)
    return sc.transform(Xtr), sc.transform(Xap), names


def evaluate(train, num, cat, scaler, nu, gamma, n_syn=60, threshold_pct=None):
    """recall 과 specificity 를 같은 실행에서 함께 잰다.

    합성 이상치를 섞은 뒤 임계선 하나를 긋고 양쪽을 본다.
        recall       합성 이상치 중 경고된 비율
        specificity  실제 사업 중 경고되지 않은 비율
    임계선을 안 주면 합성 이상치 수(k)만큼 상위를 경고로 본다(M13 과 같은 기준).
    """
    syn = inject_synthetic(train, n_syn)
    mixed = pd.concat([train.assign(__synthetic=False), syn], ignore_index=True)
    Xtr, Xap, names = encode(train, mixed, num, cat, scaler)
    m = OneClassSVM(kernel="rbf", gamma=gamma, nu=nu).fit(Xtr)
    s = -m.score_samples(Xap)

    is_syn = mixed["__synthetic"].to_numpy()
    k = int(is_syn.sum())
    if threshold_pct is None:
        thr = np.sort(s)[::-1][k - 1]
    else:
        thr = np.percentile(s, threshold_pct)
    flagged = s >= thr

    recall = float(flagged[is_syn].mean())
    spec = float((~flagged[~is_syn]).mean())
    # specificity 는 경고를 적게 낼수록 저절로 올라간다(2,339행 중 60건만 경고하면
    # 안 건드린 2,279행이 전부 '정상 유지'로 계산된다). 그 값만 보면 오탐이 적은지
    # 알 수 없으므로 precision 과 경고율을 함께 낸다 — 담당자가 실제로 겪는
    # '경고 피로'는 경고 중 헛것의 비율이지 안 건드린 행의 비율이 아니다.
    prec = float(flagged[is_syn].sum() / max(flagged.sum(), 1))
    return {"recall": round(recall, 4), "specificity": round(spec, 4),
            "precision": round(prec, 4),
            "flag_rate": round(float(flagged.mean()), 4),
            "false_positive_rate": round(1 - spec, 4),
            "n_flagged": int(flagged.sum()),
            "n_flagged_real": int(flagged[~is_syn].sum())}, s, names


def calibrate(train, num, cat, scaler, nu, gamma):
    """계획서 5절 — recall 을 지키면서 specificity 를 최대로 하는 임계선.

    기본 decision boundary 를 그대로 쓰지 않는다. 경고 개수를 줄이면 오탐은
    줄지만 놓치는 것도 늘어난다. recall 하한을 먼저 못박고 그 안에서 고른다.
    """
    best = None
    for pct in (90, 92, 94, 95, 96, 97, 98, 99):
        r, _, _ = evaluate(train, num, cat, scaler, nu, gamma, threshold_pct=pct)
        r["threshold_pct"] = pct
        if r["recall"] >= TARGET_RECALL and (best is None
                                             or r["specificity"] > best["specificity"]):
            best = r
    return best


def conditional_eval(train, num, cat, scaler, nu, gamma, key, min_n=60):
    """계획서 3절 — 비교군별로 따로 학습한다.

    전체 한 덩어리로 학습하면 '융자라서 금액이 큰 것'이 이례로 잡힌다.
    비교군 안에서 재면 그 사업이 같은 부류 안에서 이례적인지를 본다.
    표본이 모자란 비교군은 전체 모델로 되돌린다(fallback).
    """
    syn = inject_synthetic(train, 60)
    mixed = pd.concat([train.assign(__synthetic=False), syn], ignore_index=True)
    is_syn = mixed["__synthetic"].to_numpy()
    scores = np.full(len(mixed), np.nan)

    groups = mixed.groupby(key, dropna=False).groups
    fallback_rows, n_grp = 0, 0
    for g, idx in groups.items():
        idx = np.asarray(idx)
        tr = train[train[key].fillna("__na__").eq(
            g if pd.notna(g) else "__na__")] if not isinstance(key, list) else None
        if tr is None or len(tr) < min_n:
            fallback_rows += len(idx)
            continue
        Xtr, Xap, _ = encode(tr, mixed.iloc[idx], num, cat, scaler)
        scores[idx] = -OneClassSVM(kernel="rbf", gamma=gamma,
                                   nu=nu).fit(Xtr).score_samples(Xap)
        n_grp += 1

    # fallback — 비교군이 얇은 행은 전체 모델 점수를 쓴다
    miss = np.isnan(scores)
    if miss.any():
        Xtr, Xap, _ = encode(train, mixed.iloc[miss], num, cat, scaler)
        scores[miss] = -OneClassSVM(kernel="rbf", gamma=gamma,
                                    nu=nu).fit(Xtr).score_samples(Xap)

    k = int(is_syn.sum())
    thr = np.sort(scores)[::-1][k - 1]
    flagged = scores >= thr
    return {"n_groups": n_grp, "fallback_rows": int(fallback_rows),
            "recall": round(float(flagged[is_syn].mean()), 4),
            "specificity": round(float((~flagged[~is_syn]).mean()), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    print("모델 4 튜닝 대상: %d행" % len(train))
    print("기준선(M13 OneClassSVM): recall 78.3%")
    print("목표(계획서): recall >= %.0f%%, specificity >= %.0f%%"
          % (TARGET_RECALL * 100, TARGET_SPECIFICITY * 100))

    t0 = time.time()
    out = {}

    # ---- 1. Scaling 비교 -------------------------------------------------
    print("\n== 1. Scaling (계획서 2절)")
    scal = {}
    for sc in ("standard", "robust"):
        r, _, _ = evaluate(train, NUM_FEATS, CAT_FEATS, sc, 0.05, "scale")
        scal[sc] = r
        print("  %-10s recall %.3f / specificity %.3f / precision %.3f"
              % (sc, r["recall"], r["specificity"], r["precision"]))
    best_scaler = max(scal, key=lambda k: scal[k]["specificity"])
    out["scaling"] = {"results": scal, "chosen": best_scaler}

    # ---- 2. Feature Ablation --------------------------------------------
    print("\n== 2. Feature Ablation (계획서 6절)")
    abl = {}
    for name, f in EXPERIMENTS.items():
        r, _, _ = evaluate(train, f["num"], f["cat"], best_scaler, 0.05, "scale")
        abl[name] = r
        print("  %-14s recall %.3f / specificity %.3f / precision %.3f"
              % (name, r["recall"], r["specificity"], r["precision"]))
    best_feat = max(abl, key=lambda k: (abl[k]["recall"] >= TARGET_RECALL,
                                        abl[k]["specificity"]))
    feats = EXPERIMENTS[best_feat]
    out["feature_ablation"] = {"results": abl, "chosen": best_feat}

    # ---- 3. nu / gamma 격자 ----------------------------------------------
    print("\n== 3. nu / gamma (계획서 4절)")
    nus = [0.02, 0.03, 0.05, 0.08, 0.10] if not a.quick else [0.03, 0.05]
    gammas = ["scale", 0.05, 0.1, 0.5] if not a.quick else ["scale", 0.1]
    grid = []
    for nu in nus:
        for g in gammas:
            r, _, _ = evaluate(train, feats["num"], feats["cat"], best_scaler, nu, g)
            r.update(nu=nu, gamma=str(g))
            grid.append(r)
    gdf = pd.DataFrame(grid)
    print(gdf[["nu", "gamma", "recall", "specificity", "precision"]].to_string(index=False))
    ok = gdf[gdf["recall"] >= TARGET_RECALL]
    pick = (ok.sort_values("specificity", ascending=False).iloc[0] if len(ok)
            else gdf.sort_values("recall", ascending=False).iloc[0])
    nu, gamma = float(pick["nu"]), pick["gamma"]
    gamma = float(gamma) if gamma != "scale" else "scale"
    print("  -> nu=%s gamma=%s (recall %.3f / specificity %.3f)"
          % (nu, gamma, pick["recall"], pick["specificity"]))
    out["nu_gamma"] = {"grid": grid, "chosen": {"nu": nu, "gamma": str(gamma)}}

    # ---- 4. 조건부 탐지 ---------------------------------------------------
    print("\n== 4. 조건부 탐지 (계획서 3절)")
    cond = {}
    for key in ("support_type", "support_method"):
        cond[key] = conditional_eval(train, feats["num"], feats["cat"],
                                     best_scaler, nu, gamma, key)
        print("  %-16s 비교군 %d개 / recall %.3f / specificity %.3f"
              % (key, cond[key]["n_groups"], cond[key]["recall"],
                 cond[key]["specificity"]))
    out["conditional"] = cond

    # ---- 5. Threshold Calibration ---------------------------------------
    print("\n== 5. Threshold Calibration (계획서 5절)")
    cal = calibrate(train, feats["num"], feats["cat"], best_scaler, nu, gamma)
    if cal:
        print("  상위 %d%% 경고 -> recall %.3f / precision %.3f / specificity %.3f"
              " (경고 %d건 중 헛것 %d건)"
              % (100 - cal["threshold_pct"], cal["recall"], cal["precision"],
                 cal["specificity"], cal["n_flagged"], cal["n_flagged_real"]))
    else:
        print("  recall %.0f%% 를 지키는 임계선이 없다" % (TARGET_RECALL * 100))
    out["calibration"] = cal

    # ---- 최종 ------------------------------------------------------------
    final, s, names = evaluate(train, feats["num"], feats["cat"], best_scaler,
                               nu, gamma,
                               threshold_pct=cal["threshold_pct"] if cal else None)
    verdict = judge(final, cond, out["scaling"]["results"])
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    Xtr, Xap, _ = encode(train, train, feats["num"], feats["cat"], best_scaler)
    sc_all = -OneClassSVM(kernel="rbf", gamma=gamma, nu=nu).fit(Xtr).score_samples(Xap)
    train2 = train.assign(
        anomaly_score=np.round((sc_all - sc_all.min())
                               / max(sc_all.max() - sc_all.min(), 1e-9), 4),
        score_pct=pd.Series(sc_all).rank(pct=True).to_numpy() * 100)
    train2["flagged"] = train2["score_pct"] >= (cal["threshold_pct"] if cal else 95)
    train2[["row_id", "cohort", "title", "support_type", "support_method",
            "n_axes", "anomaly_score", "score_pct", "flagged"]].to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("m16_m4_tuning.json", {
        "n_rows": int(len(train)),
        "note": ("계획서의 '정상 유지율 82.7%' 는 M13 의 재학습 목록 유지율이지 "
                 "오탐 지표가 아니다. 정상을 정상으로 두는 비율(specificity)을 "
                 "여기서 처음 측정했다."),
        "targets": {"recall": TARGET_RECALL, "specificity": TARGET_SPECIFICITY},
        "baseline_m13": {"OneClassSVM_recall": 0.7833},
        "chosen": {"scaler": best_scaler, "features": best_feat,
                   "nu": nu, "gamma": str(gamma),
                   "threshold_pct": cal["threshold_pct"] if cal else None},
        "final": final, "verdict": verdict, **out,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })
    write_md(out, final, cal, verdict, best_scaler, best_feat, nu, gamma)


def judge(final, cond, scal):
    reasons, v = [], "Conditional"
    r, s = final["recall"], final["specificity"]
    reasons.append("recall %.1f%% (목표 %.0f%%)" % (r * 100, TARGET_RECALL * 100))
    reasons.append("specificity %.1f%% (목표 %.0f%%) — 이번에 처음 측정한 값"
                   % (s * 100, TARGET_SPECIFICITY * 100))
    reasons.append("precision %.1f%% / 경고율 %.1f%% — specificity 는 경고를 적게 낼수록"
                   " 저절로 오르므로 이 둘을 함께 봐야 한다"
                   % (final["precision"] * 100, final["flag_rate"] * 100))
    if r >= TARGET_RECALL and s >= TARGET_SPECIFICITY:
        v = "Go"
        reasons.append("두 목표를 동시에 만족한다")
    elif r >= TARGET_RECALL:
        reasons.append("recall 은 지켰으나 specificity 가 목표에 못 미친다")
    else:
        reasons.append("recall 이 목표에 못 미친다 — 경고를 줄이려다 놓치는 것이 늘었다")
    best_cond = max(cond, key=lambda k: cond[k]["specificity"])
    reasons.append("조건부 탐지 최고는 %s 기준 (recall %.1f%% / specificity %.1f%%)"
                   % (best_cond, cond[best_cond]["recall"] * 100,
                      cond[best_cond]["specificity"] * 100))
    return {"verdict": v, "reasons": reasons}


def write_md(out, final, cal, verdict, scaler, feat, nu, gamma):
    L = ["# 모델 4 성능 개선 — Scaling · 조건부 탐지 · nu/gamma · Threshold", "",
         "## 0. 계획서의 '정상 유지율' 을 다시 정의한 이유", "",
         "> 계획서: \"One-Class SVM — Recall 78.3% / 정상 유지율 82.7%\"", "",
         "82.7% 는 정상 유지율이 아닙니다. M13 이 낸 그 값은 **표본 80% 로 재학습했을 때**",
         "**상위 30건 이상 사례가 얼마나 유지되는가**(목록 재현성)이고 오탐과 무관합니다.", "",
         "계획서가 실제로 원하는 것은 분명합니다 — \"경고 피로 때문에 정상 유지율이 중요\".",
         "그건 **정상 사업을 정상으로 두는 비율**, 즉 specificity 입니다. 여기서 처음",
         "측정했습니다.", "",
         "| 지표 | 무엇을 재는가 |", "|---|---|",
         "| recall | 합성 이상치를 놓치지 않는가 |",
         "| **specificity** | **실제 사업을 괜히 경고하지 않는가** |",
         "| precision | 낸 경고 중 진짜의 비율 |",
         "| 재학습 유지율 | 표본이 바뀌어도 상위 목록이 유지되는가 |", "",
         "> **specificity 는 혼자 보면 안 됩니다.** 2,339행 중 60건만 경고하면 안 건드린",
         "> 2,279행이 전부 '정상 유지'로 계산돼 값이 저절로 99% 대가 됩니다. 담당자가",
         "> 겪는 경고 피로는 *경고 중 헛것의 비율*(precision)이지 안 건드린 행의",
         "> 비율이 아닙니다. 그래서 precision 과 경고율을 함께 냅니다.", "",
         "## 1. Scaling (계획서 2절)", "",
         "| scaler | recall | specificity | precision |", "|---|---:|---:|---:|"]
    for k, r in out["scaling"]["results"].items():
        L.append("| %s | %.1f%% | %.1f%% | %.1f%% |"
                 % (k, r["recall"] * 100, r["specificity"] * 100, r["precision"] * 100))
    L += ["",
          "> 금액에 log1p 를 다시 씌우지 않았습니다. 이미 log10 이라 두 번 취하면",
          "> 자릿수 차이가 뭉개져 '기업당 1억'과 '10억'이 거의 같은 값이 됩니다.", "",
          "## 2. Feature Ablation (계획서 6절)", "",
          "| 조합 | recall | specificity | precision |", "|---|---:|---:|---:|"]
    for k, r in out["feature_ablation"]["results"].items():
        L.append("| %s | %.1f%% | %.1f%% | %.1f%% |"
                 % (k, r["recall"] * 100, r["specificity"] * 100, r["precision"] * 100))

    L += ["", "## 3. nu / gamma 격자 (계획서 4절)", "",
          "| nu | gamma | recall | specificity | precision |", "|---:|---|---:|---:|---:|"]
    for r in sorted(out["nu_gamma"]["grid"], key=lambda x: -x["recall"])[:10]:
        L.append("| %s | %s | %.1f%% | %.1f%% | %.1f%% |"
                 % (r["nu"], r["gamma"], r["recall"] * 100, r["specificity"] * 100,
                    r["precision"] * 100))

    L += ["", "## 4. 조건부 탐지 (계획서 3절)", "",
          "비교군별로 따로 학습합니다. 전체 한 덩어리로 학습하면 '융자라서 금액이 큰 것'이",
          "이례로 잡힙니다. 표본이 60건 미만인 비교군은 전체 모델로 되돌립니다.", "",
          "| 기준 | 비교군 수 | recall | specificity |", "|---|---:|---:|---:|"]
    for k, r in out["conditional"].items():
        L.append("| %s | %d | %.1f%% | %.1f%% |"
                 % (k, r["n_groups"], r["recall"] * 100, r["specificity"] * 100))

    L += ["", "## 5. Threshold Calibration (계획서 5절)", ""]
    if cal:
        L += ["recall %.0f%% 를 하한으로 두고 specificity 를 최대화했습니다." % (TARGET_RECALL * 100), "",
              "```text",
              "상위 %d%% 를 경고로 둔다" % (100 - cal["threshold_pct"]),
              "recall        %.1f%%" % (cal["recall"] * 100),
              "precision     %.1f%%" % (cal["precision"] * 100),
              "specificity   %.1f%%" % (cal["specificity"] * 100),
              "경고율        %.1f%%" % (cal["flag_rate"] * 100),
              "경고 %d건 중 헛것 %d건" % (cal["n_flagged"], cal["n_flagged_real"]),
              "```", ""]
    else:
        L += ["recall %.0f%% 를 지키는 임계선이 없었습니다." % (TARGET_RECALL * 100), ""]

    L += ["## 6. 최종 설정", "", "```text",
          "scaler       %s" % scaler,
          "features     %s" % feat,
          "nu           %s" % nu,
          "gamma        %s" % gamma,
          "threshold    상위 %s%%" % (100 - cal["threshold_pct"] if cal else "—"),
          "```", "",
          "## 7. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = os.path.join(C.REPORTS, "m16_m4_tuning.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
