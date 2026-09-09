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

M1_NAME, M1_VERSION, M1_TYPE = "support_type_classifier", "1.0", "KLUE-BERT"
M2_NAME, M2_VERSION, M2_TYPE = "support_amount_regressor", "1.0", "XGBoost"
M3_NAME, M3_VERSION, M3_TYPE = ("design_anomaly_detector", "1.0",
                                "unsupervised_distance_based")
SIMR_NAME, SIMR_VERSION, SIMR_TYPE = "similar_program_retriever", "1.0", "vector_search"

M2_MIN_COHORT = 30
M3_MIN_COHORT = 20


def _model1():
    import runner
    return runner


def run_model_1(
    analysis_id,
    cpl_result=None,
    title=None,
    include_scale_field=False,
    already_cleaned=True,
):
    """Model 1 진입 함수 — CPL 결과 → 지원성격 분류 Result Envelope."""
    # Context 객체 또는 dict가 첫 인자로 전달된 경우 자동 언패킹
    if hasattr(analysis_id, "cpl_results") or (
        isinstance(analysis_id, dict) and "cpl_results" in analysis_id
    ):
        ctx = analysis_id
        cpl_res = (
            getattr(ctx, "cpl_results", None)
            if not isinstance(ctx, dict)
            else ctx.get("cpl_results")
        )
        if not cpl_res and hasattr(ctx, "cpl_result") and ctx.cpl_result is not None:
            cpl_res = ctx.cpl_result
        case_id = (
            getattr(ctx, "case_id", None)
            if not isinstance(ctx, dict)
            else ctx.get("case_id")
        )
        doc_text = (
            getattr(ctx, "document_text", None)
            if not isinstance(ctx, dict)
            else ctx.get("document_text", "")
        )
        doc_title = (
            getattr(ctx, "title", None) or getattr(ctx, "document_title", None)
            if not isinstance(ctx, dict)
            else (ctx.get("title") or ctx.get("document_title"))
        )
        if not doc_title and doc_text:
            lines = [
                line.strip() for line in doc_text.splitlines() if line.strip()
            ]
            doc_title = lines[0][:50] if lines else "사전협의 요청서"
        return run_model_1(
            analysis_id=str(case_id or "AN-UNKNOWN"),
            cpl_result=cpl_res,
            title=title or doc_title,
            include_scale_field=include_scale_field,
            already_cleaned=already_cleaned,
        )

    try:
        M1 = _model1()
        return M1.run_model_1(
            analysis_id=analysis_id,
            cpl_result=cpl_result,
            title=title,
            include_scale_field=include_scale_field,
            already_cleaned=already_cleaned,
        )
    except Exception as e:
        return RE.failed(
            analysis_id=analysis_id,
            name=M1_NAME,
            version=M1_VERSION,
            model_type=M1_TYPE,
            code="MODEL1_EXECUTION_FAILED",
            message="%s: %s" % (type(e).__name__, e),
        )


async def run_model_1_async(
    analysis_id,
    cpl_result=None,
    title=None,
    include_scale_field=False,
    already_cleaned=True,
):
    """Model 1 비동기 진입 함수 (CPU 추론 및 가중치 로드를 스레드로 오프로드)."""
    import asyncio
    import functools

    return await asyncio.to_thread(
        functools.partial(
            run_model_1,
            analysis_id,
            cpl_result=cpl_result,
            title=title,
            include_scale_field=include_scale_field,
            already_cleaned=already_cleaned,
        )
    )



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
# 비교군 참조표의 `cohort` 는 **출처**다. 사다리 세 단계가 전부 출처를 키에
# 포함하므로, 값이 없거나 엉뚱하면 어느 단계에도 걸리지 않고 조용히 '비교불가'가
# 된다(실측: "연구개발|grant" 를 넘겼더니 percentile 이 null 로 나왔다).
COHORTS = ("taxonomy", "bizinfo")

