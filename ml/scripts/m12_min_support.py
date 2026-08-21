"""M12 — 학습 제외 기준(MIN_SUPPORT)을 실측으로 정한다.

문제
    '연구장비'(4건) 같은 클래스는 개념이 잘못된 게 아니라 표본이 없어서 측정이
    안 되는 경우다. catch-all 이라 뺀 '기타지원'과는 성격이 다르므로, 개념이 아니라
    통계적 신뢰도를 기준으로 잘라야 한다.

함정
    컷오프를 올리면 macro F1 은 저절로 오른다. 점수가 낮은 소수 클래스를 지웠기
    때문이지 모델이 좋아진 게 아니다. 그래서 "macro F1 이 최대인 지점"으로 고르면
    컷오프가 무한정 올라간다. 버리는 데이터의 양(coverage)을 같이 봐야 한다.

진짜 기준
    컷오프는 탐색해서 찾는 하이퍼파라미터가 아니다. "클래스별 F1 을 얼마나 못 믿어도
    참을 수 있나"라는 신뢰도 기준이고, 그건 표본 수만으로 정해진다. 그래서 컷오프마다
    CV 를 다시 도는 건 낭비다. 이 스크립트는 싼 방법부터 순서대로 낸다.

    B. 학습 0회 — 표본 수로 Wilson 신뢰구간 폭을 계산한다. 이게 본 판단 근거다.
       n=4 인 '근속장려금'의 F1 1.000 은 "완벽"이 아니라 "0.51 일 수도 있다"는 뜻이다.
    A. CV 1회 — 성능 곡선까지 보고 싶을 때. 가장 낮은 컷오프로 한 번만 돌린 뒤
       클래스별 F1 을 재사용해 나머지 컷오프를 평균으로 근사한다.
    C. CV 6회(컷오프별 전량 재실행) — A 의 근사 오차를 확인하려고만 남겨둔다.
       --full 을 줘야 돈다. 평소에는 돌릴 이유가 없다.

    참고로 5-fold CV 는 구조적으로 클래스당 최소 5건을 요구한다. 그보다 적으면
    어떤 fold 에는 그 클래스가 한 건도 안 들어가, 그 fold 에서는 맞힐 수가 없다.

시드 표준편차를 믿지 말 것
    표본 3~4 건 구간은 시드를 바꿔도 F1 이 안 변해 표준편차가 0 으로 나온다.
    안정적이어서가 아니라, 표본이 3 건이면 각 표본이 딱 한 번씩만 예측돼 결과가
    결정적이기 때문이다. 같은 4 건짜리 클래스들의 F1 이 0.000~1.000 으로 흩어지는
    것이 실제 불안정성이다. 그래서 판단은 신뢰구간(B)으로 한다.
"""
import argparse
import math
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder

from common import PROC, save_report
from m06_support_type import coarsen, tfidf

warnings.filterwarnings("ignore")
TAX = PROC + "/business_taxonomy.parquet"
CUTOFFS = [3, 5, 8, 10, 15, 20]
SEEDS = [42, 7, 123, 2024, 31]
REFERENCE_N = [3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 50, 100, 150]

# 신뢰구간 폭에 따른 판정. 컷오프는 이 표에서 "어느 폭까지 참을 것인가"로 정한다.
VERDICTS = [(0.40, "무의미"), (0.30, "매우 불안정"), (0.22, "참고용"),
            (0.15, "사용가능"), (0.00, "신뢰가능")]


def wilson(p, n, z=1.96):
    """비율 p 의 Wilson 95% 신뢰구간. 표본이 적거나 p 가 0/1 이어도 무너지지 않는다."""
    if n <= 0:
        return 0.0, 1.0
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, center - half), min(1.0, center + half)


def verdict(half_width):
    for threshold, label in VERDICTS:
        if half_width > threshold:
            return label
    return VERDICTS[-1][1]


def model(seed):
    return Pipeline([("t", tfidf()), ("m", LogisticRegression(
        max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))])


def make_groups(sub):
    """같은 사업(program_stem)은 한 그룹으로 묶어 학습·검증에 갈라 들어가지 않게 한다."""
    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    return np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str))


def cv_predict(X, y, groups, seed, folds=5):
    pred = np.zeros(len(y), dtype=int)
    sgkf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    for tr, te in sgkf.split(X, y, groups):
        m = clone(model(seed))
        m.fit(X[tr], y[tr])
        pred[te] = np.asarray(m.predict(X[te])).ravel().astype(int)
    return pred


def load():
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    return t.dropna(subset=["support_type"]).reset_index(drop=True)


