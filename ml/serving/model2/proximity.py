"""Model 2 serving — proximity 추출 (M82/P3 의 P1 + P2 입력).

학습에서 쓴 `m82_m2_proximity_features` 의 정규식·window·컬럼 목록을 **그대로
import** 한다. 서빙에서 정규식을 다시 적으면 학습과 갈라진다.

    P1  explicit proximity feature (값 · 거리 · 후보수 · 플래그)
    P2  masked proximity context text -> TF-IDF/SVD (feature_builder 가 변환)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)

import m82_m2_proximity_features as M82   # noqa: E402

WINDOW = M82.WINDOW_PRIMARY               # 30 — M82 승격 스펙
NUMERIC_COLS = M82.NUMERIC_COLS
FLAG_COLS = M82.FLAG_COLS
EXPLICIT_COLS = NUMERIC_COLS + FLAG_COLS


def build(texts, window=WINDOW):
    """원문 텍스트 목록 -> proximity 프레임.

    `prox_context_text` 는 이미 마스킹된 상태로 나온다(M82.build_proximity 가
    `SF.mask_text` 를 적용한다).
    """
    return M82.build_proximity(list(texts), window)


def explicit(P):
    """P1 — 모델에 들어가는 명시 feature 만."""
    return P[EXPLICIT_COLS].reset_index(drop=True)


def context_text(P):
    """P2 입력 — 마스킹된 문맥 텍스트."""
    return P["prox_context_text"].to_numpy()
