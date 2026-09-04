"""Model 2 serving — 마스킹.

학습에서 쓰는 규칙을 **다시 구현하지 않는다.** `m2_source_features.mask_text`
하나만 쓴다 — 서빙이 규칙을 복제하면 학습과 조용히 갈라진다(M82 에서 실제로
겪은 문제: proximity 문맥을 마스킹하지 않아 target 자릿수가 char n-gram 으로
샐 수 있었다).

    금액 표현 -> [AMOUNT]
    남은 숫자 -> #
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

import m2_source_features as SF          # noqa: E402

BODY_CAP = SF._BODY_CAP


def mask_body(text):
    """본문 텍스트 feature 용 마스킹 (앞 4,000자)."""
    return SF.mask_text(text)


def mask_context(text, cap=2000):
    """proximity 문맥용 마스킹. TF-IDF 에 넘기기 전 반드시 통과시킨다."""
    return SF.mask_text(text, cap=cap)


def has_digit_residue(text):
    """마스킹이 실제로 먹었는지 확인하는 감사용 헬퍼."""
    import re
    return bool(re.search(r"\d", str(text or "")))
