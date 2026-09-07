"""Model 1·2·3 공통 Result JSON envelope.

왜 공통 envelope 인가
--------------------
Orchestrator 가 세 모델을 순서대로 부르는데, 각자 다른 모양으로 답하면 실패
처리를 모델마다 따로 써야 한다. 특히 구분해야 하는 상태가 둘 있다.

    failed             그 모델이 실행되다가 죽었다
    not_available      선행 의존(Model 1)이 없어 **실행 자체를 못 했다**

이 둘을 뭉개면 Final Report 가 "모델 2 가 고장났다" 와 "모델 1 이 없어서 못
돌렸다" 를 같은 말로 표시한다. 원인이 다르면 사용자가 할 일도 다르다.

    insufficient_data  실행은 가능했지만 근거가 모자라 **점수를 내지 않았다**
                       (모델 3 의 유효 축 2개 미만이 여기 해당한다)

status 가 success 가 아니면 `result` 는 항상 None 이다 — 부분적으로 채워진
결과를 내보내면 하류가 그것을 정상값으로 읽는다.
"""

SUCCESS = "success"
FAILED = "failed"
NOT_AVAILABLE = "not_available"
INSUFFICIENT_DATA = "insufficient_data"

STATUSES = (SUCCESS, FAILED, NOT_AVAILABLE, INSUFFICIENT_DATA)


def envelope(analysis_id, name, version, model_type, status,
             result=None, input=None, metadata=None, error=None):
    """공통 껍데기. status 가 success 가 아니면 result 를 강제로 비운다."""
    if status not in STATUSES:
        raise ValueError("알 수 없는 status: %r (허용: %s)" % (status, list(STATUSES)))
    return {
        "analysis_id": analysis_id,
        "model": {"name": name, "version": version, "model_type": model_type},
        "status": status,
        "input": input or {},
        "result": result if status == SUCCESS else None,
        "metadata": metadata or {},
        "error": error,
    }


def success(analysis_id, name, version, model_type, result,
            input=None, metadata=None):
    return envelope(analysis_id, name, version, model_type, SUCCESS,
                    result=result, input=input, metadata=metadata)


def failed(analysis_id, name, version, model_type, code, message,
           input=None, metadata=None):
    """모델 자체가 실행 중 죽은 경우."""
    return envelope(analysis_id, name, version, model_type, FAILED,
                    input=input, metadata=metadata,
                    error={"code": code, "message": message})


def not_available(analysis_id, name, version, model_type, depends_on,
                  metadata=None):
    """선행 의존이 없어 실행하지 못한 경우 — 이 모델의 잘못이 아니다."""
    meta = dict(metadata or {})
    meta["depends_on"] = depends_on
    return envelope(analysis_id, name, version, model_type, NOT_AVAILABLE,
                    metadata=meta,
                    error={"code": "DEPENDENCY_NOT_AVAILABLE",
                           "message": "%s 결과가 없어 실행하지 않았다" % depends_on})


def insufficient_data(analysis_id, name, version, model_type, code, message,
                      input=None, metadata=None):
    """실행은 가능했으나 근거가 모자라 점수를 내지 않은 경우."""
    return envelope(analysis_id, name, version, model_type, INSUFFICIENT_DATA,
                    input=input, metadata=metadata,
                    error={"code": code, "message": message})


def is_ok(env):
    return isinstance(env, dict) and env.get("status") == SUCCESS


def summarize(results):
    """Orchestrator 결과 묶음 → 부분 실패 요약.

    하나가 실패해도 나머지를 버리지 않는다(부분 실패 허용). 어떤 단계가 어떤
    상태였는지만 한 곳에 모아 Final Report 가 그대로 표시할 수 있게 한다.
    """
    by_status = {}
    for key, env in results.items():
        st = env.get("status") if isinstance(env, dict) else "failed"
        by_status.setdefault(st, []).append(key)
    return {
        "counts": {st: len(v) for st, v in sorted(by_status.items())},
        "by_status": {st: sorted(v) for st, v in sorted(by_status.items())},
        "all_success": set(by_status) == {SUCCESS},
        "partial_failure": SUCCESS in by_status and set(by_status) != {SUCCESS},
    }
