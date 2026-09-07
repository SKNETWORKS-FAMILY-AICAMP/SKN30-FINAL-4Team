"""Slice 5 L1: ML 참고정보 경계 테스트. 네트워크·DB·추론 런타임을 타지 않는다.

모델 어댑터는 L2 다. 여기서 고정하는 것은 경계의 성질 여섯 가지다.

1. 결과는 항상 세 건이고 ``MlModelId`` 선언 순서다.
2. artifact 가 없는 모델은 **찾던 경로와 함께** 돌릴 수 없음으로 남고, 나머지
   두 모델은 같은 실행에서 그대로 결과를 낸다 (초안 §9.4 국소 실패).
3. 모델 하나가 아무 예외나 던져도 나머지 둘은 멀쩡하고, 진입 함수는 예외를
   밖으로 내보내지 않는다.
4. 원문이 없으면 모델 2 는 ``INPUT_EVIDENCE_MISSING`` 이다. 구조화 요약문을
   원문 자리에 **대신 넣지 않는다** (초안 §8).
5. 모델 1 의 ``판단보류`` 는 모델 2·3 입력으로 넘어가지 않는다.
6. 확률·신뢰도·퍼센타일은 ``internal`` 밖으로 새지 않는다 (초안 §8).

여기에 더해 실제로 물렸던 결함 하나를 회귀로 고정한다: model1/2/3 폴더에
``inference.py`` 가 각각 있어 이름으로 import 하면 ``sys.modules["inference"]``
캐시 때문에 다른 모델 구현이 잡힌다 (팀 docstring: "API 스모크에서 모델 1
자리에 모델 3 이 불렸다").
"""

from dataclasses import asdict
import json
from pathlib import Path
import sys

import pytest

from worker.contracts.ml_result import (
    INPUT_EVIDENCE_MISSING,
    MODEL_INVALID_RESPONSE,
    MODEL_ARTIFACT_MISSING,
    MODEL_EXECUTION_FAILED,
    PREDICTION_WITHHELD,
    MlModelId,
)
from worker.contracts.profile_snapshot import CommonIrArtifact
from worker.cpl import build_cpl_result
from worker.ml_reference import (
    MODEL_1_FIELDS,
    WITHHELD_STATUS,
    FakeMlModel,
    MlOutputInvalid,
    _validate_reference,
    build_ml_inputs,
    load_entry_module,
    missing_artifact_model,
    run_ml_reference,
)


_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "profile_structuring"
    / "examples"
    / "request"
    / "structured_profile_v012.json"
)

# 원문 한 줄. 구조화 인용문과 글자가 겹치지 않아야 "요약문으로 대체하지
# 않았다" 를 확인할 수 있다.
_SOURCE_TEXT = "본 사업은 도내 중소기업의 판로개척을 지원한다."


@pytest.fixture(scope="module")
def profile() -> dict:
    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cpl_result(profile):
    return build_cpl_result(profile)


@pytest.fixture(scope="module")
def common_ir(cpl_result) -> CommonIrArtifact:
    """CPL 근거가 가리키는 블록 하나만 담은 Common IR 문서."""

    block_id = next(
        fact.source_block_id
        for item in cpl_result.items
        for subfield in item.subfields
        for fact in subfield.facts
        if fact.source_block_id
    )
    return CommonIrArtifact(
        run_dir="",
        notice_id="PREREVIEW-TEST",
        source_kind="markdown_fixture",
        source_path="",
        source_sha256="",
        common_ir_path="",
        common_ir_document_id="request:PREREVIEW-TEST",
        manifest={},
        block_count=1,
        document={"blocks": [{"block_id": block_id, "text": _SOURCE_TEXT}]},
    )


def _fakes(**overrides) -> dict:
    """세 모델 다 정상인 기본 구성. 필요한 자리만 갈아 끼운다."""

    models = {
        MlModelId.MODEL_1_SUPPORT_TYPE: FakeMlModel(
            MlModelId.MODEL_1_SUPPORT_TYPE,
            {"support_type_pred": "판로", "confidence": 0.61, "status": "신뢰"},
        ),
        MlModelId.MODEL_2_AMOUNT: FakeMlModel(
            MlModelId.MODEL_2_AMOUNT,
            {"pred_won": 200_000_000, "percentile_rank": 0.72, "bucket": "Mid"},
        ),
        MlModelId.MODEL_3_ANOMALY: FakeMlModel(
            MlModelId.MODEL_3_ANOMALY,
            {"anomaly_score": 1.4, "level": "희귀한 설계 조합",
             "cause_axes": ["지원비율", "사업기간"]},
        ),
    }
    models.update(overrides)
    return models