def reference_table():
    """[B] 학습 0회. 최악(p=0.5) 기준으로 표본 수별 신뢰구간 폭만 낸다."""
    out = {}
    for n in REFERENCE_N:
        lo, hi = wilson(0.5, n)
        half = (hi - lo) / 2
        out[n] = {"ci_half_width": round(half, 4), "verdict": verdict(half)}
    return out


def per_class_f1(full, cutoff, seed, folds):
    """[A] CV 1회. 클래스별 F1·표본 수·신뢰구간. 나머지 표는 전부 여기서 파생된다."""
    vc = full["support_type"].value_counts()
    sub = full[full["support_type"].isin(vc[vc >= cutoff].index)].reset_index(drop=True)
    X = sub["text_for_model"].fillna("").astype(str).values
    le = LabelEncoder().fit(sub["support_type"].values)
    y = le.transform(sub["support_type"].values)
    pred = cv_predict(X, y, make_groups(sub), seed, folds)
    f1s = f1_score(y, pred, average=None, labels=range(len(le.classes_)), zero_division=0)
    rows = []
    for cls, v in zip(le.classes_, f1s):
        n = int(vc[cls])
        lo, hi = wilson(float(v), n)
        half = (hi - lo) / 2
        rows.append({"class": cls, "n": n, "f1": round(float(v), 4),
                     "ci_low": round(lo, 4), "ci_high": round(hi, 4),
                     "ci_half_width": round(half, 4), "verdict": verdict(half)})
    rows.sort(key=lambda r: r["n"])
    return rows, int(len(sub))


def approx_sweep(rows, n_labeled):
    """[A] CV 1회 결과만으로 컷오프별 macro F1 을 근사한다. 재학습 없음.

    클래스를 빼면 남은 클래스의 결정경계도 조금 달라지므로 근사값이다.
    실측 대비 오차는 컷오프 3~10 구간에서 0.008 이하였다(--full 로 확인).
    """
    out = {}
    for cut in CUTOFFS:
        sel = [r for r in rows if r["n"] >= cut]
        if not sel:
            continue
        kept = sum(r["n"] for r in sel)
        out[cut] = {
            "classes": len(sel),
            "rows": kept,
            "dropped_rows": n_labeled - kept,
            "dropped_pct": round((n_labeled - kept) / n_labeled * 100, 2),
            "macro_f1_approx": round(float(np.mean([r["f1"] for r in sel])), 4),
            "worst_ci_half_width": round(max(r["ci_half_width"] for r in sel), 4),
        }
    return out


def full_sweep(full, n_labeled, seed, folds):
    """[C] 검증 전용 — 컷오프마다 전량 재학습. approx_sweep 의 오차 확인용."""
    out = {}
    for cut in CUTOFFS:
        vc = full["support_type"].value_counts()
        sub = full[full["support_type"].isin(vc[vc >= cut].index)].reset_index(drop=True)
        X = sub["text_for_model"].fillna("").astype(str).values
        y = LabelEncoder().fit_transform(sub["support_type"].values)
        pred = cv_predict(X, y, make_groups(sub), seed, folds)
        out[cut] = {
            "classes": int(len(set(y))), "rows": int(len(sub)),
            "dropped_rows": int(n_labeled - len(sub)),
            "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
            "weighted_f1": round(float(f1_score(y, pred, average="weighted", zero_division=0)), 4),
            "accuracy": round(float(accuracy_score(y, pred)), 4),
        }
    return out