# ------------------------------------------------- support_type 호환성
# Model 1 은 19종을 내놓는데 Model 2 는 23종으로 학습했고, 그 둘이 포함관계가
# 아니다 — `상담` 하나가 Model 1 에만 있다. 이 값이 들어오면 Model 2 에서
# **미학습 범주라 조용히 결측 처리**된다. 예측은 나오고 겉으로는 정상이다.
#
# 게다가 같은 값이 비교군 참조표에도 없어서 percentile 조회까지 함께 무너진다
# (실측: cohort_reference 에 `상담` 없음). 두 증상이 한 원인에서 나오므로
# 아래에서 함께 묶어 남긴다.
#
# 값을 코드에 적지 않고 학습 artifact 에서 읽는다 — 재학습으로 레벨이 바뀌면
# 손으로 적은 목록은 조용히 어긋난다.
UNSEEN_WARNING = "Model 2 학습 시 존재하지 않았던 support_type 입니다."
_ST_LEVELS = None


def model2_support_type_levels(path=None):
    """Model 2 가 학습한 support_type 레벨. 번들에서 읽고 캐시한다."""
    global _ST_LEVELS
    if _ST_LEVELS is None:
        bundle = _model2().load(path)
        levels = (bundle.get("features") or {}).get("cat_levels", {})
        _ST_LEVELS = tuple(levels.get("support_type") or ())
    return _ST_LEVELS


def check_support_type(support_type, path=None):
    """(호환성, 미학습값). 레벨을 못 읽으면 판정하지 않는다(unknown)."""
    levels = model2_support_type_levels(path)
    if not levels or support_type is None:
        return "unknown", None
    if support_type in levels:
        return "known", None
    return "unseen_by_model2", support_type


