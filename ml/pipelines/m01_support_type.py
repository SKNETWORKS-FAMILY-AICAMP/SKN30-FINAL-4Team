"""M01 — 지원 성격 분류 (중분류를 대분류로 묶어서 재학습).

배경: "융자를 지원해줄지 연구비를 지원해줄지" 같은 지원 방식 분류가 목적이다.
이건 업종(식품/ICT/로봇 등 산업분야)이 아니라 중분류(연구개발/융자/보증/
사업화/컨설팅 등)의 역할이다. 업종 분류는 이 목적에 직접 관련이 없어 제외한다.

중분류 원본 61종은 "사업화(일반)/사업화(콘텐츠)/사업화(기술)/사업화(SW·서비스)/
사업화(수출)"처럼 괄호 안 세부 유형까지 쪼개져 있어 지나치게 세분화됐다.
괄호 앞 대분류만 남기면 지원 성격 단위로 자연스럽게 묶인다.

원천은 중앙부처 엑셀 2022·2023 두 해다(기업마당 두 원천에는 이 라벨이 없음).
F03 이 병합하면서 연도 간 재공고 중복은 걷어냈다.

알려진 한계 — '상담' 재현율
    '상담'(14건)은 개념이 '컨설팅'(172건)과 분명히 다르다.
      상담   상담창구·센터를 상시 운영해 누구나 전화·방문으로 물어보게 하는 것.
             산출물이 없고 선정 절차도 없다. (특허고객상담센터, 근로자 건강센터 등)
      컨설팅 선정된 기업에 컨설턴트를 투입해 진단·개선안을 내는 것.
             모집·선정이 있고 산출물이 있다. (일터혁신 컨설팅, 혁신바우처 등)
    그런데 재현율이 0.429 로 낮다. 원인은 어휘 오염이다. 상담 14건 중 5건(36%)이
    본문에 '컨설팅'이라는 단어를 담고 있다(ESG 경영지원의 "ESG 경영컨설팅",
    FTA 지원의 "방문컨설팅" 등). 반대 방향은 10% 뿐이라 오염이 한쪽으로만 흐르고,
    표본도 14 대 172 로 12배 차이가 나 다수 클래스로 끌려간다.

    그래도 클래스는 유지한다. 정밀도가 0.857 이라 '상담'이라고 예측하면 대체로
    맞고, 놓친 건은 확신도가 낮아 판단보류로 흘러가지 오답으로 나가지 않는다.
    (실측: 컨설팅 172건 중 상담으로 잘못 예측된 건은 0건. 오염은 일방향이다.)
    표본이 적어 재현율이 낮은 것이지 라벨이 틀린 게 아니므로, '판로지원'처럼
    통합해서는 안 된다.
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

import re
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.decomposition import TruncatedSVD
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score)
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import LinearSVC

from common import PROC, save_report

warnings.filterwarnings("ignore")
TAX = PROC + "/business_taxonomy.parquet"
# 학습에서 뺄 소수 클래스 기준. M06 에서 정했다.
#   규칙: 남는 클래스 가운데 F1 신뢰구간이 '매우 불안정'(±0.30 초과)인 게 하나도
#         없는 가장 낮은 값. 성능 최대화로 고르면 컷오프가 무한정 올라간다.
#   실측: 10 이면 남는 19종의 최악 CI 폭이 ±0.231, 버리는 데이터는 43건(3.0%).
#         8 이면 자격인증(8건)이 ±0.280, 5 면 공공구매(5건)가 ±0.305 로 남는다.
#   참고: 5-fold CV 는 구조적으로 클래스당 최소 5건을 요구한다. 이전 값 3은 그 아래라
#         일부 fold 에 클래스가 아예 없어 F1 측정 자체가 성립하지 않았다.
MIN_SUPPORT = 10


# catch-all 라벨. 2023 은 '기타지원', 2022 는 '기타' 로 이름만 다르고 성격이 같다.
EXCLUDED_TYPES = {"기타지원", "기타"}

# 두 해가 같은 개념을 다른 이름으로 적은 것들. 실제 사업명을 대조해 확인했다.
#   기술평가/기술·IP평가 — 양쪽에 'SW기술금융', '보건산업 기술가치평가'가 동일 등장
#   입주공간/입주지원   — 수출인큐베이터 / 글로벌비즈니스센터, 둘 다 해외거점 입주
#   판로지원/판로       — 수출마케팅·전자상거래수출로 판로개척과 같은 성격
#   헤외수주·실증       — '해'의 오타. 안 고치면 별도 클래스가 된다.
ALIAS = {
    "기술평가": "기술·IP평가",
    "입주공간": "입주지원",
    "판로지원": "판로",
    "헤외수주·실증": "해외수주·실증",
}

# 복수라벨 구분자. 2023 은 ','('연구개발, 컨설팅'), 2022 는 '+'('사업화(기술연계)+연구개발'),
# 그리고 '/'('성능인증/판로(종합마케팅)') 를 쓴다. 셋 다 첫 값을 대표로 삼는다.
MULTI_LABEL_SEP = re.compile(r"[,+/]")


def coarsen(v):
    """'사업화(일반)' -> '사업화'. 복수라벨은 첫 번째를 대표값으로 채택.

    '설비(스마트, 저감)'처럼 괄호 안에 콤마가 있는 값이 있어, 먼저 괄호를
    제거한 뒤 나눠야 한다(순서를 바꾸면 괄호 안 콤마에서 잘못 잘린다).

    catch-all 라벨은 학습에서 제외한다. 2023 원천에서는 사회보험료·퇴직연금 같은
    제도성 지원을 묶은 라벨인데, 2026 Open API 추론 대상(관광객 유치 인센티브
    등 이질적 항목)은 성격이 달라 학습에 넣으면 근거 없는 확신만 부여한다.
    실측: 이 라벨을 빼고 재학습한 모델에 (a) 2023 held-out 기타지원 36건,
    (b) RoBERTa가 실서비스에서 '기타지원'으로 예측한 2026년 221건을 넣었더니
    각각 86.1%/93.7%가 임계값 미만으로 자연스럽게 판단보류로 떨어졌다 —
    클래스로 유지할 근거가 없다.
    """
    if not isinstance(v, str) or not v.strip():
        return None
    if v.strip() in EXCLUDED_TYPES:
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = MULTI_LABEL_SEP.split(stripped)[0].strip()
    if not first:
        return None
    first = ALIAS.get(first, first)
    return None if first in EXCLUDED_TYPES else first


def tfidf():
    return TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                           min_df=2, sublinear_tf=True, max_features=60000)


def models(seed):
    from lightgbm import LGBMClassifier
    from xgboost import XGBClassifier
    from catboost import CatBoostClassifier
    svd = lambda: TruncatedSVD(n_components=150, random_state=seed)
    return {
        "Majority(하한선)": Pipeline([("t", tfidf()),
                                     ("m", DummyClassifier(strategy="most_frequent"))]),
        "TFIDF+LogisticRegression": Pipeline([("t", tfidf()), ("m", LogisticRegression(
            max_iter=2000, C=5.0, class_weight="balanced", random_state=seed))]),
        "TFIDF+LinearSVM": Pipeline([("t", tfidf()), ("m", LinearSVC(
            C=1.0, class_weight="balanced", random_state=seed))]),
        "SVD+RandomForest": Pipeline([("t", tfidf()), ("s", svd()), ("m", RandomForestClassifier(
            n_estimators=500, class_weight="balanced_subsample", n_jobs=-1, random_state=seed))]),
        "SVD+XGBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.1, subsample=0.9,
            colsample_bytree=0.9, tree_method="hist", n_jobs=-1,
            random_state=seed, verbosity=0))]),
        "SVD+LightGBM": Pipeline([("t", tfidf()), ("s", svd()), ("m", LGBMClassifier(
            n_estimators=300, learning_rate=0.1, num_leaves=15,
            min_child_samples=1, min_split_gain=0.0, n_jobs=-1,
            random_state=seed, verbose=-1))]),
        "SVD+CatBoost": Pipeline([("t", tfidf()), ("s", svd()), ("m", CatBoostClassifier(
            iterations=300, depth=5, learning_rate=0.1, verbose=0,
            random_seed=seed, allow_writing_files=False))]),
    }


def evaluate(X, y_raw, folds, seed):
    y = LabelEncoder().fit_transform(y_raw)
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    out, best_pred = {}, None
    for name, mk in models(seed).items():
        pred = np.zeros(len(y), dtype=int)
        for tr, te in skf.split(X, y):
            m = clone(mk)
            m.fit(X[tr], y[tr])
            # CatBoostClassifier.predict는 (n,1) 형태를 반환해 평탄화가 필요하다
            pred[te] = np.asarray(m.predict(X[te])).ravel().astype(int)
        out[name] = {
            "accuracy": round(float(accuracy_score(y, pred)), 4),
            "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
            "weighted_f1": round(float(f1_score(y, pred, average="weighted", zero_division=0)), 4),
        }
        print("  %-26s acc %.4f  macroF1 %.4f  wF1 %.4f"
              % (name, out[name]["accuracy"], out[name]["macro_f1"], out[name]["weighted_f1"]),
              flush=True)
        if name == max(out, key=lambda k: out[k]["macro_f1"]):
            best_pred = pred
    return out, y, best_pred


def main():
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)

    vc_before = t["middle_category"].value_counts(dropna=False)
    vc_after = t["support_type"].value_counts(dropna=False)
    print("중분류 원본 %d종 -> 지원성격 %d종으로 축소" % (t["middle_category"].nunique(), t["support_type"].nunique()))
    print()
    print("지원성격 분포:")
    for k, v in vc_after.items():
        print("  %-16s%4d건 (%.1f%%)" % (k, v, v / len(t) * 100))
    print()

    sub = t.dropna(subset=["support_type"]).copy()
    keep_vc = sub["support_type"].value_counts()
    keep = keep_vc[keep_vc >= MIN_SUPPORT].index
    n_excluded = int((~sub["support_type"].isin(keep)).sum())
    sub = sub[sub["support_type"].isin(keep)]

    X = sub["text_for_model"].fillna("").astype(str).values
    y_raw = sub["support_type"].values
    print("학습 대상: %d클래스 / %d건 (지원>=%d, 제외 %d건)"
          % (len(keep), len(sub), MIN_SUPPORT, n_excluded))
    print()

    results, y_enc, best_pred = evaluate(X, y_raw, folds=5, seed=42)
    best_name = max(results, key=lambda k: results[k]["macro_f1"])
    names = sorted(pd.Series(y_raw).unique())  # LabelEncoder alphabetical order matches sorted unique
    le_classes = LabelEncoder().fit(y_raw).classes_

    cm = confusion_matrix(y_enc, best_pred, labels=list(range(len(le_classes))))
    report = classification_report(y_enc, best_pred, target_names=le_classes,
                                   output_dict=True, zero_division=0)

    print("=" * 66)
    print("최종 채택: %s (macroF1 %.4f)" % (best_name, results[best_name]["macro_f1"]))
    print()
    print("혼동행렬 (행=실제, 열=예측)")
    header = "".join("%10s" % c[:8] for c in le_classes)
    print("%14s" % "" + header)
    for i, c in enumerate(le_classes):
        print("%14s" % c[:12] + "".join("%10d" % v for v in cm[i]))
    print()
    print(classification_report(y_enc, best_pred, target_names=le_classes,
                                digits=3, zero_division=0))

    save_report("m01_support_type.json", {
        "source": "2023 중앙부처 엑셀 909건 (기업마당 두 원천에는 이 라벨 없음)",
        "regroup_rule": "괄호 앞 대분류만 유지, 복수라벨은 첫 값 채택",
        "classes_before": int(t["middle_category"].nunique()),
        "classes_after": int(t["support_type"].nunique()),
        "class_dist_before": vc_before.to_dict(),
        "class_dist_after": vc_after.to_dict(),
        "min_support": MIN_SUPPORT,
        "classes_evaluated": int(len(keep)),
        "rows_evaluated": int(len(sub)),
        "excluded_rows": n_excluded,
        "folds": 5, "seed": 42,
        "split": "Stratified 5-Fold CV, TFIDF는 Pipeline으로 fold train에만 fit",
        "results": results,
        "best_model": best_name, "best_macro_f1": results[best_name]["macro_f1"],
        "confusion_matrix": cm.tolist(), "class_order": le_classes.tolist(),
        "per_class": {k: v for k, v in report.items() if k in le_classes},
        "macro_avg": report["macro avg"], "weighted_avg": report["weighted avg"],
        "known_limitations": {
            "상담_재현율": "0.43. '컨설팅'과 개념은 다르나(상시 상담창구 vs 선정 후 "
                          "컨설턴트 투입) 상담 14건 중 5건(36%)이 본문에 '컨설팅'을 "
                          "포함해 어휘가 오염됐고, 표본도 14 대 172 로 12배 차이다. "
                          "정밀도는 0.857 이고 역방향 오분류는 0건이라 클래스는 유지한다.",
            "소수_클래스_신뢰구간": "표본 10~20건 클래스(기술·IP평가·보증·상담·수출통관 등)는 "
                                  "F1 95% 신뢰구간이 ±0.2 수준이다. 점추정만 보면 안 된다. "
                                  "M06 참고.",
        },
    })


if __name__ == "__main__":
    main()
