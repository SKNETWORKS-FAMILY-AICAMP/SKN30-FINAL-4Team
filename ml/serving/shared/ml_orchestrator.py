"""ML 파이프라인 Orchestrator — Model 1 → Model 2·3 (+ SIM-R 훅).

저장소에 빠져 있던 연결 계층이다. Model 2·3 은 Model 1 을 호출하지 않고
`support_type` 을 **입력으로 받기만** 한다. 그 값을 넣어 주는 코드가 없었다.

    CPL
     └→ Model 1 ── support_type ──┬→ Model 2
                                  ├→ Model 3
                                  └→ SIM-R (훅)

모델 알고리즘은 하나도 새로 만들지 않는다. 기존 진입 함수를 부르고 결과를
공통 envelope 으로 감싸기만 한다.

실패 처리 두 가지
----------------
    Model 1 이 실패하면 Model 2·3 은 `failed` 가 아니라 `not_available` 이다.
    자기 잘못이 아니라 선행 의존이 없어 실행하지 못한 것이기 때문이다.

    Model 2 가 죽어도 Model 3 은 계속 돌린다(부분 실패 허용). 워커를 즉시
    죽이면 살아 있는 결과까지 잃는다.

SIM-R 은 여기서 직접 부르지 않는다
---------------------------------
SIM-R(retrieval)은 backend 쪽 async 함수(`retrieval.retrieve_top_five`)이고
DB·임베딩 클라이언트를 요구한다. ml 이 backend 를 import 하면 의존이 거꾸로
서므로, 호출부가 `sim_r_runner` 로 주입하게 열어 둔다. 주입하지 않으면
`not_available` 로 남는다.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVING = os.path.abspath(os.path.join(_HERE, ".."))
for _p in (_HERE, os.path.join(_SERVING, "model1"),
           os.path.join(_SERVING, "model2"), os.path.join(_SERVING, "model3")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import result_envelope as RE                   # noqa: E402

M2_NAME, M2_VERSION, M2_TYPE = "support_amount_regressor", "1.0", "XGBoost"
M3_NAME, M3_VERSION, M3_TYPE = ("design_anomaly_detector", "1.0",
                                "unsupervised_distance_based")
SIMR_NAME, SIMR_VERSION, SIMR_TYPE = "similar_program_retriever", "1.0", "vector_search"

M2_MIN_COHORT = 30
M3_MIN_COHORT = 20


def _model1():
    import runner
    return runner


def _model2():
    import importlib.util
    name = "model2_predict_entry"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SERVING, "model2", "predict.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _model3():
    import score
    return score


# ------------------------------------------------------------------ Model 2
def run_model_2(analysis_id, record, text=None, cohort=None):
    """기존 Model 2 서빙 호출 → Result JSON.

    record 는 canonical feature dict(어댑터 출력 `features` 와 같은 모양).
    `support_type` 은 Model 1 결과가 이미 채워 넣은 상태여야 한다.
    """
    try:
        M2 = _model2()
        rec = dict(record)
        rec.setdefault("title", (record or {}).get("title") or "")
        rec.setdefault("evidence_text", text or rec.get("evidence_text") or "")
        out = M2.predict([rec])
        row = out["predictions"][0]
    except Exception as e:                                  # noqa: BLE001
        return RE.failed(analysis_id, M2_NAME, M2_VERSION, M2_TYPE,
                         code="MODEL2_INFERENCE_FAILED",
                         message="%s: %s" % (type(e).__name__, e))

    # 비교군 percentile 은 회귀와 독립이다. 실패해도 예측은 유지한다.
    ref = None
    try:
        cmp_out = M2.percentile(
            row["pred_won"], rec.get("support_type"), rec.get("support_method"),
            rec.get("support_unit"), cohort)
        if cmp_out.get("status") != "비교불가":
            ref = cmp_out
    except Exception:                                       # noqa: BLE001
        ref = None

    observed = rec.get("per_recipient")
    return RE.success(
        analysis_id, M2_NAME, M2_VERSION, M2_TYPE,
        result={
            "observed_per_recipient": {
                "amount": None if observed is None else int(observed),
                "unit": "KRW"},
            "predicted_per_recipient": {"amount": int(row["pred_won"]),
                                        "unit": "KRW"},
            "cohort_percentile": (None if ref is None
                                  else ref.get("percentile_rank")),
            "level": row["bucket"].lower(),
            "reference": {
                "cohort_level": None if ref is None else ref.get("level"),
                "cohort_key": {k: rec.get(k) for k in
                               ("support_type", "support_method",
                                "support_unit")} | {"cohort": cohort},
                "sample_count": None if ref is None else ref.get("n"),
                "min_cohort": M2_MIN_COHORT,
            },
        },
        input={"input_completeness": row["input_completeness"],
               "missing_features": row["missing_features"]},
        metadata={
            "pred_log10": row["pred_log10"],
            "bucket_proba": row["bucket_proba"],
            "bucket_edges_won": row["bucket_edges_won"],
            "percentile_status": None if ref is None else ref.get("status"),
            # level 문턱은 모델이 학습에서 정한 bucket 경계다 — 여기서 새로
            # 고르지 않는다.
            "level_source": "model2 bucket_edges_won",
        },
    )


# ------------------------------------------------------------------ Model 3
def run_model_3(analysis_id, record, adapter_meta=None):
    """기존 Model 3 서빙 호출 → Result JSON. 유효 축 2개 미만이면 채점하지 않는다."""
    try:
        M3 = _model3()
        rec = dict(record)
        axes = M3.axis_report([rec])
    except Exception as e:                                  # noqa: BLE001
        return RE.failed(analysis_id, M3_NAME, M3_VERSION, M3_TYPE,
                         code="MODEL3_INFERENCE_FAILED",
                         message="%s: %s" % (type(e).__name__, e))

    if not axes:
        return RE.insufficient_data(
            analysis_id, M3_NAME, M3_VERSION, M3_TYPE,
            code="SUPPORT_TYPE_MISSING",
            message="support_type 이 없어 비교군을 정할 수 없다",
            metadata={"minimum_required_axes": M3.MIN_AXES})

    a = axes[0]
    if not a["scorable"]:
        return RE.insufficient_data(
            analysis_id, M3_NAME, M3_VERSION, M3_TYPE,
            code="INSUFFICIENT_NUMERIC_AXES",
            message="유효 수치축 %d개 (최소 %d개 필요)" % (a["n_axes"], M3.MIN_AXES),
            input={"available_axis_count": a["n_axes"],
                   "axes_present": a["axes_present"],
                   "axes_missing": a["axes_missing"]},
            metadata={"minimum_required_axes": M3.MIN_AXES,
                      "distance_metric": "euclidean"})

    try:
        res = M3.predict([rec])
        row = res.to_dict("records")[0]
    except Exception as e:                                  # noqa: BLE001
        return RE.failed(analysis_id, M3_NAME, M3_VERSION, M3_TYPE,
                         code="MODEL3_SCORING_FAILED",
                         message="%s: %s" % (type(e).__name__, e))

    validity = None
    if adapter_meta is not None:
        try:
            validity = M3.confidence(M3.axis_validity(adapter_meta))
        except Exception:                                   # noqa: BLE001
            validity = None

    return RE.success(
        analysis_id, M3_NAME, M3_VERSION, M3_TYPE,
        result={
            "distance_percentile": round(float(row["score"]), 4),
            "anomaly_level": None,          # 문턱 미확정 — 아래 metadata 참조
            "used_axes": a["axes_present"],
            "available_axis_count": a["n_axes"],
            "reference": {
                "cohort_level": row["level"],
                "cohort_key": row["cohort_key"],
                "sample_count": int(row["cohort_n"]),
                "min_cohort": M3_MIN_COHORT,
            },
            "top1_axis": row["top1_axis"],
        },
        input={"axes_missing": a["axes_missing"]},
        metadata={
            "distance_metric": "euclidean",
            "minimum_required_axes": M3.MIN_AXES,
            # anomaly_level 문턱은 아직 확정된 근거가 없다. 임의로 정하면
            # 사용자에게 판정처럼 보이므로 None 으로 두고 백분위만 넘긴다.
            "anomaly_level_status": "threshold_undetermined",
            "axis_validity": None if validity is None else validity["axis_validity"],
            "evidence_confidence": None if validity is None else validity["confidence"],
        },
    )


# ------------------------------------------------------------------ 파이프라인
def run_ml_pipeline(analysis_id, cpl_result, structured_data=None, title=None,
                    text=None, cohort=None, adapter_meta=None,
                    sim_r_runner=None):
    """CPL → Model 1 → Model 2·3 (+SIM-R). 부분 실패를 허용한다."""
    M1 = _model1()
    model_1 = M1.run_model_1(analysis_id, cpl_result, title=title)

    if not RE.is_ok(model_1):
        blocked = {
            "model_2": RE.not_available(analysis_id, M2_NAME, M2_VERSION,
                                        M2_TYPE, depends_on="model_1"),
            "model_3": RE.not_available(analysis_id, M3_NAME, M3_VERSION,
                                        M3_TYPE, depends_on="model_1"),
            "sim_r": RE.not_available(analysis_id, SIMR_NAME, SIMR_VERSION,
                                      SIMR_TYPE, depends_on="model_1"),
        }
        results = {"model_1": model_1, **blocked}
        return {"analysis_id": analysis_id, **results,
                "summary": RE.summarize(results)}

    support_type = model_1["result"]["support_type"]
    rec = dict(structured_data or {})
    rec["support_type"] = support_type          # Model 1 결과 주입 지점

    meta = adapter_meta
    if meta is not None:
        meta = dict(meta)
        meta["features"] = dict(meta.get("features") or {})
        meta["features"]["support_type"] = support_type

    model_2 = run_model_2(analysis_id, rec, text=text, cohort=cohort)
    model_3 = run_model_3(analysis_id, rec, adapter_meta=meta)

    if sim_r_runner is None:
        sim_r = RE.not_available(analysis_id, SIMR_NAME, SIMR_VERSION, SIMR_TYPE,
                                 depends_on="sim_r_runner (미주입)")
    else:
        try:
            sim_r = sim_r_runner(analysis_id=analysis_id,
                                 support_type=support_type, record=rec)
        except Exception as e:                              # noqa: BLE001
            sim_r = RE.failed(analysis_id, SIMR_NAME, SIMR_VERSION, SIMR_TYPE,
                              code="SIMR_FAILED",
                              message="%s: %s" % (type(e).__name__, e))

    results = {"model_1": model_1, "model_2": model_2,
               "model_3": model_3, "sim_r": sim_r}
    return {"analysis_id": analysis_id, **results,
            "summary": RE.summarize(results)}


async def run_ml_pipeline_async(*args, **kwargs):
    """backend 의 async 파이프라인에서 부르기 위한 래퍼.

    세 모델 모두 동기 CPU 작업이고 Model 1 은 442MB 가중치를 올린다. 이벤트
    루프에서 그대로 돌리면 그 시간 동안 다른 요청이 멈추므로 스레드로 넘긴다.

    backend 통합 지점 (`analysis_pipeline.run_analysis_pipeline`):

        ml_results = await run_ml_pipeline_async(
            analysis_id=str(case_id),
            cpl_result=result,              # 이미 갖고 있는 CplResult
            structured_data=record,         # canonical feature dict
            title=document_title,
        )

    SIM-R 은 여기서 부르지 않는다(§9 의존 방향). backend 가 기존
    `retrieve_top_five()` 를 그대로 이어서 부르면 된다 — 그쪽이 단순하다.
    """
    import asyncio
    import functools
    return await asyncio.to_thread(
        functools.partial(run_ml_pipeline, *args, **kwargs))