def _run(profile, cpl_result, common_ir, models):
    return run_ml_reference(
        profile,
        models,
        cpl_result=cpl_result,
        common_ir=common_ir,
        title="사전협의서 시험 문서",
    )


def test_three_results_always_in_declared_order(profile, cpl_result, common_ir):
    result = _run(profile, cpl_result, common_ir, _fakes())

    assert [row.model_id for row in result.results] == list(MlModelId)
    assert all(row.status == "OK" for row in result.results)
    # 참고 문구는 사람이 읽는 한 줄이지 판정이 아니다.
    assert "판로" in result.result(MlModelId.MODEL_1_SUPPORT_TYPE).reference_text


def test_missing_artifact_reports_the_path_and_spares_the_others(
    profile, cpl_result, common_ir
):
    """모델 1 의 오늘 상태. 가중치가 없다 — 그래도 2·3 은 돈다."""

    looked_for = "ml/serving/model1/model/model.safetensors"
    result = _run(
        profile,
        cpl_result,
        common_ir,
        _fakes(
            **{
                MlModelId.MODEL_1_SUPPORT_TYPE: missing_artifact_model(
                    MlModelId.MODEL_1_SUPPORT_TYPE, looked_for
                )
            }
        ),
    )

    first = result.result(MlModelId.MODEL_1_SUPPORT_TYPE)
    assert (first.status, first.reason_code) == ("UNAVAILABLE", MODEL_ARTIFACT_MISSING)
    # "없다" 만으로는 어디에 무엇을 두어야 하는지 알 수 없다.
    messages = [
        row.message
        for row in result.diagnostics
        if row.unit == MlModelId.MODEL_1_SUPPORT_TYPE.value
        and row.reason_code == MODEL_ARTIFACT_MISSING
    ]
    assert messages == [looked_for]

    for model_id in (MlModelId.MODEL_2_AMOUNT, MlModelId.MODEL_3_ANOMALY):
        assert result.result(model_id).status == "OK"


def test_one_model_raising_stays_local(profile, cpl_result, common_ir):
    boom = FakeMlModel(MlModelId.MODEL_2_AMOUNT, raises=RuntimeError("번들이 깨졌다"))
    result = _run(profile, cpl_result, common_ir, _fakes(**{MlModelId.MODEL_2_AMOUNT: boom}))

    second = result.result(MlModelId.MODEL_2_AMOUNT)
    assert (second.status, second.reason_code) == ("FAILED", MODEL_EXECUTION_FAILED)
    assert second.reference_text is None
    assert result.result(MlModelId.MODEL_1_SUPPORT_TYPE).status == "OK"
    assert result.result(MlModelId.MODEL_3_ANOMALY).status == "OK"
    assert any(
        row.reason_code == MODEL_EXECUTION_FAILED and "RuntimeError" in row.message
        for row in result.diagnostics
    )


def test_missing_source_text_is_not_replaced_by_a_structured_summary(
    profile, cpl_result
):
    """Common IR 이 없으면 모델 2 는 돌지 않는다 (초안 §8)."""

    inputs = build_ml_inputs(profile, cpl_result, common_ir=None)
    payload = inputs[MlModelId.MODEL_2_AMOUNT].payload

    assert inputs[MlModelId.MODEL_2_AMOUNT].reason_code == INPUT_EVIDENCE_MISSING
    assert payload["evidence_text"] is None
    # 구조화 요약문(=모델 1 에 넘긴 CPL 인용 묶음)이 원문 자리로 새지 않았다.
    summary = inputs[MlModelId.MODEL_1_SUPPORT_TYPE].payload["target_text"]
    assert summary
    assert summary not in json.dumps(payload, ensure_ascii=False)

    models = _fakes()
    result = run_ml_reference(profile, models, cpl_result=cpl_result, common_ir=None)
    second = result.result(MlModelId.MODEL_2_AMOUNT)
    assert (second.status, second.reason_code) == ("UNAVAILABLE", INPUT_EVIDENCE_MISSING)
    # 호출 자체가 없다. 빈 원문으로 한 번 돌려 보지 않는다.
    assert models[MlModelId.MODEL_2_AMOUNT].calls == []
    # 원문을 요구하지 않는 모델 3 은 같은 실행에서 그대로 결과를 낸다.
    assert result.result(MlModelId.MODEL_3_ANOMALY).status == "OK"


