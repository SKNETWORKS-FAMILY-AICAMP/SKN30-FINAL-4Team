"""ML 모델 결과의 **출처 정보**.

`model_result.py` 에서 이 부분만 남겼다. 나머지(envelope ↔ `result.model_result`
행 변환)는 Supabase PoC 스키마 전용이라 없앴다 — 지금 챗봇은 FastAPI 가 저장한
`report_json` 을 그대로 읽는다.

왜 metadata 중에서 이 아홉 개만 프롬프트로 올리는가
------------------------------------------------
나머지 metadata 는 운영 정보라 올리면 LLM 이 그것까지 설명하려 든다(모델 버전,
bucket 확률 같은 것). 이 아홉은 예외다. **숫자가 어떤 비교군에서 나왔는지**를
말해 주는 값이라, 없으면 챗봇이 "같은 연구개발 사업 중 75.2 백분위" 처럼 실제로
쓰이지 않은 비교군을 지어낸다.

실측: 연구개발|grant|project|taxonomy 비교군이 15건뿐이라 최소 표본(30)에 못
미쳐 `단위x출처` 단계로 물러났다. 그 단계는 support_type 을 키에 쓰지 않는다.
그런데 백분위 숫자는 정상으로 보여서, 비교군을 잘못 말해도 아무도 모른다.
값 자체보다 이 provenance 가 문장의 정확도를 좌우한다.
"""
from typing import Any

PROVENANCE_KEYS: tuple[str, ...] = (
    "support_type_compatibility",     # Model 2 가 그 label 을 학습했는가
    "unseen_support_type",
    "prediction_scope_warning",
    "percentile_cohort_level",        # 실제로 어느 단계에서 나온 백분위인가
    "percentile_uses_support_type",   # 그 단계가 support_type 을 썼는가
    "percentile_caveat",
    "top1_axis",                      # '가장 크게 벗어난 축' — 원인이 아니다
    "minimum_required_axes",
    "distance_metric",
)

# ---------------------------------------------------------------- 비노출 정책
#
# 사용자 표면에 나가면 안 되는 내부 진단값. **Context 를 만드는 단계에서 지운다** —
# 프롬프트로만 막으면 값은 이미 프롬프트에 실려 있고, 모델이 규칙을 어기는 순간
# 그대로 새어 나간다. Context 에 없으면 새어 나갈 값 자체가 없다.
#
# 키 **이름에 포함되면** 지운다. `support_type_confidence`,
# `percentile_cohort_level`, `class_probabilities` 처럼 접두·접미가 붙어 나오기
# 때문에 완전일치로는 못 잡는다.
#
# 공개 가능한 것은 따로 있다.
#   Model 1  지원유형 결과
#   Model 2  예측 지원금액          — 금액은 확률·점수가 아니라 결과값이다
#   Model 3  공개용 이례성 결과·설명
INTERNAL_VALUE_KEYS: tuple[str, ...] = (
    "percentile",
    "confidence",
    "probability",
    "probabilities",
    "raw_score",
    "raw_scores",
    # 유사도 점수. 결과 화면도 쓰지 않는 값이라(ReportSimCandidateDisplay)
    # 챗봇만 숫자를 말하면 화면과 답변이 어긋난다. 순위(rank)는 점수가 아니라
    # 순서라 남긴다.
    "semantic_similarity",
)


def is_internal_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in INTERNAL_VALUE_KEYS)


def strip_internal_values(value: Any) -> Any:
    """중첩 구조를 따라 내려가며 내부 진단값을 지운다.

    ML 섹션만이 아니라 Context 전체에 건다. 같은 이름의 값이 CPL·FIT·SIM 쪽에
    섞여 들어와도 한 곳에서 막힌다 — 어느 Agent 가 무엇을 담는지는 저장 구조가
    바뀔 때마다 달라지는데, 이 정책은 그것과 무관하게 지켜져야 한다.
    """
    if isinstance(value, dict):
        return {
            key: strip_internal_values(nested)
            for key, nested in value.items()
            if not is_internal_key(key)
        }
    if isinstance(value, list):
        return [strip_internal_values(nested) for nested in value]
    return value


def provenance_of(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """metadata 에서 provenance 만 골라낸다. 없으면 빈 dict."""
    if not isinstance(metadata, dict):
        return {}
    return {key: metadata[key] for key in PROVENANCE_KEYS if key in metadata}
