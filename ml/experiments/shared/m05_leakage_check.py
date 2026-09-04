"""M05 — 2022 병합이 준 성능 향상이 진짜인지 검증(누수 점검).

문제
    2022·2023 은 같은 사업을 해마다 다시 공고한다. F03 이 본문 유사도 0.9 이상인
    재공고를 걷어냈지만, 그보다 낮은 유사도로 남은 사업이 아직 215개 있다
    (`program_stem` 이 두 해에 모두 나타나는 경우). 이 사업들은 문구가 조금 달라도
    같은 사업이라, 일반 K-Fold 에서는 2022 판이 학습에 2023 판이 검증에 들어가
    "정답을 이미 본" 상태가 된다. 성능이 실제보다 좋게 나온다.

측정
    같은 데이터·같은 모델을 두 가지 분할로 나란히 잰다.
      ① StratifiedKFold       — M01 이 쓰는 방식. 같은 사업이 갈라질 수 있다.
      ② StratifiedGroupKFold  — program_stem 을 그룹으로 묶어 통째로 한쪽에만 넣는다.
    ②가 실제 운영 성능(처음 보는 사업)에 가깝다. ①과 ②의 차이가 누수분이다.

    참고로 병합 전(2023 단독)에도 같은 두 분할을 재서, 향상폭을 같은 잣대로 비교한다.
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
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import LinearSVC

from common import PROC, save_report
from m01_support_type import MIN_SUPPORT, coarsen, tfidf

warnings.filterwarnings("ignore")
TAX = PROC + "/business_taxonomy.parquet"


def candidates(seed):
    """M01 상위 2개(선형 계열)만 본다. 트리 계열은 이 데이터에서 크게 뒤쳐진다."""
    return {
        "TFIDF+LogisticRegression": Pipeline([("t", tfidf()), ("m", LogisticRegression(
            max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))]),
        "TFIDF+LinearSVM": Pipeline([("t", tfidf()), ("m", LinearSVC(
            C=1.0, class_weight="balanced", random_state=seed))]),
    }


def cv_scores(X, y, groups, splitter, seed):
    out = {}
    for name, mk in candidates(seed).items():
        pred = np.zeros(len(y), dtype=int)
        args = (X, y, groups) if groups is not None else (X, y)
        for tr, te in splitter.split(*args):
            m = clone(mk)
            m.fit(X[tr], y[tr])
            pred[te] = np.asarray(m.predict(X[te])).ravel().astype(int)
        out[name] = {
            "accuracy": round(float(accuracy_score(y, pred)), 4),
            "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
            "weighted_f1": round(float(f1_score(y, pred, average="weighted", zero_division=0)), 4),
        }
    return out


def prepare(t):
    t = t.copy()
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)]
    X = sub["text_for_model"].fillna("").astype(str).values
    y = LabelEncoder().fit_transform(sub["support_type"].values)
    # 같은 사업(program_stem)은 한 그룹. 단독 사업은 각자 고유 그룹이 되게 한다.
    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    groups = np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str))
    return X, y, groups, sub


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()

    full = pd.read_parquet(TAX)
    datasets = {
        "병합 (2022+2023)": full,
        "병합 전 (2023 단독)": full[full["source_year"] == 2023],
    }

    report, table = {}, []
    for label, df in datasets.items():
        X, y, groups, sub = prepare(df)
        n_grp = len(set(groups))
        plain = cv_scores(X, y, None,
                          StratifiedKFold(n_splits=args.folds, shuffle=True,
                                          random_state=args.seed), args.seed)
        grouped = cv_scores(X, y, groups,
                            StratifiedGroupKFold(n_splits=args.folds, shuffle=True,
                                                 random_state=args.seed), args.seed)
        report[label] = {
            "rows": int(len(sub)),
            "classes": int(len(set(y))),
            "groups": n_grp,
            "rows_in_shared_programs": int(len(sub) - n_grp),
            "stratified_kfold": plain,
            "stratified_group_kfold": grouped,
        }
        for model in plain:
            table.append((label, model, plain[model]["macro_f1"],
                          grouped[model]["macro_f1"],
                          plain[model]["accuracy"], grouped[model]["accuracy"]))

    print("%-20s%-26s%10s%10s%9s" % ("데이터", "모델", "일반CV", "그룹CV", "누수분"))
    print("-" * 76)
    for label, model, p_f1, g_f1, _, _ in table:
        print("%-20s%-26s%10.4f%10.4f%9.4f" % (label, model, p_f1, g_f1, p_f1 - g_f1))
    print()
    print("(macro F1 기준. 누수분 = 일반CV - 그룹CV, 클수록 같은 사업이 학습·검증에 나뉘어 든 것)")
    print()

    for model in candidates(args.seed):
        before = report["병합 전 (2023 단독)"]["stratified_group_kfold"][model]["macro_f1"]
        after = report["병합 (2022+2023)"]["stratified_group_kfold"][model]["macro_f1"]
        print("%-26s 그룹CV 기준 실제 향상: %.4f -> %.4f (%+.4f)"
              % (model, before, after, after - before))

    save_report("m05_leakage_check.json", {
        "purpose": "2022 병합의 성능 향상에서 연도 간 재공고 누수분을 분리",
        "design": "같은 데이터·모델을 StratifiedKFold 와 StratifiedGroupKFold 로 각각 측정. "
                  "그룹 키는 제목에서 연도를 뺀 program_stem.",
        "folds": args.folds, "seed": args.seed,
        "min_support": MIN_SUPPORT,
        "datasets": report,
        "caution": "일반 K-Fold 수치(M01 이 보고하는 값)는 두 해에 걸친 사업 때문에 "
                   "낙관적으로 치우친다. 운영 성능 추정에는 그룹 CV 값을 쓴다.",
    })


if __name__ == "__main__":
    main()