def test_source_text_comes_from_the_common_ir_document(profile, cpl_result, common_ir):
    inputs = build_ml_inputs(profile, cpl_result, common_ir=common_ir)
    entry = inputs[MlModelId.MODEL_2_AMOUNT]

    assert entry.reason_code is None
    assert entry.payload["evidence_text"] == _SOURCE_TEXT
    assert entry.sources


def test_withheld_model_1_is_not_fed_onward(profile, cpl_result, common_ir):
    """판단보류를 임의 유형으로 승격하지 않는다 (초안 §8)."""

    withheld = FakeMlModel(
        MlModelId.MODEL_1_SUPPORT_TYPE,
        {"support_type_pred": "판로", "confidence": 0.11, "status": WITHHELD_STATUS},
    )
    models = _fakes(**{MlModelId.MODEL_1_SUPPORT_TYPE: withheld})
    result = _run(profile, cpl_result, common_ir, models)

    first = result.result(MlModelId.MODEL_1_SUPPORT_TYPE)
    assert (first.status, first.reason_code) == ("UNAVAILABLE", PREDICTION_WITHHELD)
    assert first.reference_text is None
    # 보류한 유형은 비교군 입력에 없다.
    for model_id in (MlModelId.MODEL_2_AMOUNT, MlModelId.MODEL_3_ANOMALY):
        (call,) = models[model_id].calls
        assert "support_type" not in call
    assert any(row.reason_code == PREDICTION_WITHHELD for row in result.diagnostics)


def test_usable_model_1_carries_status_and_version_onward(profile, cpl_result, common_ir):
    models = _fakes()
    _run(profile, cpl_result, common_ir, models)

    (call,) = models[MlModelId.MODEL_2_AMOUNT].calls
    assert call["support_type"] == "판로"
    # 상태·버전을 함께 넘긴다 (초안 §8).
    assert call["support_type_status"] == "신뢰"
    assert call["support_type_model_version"] == "fake-artifact"


def _numbers_outside_internal(node, path="result"):
    """``internal`` 을 뺀 나머지에서 발견된 숫자 값의 경로."""

    if isinstance(node, dict):
        found = []
        for key, value in node.items():
            if key == "internal":
                continue
            found.extend(_numbers_outside_internal(value, f"{path}.{key}"))
        return found
    if isinstance(node, list):
        return [
            hit
            for index, value in enumerate(node)
            for hit in _numbers_outside_internal(value, f"{path}[{index}]")
        ]
    # StrEnum 은 str 이므로 여기 걸리지 않는다. 진짜 수치만 걸린다.
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return [f"{path}={node!r}"]
    return []


def test_no_numbers_reach_the_user_surface(profile, cpl_result, common_ir):
    """확률·퍼센타일은 ``internal`` 안에만 산다 (초안 §8).

    모델이 숫자가 섞인 문구를 돌려줘도 사용자 표면에는 실리지 않는다. 표시
    계층이 실수로 집계할 수 있는 자리를 만들지 않는 것이 계약이다.
    """

    # 정규식으로는 못 거르는 표현들이다. 서버가 statement 를 아예 쓰지
    # 않으므로 무엇을 넣든 표면에 나가지 않는다.
    leaky = FakeMlModel(
        MlModelId.MODEL_3_ANOMALY,
        {
            "anomaly_score": 2.3,
            "percentile": 0.94,
            "statement": "신뢰도 0.42, 점수 82점, 백분위 0.71 이다.",
        },
    )
    result = _run(profile, cpl_result, common_ir, _fakes(**{MlModelId.MODEL_3_ANOMALY: leaky}))

    assert _numbers_outside_internal(asdict(result)) == []
    # 백분위를 문장으로 흘리는 표현은 표면에 실리지 않는다.
    anomaly = result.result(MlModelId.MODEL_3_ANOMALY)
    # 모델 문장은 통째로 버려지고 서버 조립 문구만 남는다.
    for leaked in ("신뢰도", "점수", "백분위", "0.42", "82", "0.71"):
        assert leaked not in (anomaly.reference_text or ""), anomaly.reference_text
    # 숫자는 버려지지 않고 내부에 남는다.
    assert anomaly.internal["percentile"] == 0.94


