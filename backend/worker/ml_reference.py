"""Slice 5 L1: 프로파일·CPL → ML 참고정보 경계 (초안 §8, §9.4).

**ML 은 CPL·FIT·SIM 판정 입력이 아니며, 예측값으로 원문의 누락을 채우지
않는다.** 모델 1 은 검색 필터로도 쓰지 않는다. 여기서 나오는 것은 요청서
판정과 별도로 붙는 참고정보 세 줄뿐이다 (초안 §8).

이 파일이 하는 일은 네 가지다.

1. 입력 매핑. 모델이 학습한 스키마 자리에 **검증된 원문·정량값만** 놓는다.
   모델 2 는 원문 ``evidence_text`` 가 필요하므로 구조화 요약문으로 임의
   대체하지 않는다. 원문이 없으면 그 모델은 ``INPUT_EVIDENCE_MISSING`` 이다.
2. 격리 실행. 세 모델은 서로 독립이고, 하나가 터지거나 없어도 나머지는 그대로
   돈다. ``run_ml_reference`` 는 예외를 밖으로 던지지 않는다 (초안 §9.4).
3. 상태 전달. 모델 1 의 결과를 모델 2·3 입력에 쓸 때 상태·버전을 함께 넘기고,
   ``판단보류`` 는 임의 유형으로 승격하지 않는다 (초안 §8).
4. 숫자 차단. 확률·신뢰도·퍼센타일은 ``internal`` 에만 남고 사용자 표면
   (``reference_text``)에는 숫자가 실리지 않는다.

실제 모델 어댑터는 L2 다. 여기에는 포트(``MlModel``)와 결정적인 가짜만 있고,
``torch`` · ``joblib`` · ``pandas`` 같은 추론 런타임을 import 하지 않는다.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from app.schemas.cpl import CplFieldCode

from .analysis_inputs import facts_at
from .contracts.cpl_result import CplResult
from .contracts.ml_result import (
    INPUT_EVIDENCE_MISSING,
    MODEL_INVALID_RESPONSE,
    ML_RUNTIME_MISSING,
    MODEL_ARTIFACT_MISSING,
    MODEL_EXECUTION_FAILED,
    PREDICTION_WITHHELD,
    MlModelId,
    MlModelResult,
    MlReferenceResult,
    StageDiagnostic,
)
from .contracts.profile_snapshot import CommonIrArtifact

__all__ = [
    "ML_BOUNDARY_VERSION",
    "WITHHELD_STATUS",
    "MlModel",
    "MlModelInput",
    "MlUnavailable",
    "UnavailableModel",
    "FakeMlModel",
    "MODEL_INPUT_SOURCES",
    "QUANTITY_SOURCES",
    "MODEL_1_CPL_SECTIONS",
    "missing_artifact_model",
    "missing_runtime_model",
    "load_entry_module",
    "build_ml_inputs",
    "run_ml_reference",
]

_STAGE = "run_ml_reference"

ML_BOUNDARY_VERSION = "ml-reference-v0.1"

# 모델 1 이 스스로 예측을 보류할 때 쓰는 상태 문자열. 임계값은 모델 쪽 계약이라
# 여기서 다시 정하지 않는다 — 상태 문자열만 읽는다.
WITHHELD_STATUS = "판단보류"



# ----------------------------------------------------------------- 입력 매핑
#
# 매핑은 코드가 아니라 표다 (analysis_inputs.CPL_FIELD_SOURCES 와 같은 규약).
# 여기 없는 연결은 존재하지 않는 연결이고, 표에 있는데 문서에 없는 값은 "값
# 없음" 으로 남는다. 다른 필드에서 끌어와 메우지 않는다.
#
# 모델 1 은 학습 때 제목·목적·내용·대상 네 조각을 이어 붙인 문자열 하나로
# 배웠다. **지원규모는 일부러 뺐다** — 모델 2 의 타깃 원천이라 모델 1 입력에
# 넣으면 누수다.
MODEL_1_CPL_SECTIONS: dict[str, CplFieldCode] = {
    "purpose": CplFieldCode.PURPOSE_GOAL,
    "content": CplFieldCode.NEW_OR_CHANGED_CONTENT,
    "target_text": CplFieldCode.TARGET_AND_CONDITIONS,
}

# 모델 2·3 이 공통으로 보는 설계 정량 축의 출처 경로. 여기서 숫자를 파싱하지
# 않는다 — 검증된 원문 인용을 그대로 실어 보내고, 학습 스키마에 맞춘 정규화는
# L2 어댑터가 한다. L1 이 정규식으로 "20명" 을 20 으로 바꾸기 시작하면 그 값이
# 근거 있는 값인지 만들어낸 값인지 호출부가 구별할 수 없다.
QUANTITY_SOURCES: tuple[str, ...] = (
    "comparison_profile.support_scale",
    "comparison_profile.cost_sharing",
    "comparison_profile.support_period",
    "comparison_profile.total_budget",
)

MODEL_INPUT_SOURCES: dict[MlModelId, tuple[str, ...]] = {
    MlModelId.MODEL_1_SUPPORT_TYPE: tuple(
        f"cpl:{code.value}" for code in MODEL_1_CPL_SECTIONS.values()
    ),
    # 모델 2 만 원문을 요구한다 (초안 §8).
    MlModelId.MODEL_2_AMOUNT: ("common_ir:evidence_text", *QUANTITY_SOURCES),
    MlModelId.MODEL_3_ANOMALY: QUANTITY_SOURCES,
}


@dataclass(frozen=True, slots=True)
class MlModelInput:
    """모델 하나에 넘길 입력과 그 출처.

    ``reason_code`` 가 있으면 이 입력으로는 모델을 돌리지 않는다. 부족한 칸을
    다른 값으로 채워 억지로 성립시키지 않는다 (초안 §8).
    """

    model_id: MlModelId
    payload: dict[str, Any]
    sources: list[str] = field(default_factory=list)
    reason_code: str | None = None
    # 팀원 입력 스키마를 오염시키지 않으려고 payload 밖에 둔다. predict() 에는
    # 계약이 기대하는 필드만 간다.
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_model_1(self, label: str, status: str, version: str | None) -> "MlModelInput":
        """모델 1 결과를 비교군 입력에 얹는다. **상태·버전을 함께** 넘긴다."""

        payload = {
            **self.payload,
            "support_type": label,
            "support_type_status": status,
            "support_type_model_version": version,
        }
        return MlModelInput(
            model_id=self.model_id,
            payload=payload,
            sources=[*self.sources, f"{MlModelId.MODEL_1_SUPPORT_TYPE.value}:{status}"],
            reason_code=self.reason_code,
        )


@runtime_checkable
class MlModel(Protocol):
    """모델 하나의 포트. 실제 어댑터는 L2 에서 이 모양으로 들어온다."""

    model_id: MlModelId
    artifact_version: str | None

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]: ...


class MlUnavailable(Exception):
    """"돌릴 수 없다" 를 "돌리다 터졌다" 와 구분하는 예외.

    가중치가 없거나 런타임이 없는 것은 버그가 아니라 상태다. 일반 예외로
    올리면 ``MODEL_EXECUTION_FAILED`` 로 뭉개져 둘을 구분할 수 없다.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class UnavailableModel:
    """돌릴 수 없는 모델의 자리. 호출하면 이유와 찾던 경로를 들고 올라온다."""

    model_id: MlModelId
    reason_code: str
    detail: str
    artifact_version: str | None = None

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        raise MlUnavailable(self.reason_code, self.detail)


