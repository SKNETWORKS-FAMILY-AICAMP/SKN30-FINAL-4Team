"""Slice 5: ML 참고정보 결과 계약 (초안 §8, §9.4).

원칙 네 줄.

1. **ML 은 요청서 판정과 별도의 참고정보다.** CPL·FIT·SIM 판정 입력이 아니고,
   검색 필터도 아니다 (초안 §8). 이 계약이 판정 계약(``cpl_result`` ·
   ``fit_result`` · ``sim_result``)과 섞이지 않는 별도 파일인 이유다.
2. **확률·점수는 사용자 표면에 없다.** 확률·신뢰도·퍼센타일은 ``internal``
   안에만 산다. 모델 2의 예측 지원금액은 계약상 사용자에게 표시한다. ``sim_result``
   의 ``InternalRanking`` 이 점수를 가둔 것과 같은 계약이다 (초안 §7.2, §8).
3. **실패는 모델 단위로 격리한다.** 세 모델은 서로 독립이고, 하나가 무너져도
   나머지 결과는 그대로 남는다 (초안 §9.4 "ML 실패는 국소 실패다").
4. **판정을 지어내지 않는다.** 모델이 예측을 보류했으면 임의 유형으로 강행하지
   않고 ``PREDICTION_WITHHELD`` 로 남긴다. 예측값으로 원문의 누락을 메우는
   자리는 이 계약에 없다 (초안 §8).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .profile_snapshot import StageDiagnostic

__all__ = [
    "StageDiagnostic",
    "MlModelId",
    "MlModelStatus",
    "MODEL_ARTIFACT_MISSING",
    "ML_RUNTIME_MISSING",
    "INPUT_EVIDENCE_MISSING",
    "MODEL_EXECUTION_FAILED",
    "MODEL_INVALID_RESPONSE",
    "PREDICTION_WITHHELD",
    "ML_REASON_CODES",
    "MlModelResult",
    "MlReferenceResult",
]


class MlModelId(StrEnum):
    """세 모델. **선언 순서가 결과 순서다** (초안 §8 의 모델 1·2·3 순서)."""

    MODEL_1_SUPPORT_TYPE = "model_1_support_type"
    MODEL_2_AMOUNT = "model_2_amount"
    MODEL_3_ANOMALY = "model_3_anomaly"


# 상태 세 값. ``UNAVAILABLE`` 은 "돌릴 수 없었다"(가중치·런타임·입력 부재,
# 예측 보류)이고 ``FAILED`` 는 "돌리다 터졌다" 다. 두 가지를 한 값으로 뭉치면
# 화면이 "아직 준비되지 않음" 과 "오류" 를 구분하지 못한다 (초안 §9.5).
MlModelStatus = Literal["OK", "UNAVAILABLE", "FAILED"]


# reason code. 값 자체가 API·로그에 실려 나가므로 StrEnum 대신 문자열 상수다
# (profile_snapshot.py · sim_result.py 와 같은 이유).
#
# 학습된 가중치·비교군 표 같은 artifact 가 없다. 모델 1 이 지금 이 상태다.
MODEL_ARTIFACT_MISSING = "MODEL_ARTIFACT_MISSING"
# 추론 런타임(torch·joblib·pandas 등)이 설치돼 있지 않다.
ML_RUNTIME_MISSING = "ML_RUNTIME_MISSING"
# 모델이 요구하는 원문·정량 근거가 없다. 구조화 요약문으로 대체하지 않는다.
INPUT_EVIDENCE_MISSING = "INPUT_EVIDENCE_MISSING"
# 입력도 artifact 도 있었는데 실행 중 터졌다.
MODEL_EXECUTION_FAILED = "MODEL_EXECUTION_FAILED"
# 모델이 돌긴 했는데 출력이 계약을 어겼다. 정상 참고정보로 위장하지 않는다.
MODEL_INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"
# 모델이 스스로 예측을 보류했다(모델 1 의 ``판단보류``). 임의 유형으로
# 승격하지 않는다 (초안 §8).
PREDICTION_WITHHELD = "PREDICTION_WITHHELD"

ML_REASON_CODES = frozenset(
    {
        MODEL_ARTIFACT_MISSING,
        ML_RUNTIME_MISSING,
        INPUT_EVIDENCE_MISSING,
        MODEL_EXECUTION_FAILED,
        MODEL_INVALID_RESPONSE,
        PREDICTION_WITHHELD,
    }
)


@dataclass(frozen=True, slots=True)
class MlModelResult:
    """모델 하나의 참고 결과.

    ``reference_text`` 는 서버가 조립한 사용자 표시 문구다. 확률·신뢰도·퍼센타일은
    전부 ``internal`` 이고, 모델 2의 예측 지원금액만 허용된 숫자다. 표시 계층이
    모델의 자유 문장을 집계하지 않도록 한다 (초안 §8).

    ``input_sources`` 는 이 입력이 어느 fact·필드·원문 블록에서 왔는지다.
    비어 있으면 "근거 없이 돌렸다" 가 아니라 "돌리지 못했다" 여야 한다.
    ``artifact_version`` · ``producer_version`` 은 모델 1 의 출력을 모델 2·3
    입력으로 넘길 때 상태와 함께 실어 나르는 값이기도 하다 (초안 §8).
    """

    model_id: MlModelId
    status: MlModelStatus
    reason_code: str | None
    reference_text: str | None = None
    input_sources: list[str] = field(default_factory=list)
    artifact_version: str | None = None
    producer_version: str | None = None
    # **내부 전용.** 확률·신뢰도·퍼센타일이 사는 유일한 자리다.
    internal: dict[str, Any] = field(default_factory=dict)
    # 부분 입력으로 돌았다는 사실이 결과까지 따라와야 한다. 팀원 입력 빌더가
    # 돌려주는 missing_fields 가 여기 실린다.
    input_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MlReferenceResult:
    """한 요청서에 대한 세 모델의 참고정보 묶음.

    ``results`` 는 항상 세 건이고 ``MlModelId`` 선언 순서다. 실패한 모델도
    자리를 비우지 않는다 — 결과가 없는 것과 모델이 없는 것은 다르다.
    """

    results: list[MlModelResult]
    diagnostics: list[StageDiagnostic] = field(default_factory=list)

    def result(self, model_id: MlModelId) -> MlModelResult:
        return next(row for row in self.results if row.model_id is model_id)