def seed_stability(full, seeds, folds):
    """[C] 검증 전용 — 시드별 F1 변동.

    표본 3~4건 구간에서는 이 값이 0 으로 나와 안정적인 것처럼 보이므로 판단
    근거로 쓰지 않는다(모듈 docstring 참고). 신뢰구간과 대조하려고만 남긴다.
    """
    vc = full["support_type"].value_counts()
    base = full[full["support_type"].isin(vc[vc >= 3].index)].reset_index(drop=True)
    X = base["text_for_model"].fillna("").astype(str).values
    le = LabelEncoder().fit(base["support_type"].values)
    y = le.transform(base["support_type"].values)
    groups = make_groups(base)
    per = defaultdict(list)
    for seed in seeds:
        pred = cv_predict(X, y, groups, seed, folds)
        f1s = f1_score(y, pred, average=None, labels=range(len(le.classes_)), zero_division=0)
        for cls, v in zip(le.classes_, f1s):
            per[cls].append(float(v))
    rows = [{"class": c, "n": int(vc[c]),
             "f1_mean": round(float(np.mean(v)), 4),
             "f1_std": round(float(np.std(v)), 4),
             "f1_min": round(float(np.min(v)), 4),
             "f1_max": round(float(np.max(v)), 4)} for c, v in per.items()]
    rows.sort(key=lambda r: r["n"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--full", action="store_true",
                    help="컷오프별 전량 재학습·시드 반복까지 실행(근사 검증용, 6배 이상 느림)")
    args = ap.parse_args()

    full = load()
    n_labeled = len(full)

    # ---------- B. 학습 0회 ----------
    ref = reference_table()
    print("[B] 표본 수만으로 계산한 신뢰구간 (학습 0회)")
    print("%8s%16s%14s" % ("표본", "95%CI 폭(±)", "판정"))
    print("-" * 38)
    for n, v in ref.items():
        print("%8d%16.3f%14s" % (n, v["ci_half_width"], v["verdict"]))
    print("\n→ 컷오프는 '어느 폭까지 참을 것인가'로 정한다. 성능 최대화로 고르면 안 된다.\n")

    # ---------- A. CV 1회 ----------
    rows, _ = per_class_f1(full, min(CUTOFFS), args.seed, args.folds)
    print("[A] CV 1회 — 클래스별 F1 과 그 신뢰구간")
    print("%-14s%6s%9s%18s%9s%12s" % ("클래스", "표본", "F1", "95% CI", "폭(±)", "판정"))
    print("-" * 70)
    for r in rows:
        print("%-14s%6d%9.3f%18s%9.3f%12s"
              % (r["class"], r["n"], r["f1"],
                 "%.2f ~ %.2f" % (r["ci_low"], r["ci_high"]),
                 r["ci_half_width"], r["verdict"]))

    approx = approx_sweep(rows, n_labeled)
    print("\n[A] 같은 CV 1회 결과로 근사한 컷오프별 성능·손실 (재학습 없음)")
    print("%8s%9s%9s%14s%14s%12s"
          % ("컷오프", "클래스", "학습건수", "버린건수", "macroF1(근사)", "최악CI폭"))
    print("-" * 70)
    for cut, v in approx.items():
        print("%8d%9d%9d%14s%14.4f%12.3f"
              % (cut, v["classes"], v["rows"],
                 "%d (%.1f%%)" % (v["dropped_rows"], v["dropped_pct"]),
                 v["macro_f1_approx"], v["worst_ci_half_width"]))

    report = {
        "purpose": "MIN_SUPPORT 컷오프를 통계적 신뢰도로 결정 (탐색이 아니라 신뢰도 기준)",
        "method": {
            "B_free": "표본 수 -> Wilson 95% CI 폭. 학습 0회. 본 판단 근거.",
            "A_one_cv": "가장 낮은 컷오프로 CV 1회 -> 클래스별 F1 재사용해 전 컷오프 근사.",
            "C_full": "컷오프별 전량 재학습. --full 일 때만. A 의 근사 오차 확인용.",
        },
        "folds": args.folds, "seed": args.seed,
        "labeled_rows": n_labeled,
        "reference_ci_by_n": {str(k): v for k, v in ref.items()},
        "per_class": rows,
        "cutoff_approx": {str(k): v for k, v in approx.items()},
        "caution": "컷오프를 올리면 macro F1 은 어려운 클래스를 제거한 효과로 저절로 오른다. "
                   "성능 최대화로 컷오프를 고르면 안 된다.",
        "structural_note": f"{args.folds}-fold CV 는 클래스당 최소 {args.folds}건을 요구한다. "
                           f"그 미만이면 일부 fold 에 해당 클래스가 없어 구조적으로 맞힐 수 없다.",
        "seed_std_warning": "표본 3~4건 구간은 시드 표준편차가 0 으로 나오지만 안정적이라는 뜻이 "
                            "아니다. 각 표본이 한 번씩만 예측돼 결과가 결정적일 뿐이다.",
    }

    # ---------- C. 검증 전용 ----------
    if args.full:
        exact = full_sweep(full, n_labeled, args.seed, args.folds)
        print("\n[C] 컷오프별 전량 재학습 — 근사 검증")
        print("%8s%14s%14s%10s" % ("컷오프", "근사", "실측", "오차"))
        print("-" * 46)
        errs = []
        for cut, v in exact.items():
            a = approx[cut]["macro_f1_approx"]
            errs.append(abs(a - v["macro_f1"]))
            print("%8d%14.4f%14.4f%10.4f" % (cut, a, v["macro_f1"], a - v["macro_f1"]))
        print("\n평균 절대오차 %.4f / 최대 %.4f" % (np.mean(errs), np.max(errs)))
        report["cutoff_exact"] = {str(k): v for k, v in exact.items()}
        report["approx_error"] = {"mean_abs": round(float(np.mean(errs)), 4),
                                  "max_abs": round(float(np.max(errs)), 4)}
        report["seed_stability"] = seed_stability(full, SEEDS, args.folds)
        report["seeds"] = SEEDS

    save_report("m12_min_support.json", report)


if __name__ == "__main__":
    main()