def missing_artifact_model(
    model_id: MlModelId, artifact_path: str | Path
) -> UnavailableModel:
    """가중치·비교군 표가 없는 모델. 모델 1 이 지금 이 상태다.

    **찾던 경로를 그대로 들고 다닌다.** "없다" 만 남기면 어디에 무엇을 두어야
    하는지 알 수 없다.
    """

    return UnavailableModel(
        model_id=model_id,
        reason_code=MODEL_ARTIFACT_MISSING,
        detail=str(artifact_path),
    )


def missing_runtime_model(model_id: MlModelId, package: str) -> UnavailableModel:
    """추론 런타임이 설치돼 있지 않은 모델."""

    return UnavailableModel(
        model_id=model_id,
        reason_code=ML_RUNTIME_MISSING,
        detail=package,
    )


class FakeMlModel:
    """결정적인 가짜. 같은 입력에 항상 같은 출력을 낸다.

    L1 에는 실제 어댑터가 없으므로 경계의 동작(격리·상태 전달·숫자 차단)은 이
    가짜로 고정한다. 넘어온 입력을 ``calls`` 에 남겨서, 호출부가 넣지 않기로 한
    값(보류된 지원유형 등)이 새어 들어갔는지 볼 수 있게 한다.
    """

    def __init__(
        self,
        model_id: MlModelId,
        output: dict[str, Any] | None = None,
        *,
        artifact_version: str | None = "fake-artifact",
        raises: BaseException | None = None,
    ) -> None:
        self.model_id = model_id
        self.artifact_version = artifact_version
        self.output = output or {}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(inputs))
        if self.raises is not None:
            raise self.raises
        return dict(self.output)


