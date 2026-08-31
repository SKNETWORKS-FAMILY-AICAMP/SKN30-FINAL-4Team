"""Model 2 serving wrapper — 비교군 percentile 대비 지원규모 회귀 참고값.

기존 canonical pipeline(`ml/pipelines/m56_m2_canonical.py`)의 `serve()` 를
그대로 호출한다. feature 생성·전처리(TF-IDF+SVD·범주 정렬·컬럼 순서)는
전부 그 모듈이 하고, 여기서는 재구현하지 않는다.

artifact 출처: ml/models/m65_model2_canonical/  (M65 canonical,
ml/docs/02_모델_1_2_3_성능_결과서.md 2.5절)

주의: 이 wrapper 가 내는 것은 회귀 참고값(pred/lo/hi)뿐이다. 모델 2 의
1차 산출물인 "비교군 percentile 위치"(M45.compare)는 전체 비교군 테이블이
있어야 계산되는데, 그 테이블은 이 최소 패키지에 포함되어 있지 않다
(README 참조).
"""
import os
import sys

import joblib

_SERVING_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_ROOT = os.path.abspath(os.path.join(_SERVING_DIR, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = os.path.join(_ML_ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import m56_m2_canonical as M56  # noqa: E402

BUNDLE_PATH = os.path.join(_SERVING_DIR, "model2_canonical.joblib")
SERVING_FIELDS = M56.SERVING_FIELDS  # 입력 레코드가 가져야 하는 필드 목록

_bundle = None


def _get_bundle():
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(BUNDLE_PATH)
    return _bundle


def predict(records):
    """records: SERVING_FIELDS 를 키로 갖는 dict 의 list.

    반환 (pandas.DataFrame, 행 순서는 입력과 동일):
        pred_log10, lo_log10, hi_log10  log10(원) 스케일 점추정·구간
        pred_won, lo_won, hi_won        원 단위로 환산한 값
    """
    bundle = _get_bundle()
    return M56.serve(bundle, records)


if __name__ == "__main__":
    demo = [{
        "title": "2023년 수출유망상품화 사업",
        "support_type": "판로", "support_method": "보조금", "support_unit": "기업당",
        "cohort": "중소기업", "category_large": "수출", "industry": "제조업",
        "agency_type": "공공기관", "amount_type": "per_company",
        "support_count": 50, "support_ratio": 70, "self_burden_ratio": 30,
        "project_duration": 12, "year": 2023,
    }]
    print(predict(demo))
