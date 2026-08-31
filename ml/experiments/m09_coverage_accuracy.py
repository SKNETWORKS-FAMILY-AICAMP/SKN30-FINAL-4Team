"""M09 — 커버리지를 올릴 때 정확도·오분류 편향이 어떻게 변하는지 측정.

왜 필요한가
    지원규모를 지원성격별로 보려는데 라벨 커버리지가 48~53% 라 연도 칸이
    한 자릿수다(A03). 커버리지를 올려야 추이를 볼 수 있다. 그런데 임계값만
    내리면 틀린 라벨이 섞여 들어와, 그 유형의 금액 분포 자체가 오염된다.

    특히 위험한 게 방향성 있는 오염이다. M07(수동 정답 41건)에서 오답 5건 중
    3건이 '사업화'로 갔다. 확신이 낮은 건은 최다 클래스로 쏠린다. 그대로
    커버리지를 올리면 사업화 표본에 남의 건이 섞여 사업화 금액 중앙값이
    엉뚱해진다. 커버리지만 보고 정하면 안 되는 이유다.

무엇을 재는가 — 세 가지를 동시에
    ① 정확도    임계값 위 건들의 정답률·macro F1
    ② 오분류율  1 - 정확도. 그 유형 표본에 섞이는 남의 건 비율
    ③ 클래스 편향
        - 클래스별 정밀도: 그 유형으로 예측된 것 중 실제 그 유형인 비율.
          지원규모 추정에 직접 걸리는 값이다. 정밀도가 낮으면 그 유형의
          금액 분포가 남의 금액으로 오염된다.
        - 분포 왜곡(TVD): 임계값 위에서 '예측 분포'와 '정답 분포'가 얼마나
          벌어지는지. 특정 클래스로 쏠리는지 본다.
        - 유입/유출: 각 클래스가 어디서 오염되는지(최다 오답 출처).

임계값만 내리지 않는다 — 세 경로를 비교
    A. 임계값 인하        현행 방식. 0.10~0.40 스윕.
    B. 확률 보정 후 인하  isotonic 으로 확률을 보정하면 같은 정확도에서 더 많은
                          건을 건질 수 있는지. 보정은 fold train 안에서만 적합해
                          누수를 막는다.
    C. 클래스별 임계값    전역 임계값 하나 대신 클래스마다 목표 정밀도를 만족하는
                          임계값을 따로 잡는다. 사업화처럼 쏠림을 받는 클래스는
                          높게, 어휘가 뚜렷한 클래스는 낮게.

검증 설계
    학습셋(1,404건)에 StratifiedGroupKFold 를 적용해 out-of-fold 확률을 얻는다.
    program_stem 으로 묶어 같은 사업이 학습·검증에 갈리지 않게 한다(M05 참고).
    도메인 밖 확인은 M07 수동 정답 41건으로 따로 한다.

주의
    학습셋 정확도는 도메인 내부값이라 실제 적용(2026 지자체 공고)보다 낙관적이다.
    M07 대조를 같이 보고, 최종 임계값은 두 곳에서 모두 견디는 값으로 고른다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import argparse
import json
import os
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder

from common import PROC, save_report
from m01_support_type import MIN_SUPPORT, coarsen, tfidf

warnings.filterwarnings("ignore")

TAX = PROC + "/business_taxonomy.parquet"
LABELS = PROC + "/../labels/openapi_manual_50.csv"
OUT = PROC + "/coverage_accuracy_sweep.parquet"

THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
CURRENT = 0.25
TARGET_PRECISION = 0.70     # 지원규모 추정에 쓰려면 이 정도는 맞아야 한다


def won(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    x = float(x)
    if x >= 1e8:
        return "{:.1f}억원".format(x / 1e8)
    if x >= 1e4:
        return "{:,.0f}만원".format(x / 1e4)
    return "{:,.0f}원".format(x)


def base_model(seed):
    return Pipeline([("t", tfidf()),
                     ("m", LogisticRegression(max_iter=2000, C=5.0,
                                              class_weight="balanced",
                                              random_state=seed))])


def load_train():
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)
    le = LabelEncoder()
    y = le.fit_transform(sub["support_type"].values)
    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    groups = np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str))
    # pandas 3 은 문자열을 Arrow 백엔드로 담는다. CalibratedClassifierCV 내부의
    # 인덱싱이 그 배열을 다루지 못해 깨지므로 평범한 object 배열로 바꾼다.
    X = np.asarray(sub["text_for_model"].fillna("").astype(str).tolist(), dtype=object)
    return X, y, groups, le


def oof_proba(X, y, groups, folds, seed, calibrate=False):
    """out-of-fold 확률. 보정은 fold train 안에서만 적합한다(누수 방지)."""
    n_cls = len(np.unique(y))
    proba = np.zeros((len(y), n_cls))
    sgkf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    for tr, te in sgkf.split(X, y, groups):
        m = clone(base_model(seed))
        if calibrate:
            # 내부 CV 로 보정. 최소 클래스가 MIN_SUPPORT(10)이고 outer train 이
            # 그 4/5 라, 내부 fold 를 3으로 두면 한 fold 에 2건 미만이 되는
            # 클래스가 생겨 깨진다. 2-fold 로 낮춘다. sigmoid(Platt) 사용.
            n_min = int(np.bincount(y[tr]).min())
            cv = max(2, min(3, n_min // 2))
            m = CalibratedClassifierCV(m, method="sigmoid", cv=cv)
        m.fit(X[tr], y[tr])
        proba[te] = m.predict_proba(X[te])
    return proba


def sweep(proba, y, le, thresholds, tag):
    """임계값별 커버리지·정확도·편향."""
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    rows = []
    n = len(y)
    true_dist = pd.Series(y).value_counts(normalize=True)
    for t in thresholds:
        m = conf >= t
        if m.sum() == 0:
            continue
        acc = accuracy_score(y[m], pred[m])
        mf1 = f1_score(y[m], pred[m], average="macro", zero_division=0)
        pd_dist = pd.Series(pred[m]).value_counts(normalize=True)
        td = pd.Series(y[m]).value_counts(normalize=True)
        tvd = 0.5 * sum(abs(pd_dist.get(c, 0.0) - td.get(c, 0.0))
                        for c in range(len(le.classes_)))
        rows.append({"variant": tag, "threshold": t,
                     "coverage": round(float(m.mean()), 4),
                     "n_covered": int(m.sum()),
                     "accuracy": round(float(acc), 4),
                     "error_rate": round(float(1 - acc), 4),
                     "macro_f1": round(float(mf1), 4),
                     "pred_vs_true_tvd": round(float(tvd), 4)})
    return pd.DataFrame(rows)


def per_class_precision(proba, y, le, t):
    """클래스별 정밀도와 최다 오염 출처. 지원규모 추정에 직접 걸리는 값."""
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    m = conf >= t
    out = []
    for ci, name in enumerate(le.classes_):
        sel = m & (pred == ci)
        if sel.sum() == 0:
            continue
        correct = (y[sel] == ci).sum()
        prec = correct / sel.sum()
        wrong = y[sel][y[sel] != ci]
        top_src, top_n = None, 0
        if len(wrong):
            vc = pd.Series(wrong).value_counts()
            top_src, top_n = le.classes_[vc.index[0]], int(vc.iloc[0])
        out.append({"support_type": name, "n_predicted": int(sel.sum()),
                    "precision": round(float(prec), 4),
                    "n_wrong": int(len(wrong)),
                    "top_contaminant": top_src,
                    "top_contaminant_n": top_n})
    return sorted(out, key=lambda d: -d["n_predicted"])


def class_thresholds(proba, y, le, target, grid):
    """클래스별로 목표 정밀도를 만족하는 최소 임계값. 없으면 최대값."""
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    out = {}
    for ci, name in enumerate(le.classes_):
        chosen, best = None, None
        for t in grid:
            sel = (conf >= t) & (pred == ci)
            if sel.sum() < 5:
                continue
            prec = (y[sel] == ci).mean()
            best = (t, float(prec), int(sel.sum()))
            if prec >= target:
                chosen = (t, float(prec), int(sel.sum()))
                break
        pick = chosen or best
        if pick:
            out[name] = {"threshold": pick[0], "precision": round(pick[1], 4),
                         "n": pick[2], "meets_target": bool(chosen)}
    return out


def apply_class_thresholds(proba, y, le, th):
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    keep = np.zeros(len(y), dtype=bool)
    for ci, name in enumerate(le.classes_):
        t = th.get(name, {}).get("threshold", 1.1)
        keep |= (pred == ci) & (conf >= t)
    if keep.sum() == 0:
        return None
    return {"coverage": round(float(keep.mean()), 4), "n_covered": int(keep.sum()),
            "accuracy": round(float(accuracy_score(y[keep], pred[keep])), 4),
            "macro_f1": round(float(f1_score(y[keep], pred[keep],
                                             average="macro", zero_division=0)), 4)}


def amount_distortion(proba, y, le, thresholds, seed=42, n_boot=400):
    """오분류가 '금액 추정'을 얼마나 흔드는지 시뮬레이션.

    왜 시뮬레이션인가
        학습셋에는 per_company 금액이 57건뿐이라 예측라벨/정답라벨로 금액을
        직접 비교할 표본이 안 된다. 그래서 두 재료를 합친다.
          ㄱ. 혼동행렬 — 임계값 t 에서 '유형 X 로 예측된 것' 중 실제 무엇이
              얼마나 섞이는지 (도메인 내부 OOF)
          ㄴ. 유형별 금액 분포 — 관측 테이블(F05)에서 유형별 per_company 금액

        X 로 예측된 표본을 ㄱ의 비율대로 ㄴ에서 뽑아 섞은 뒤 중앙값을 낸다.
        오염이 없을 때(순수 X)의 중앙값과 비교하면, 임계값을 낮춰 커버리지를
        올릴 때 그 유형의 금액 추정이 어느 쪽으로 얼마나 밀리는지 나온다.

    가정과 한계
        ㄴ의 유형별 금액 분포 자체가 이미 예측라벨로 만들어진 것이라 완전히
        깨끗하지 않다. 따라서 절대값이 아니라 '임계값을 바꿀 때 얼마나 더
        밀리는가'라는 상대 비교로만 읽는다.
    """
    # 금액은 이 브랜치(machine-learning)에서 구할 수 있는 것만 쓴다.
    # support_amount_observations 는 F05(timeseries-analysis) 산출물이라
    # 여기서 읽으면 상류가 하류를 읽는 역류가 된다.
    enr = PROC + "/announcement_detail_enriched.parquet"
    prd = PROC + "/announcement_detail_with_support_type_v2.parquet"
    if not (os.path.exists(enr) and os.path.exists(prd)):
        return []
    e = pd.read_parquet(enr)[["announcement_id", "support_amount_type",
                              "support_amount_max"]]
    p = pd.read_parquet(prd)[["announcement_id", "support_type_pred",
                              "support_type_status"]]
    o = e.merge(p, on="announcement_id", how="inner")
    o = o[(o["support_amount_type"] == "per_company")
          & o["support_amount_max"].notna()
          & (o["support_type_status"] != "판단보류")]
    amounts = {k: g["support_amount_max"].values
               for k, g in o.groupby("support_type_pred") if len(g) >= 5}
    if not amounts:
        return []

    rng = np.random.default_rng(seed)
    conf = proba.max(axis=1)
    pred = proba.argmax(axis=1)
    out = []
    for t in thresholds:
        m = conf >= t
        for ci, name in enumerate(le.classes_):
            if name not in amounts:
                continue
            sel = m & (pred == ci)
            if sel.sum() < 10:
                continue
            comp = pd.Series(y[sel]).value_counts(normalize=True)
            comp = {le.classes_[i]: w for i, w in comp.items()
                    if le.classes_[i] in amounts}
            if not comp or name not in comp:
                continue
            tot = sum(comp.values())
            comp = {k: v / tot for k, v in comp.items()}

            pure = float(np.median(amounts[name]))
            meds = []
            size = int(sel.sum())
            keys = list(comp)
            probs = [comp[k] for k in keys]
            for _ in range(n_boot):
                pick = rng.choice(len(keys), size=size, p=probs)
                vals = np.concatenate([
                    rng.choice(amounts[keys[i]], size=int((pick == i).sum()),
                               replace=True)
                    for i in range(len(keys)) if (pick == i).sum() > 0])
                meds.append(np.median(vals))
            mixed = float(np.median(meds))
            out.append({"threshold": t, "support_type": name,
                        "precision": round(float(comp[name]), 4),
                        "pure_median": pure, "contaminated_median": mixed,
                        "shift_ratio": round(float(mixed / pure), 3) if pure else None,
                        "shift_pct": round(float((mixed / pure - 1) * 100), 1) if pure else None,
                        "n_predicted": size})
    return out


def manual_check(thresholds):
    """도메인 밖 확인 — M07 수동 정답 41건. 예측 파일에서 확신도를 읽는다."""
    pred_path = PROC + "/announcement_detail_with_support_type_v2.parquet"
    if not (os.path.exists(LABELS) and os.path.exists(pred_path)):
        return []
    lab = pd.read_csv(LABELS, encoding="utf-8-sig")
    lab["announcement_id"] = lab["announcement_id"].astype(str)
    lab = lab[lab["label_19class"].fillna("").astype(str) != ""]
    p = pd.read_parquet(pred_path)
    p["announcement_id"] = p["announcement_id"].astype(str)
    m = lab.merge(p[["announcement_id", "support_type_pred",
                     "support_type_confidence"]], on="announcement_id", how="left")
    m = m[m["support_type_pred"].notna()]
    out = []
    for t in thresholds:
        s = m[m["support_type_confidence"] >= t]
        if len(s) < 5:
            continue
        acc = (s["support_type_pred"] == s["label_19class"]).mean()
        out.append({"threshold": t, "n_covered": int(len(s)),
                    "coverage": round(float(len(s) / len(m)), 4),
                    "accuracy": round(float(acc), 4)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--target-precision", type=float, default=TARGET_PRECISION)
    args = ap.parse_args()

    X, y, groups, le = load_train()
    print("학습셋 %d건 / %d클래스 / 그룹 %d개 (StratifiedGroupKFold %d-fold)"
          % (len(y), len(le.classes_), len(set(groups)), args.folds))
    print("목표 정밀도 %.2f — 지원규모 추정에 쓰려면 이 정도는 맞아야 한다" % args.target_precision)
    print()

    # ---- A. 임계값 인하 ----
    print("[A] 임계값 인하 (현행 방식)")
    pa = oof_proba(X, y, groups, args.folds, args.seed, calibrate=False)
    sa = sweep(pa, y, le, THRESHOLDS, "A_threshold")
    print("%10s%10s%10s%10s%10s%12s" % ("임계값", "커버리지", "정확도", "오분류율", "macroF1", "분포왜곡"))
    print("-" * 62)
    for _, r in sa.iterrows():
        mark = "  <- 현행" if abs(r["threshold"] - CURRENT) < 1e-9 else ""
        print("%10.2f%9.1f%%%9.1f%%%9.1f%%%10.4f%12.4f%s"
              % (r["threshold"], r["coverage"] * 100, r["accuracy"] * 100,
                 r["error_rate"] * 100, r["macro_f1"], r["pred_vs_true_tvd"], mark))

    # ---- B. 확률 보정 ----
    print()
    print("[B] 확률 보정(isotonic 대신 sigmoid, fold train 내부 적합) 후 인하")
    pb = oof_proba(X, y, groups, args.folds, args.seed, calibrate=True)
    sb = sweep(pb, y, le, THRESHOLDS, "B_calibrated")
    print("%10s%10s%10s%10s%10s%12s" % ("임계값", "커버리지", "정확도", "오분류율", "macroF1", "분포왜곡"))
    print("-" * 62)
    for _, r in sb.iterrows():
        print("%10.2f%9.1f%%%9.1f%%%9.1f%%%10.4f%12.4f"
              % (r["threshold"], r["coverage"] * 100, r["accuracy"] * 100,
                 r["error_rate"] * 100, r["macro_f1"], r["pred_vs_true_tvd"]))

    # 같은 정확도에서 커버리지 비교
    print()
    print("같은 정확도 수준에서 커버리지 비교 (보정이 이득인가)")
    print("%12s%16s%16s" % ("정확도 기준", "A 커버리지", "B 커버리지"))
    print("-" * 46)
    for lvl in (0.80, 0.75, 0.70, 0.65):
        ca = sa[sa["accuracy"] >= lvl]["coverage"].max()
        cb = sb[sb["accuracy"] >= lvl]["coverage"].max()
        print("%12.2f%15s%16s"
              % (lvl,
                 "%.1f%%" % (ca * 100) if pd.notna(ca) else "달성불가",
                 "%.1f%%" % (cb * 100) if pd.notna(cb) else "달성불가"))

    # ---- C. 클래스별 임계값 ----
    print()
    print("[C] 클래스별 임계값 (목표 정밀도 %.2f)" % args.target_precision)
    grid = [round(x, 2) for x in np.arange(0.10, 0.71, 0.05)]
    ct = class_thresholds(pa, y, le, args.target_precision, grid)
    print("%-12s%10s%12s%8s%10s" % ("지원성격", "임계값", "정밀도", "n", "목표달성"))
    print("-" * 54)
    for k, v in sorted(ct.items(), key=lambda kv: -kv[1]["n"]):
        print("%-12s%10.2f%12.4f%8d%10s"
              % (k, v["threshold"], v["precision"], v["n"],
                 "O" if v["meets_target"] else "X"))
    cres = apply_class_thresholds(pa, y, le, ct)
    if cres:
        print("→ 클래스별 임계값 적용 시: 커버리지 %.1f%% / 정확도 %.1f%% / macroF1 %.4f"
              % (cres["coverage"] * 100, cres["accuracy"] * 100, cres["macro_f1"]))

    # ---- 클래스별 정밀도: 현행 vs 완화 ----
    print()
    print("[편향] 클래스별 정밀도와 최다 오염 출처")
    for t in (CURRENT, 0.15):
        pc = per_class_precision(pa, y, le, t)
        print()
        print("  임계값 %.2f" % t)
        print("  %-12s%8s%10s%10s%16s" % ("지원성격", "예측수", "정밀도", "오답수", "최다오염원"))
        print("  " + "-" * 58)
        for r in pc[:10]:
            src = "%s(%d)" % (r["top_contaminant"], r["top_contaminant_n"]) if r["top_contaminant"] else "—"
            print("  %-12s%8d%10.4f%10d%16s"
                  % (r["support_type"], r["n_predicted"], r["precision"],
                     r["n_wrong"], src))

    # ---- 오분류 -> 금액 왜곡 ----
    dist = amount_distortion(pa, y, le, [0.15, 0.20, 0.25, 0.30])
    if dist:
        print()
        print("[금액 왜곡] 오분류가 유형별 금액 중앙값을 얼마나 미는가 (시뮬레이션)")
        df = pd.DataFrame(dist)
        for t in sorted(df["threshold"].unique()):
            s = df[df["threshold"] == t].sort_values("n_predicted", ascending=False)
            print()
            print("  임계값 %.2f" % t)
            print("  %-12s%10s%14s%14s%10s" % ("지원성격", "정밀도", "순수중앙값", "오염중앙값", "왜곡"))
            print("  " + "-" * 62)
            for _, r in s.iterrows():
                print("  %-12s%10.3f%14s%14s%9.1f%%"
                      % (r["support_type"], r["precision"],
                         won(r["pure_median"]), won(r["contaminated_median"]),
                         r["shift_pct"]))

    # ---- 도메인 밖 확인 ----
    mc = manual_check(THRESHOLDS)
    if mc:
        print()
        print("[도메인 밖] M07 수동 정답 대조 (Open API)")
        print("%10s%10s%10s%10s" % ("임계값", "채점수", "커버리지", "정확도"))
        print("-" * 42)
        for r in mc:
            print("%10.2f%10d%9.1f%%%9.1f%%"
                  % (r["threshold"], r["n_covered"], r["coverage"] * 100,
                     r["accuracy"] * 100))

    allsweep = pd.concat([sa, sb], ignore_index=True)
    allsweep.to_parquet(OUT, index=False)

    save_report("m09_coverage_accuracy.json", {
        "question": ("커버리지를 올릴 때 정확도·오분류 편향이 어떻게 변하는가. "
                     "지원규모 추정에 쓸 최적 커버리지를 정하기 위한 근거."),
        "cv": "StratifiedGroupKFold (program_stem)",
        "folds": args.folds, "seed": args.seed,
        "n_rows": int(len(y)), "n_classes": int(len(le.classes_)),
        "current_threshold": CURRENT,
        "target_precision": args.target_precision,
        "A_threshold_sweep": sa.to_dict("records"),
        "B_calibrated_sweep": sb.to_dict("records"),
        "C_per_class_thresholds": ct,
        "C_result": cres,
        "per_class_precision_current": per_class_precision(pa, y, le, CURRENT),
        "per_class_precision_relaxed": per_class_precision(pa, y, le, 0.15),
        "manual_check_openapi": mc,
        "amount_distortion": dist,
        "caveat": ("학습셋 정확도는 도메인 내부값이라 실제 적용(2026 지자체 공고)보다 "
                   "낙관적이다. M07 대조를 함께 보고 두 곳에서 모두 견디는 값을 고른다."),
        "output": OUT,
    })
    print()
    print("→ %s" % OUT)


if __name__ == "__main__":
    main()