def load_entry_module(name: str, path: str | Path):
    """진입점 파일을 **고유 이름으로** 불러온다. ``import inference`` 를 쓰지 않는다.

    model1·model2·model3 폴더에 ``inference.py`` 가 각각 있다. 이름으로 부르면
    ``sys.modules["inference"]`` 캐시 때문에 두 번째부터는 다른 모델의 구현이
    잡힌다 (실제로 API 스모크에서 모델 1 자리에 모델 3 이 불렸다). 그래서 파일
    경로로 모듈을 만들고 모델별 고유 이름으로 등록한다.

    실행 도중 터지면 캐시에서 지운다. 반쯤 초기화된 모듈이 남아 있으면 다음
    호출이 그것을 성공한 모듈로 착각한다.
    """

    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise MlUnavailable(ML_RUNTIME_MISSING, f"진입점을 읽지 못했다: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# ------------------------------------------------------------------ 입력 생성


def _cpl_section(
    cpl_result: CplResult | None, code: CplFieldCode
) -> tuple[str, list[str]]:
    """CPL 항목 하나의 검증된 원문 인용과 그 fact id.

    값을 만들어내지 않는다. 항목이 비어 있으면 빈 문자열이고, 그 사실은
    ``sources`` 가 비는 것으로 드러난다.
    """

    if cpl_result is None:
        return "", []
    item = next((row for row in cpl_result.items if row.field_code is code), None)
    if item is None:
        return "", []
    quotes: list[str] = []
    sources: list[str] = []
    for subfield in item.subfields:
        for fact in subfield.facts:
            if fact.value_raw:
                quotes.append(fact.value_raw)
                sources.append(fact.fact_id or f"{subfield.profile_field}:{len(sources)}")
    return "\n".join(quotes), sources


def _evidence_text(
    cpl_result: CplResult | None, common_ir: CommonIrArtifact | None
) -> tuple[str | None, list[str]]:
    """모델 2 가 요구하는 **원문**. Common IR 블록의 텍스트만 쓴다.

    구조화 요약문을 원문 자리에 넣지 않는다 (초안 §8). 그래서 CPL 근거가
    가리키는 블록 id 를 Common IR 문서에서 찾아 그 블록의 원문만 모은다.
    Common IR 이 없거나 접지된 블록이 하나도 없으면 ``None`` 이고, 호출부는 그
    모델을 ``INPUT_EVIDENCE_MISSING`` 으로 남긴다.
    """

    if cpl_result is None or common_ir is None:
        return None, []
    grounded: set[str] = set()
    for item in cpl_result.items:
        for subfield in item.subfields:
            for fact in subfield.facts:
                for evidence in fact.evidence:
                    block_id = evidence.common_ir_block_id or evidence.source_block_id
                    if block_id:
                        grounded.add(block_id)
                if fact.source_block_id:
                    grounded.add(fact.source_block_id)
    blocks = common_ir.document.get("blocks")
    if not isinstance(blocks, list) or not grounded:
        return None, []
    # 문서의 reading order 를 그대로 따른다. 근거 순서로 다시 늘어놓으면 원문에
    # 없던 배열이 만들어진다.
    texts: list[str] = []
    used: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = block.get("block_id")
        text = block.get("text")
        if block_id in grounded and isinstance(text, str) and text.strip():
            texts.append(text)
            used.append(str(block_id))
    if not texts:
        return None, []
    return "\n".join(texts), used


def _quantities(profile: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    """설계 정량 축의 원문 인용. 숫자로 바꾸지 않는다."""

    values: dict[str, list[str]] = {}
    sources: list[str] = []
    for path in QUANTITY_SOURCES:
        quotes: list[str] = []
        for fact in facts_at(profile, path):
            if fact.value_raw:
                quotes.append(fact.value_raw)
                if fact.fact_id:
                    sources.append(fact.fact_id)
        values[path.rsplit(".", 1)[-1]] = quotes
    return values, sources


def build_ml_inputs(
    profile: dict[str, Any],
    cpl_result: CplResult | None = None,
    *,
    common_ir: CommonIrArtifact | None = None,
    title: str | None = None,
) -> dict[MlModelId, MlModelInput]:
    """세 모델의 입력을 매핑표대로 만든다. 없는 값은 없는 채로 둔다.

    ``common_ir`` 는 모델 2 의 원문 출처다. 프로파일에는 인용문과 블록 좌표만
    있고 원문 전체가 없어서, 원문을 요구하는 모델에는 Common IR 문서가 함께
    있어야 한다. 없으면 그 모델만 ``INPUT_EVIDENCE_MISSING`` 이 된다.
    """

    sections = {
        name: _cpl_section(cpl_result, code)
        for name, code in MODEL_1_CPL_SECTIONS.items()
    }
    model_1_sources = [source for _, ids in sections.values() for source in ids]
    # 팀원 input_builder.validate_model1_fields 는 "전부 비었을 때만" 예외를
    # 던지고, 일부만 있으면 missing_fields 를 돌려주며 실행을 허용한다.
    # 그 계약을 그대로 따르되, 부분 입력으로 돌았다는 사실이 조용히 묻히지
    # 않도록 빠진 항목을 payload 와 진단에 남긴다.
    # 팀원 계약의 4 개 필드 전체가 기준이다: title, purpose, content, target_text.
    # title 은 CPL 이 아니라 호출자가 준다. 이걸 빼고 세면 제목만 있는 유효
    # 입력이 잘못 차단된다.
    model_1_payload = {
        "title": title or "",
        **{name: text for name, (text, _) in sections.items()},
    }
    model_1_missing = sorted(
        name for name in MODEL_1_FIELDS if not (model_1_payload.get(name) or "").strip()
    )
    model_1 = MlModelInput(
        model_id=MlModelId.MODEL_1_SUPPORT_TYPE,
        payload=model_1_payload,
        sources=model_1_sources,
        # 전부 비었을 때만 막는다 (팀원 validate_model1_fields 와 같은 조건).
        reason_code=(
            INPUT_EVIDENCE_MISSING if len(model_1_missing) == len(MODEL_1_FIELDS) else None
        ),
        metadata={"missing_fields": model_1_missing},
    )

    quantities, quantity_sources = _quantities(profile)
    evidence_text, evidence_sources = _evidence_text(cpl_result, common_ir)
    model_2 = MlModelInput(
        model_id=MlModelId.MODEL_2_AMOUNT,
        payload={
            "title": title or "",
            "evidence_text": evidence_text,
            "quantities": quantities,
        },
        sources=[*evidence_sources, *quantity_sources],
        # 원문이 없으면 여기서 멈춘다. 구조화 요약문으로 바꿔 넣지 않는다.
        reason_code=None if evidence_text else INPUT_EVIDENCE_MISSING,
    )

    model_3 = MlModelInput(
        model_id=MlModelId.MODEL_3_ANOMALY,
        payload={"quantities": quantities},
        sources=list(quantity_sources),
        reason_code=None if quantity_sources else INPUT_EVIDENCE_MISSING,
    )

    return {
        MlModelId.MODEL_1_SUPPORT_TYPE: model_1,
        MlModelId.MODEL_2_AMOUNT: model_2,
        MlModelId.MODEL_3_ANOMALY: model_3,
    }


# --------------------------------------------------------------- 결과 정규화

# 모델 출력에서 **내부에만** 남길 키. 여기 있는 값은 사용자 표면으로 나가지
# 않는다 (초안 §8).
_INTERNAL_KEYS: dict[MlModelId, tuple[str, ...]] = {
    MlModelId.MODEL_1_SUPPORT_TYPE: ("confidence", "status", "support_type_pred"),
    MlModelId.MODEL_2_AMOUNT: (
        "pred_won",
        "pred_log10",
        "percentile_rank",
        "bucket",
        "bucket_proba",
        "input_completeness",
    ),
    MlModelId.MODEL_3_ANOMALY: ("anomaly_score", "percentile", "level", "n"),
}

# 모델이 문장을 주지 않았을 때의 기본 문구. 숫자가 없다.
# 팀원 input_builder.MODEL1_FIELDS 와 같은 순서·이름이다.
MODEL_1_FIELDS: tuple[str, ...] = ("title", "purpose", "content", "target_text")

# 팀원 label_mapping.json 의 19 개 클래스. 이 밖의 라벨은 모델 출력이 아니라
# 결함이므로 fallback 으로 덮지 않고 해당 모델을 실패시킨다.
MODEL_1_CLASSES: frozenset[str] = frozenset(
    {
        "SW·솔루션", "경진대회", "고용보조", "교육훈련", "기술·IP평가",
        "보증", "사업화", "상담", "설비", "성능인증",
        "수출물류", "수출통관", "연구개발", "융자", "창업보육",
        "컨설팅", "판로", "해외수주·실증", "해외인증",
    }
)

# 팀원 m13_m3_anomaly.AXIS_LABEL 의 4 축.
MODEL_3_ALLOWED_AXES: frozenset[str] = frozenset(
    {"기업(과제)당 지원한도", "지원 기업/과제 수", "지원비율", "사업기간"}
)

# 모델 3 문구는 팀원 m13_m3_anomaly.ALLOWED 밖으로 나가지 않는다.
# FORBIDDEN("잘못 설계됨", "지원규모 과다" 등) 은 절대 만들지 않는다.
MODEL_3_ALLOWED_LEVELS: tuple[str, ...] = (
    "과거 사업 패턴과 차이가 큼",
    "희귀한 설계 조합",
    "동일 유형 대비 비전형적",
    "확인 필요",
)

_FALLBACK_TEXT: dict[MlModelId, str] = {
    MlModelId.MODEL_1_SUPPORT_TYPE: "유사 사업의 지원유형 참고 분류를 함께 본다.",
    MlModelId.MODEL_2_AMOUNT: "유사 사업들의 지원규모 분포를 참고로 함께 본다.",
    MlModelId.MODEL_3_ANOMALY: "유사 사업 비교군과 견준 설계 특징을 참고로 함께 본다.",
}


# 화면에서 감추는 것은 확률·점수·백분위지 숫자 전체가 아니다.
# 초안 30 행: "ML 은 지원유형·예측 금액·이례성 설명을 제공하고 확률·점수는
# 숨긴다." 289 행: "원 단위 예측 금액. 비교군 백분위는 내부 결과로 보존."
# 즉 모델 2 의 예측 금액과 설명 속 도메인 숫자는 나가야 한다.
_WITHHELD_KEYS = frozenset(
    {
        "confidence",
        "probability",
        "proba",
        "score",
        "percentile",
        "percentile_rank",
        "cohort_percentile",
        "anomaly_score",
        "distance",
    }
)

def _amount_phrase(pred_won: Any) -> str | None:
    """원 단위 예측 금액을 문구로 만든다.

    초안 30·289 행이 표시하라고 한 값이라 숫자가 그대로 나간다. 확률·백분위와
    달리 이건 감추는 대상이 아니다.
    """

    # bool 은 int 의 하위형이라 True 가 1 원이 된다. 먼저 막는다.
    if isinstance(pred_won, bool):
        return None
    try:
        value = float(pred_won)
    except (TypeError, ValueError):
        return None
    # NaN·무한대는 int() 에서 OverflowError/ValueError 로 터진다. 값으로 거른다.
    if not math.isfinite(value) or value <= 0:
        return None
    won = int(round(value))
    if won >= 100_000_000 and won % 100_000_000 == 0:
        return f"{won // 100_000_000}억원"
    if won >= 10_000 and won % 10_000 == 0:
        return f"{won // 10_000:,}만원"
    return f"{won:,}원"


class MlOutputInvalid(RuntimeError):
    """모델 출력이 계약을 어겼다. fallback 으로 덮지 않고 그 모델만 실패시킨다."""


def _validate_reference(model_id: MlModelId, output: dict[str, Any]) -> str:
    """모델 출력을 검증하고 사용자 문구를 **서버가 조립한다**.

    모델이 준 자유 문장(``statement``)은 절대 쓰지 않는다. 정규식으로 거르면
    "신뢰도 0.42", "점수 82점", "백분위 0.71" 이 계속 빠져나간다. 허용된
    구조값만 읽으면 그 경로 자체가 없어진다.

    유효한 구조값이 없으면 ``OK + fallback`` 이 아니라 ``MlOutputInvalid`` 다.
    잘못된 출력을 정상 참고정보로 위장하지 않는다.
    """

    if model_id is MlModelId.MODEL_1_SUPPORT_TYPE:
        label = output.get("support_type_pred")
        if not isinstance(label, str) or label.strip() not in MODEL_1_CLASSES:
            raise MlOutputInvalid(
                f"지원유형이 팀원 19 개 클래스 밖이다: {label!r}"
            )
        return f"유사 사업의 지원유형 참고 분류는 '{label.strip()}' 계열이다."

    if model_id is MlModelId.MODEL_2_AMOUNT:
        phrase = _amount_phrase(output.get("pred_won"))
        if phrase is None:
            raise MlOutputInvalid(
                f"예측 금액이 유한 양수가 아니다: {output.get('pred_won')!r}"
            )
        return f"비교군 기준 참고 예측 지원액은 {phrase} 수준이다."

    level = output.get("level")
    if not isinstance(level, str) or level.strip() not in MODEL_3_ALLOWED_LEVELS:
        # FORBIDDEN("지원규모 과다" 등) 을 포함해 허용 어휘 밖은 전부 여기서 막힌다.
        raise MlOutputInvalid(f"이례성 level 이 허용 어휘 밖이다: {level!r}")
    raw_axes = output.get("cause_axes")
    if raw_axes is None:
        axes: list[str] = []
    elif isinstance(raw_axes, list) and all(isinstance(a, str) for a in raw_axes):
        # 문자열 하나를 주면 문자 단위로 순회하므로 list[str] 을 강제한다.
        axes = [a.strip() for a in raw_axes if a.strip()]
    else:
        raise MlOutputInvalid(f"cause_axes 가 list[str] 이 아니다: {raw_axes!r}")
    unknown = [a for a in axes if a not in MODEL_3_ALLOWED_AXES]
    if unknown:
        raise MlOutputInvalid(f"허용 축 밖이다: {unknown}")
    if axes:
        return f"비교군 대비 {level.strip()} — 관련 축: {', '.join(axes)}."
    return f"비교군 대비 {level.strip()}."



def _failed(
    model_id: MlModelId, model_input: MlModelInput, artifact_version: str | None
) -> MlModelResult:
    return MlModelResult(
        model_id=model_id,
        status="FAILED",
        reason_code=MODEL_EXECUTION_FAILED,
        reference_text=None,
        input_sources=list(model_input.sources),
        artifact_version=artifact_version,
        producer_version=ML_BOUNDARY_VERSION,
    )


def _unavailable(
    model_id: MlModelId,
    reason_code: str,
    *,
    artifact_version: str | None = None,
) -> MlModelResult:
    return MlModelResult(
        model_id=model_id,
        status="UNAVAILABLE",
        reason_code=reason_code,
        reference_text=None,
        artifact_version=artifact_version,
        producer_version=ML_BOUNDARY_VERSION,
    )


def _normalise(
    model_id: MlModelId,
    output: dict[str, Any],
    model_input: MlModelInput,
    artifact_version: str | None,
) -> MlModelResult:
    """모델 출력 하나를 계약 모양으로 옮긴다.

    모델 1 이 ``판단보류`` 면 결과가 아니라 보류다. 신뢰도는 ``internal`` 에
    남기되 쓸 수 있는 유형은 만들지 않는다 (초안 §8).
    """

    # 모델별 큐레이션 키 + 확률·점수 계열 공통 키의 합집합이다. 허용목록에
    # 빠졌다는 이유만으로 민감값이 표면으로 새지 않게 한다.
    internal = {
        key: value
        for key, value in output.items()
        if key in _INTERNAL_KEYS[model_id] or key in _WITHHELD_KEYS
    }
    metadata = dict(model_input.metadata)
    if model_id is MlModelId.MODEL_1_SUPPORT_TYPE and output.get("status") == WITHHELD_STATUS:
        return MlModelResult(
            model_id=model_id,
            status="UNAVAILABLE",
            reason_code=PREDICTION_WITHHELD,
            reference_text=None,
            input_sources=list(model_input.sources),
            artifact_version=artifact_version,
            producer_version=ML_BOUNDARY_VERSION,
            internal=internal,
            input_metadata=metadata,
        )
    try:
        reference_text = _validate_reference(model_id, output)
    except Exception as error:
        # 검증기 자체가 터져도 이 모델만 실패한다. 잘못된 출력을 정상
        # 참고정보로 위장하지 않는다 (fallback 으로 덮지 않는다).
        return MlModelResult(
            model_id=model_id,
            status="FAILED",
            reason_code=MODEL_INVALID_RESPONSE,
            reference_text=None,
            input_sources=list(model_input.sources),
            artifact_version=artifact_version,
            producer_version=ML_BOUNDARY_VERSION,
            internal=internal,
            input_metadata=metadata,
        )
    return MlModelResult(
        model_id=model_id,
        status="OK",
        reason_code=None,
        reference_text=reference_text,
        input_sources=list(model_input.sources),
        artifact_version=artifact_version,
        producer_version=ML_BOUNDARY_VERSION,
        internal=internal,
        input_metadata=metadata,
    )


# -------------------------------------------------------------------- 실행


def _run_one(
    model_id: MlModelId,
    model: MlModel | None,
    model_input: MlModelInput,
    diagnostics: list[StageDiagnostic],
) -> MlModelResult:
    """모델 하나를 돌린다. **예외를 밖으로 내보내지 않는다** (초안 §9.4)."""

    artifact_version = getattr(model, "artifact_version", None)
    if model is None:
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=model_id.value,
                reason_code=ML_RUNTIME_MISSING,
                message="어댑터가 주입되지 않았다.",
            )
        )
        return _unavailable(model_id, ML_RUNTIME_MISSING)
    if model_input.reason_code is not None:
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=model_id.value,
                reason_code=model_input.reason_code,
                message="학습 스키마에 맞출 근거가 없어 호출하지 않았다.",
            )
        )
        return _unavailable(model_id, model_input.reason_code, artifact_version=artifact_version)
    try:
        output = model.predict(model_input.payload)
    except MlUnavailable as error:
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=model_id.value,
                reason_code=error.reason_code,
                # 찾던 경로·패키지 이름을 그대로 남긴다.
                message=error.detail,
            )
        )
        return _unavailable(model_id, error.reason_code, artifact_version=artifact_version)
    except Exception as error:  # noqa: BLE001 - 국소 실패로 가둔다
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=model_id.value,
                reason_code=MODEL_EXECUTION_FAILED,
                message=f"{type(error).__name__}: {error}"[:2000],
            )
        )
        return _failed(model_id, model_input, artifact_version)
    if not isinstance(output, dict):
        diagnostics.append(
            StageDiagnostic(
                stage=_STAGE,
                unit=model_id.value,
                reason_code=MODEL_EXECUTION_FAILED,
                message=f"출력이 dict 가 아니다: {type(output).__name__}",
            )
        )
        return _failed(model_id, model_input, artifact_version)
    return _normalise(model_id, output, model_input, artifact_version)