def run_model_2(analysis_id, record=None, text=None, cohort=None, adapter_output=None):
    """기존 Model 2 서빙 호출 → Result JSON.

    record 는 canonical feature dict(어댑터 출력 `features` 와 같은 모양).
    `support_type` 은 Model 1 결과가 이미 채워 넣은 상태여야 한다.

    cohort 는 비교 모집단의 **출처**이며 COHORTS 중 하나다. 비교군 키(성격·방식·
    단위)와 헷갈리기 쉬운데 그것들은 record 에서 읽는다. 값이 유효하지 않으면
    회귀 예측은 그대로 내고 percentile 만 비운 뒤 이유를 metadata 에 남긴다.
    """
    # 패턴 감지: run_model_2(model1_result, frozen_context, cohort=...)
    if (
        isinstance(analysis_id, dict)
        and ("model" in analysis_id or "status" in analysis_id)
        and (
            hasattr(record, "document_text")
            or (isinstance(record, dict) and "document_text" in record)
        )
    ):
        model1_result = analysis_id
        ctx = record
        case_id = (
            getattr(ctx, "case_id", None)
            if not isinstance(ctx, dict)
            else ctx.get("case_id")
        )
        doc_text = (
            getattr(ctx, "document_text", None)
            if not isinstance(ctx, dict)
            else ctx.get("document_text", "")
        )
        doc_title = (
            getattr(ctx, "title", None) or getattr(ctx, "document_title", None)
            if not isinstance(ctx, dict)
            else (ctx.get("title") or ctx.get("document_title"))
        )
        aid = str(case_id or "AN-UNKNOWN")

        if not RE.is_ok(model1_result):
            return RE.not_available(
                aid,
                M2_NAME,
                M2_VERSION,
                M2_TYPE,
                depends_on="model_1",
            )

        support_type = (model1_result.get("result") or {}).get("support_type")
        if not support_type:
            return RE.not_available(
                aid,
                M2_NAME,
                M2_VERSION,
                M2_TYPE,
                depends_on="model_1.support_type",
            )

        if adapter_output is None:
            import preconsultation_adapter as PA
            adapter_output = PA.adapt(doc_text)
        rec = dict(adapter_output.get("features") or {})
        rec["support_type"] = support_type

        if not doc_title and doc_text:
            lines = [
                line.strip() for line in doc_text.splitlines() if line.strip()
            ]
            doc_title = lines[0][:50] if lines else "사전협의 요청서"
        rec["title"] = doc_title or "사전협의 요청서"
        rec["evidence_text"] = doc_text

        if not rec.get("support_unit"):
            amount_type = (adapter_output.get("amounts") or {}).get(
                "support_amount_type"
            )
            rec["support_unit"] = (
                "company" if amount_type == "per_company" else "project"
            )
        if not rec.get("support_method"):
            rec["support_method"] = "grant"

        selected_cohort = cohort or "taxonomy"
        return run_model_2(aid, rec, text=doc_text, cohort=selected_cohort)

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

    compat, unseen_st = check_support_type(rec.get("support_type"))

    # 비교군 percentile 은 회귀와 독립이다. 실패해도 예측은 유지하되, 왜 비었는지
    # 남긴다 — 값이 null 인 것과 이유를 모르는 것은 다르다.
    ref, pct_reason = None, None
    if cohort not in COHORTS:
        pct_reason = ("cohort 가 %r 이다. 출처(%s) 중 하나여야 비교군을 찾는다"
                      % (cohort, "/".join(COHORTS)))
    else:
        try:
            cmp_out = M2.percentile(
                row["pred_won"], rec.get("support_type"),
                rec.get("support_method"), rec.get("support_unit"), cohort)
            if cmp_out.get("status") == "비교불가":
                pct_reason = cmp_out.get("reason") or "비교불가"
            else:
                ref = cmp_out
        except Exception as e:                              # noqa: BLE001
            pct_reason = "%s: %s" % (type(e).__name__, e)

    # 미학습 support_type 이면 percentile 이 **null 이 되지 않는다.** 사다리
    # 마지막 단계(`단위x출처`)가 support_type 을 키에 쓰지 않아서 거기 걸린다.
    # 실측: 상담 -> 비교가능 · 단위x출처 · n=38 · 76.7백분위.
    #
    # 값이 비는 것보다 이쪽이 더 위험하다. 숫자가 정상으로 보이는데 실제로는
    # **지원성격을 전혀 반영하지 않은 비교**다. 그래서 어느 단계에서 나온
    # 값인지, 그 단계가 support_type 을 썼는지를 함께 남긴다.
    # 그리고 이건 미학습 label 만의 문제가 아니다. 비교군이 얇으면(MIN_COHORT=30)
    # known label 도 같은 단계로 물러난다 — 실측: 연구개발|grant|project|taxonomy
    # 는 15건뿐이라 `단위x출처` 로 떨어진다. 그래서 조건을 unseen 에 걸지 않고
    # **실제로 어느 단계에서 나왔는지**에 건다.
    pct_level = ref.get("level") if ref else None
    pct_uses_st = bool(pct_level) and pct_level.startswith("성격")
    pct_cause = None
    if ref is not None and not pct_uses_st:
        pct_cause = "fell_back_to_cohort_without_support_type"

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
            "percentile_status": ref.get("status") if ref else "비교불가",
            "percentile_unavailable_reason": pct_reason,
            "percentile_cohort_level": pct_level,
            # 이 단계가 support_type 을 비교 키로 썼는가. False 면 지원성격을
            # 반영하지 않은 백분위다 — 문구로 옮길 때 "같은 성격 사업 중에서"
            # 라고 말하면 안 된다.
            "percentile_uses_support_type": pct_uses_st,
            "percentile_caveat": pct_cause,
            "cohort_source": cohort,
            # 예측은 그대로 낸다(나머지 210열로 계산된다). 다만 이 값이 학습
            # 범위 밖이라는 사실을 결과에 남겨 둔다 — 조용히 넘어가면 안 된다.
            "support_type_compatibility": compat,
            "unseen_support_type": unseen_st,
            "prediction_scope_warning": (UNSEEN_WARNING
                                         if compat == "unseen_by_model2"
                                         else None),
            # level 문턱은 모델이 학습에서 정한 bucket 경계다 — 여기서 새로
            # 고르지 않는다.
            "level_source": "model2 bucket_edges_won",
        },
    )


