"""ML Result Envelope ↔ `result.model_result` 행 변환.

Model 1·2·3 결과는 `axis_result.result_data` 에 넣지 않고 전용 테이블에 둔다.
CPL·FIT 축 결과가 아니고, envelope 을 통째로 보존하는 편이 조회도 단순하다.

    UNIQUE (analysis_case_pk, model_name)

같은 분석 건에 모델별로 한 행이다. 재실행하면 upsert 로 덮는다.

status 를 그대로 옮기는 것이 이 변환의 핵심이다
----------------------------------------------
envelope 의 4종 status 는 원인이 서로 다르고, DB 에서도 구분돼야 한다.

    success            정상
    failed             그 모델이 실행되다가 죽었다
    not_available      선행 의존(Model 1)이 없어 실행 자체를 못 했다
    insufficient_data  실행은 됐지만 근거가 모자라 점수를 내지 않았다

Model 1 이 실패해도 Model 2·3 을 `failed` 로 바꾸지 않는다. 자기 잘못이 아니다.
부분 실패도 그대로 저장한다 — 전체 분석을 즉시 실패시키지 않는다.
"""
from typing import Any, Dict, List, Optional

# 파이프라인 키 -> DB CHECK 제약의 model_name
PIPELINE_TO_MODEL_NAME = {
    "model_1": "MODEL_1",
    "model_2": "MODEL_2",
    "model_3": "MODEL_3",
}
MODEL_NAMES = tuple(PIPELINE_TO_MODEL_NAME.values())

# Intent -> model_name. 챗봇 조회에서 쓴다.
INTENT_TO_MODEL_NAME = {
    "MODEL_1": "MODEL_1",
    "MODEL_2": "MODEL_2",
    "MODEL_3": "MODEL_3",
}

# result.model_result.status CHECK
DB_STATUSES = ("success", "failed", "not_available", "insufficient_data")


def envelope_to_row(analysis_case_pk: str, model_name: str,
                    envelope: Dict[str, Any]) -> Dict[str, Any]:
    """envelope 하나 → 테이블 행 하나.

    `input` 과 `model` 은 result_data 에 섞지 않고 metadata 아래에 둔다 —
    result_data 는 챗봇이 근거로 그대로 읽는 자리라, 운영 정보가 섞이면
    프롬프트에 실려 LLM 이 그것까지 설명하려 든다.
    """
    if model_name not in MODEL_NAMES:
        raise ValueError("알 수 없는 model_name: %r (허용 %s)"
                         % (model_name, list(MODEL_NAMES)))
    status = envelope.get("status")
    if status not in DB_STATUSES:
        raise ValueError("알 수 없는 status: %r (허용 %s)"
                         % (status, list(DB_STATUSES)))

    metadata = dict(envelope.get("metadata") or {})
    if envelope.get("model"):
        metadata["model_info"] = envelope["model"]
    if envelope.get("input"):
        metadata["input_data"] = envelope["input"]

    return {
        "analysis_case_pk": analysis_case_pk,
        "model_name": model_name,
        "status": status,
        # 실패 상태의 envelope 은 result 가 None 이다. 그대로 둔다 —
        # 부분적으로 채워진 결과를 저장하면 하류가 정상값으로 읽는다.
        "result_data": envelope.get("result"),
        "metadata": metadata or None,
        "error": envelope.get("error"),
    }


def to_rows(ml_results: Dict[str, Any],
            analysis_case_pk: str) -> List[Dict[str, Any]]:
    """`run_ml_pipeline()` 결과 → upsert 할 행 목록.

    sim_r 은 여기 넣지 않는다. 모델 결과가 아니고 저장 위치도 다르다.
    """
    rows = []
    for key, model_name in PIPELINE_TO_MODEL_NAME.items():
        envelope = ml_results.get(key)
        if not envelope:
            continue
        rows.append(envelope_to_row(analysis_case_pk, model_name, envelope))
    return rows


# metadata 에서 **프롬프트까지 올려 보낼** 값. 나머지 metadata 는 운영 정보라
# 올리지 않는다(모델 버전·bucket 확률 같은 것을 LLM 이 설명하려 든다).
#
# 이 여섯은 예외다. 숫자가 어떤 비교군에서 나왔는지를 말해 주는 값이라, 없으면
# 챗봇이 "같은 연구개발 사업 중 75.2백분위" 처럼 **실제로 쓰이지 않은 비교군**을
# 지어낸다. 값 자체보다 이 provenance 가 문장의 정확도를 좌우한다.
PROVENANCE_KEYS = (
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


def provenance_of(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """metadata 에서 provenance 만 골라낸다. 없으면 빈 dict."""
    if not isinstance(metadata, dict):
        return {}
    return {k: metadata[k] for k in PROVENANCE_KEYS if k in metadata}


def row_to_context(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """`result.model_result` 행 → 챗봇 Context Loader 반환 계약.

    DB 의 status 를 챗봇 status 로 그대로 승계한다. `failed` 와
    `not_available` 을 뭉개면 "모델이 고장났다" 와 "선행 결과가 없어 못 돌렸다"
    가 같은 문구로 안내된다.
    """
    if row is None:
        return {"status": "not_available", "data": None, "evidence": []}

    status = row.get("status")
    if status != "success":
        mapped = ("insufficient_data" if status == "insufficient_data"
                  else "not_available")
        return {"status": mapped, "data": None, "evidence": [],
                "source_status": status, "error": row.get("error")}

    data = dict(row.get("result_data") or {})
    meta = row.get("metadata") or {}
    if meta.get("model_info"):
        data["_model"] = meta["model_info"]
    prov = provenance_of(meta)
    if prov:
        # `_` 접두어로 결과값과 구분한다 — 숫자가 아니라 그 숫자의 출처다.
        data["_provenance"] = prov
    return {"status": "success", "data": data,
            "evidence": _evidence_of(row)}


def _evidence_of(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """근거 목록. result_data 안에 evidence 가 있으면 꺼내고 source 를 채운다."""
    data = row.get("result_data") or {}
    if not isinstance(data, dict):
        return []
    out = []
    source = str(row.get("model_name") or "model_result").lower()
    for e in data.get("evidence") or []:
        if isinstance(e, dict):
            item = dict(e)
            item.setdefault("source", source)
            out.append(item)
    return out