def _carry_support_type(
    first: MlModelResult, diagnostics: list[StageDiagnostic]
) -> tuple[str, str, str | None] | None:
    """모델 1 결과를 모델 2·3 비교군 입력으로 넘길지 정한다.

    보류·실패는 넘기지 않는다. 임의 유형으로 강행하면 그 뒤 두 결과가 "근거
    있는 비교군" 인지 "지어낸 비교군" 인지 구별되지 않는다 (초안 §8).
    """

    if first.status == "OK":
        label = first.internal.get("support_type_pred")
        if isinstance(label, str) and label:
            return label, str(first.internal.get("status") or "OK"), first.artifact_version
    # 진단은 그것이 설명하는 단위에 붙인다 (sim.py 와 같은 규약). 이 줄이
    # 설명하는 것은 모델 1 의 실패가 아니라 **모델 2·3 입력에 지원유형이 없는
    # 이유** 다. 모델 1 쪽 단위에 붙이면 그 모델의 진단과 섞인다.
    diagnostics.extend(
        StageDiagnostic(
            stage=_STAGE,
            unit=model_id.value,
            reason_code=first.reason_code,
            message="모델 1 이 지원유형을 확정하지 않아 비교군 입력에 넣지 않았다.",
        )
        for model_id in (MlModelId.MODEL_2_AMOUNT, MlModelId.MODEL_3_ANOMALY)
    )
    return None


