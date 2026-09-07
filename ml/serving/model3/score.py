"""Model 3 serving — 가이드 구조상의 진입점 이름.

실제 구현은 같은 폴더의 `inference.py` 다. 기존 호출부를 깨지 않으려고 이름만
얹는다.

**`from inference import *` 로 하면 안 된다.** model1/model2/model3 폴더에
`inference.py` 가 각각 있어서, 여러 모델을 한 프로세스에서 쓰면
`sys.modules["inference"]` 캐시 때문에 **다른 모델의 구현**이 잡힌다(실제로
API 스모크에서 모델 1 자리에 모델 3 이 불렸다). 그래서 파일 경로로 고유
이름을 붙여 불러온다.
"""
import importlib.util as _ilu
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

_NAME = "model3_inference"
if _NAME in _sys.modules:
    _impl = _sys.modules[_NAME]
else:
    _spec = _ilu.spec_from_file_location(_NAME, _os.path.join(_HERE, "inference.py"))
    _impl = _ilu.module_from_spec(_spec)
    _sys.modules[_NAME] = _impl
    _spec.loader.exec_module(_impl)

for _k in dir(_impl):
    if not _k.startswith("_"):
        globals()[_k] = getattr(_impl, _k)


# --------------------------------------------------------------- 축 유효성
#
# `MIN_AXES=2` 는 **몇 칸이 찼는가**만 센다. 사전협의서에서는 그것으로 부족하다 —
# 칸은 찼는데 의미가 틀린 경우가 실제로 나오기 때문이다. 예시 문서에서 공용
# 파서는 `project_duration=28.0`(사업기간 표기 오파싱)과 `support_count=4`
# (3차년도 선별지원 과제 수)를 채웠다. 두 축 모두 '채워진' 축으로 세어지지만
# 어느 쪽도 모델 3 이 학습한 의미가 아니다.
#
# 그래서 축 개수와 **의미 유효성**을 함께 본다. 점수 계산은 그대로 두고,
# 그 점수를 얼마나 믿을 수 있는지만 함께 돌려준다.
import pandas as _pd                                       # noqa: E402

NUM_AXES = ["log_per_recipient", "log_support_count", "support_ratio",
            "project_duration"]
_AXIS_SOURCE = {"log_per_recipient": "per_recipient",
                "log_support_count": "support_count",
                "support_ratio": "support_ratio",
                "project_duration": "project_duration"}
# 개별 과제의 기간만 project_duration 으로 인정한다. 사업기간(프로그램 전체)은
# 모델 3 이 학습한 축이 아니다.
PROJECT_DURATION_BASES = {"지원기간", "과제기간", "수행기간", "협약기간", "개발기간"}


def _filled(v):
    return not (v is None or v is _pd.NA
                or (isinstance(v, float) and _pd.isna(v)))


def axis_validity(meta):
    """어댑터 출력 → 축별 유효성. meta 는 preconsultation_adapter.adapt() 결과."""
    feats = meta.get("features", {})
    dur = (meta.get("duration_evidence") or {}).get("project")
    out = {}
    for axis in NUM_AXES:
        src = _AXIS_SOURCE[axis]
        val = feats.get(src)
        if not _filled(val):
            reason = ("multiple_semantic_candidates"
                      if src == "support_count"
                      and meta.get("support_count_basis") == "multiple_semantic_candidates"
                      else "not_stated")
            out[axis] = {"valid": False, "reason": reason}
            continue
        if axis == "project_duration":
            # 무엇을 '유효' 로 볼지는 어댑터가 어느 정책으로 값을 골랐느냐에 달렸다.
            #
            #   training_rule        비교군 pool 과 **같은 규칙**으로 뽑은 값이다.
            #                        의미는 섞여 있지만 pool 도 똑같이 섞여 있어
            #                        거리 계산에서는 비교 가능하다 — valid.
            #   support_period_first 의미를 좁힌 값이라, 실제로 지원기간 계열에서
            #                        나왔을 때만 valid 로 본다.
            policy = meta.get("duration_policy")
            basis = meta.get("duration_basis") or (dur or {}).get("basis")
            if policy == "training_rule":
                out[axis] = {"valid": True, "basis": basis, "value": val,
                             "note": "training_rule_parity",
                             "semantic": (dur or {}).get("basis")}
                continue
            if basis not in PROJECT_DURATION_BASES:
                out[axis] = {"valid": False, "reason": "not_a_project_period",
                             "basis": basis, "value": val}
                continue
            out[axis] = {"valid": True, "basis": basis, "value": val,
                         "note": "verified_support_period"}
            continue
        out[axis] = {"valid": True, "value": val}
    return out


def confidence(validity):
    """축 개수 + 의미 유효성. 둘을 곱해 보지 않고 낮은 쪽을 따른다."""
    used = [a for a, v in validity.items() if v["valid"]]
    missing = [a for a, v in validity.items() if not v["valid"]]
    n = len(used)
    level = "high" if n >= 3 else "medium" if n == MIN_AXES else "low"
    return {
        "axes_used": used,
        "axes_missing": missing,
        "axis_validity": validity,
        "n_valid_axes": n,
        "min_axes": MIN_AXES,
        "scorable": n >= MIN_AXES,
        "confidence": level,
        "status": ("%d축 근거 참고" % n) if n >= MIN_AXES else "근거 축 부족",
    }


def score_document(meta):
    """어댑터 출력으로 채점하고 축 유효성을 함께 돌려준다.

    유효 축이 MIN_AXES 에 못 미치면 **채점하지 않는다** — 숫자를 내는 것보다
    근거가 부족하다고 말하는 편이 정확하다.
    """
    validity = axis_validity(meta)
    conf = confidence(validity)
    if not conf["scorable"]:
        return {"scored": False, "result": None, **conf}
    # None 을 그대로 넘기면 object 컬럼이 되어 prepare() 의 np.log10 이 깨진다.
    # 결측은 NaN 이어야 한다 — 원본 prepare 는 NaN 을 축 결측으로 세도록 짜여 있다.
    rec = dict(meta.get("features", {}))
    for c in ("per_recipient", "support_count", "project_duration", "support_ratio"):
        v = rec.get(c)
        rec[c] = float("nan") if not _filled(v) else float(v)
    res = predict([rec])                                    # noqa: F821 (_impl)
    return {"scored": bool(len(res)),
            "result": (res.to_dict("records")[0] if len(res) else None),
            **conf}