async def run_model_2_async(analysis_id, record=None, text=None, cohort="taxonomy", adapter_output=None):
    """Model 2 비동기 진입 함수 (XGBoost 회귀 및 분위수 조회를 스레드로 오프로드)."""
    import asyncio
    import functools

    return await asyncio.to_thread(
        functools.partial(run_model_2, analysis_id, record=record, text=text, cohort=cohort, adapter_output=adapter_output)
    )


# ------------------------------------------------------------------ Model 3
def run_model_3(analysis_id, record=None, adapter_meta=None, adapter_output=None):
    """기존 Model 3 서빙 호출 → Result JSON. 유효 축 2개 미만이면 채점하지 않는다."""
    # 패턴 감지: run_model_3(model1_result, frozen_context)
    if (
        isinstance(analysis_id, dict)
        and ("model" in analysis_id or "status" in analysis_id)
        and (
            hasattr(record, "document_text")
            or (isinstance(record, dict) and "document_text" in record)
        )
    ):
        model1_result = analysis_id
        ctx = record
        case_id = (
            getattr(ctx, "case_id", None)
            if not isinstance(ctx, dict)
            else ctx.get("case_id")
        )
        doc_text = (
            getattr(ctx, "document_text", None)
            if not isinstance(ctx, dict)
            else ctx.get("document_text", "")
        )
        aid = str(case_id or "AN-UNKNOWN")

        if not RE.is_ok(model1_result):
            return RE.not_available(
                aid,
                M3_NAME,
                M3_VERSION,
                M3_TYPE,
                depends_on="model_1",
            )

        support_type = (model1_result.get("result") or {}).get("support_type")
        if not support_type:
            return RE.not_available(
                aid,
                M3_NAME,
                M3_VERSION,
                M3_TYPE,
                depends_on="model_1.support_type",
            )

        if adapter_output is None:
            import preconsultation_adapter as PA
            adapter_output = PA.adapt(doc_text)
        rec = dict(adapter_output.get("features") or {})
        rec["support_type"] = support_type

        meta = dict(adapter_output)
        meta["features"] = dict(meta.get("features") or {})
        meta["features"]["support_type"] = support_type

        return run_model_3(aid, rec, adapter_meta=meta)

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
            # 문턱이 확정된 근거가 없다. 임의로 정하면 사용자에게 판정처럼
            # 보이므로 None 으로 두고, **왜 None 인지를 result 안에** 함께 둔다 —
            # 하류(챗봇)가 result 만 보고도 "수준을 말하면 안 된다" 를 알아야 한다.
            "anomaly_level": None,
            "anomaly_level_status": "threshold_undetermined",
            "used_axes": a["axes_present"],
            "available_axis_count": a["n_axes"],
            "reference": {
                "cohort_level": row["level"],
                "cohort_key": row["cohort_key"],
                "sample_count": int(row["cohort_n"]),
                "min_cohort": M3_MIN_COHORT,
            },
        },
        input={"axes_missing": a["axes_missing"]},
        metadata={
            "distance_metric": "euclidean",
            "minimum_required_axes": M3.MIN_AXES,
            # top1_axis 는 '가장 크게 벗어난 축' 이지 '이례성의 원인' 이 아니다.
            # 기여도 분해가 아니라 단일 축의 편차일 뿐인데, result 에 두면 챗봇이
            # 원인처럼 읽어 설명한다(요청서 14절이 금지하는 바로 그것). 보고서용
            # 으로는 필요하므로 버리지 않고 metadata 로 내린다.
            "top1_axis": row["top1_axis"],
            "axis_validity": None if validity is None else validity["axis_validity"],
            "evidence_confidence": None if validity is None else validity["confidence"],
        },
    )


async def run_model_3_async(analysis_id, record=None, adapter_meta=None, adapter_output=None):
    """Model 3 비동기 진입 함수 (통계 거리 산출을 스레드로 오프로드)."""
    import asyncio
    import functools

    return await asyncio.to_thread(
        functools.partial(run_model_3, analysis_id, record=record, adapter_meta=adapter_meta, adapter_output=adapter_output)
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