def run_ml_reference(
    profile: dict[str, Any],
    models: Mapping[MlModelId, MlModel | None],
    *,
    cpl_result: CplResult | None = None,
    common_ir: CommonIrArtifact | None = None,
    title: str | None = None,
) -> MlReferenceResult:
    """세 모델을 서로 독립으로 돌린다. 결과는 항상 세 건이고 예외는 없다.

    하나가 없거나 터져도 나머지 둘은 그대로 돈다 (초안 §9.4 "ML 실패는 국소
    실패다. 정상 결과를 보존한 부분 보고서를 만든다").
    """

    inputs = build_ml_inputs(profile, cpl_result, common_ir=common_ir, title=title)
    diagnostics: list[StageDiagnostic] = []
    results: dict[MlModelId, MlModelResult] = {}

    first = MlModelId.MODEL_1_SUPPORT_TYPE
    results[first] = _run_one(first, models.get(first), inputs[first], diagnostics)
    carried = _carry_support_type(results[first], diagnostics)

    for model_id in (MlModelId.MODEL_2_AMOUNT, MlModelId.MODEL_3_ANOMALY):
        model_input = inputs[model_id]
        if carried is not None:
            model_input = model_input.with_model_1(*carried)
        results[model_id] = _run_one(model_id, models.get(model_id), model_input, diagnostics)

    return MlReferenceResult(
        results=[results[model_id] for model_id in MlModelId],
        diagnostics=diagnostics,
    )
