"""Model 1 Runner — CPL 결과 → 지원성격 분류 Result JSON.

새로 만드는 것은 **입력 조립과 결과 포장뿐**이다. 토크나이저·가중치·라벨맵·
max_len·임계값은 전부 학습 산출물을 그대로 쓴다.

trust_grade 임계값을 새로 정하지 않는다
--------------------------------------
구현계획서는 "threshold 를 임의로 확정하지 말라" 고 했다. 맞는 원칙인데,
**이 프로젝트에는 이미 확정된 임계값이 있다.** M09 에서 정해 학습·평가에
그대로 쓴 값이고 `dl07_m1_apply.py` 에 상수로 박혀 있다.

    HOLD_THRESHOLD  = 0.20      미만이면 판단보류
    TRUST_THRESHOLD = 0.35      이상이면 신뢰

그래서 새 숫자를 고르는 대신 그 함수(`tier()`)를 불러 **이름만** 계획서의
enum 으로 옮긴다. 여기서 다른 값을 쓰면 학습·평가 때의 판단보류 비율과
서빙의 비율이 달라져 공표 성능이 의미를 잃는다.

    신뢰    -> trusted
    참고용  -> reference
    판단보류 -> hold
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.abspath(os.path.join(_HERE, "..", "shared"))
for _p in (_HERE, _SHARED):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import input_builder as IB                     # noqa: E402
import result_envelope as RE                   # noqa: E402

MODEL_NAME = "support_type_classifier"
MODEL_VERSION = "1.0"
MODEL_TYPE = "KLUE-BERT"

# tier() 의 한글 라벨 -> 계획서 enum. 임계값 자체는 dl07 것을 그대로 쓴다.
TRUST_GRADE = {"신뢰": "trusted", "참고용": "reference", "판단보류": "hold"}


def _impl():
    """model1/predict.py 를 파일 경로로 불러온다 — inference.py 이름 충돌 회피."""
    import importlib.util
    name = "model1_predict_entry"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_HERE, "predict.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_trust_grade(confidence):
    """확신도 -> trusted / reference / hold.

    임계값은 dl07 의 `tier()` 를 그대로 통과시킨다. 이 함수는 이름만 바꾼다.
    """
    m = _impl()
    return TRUST_GRADE[m.tier(float(confidence))]


def thresholds():
    """지금 적용 중인 임계값. 결과 metadata 에 실어 어디서 온 값인지 남긴다."""
    m = _impl()
    return {"hold_below": m.HOLD_THRESHOLD,
            "trusted_at_or_above": m.TRUST_THRESHOLD,
            "source": "dl07_m1_apply.py (M09 확정)"}


def run_model_1(analysis_id, cpl_result, title=None, include_scale_field=False,
                already_cleaned=True):
    """CPL 결과 → Model 1 Result JSON.

    already_cleaned 기본값이 True 인 이유: 학습 입력 `text_for_model` 은 F03 이
    파싱된 섹션에서 조립한 문자열이라 `clean_text()` 를 거치지 않았다. CPL 에서
    꺼낸 텍스트도 같은 성격(이미 구조화된 발췌)이고, `clean_text()` 는 900자
    예산으로 잘라내므로 여기서 다시 적용하면 학습 때보다 더 짧아진다. 원문
    공고 전문을 넣는 경우에만 False 로 둔다.
    """
    try:
        built = IB.build_model1_input(cpl_result, title=title,
                                      include_scale_field=include_scale_field)
    except (IB.Model1InputError, TypeError) as e:
        return RE.failed(analysis_id, MODEL_NAME, MODEL_VERSION, MODEL_TYPE,
                         code="MODEL1_INPUT_INVALID", message=str(e))

    try:
        m = _impl()
        out = m.predict([built["text"]], already_cleaned=already_cleaned)[0]
    except Exception as e:                                  # noqa: BLE001
        return RE.failed(analysis_id, MODEL_NAME, MODEL_VERSION, MODEL_TYPE,
                         code="MODEL1_INFERENCE_FAILED",
                         message="%s: %s" % (type(e).__name__, e),
                         input={"char_len": built["char_len"]})

    conf = float(out["confidence"])
    return RE.success(
        analysis_id, MODEL_NAME, MODEL_VERSION, MODEL_TYPE,
        result={
            "support_type": out["support_type_pred"],
            "confidence": round(conf, 4),
            "trust_grade": TRUST_GRADE[out["status"]],
        },
        input={
            "char_len": built["char_len"],
            "missing_fields": built["missing_fields"],
            "source_fields": built["source_fields"],
            "evidence_block_ids": built["evidence_block_ids"],
        },
        metadata={
            "max_length": m.MAX_LEN,
            "n_classes": len(m._classes) if getattr(m, "_classes", None) else None,
            "thresholds": thresholds(),
            "already_cleaned": already_cleaned,
            "input_rule": "title+purpose+content+target_text (f03_taxonomy.py:158)",
        },
    )