def test_predicted_amount_is_shown_not_hidden(profile, cpl_result, common_ir):
    """숫자를 전부 막으면 초안이 표시하라고 한 예측 금액까지 사라진다.

    초안 30 행: "ML 은 지원유형·예측 금액·이례성 설명을 제공하고 확률·점수는
    숨긴다." 289 행: "원 단위 예측 금액. 비교군 백분위는 내부 결과로 보존."
    즉 금액은 나가고 백분위만 internal 에 남는다.
    """

    amount = FakeMlModel(
        MlModelId.MODEL_2_AMOUNT,
        {
            "pred_won": 40_000_000,
            "percentile_rank": 0.71,
            "confidence": 0.42,
        },
    )
    result = _run(profile, cpl_result, common_ir, _fakes(**{MlModelId.MODEL_2_AMOUNT: amount}))

    row = result.result(MlModelId.MODEL_2_AMOUNT)
    assert row.reference_text is not None
    assert "4,000만원" in row.reference_text, "예측 금액이 표면에서 잘렸다"
    # 확률·백분위는 여전히 internal 에만 있다.
    assert row.internal["percentile_rank"] == 0.71
    # 모델 2 큐레이션 목록에 없던 confidence 도 공통 집합으로 잡힌다.
    assert row.internal["confidence"] == 0.42
    assert _numbers_outside_internal(asdict(result)) == []


def _write_entry(root: Path, folder: str, model_name: str) -> Path:
    path = root / folder / "inference.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'MODEL_NAME = "{model_name}"\n', encoding="utf-8")
    return path


def test_entry_modules_never_share_the_inference_name(tmp_path):
    """세 폴더의 ``inference.py`` 가 서로를 덮어쓰지 않는다.

    실제로 물렸던 결함이다 — 이름으로 import 하면 두 번째부터
    ``sys.modules["inference"]`` 캐시가 첫 번째 모듈을 돌려준다. 아래 절반이
    그 결함을 재현하고, 나머지 절반이 우리 로더가 거기 걸리지 않음을 고정한다.
    """

    first = _write_entry(tmp_path, "model1", "model1")
    third = _write_entry(tmp_path, "model3", "model3")
    names = ("t_ml_model1_inference", "t_ml_model3_inference")
    saved = sys.modules.pop("inference", None)
    saved_path = list(sys.path)
    try:
        # (1) 결함 재현: 이름으로 부르면 두 번째가 첫 번째로 잡힌다.
        sys.path.insert(0, str(first.parent))
        import inference as naive_first  # noqa: PLC0415

        sys.path.insert(0, str(third.parent))
        import inference as naive_third  # noqa: PLC0415

        assert naive_third is naive_first
        assert naive_third.MODEL_NAME == "model1"

        # (2) 우리 로더: 파일 경로 + 고유 이름이라 서로 독립이다.
        del sys.modules["inference"]
        loaded_first = load_entry_module(names[0], first)
        loaded_third = load_entry_module(names[1], third)

        assert loaded_first is not loaded_third
        assert loaded_first.MODEL_NAME == "model1"
        assert loaded_third.MODEL_NAME == "model3"
        assert loaded_first.__name__ == names[0]
        # 캐시 키가 "inference" 인 적이 없다.
        assert "inference" not in sys.modules
    finally:
        sys.path[:] = saved_path
        for name in (*names, "inference"):
            sys.modules.pop(name, None)
        if saved is not None:
            sys.modules["inference"] = saved


def test_model_1_counts_all_four_contract_fields(profile, cpl_result, common_ir):
    """팀원 MODEL1_FIELDS 는 title 포함 4 개다.

    title 을 빼고 세면 제목만 있는 유효 입력이 잘못 차단된다.
    """

    assert MODEL_1_FIELDS == ("title", "purpose", "content", "target_text")
    inputs = build_ml_inputs(profile, cpl_result, common_ir=common_ir, title="테스트 사업")
    model_1 = inputs[MlModelId.MODEL_1_SUPPORT_TYPE]
    assert "title" not in model_1.metadata["missing_fields"]
    # missing_fields 는 팀원 입력 스키마를 오염시키지 않는다.
    assert "missing_fields" not in model_1.payload
    assert set(model_1.payload) == set(MODEL_1_FIELDS)


def test_title_only_input_is_not_blocked():
    """제목만 있어도 팀원 계약상 실행 가능하다. 전부 비었을 때만 막는다."""

    empty = {"comparison_profile": {}, "field_states": []}
    with_title = build_ml_inputs(empty, None, title="청년창업 지원사업")
    assert with_title[MlModelId.MODEL_1_SUPPORT_TYPE].reason_code is None

    without = build_ml_inputs(empty, None, title=None)
    assert without[MlModelId.MODEL_1_SUPPORT_TYPE].reason_code == INPUT_EVIDENCE_MISSING


