"""[구세대 — M65] Model 2 serving wrapper.

    회귀(지원규모 추정)는 **`predict.py` (M82/P3) 로 대체됐다.**
        M65 0.4117 -> M69 0.3719 -> M73 0.3563 -> M82/P3 0.3518
    이 파일의 `predict()` 는 M65 단일 XGB 번들을 그대로 읽는다. 백엔드가
    전환을 마치면 지워도 된다.

    `percentile()` 은 아직 유효하다 — M82 는 회귀만 바꿨고 비교군 사다리
    (`m45_m2_amount.build_reference`)는 그대로다. `predict.py` 에도 같은
    함수가 있다.


기존 canonical pipeline(`ml/pipelines/model2/m56_m2_canonical.py`의 `serve()`,
`ml/pipelines/model2/m45_m2_amount.py`의 `build_reference()`/`compare()`)을 그대로
호출한다. feature 생성·전처리·비교군 사다리 로직은 전부 그 모듈들이 하고,
여기서는 재구현하지 않는다.

artifact 출처:
    model2_canonical.joblib / manifest.json   ml/models/m65_model2_canonical/
    cohort_reference.parquet                  M65 canonical 데이터셋
        (ml/data/processed/design_features_v2.parquet) 에
        `m45_m2_amount.prepare()` + `build_reference()` 를 그대로 적용해
        만든 비교군 사다리 참조표(85행). 원본 산출 방식은
        `ml/experiments/model2/core/m65_m2_canonical_v2.py` 의 STEP C 와 동일하다.
    (ml/docs/02_모델_1_2_3_성능_결과서.md 2.5절)

두 함수는 서로 독립이다. `predict()`(회귀)는 joblib 번들만 있으면 되고,
`percentile()`(비교군 위치)은 cohort_reference.parquet 만 있으면 된다 —
모델 2 의 1차 산출물은 percentile 이고 회귀는 비교군이 얇을 때의 보조
추정이라는 문서상 위치(2.0절)를 그대로 따른다.
"""
import os
import sys

import joblib
import pandas as pd

_SERVING_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_ROOT = os.path.abspath(os.path.join(_SERVING_DIR, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML_ROOT, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):          # 모델별 하위 폴더까지
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)

import m45_m2_amount as M45  # noqa: E402
import m56_m2_canonical as M56  # noqa: E402

BUNDLE_PATH = os.path.join(_SERVING_DIR, "model2_canonical.joblib")
REFERENCE_PATH = os.path.join(_SERVING_DIR, "cohort_reference.parquet")
SERVING_FIELDS = M56.SERVING_FIELDS  # 입력 레코드가 가져야 하는 필드 목록

_bundle = None
_reference = None


def _get_bundle():
    global _bundle
    if _bundle is None:
        _bundle = joblib.load(BUNDLE_PATH)
    return _bundle


def _get_reference():
    global _reference
    if _reference is None:
        _reference = pd.read_parquet(REFERENCE_PATH)
    return _reference


def predict(records):
    """records: SERVING_FIELDS 를 키로 갖는 dict 의 list.

    반환 (pandas.DataFrame, 행 순서는 입력과 동일):
        pred_log10, lo_log10, hi_log10  log10(원) 스케일 점추정·구간
        pred_won, lo_won, hi_won        원 단위로 환산한 값
    """
    bundle = _get_bundle()
    return M56.serve(bundle, records)


def percentile(value_won, support_type, support_method, unit, cohort):
    """모델 2 의 1차 산출물 — 유사사업 비교군 안에서 이 금액이 어디쯤인가.

    value_won: 원 단위 지원규모(기업당). unit/cohort 가 없으면 비교를
    포기한다("지원단위 미확정"/"비교 모집단 미선택") — `m45_m2_amount.lookup`
    의 규칙 그대로다.

    반환 (dict, `m45_m2_amount.compare()` 그대로): status, level, n,
    distribution(p1~p99), percentile_rank, spread_x, statement 등.
    `interval_tier` 는 OOF 재적합이 필요해 이 최소 패키지에서는 계산하지
    않는다(None으로 나온다) — 필요하면 `ml/reports/m65_m2_canonical_v2.md`
    의 tier 표를 참고할 것.
    """
    ref = _get_reference()
    return M45.compare(ref, value_won, support_type, support_method, unit, cohort)


if __name__ == "__main__":
    # support_method/support_unit/amount_type/agency_type/cohort 는 한글
    # 라벨이 아니라 원본 데이터의 영문 코드값이다 — 도메인은 README 참조.
    demo = [{
        "title": "2023년 수출유망상품화 사업",
        "support_type": "사업화", "support_method": "grant", "support_unit": "company",
        "cohort": "taxonomy", "category_large": "수출", "industry": "제조업",
        "agency_type": "public", "amount_type": "per_company",
        "support_count": 50, "support_ratio": 70, "self_burden_ratio": 30,
        "project_duration": 12, "year": 2023,
    }]
    print(predict(demo))
    print(percentile(200_000_000, "사업화", "grant", "company", "taxonomy"))