def test_forbidden_model_3_wording_never_reaches_the_user():
    """팀원 FORBIDDEN 어휘는 표면에 못 나가고, fallback 으로 덮지도 않는다."""

    for forbidden in ("지원규모 과다", "잘못 설계됨", "부적절함", "정책적으로 문제 있음"):
        with pytest.raises(MlOutputInvalid):
            _validate_reference(MlModelId.MODEL_3_ANOMALY, {"level": forbidden})


@pytest.mark.parametrize(
    "model_id, output, valid",
    [
        # 정상 출력
        (MlModelId.MODEL_1_SUPPORT_TYPE, {"support_type_pred": "연구개발"}, True),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": 40_000_000}, True),
        (MlModelId.MODEL_3_ANOMALY, {"level": "확인 필요"}, True),
        (
            MlModelId.MODEL_3_ANOMALY,
            {"level": "희귀한 설계 조합", "cause_axes": ["지원비율"]},
            True,
        ),
        # 빈 값
        (MlModelId.MODEL_1_SUPPORT_TYPE, {}, False),
        (MlModelId.MODEL_1_SUPPORT_TYPE, {"support_type_pred": "  "}, False),
        (MlModelId.MODEL_2_AMOUNT, {}, False),
        (MlModelId.MODEL_3_ANOMALY, {}, False),
        # 임의 라벨 — 19 개 클래스 밖
        (MlModelId.MODEL_1_SUPPORT_TYPE, {"support_type_pred": "아무거나ABC"}, False),
        (MlModelId.MODEL_1_SUPPORT_TYPE, {"support_type_pred": 7}, False),
        # 금액: bool·NaN·inf·0 이하
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": True}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": float("nan")}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": float("inf")}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": float("-inf")}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": 0}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": -1}, False),
        (MlModelId.MODEL_2_AMOUNT, {"pred_won": "많음"}, False),
        # cause_axes 형식
        (
            MlModelId.MODEL_3_ANOMALY,
            {"level": "확인 필요", "cause_axes": "지원비율"},
            False,
        ),
        (MlModelId.MODEL_3_ANOMALY, {"level": "확인 필요", "cause_axes": [3]}, False),
        # 미허용 축
        (
            MlModelId.MODEL_3_ANOMALY,
            {"level": "확인 필요", "cause_axes": ["아무축"]},
            False,
        ),
        # 금지어
        (MlModelId.MODEL_3_ANOMALY, {"level": "지원규모 과다"}, False),
        (MlModelId.MODEL_3_ANOMALY, {"level": "잘못 설계됨"}, False),
    ],
)
def test_output_contract_table(model_id, output, valid):
    """모델별 출력 불변조건. 유효하지 않으면 fallback 이 아니라 예외다."""

    if valid:
        text = _validate_reference(model_id, output)
        assert text and not any(
            leaked in text for leaked in ("신뢰도", "백분위", "점수", "%")
        )
    else:
        with pytest.raises(MlOutputInvalid):
            _validate_reference(model_id, output)


def test_invalid_output_fails_only_that_model(profile, cpl_result, common_ir):
    """계약 위반 출력은 그 모델만 FAILED / MODEL_INVALID_RESPONSE 다."""

    broken = FakeMlModel(MlModelId.MODEL_1_SUPPORT_TYPE, {"support_type_pred": "없는유형"})
    result = _run(
        profile, cpl_result, common_ir, _fakes(**{MlModelId.MODEL_1_SUPPORT_TYPE: broken})
    )

    first = result.result(MlModelId.MODEL_1_SUPPORT_TYPE)
    assert first.status == "FAILED"
    assert first.reason_code == MODEL_INVALID_RESPONSE
    assert first.reference_text is None
    # 나머지 둘은 그대로다.
    for other in (MlModelId.MODEL_2_AMOUNT, MlModelId.MODEL_3_ANOMALY):
        assert result.result(other).status == "OK"


def test_missing_fields_reach_the_final_result(profile, cpl_result, common_ir):
    """부분 입력으로 돌았다는 사실이 결과까지 따라온다."""

    result = _run(profile, cpl_result, common_ir, _fakes())
    first = result.result(MlModelId.MODEL_1_SUPPORT_TYPE)
    assert "missing_fields" in first.input_metadata
    assert isinstance(first.input_metadata["missing_fields"], list)
