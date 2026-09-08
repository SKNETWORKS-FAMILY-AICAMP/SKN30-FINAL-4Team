"""챗봇 회귀 검사 — Supabase PoC 스키마(2단계 비동기) 기준.

    python chatmessage/test_chatmessage.py

LLM 은 부르지 않는다. 호출 인자를 기록하는 Spy 와 일부러 근거 밖으로 나가는
구현으로 점검 장치가 실제로 잡는지를 본다. DB 도 붙이지 않는다 — 이 패키지는
행 payload 만 만들고 저장은 호출부가 하기 때문에, 만들어진 행이 테이블 CHECK
제약을 만족하는지까지가 여기서 볼 수 있는 전부다.
"""
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 패키지(`chatmessage`)를 이름으로 import 할 수 있도록 저장소 루트를 경로에 올린다.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from chatmessage import (MODEL_NAMES, MessageRow,                # noqa: E402
                         MockChatContextLoader, SessionNotUsableError,
                         SessionState, SupabaseChatContextLoader, TEMPERATURE,
                         TurnRequest, check_grounding, classify_intent,
                         complete_turn, create_turn, envelope_to_row,
                         retry_turn, to_rows)
from chatmessage.model_result import PROVENANCE_KEYS              # noqa: E402
from chatmessage.responder import (ChatLLMClient, axis_cause_claims,  # noqa: E402
                                   model3_cohort_claims,
                                   percentile_cohort_claims,
                                   ungrounded_numbers)


class FakeSupabase:
    """supabase-py 체인 호출만 흉내 낸다. 실제 쿼리 코드 경로를 그대로 태운다."""

    def __init__(self, tables):
        self.tables = tables
        self.calls = []
        self._t = None
        self._filters = {}
        self._limit = None

    def schema(self, _name):
        return self

    def table(self, name):
        self._t, self._filters, self._limit = name, {}, None
        return self

    def select(self, _columns="*"):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = [r for r in self.tables.get(self._t, [])
                if all(r.get(k) == v for k, v in self._filters.items())]
        if self._limit is not None:
            rows = rows[:self._limit]
        self.calls.append({"table": self._t, "filters": dict(self._filters)})
        return type("Res", (), {"data": rows})()

CASE = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"

MOCK = {
    CASE: {
        "model_1": {
            "support_type": {"code": "TYPE_01", "label": "연구개발"},
            "confidence": 0.996, "trust_grade": "trusted",
            "evidence": [{"field": "result.support_type", "value": "연구개발"}],
        },
        "model_2": {
            "observed_per_recipient": {"amount": 1500000000, "unit": "KRW"},
            "predicted_per_recipient": {"amount": 526390144, "unit": "KRW"},
            "cohort_percentile": 75.2, "level": "high",
            "reference": {"cohort_level": "단위x출처", "sample_count": 38,
                          "min_cohort": 30},
            "evidence": [
                {"field": "result.predicted_per_recipient.amount",
                 "value": 526390144,
                 "evidence_snapshot_id": "33333333-3333-4333-8333-333333333333"},
            ],
        },
        "model_3": {
            "distance_percentile": 1.0, "anomaly_level": None,
            "anomaly_level_status": "threshold_undetermined",
            "used_axes": ["log_per_recipient", "project_duration"],
            "available_axis_count": 2,
            "reference": {"cohort_level": "L1", "sample_count": 232,
                          "min_cohort": 20},
            "evidence": [],
        },
        "document": {
            "support_target": "전략적 제휴완료 또는 예정인 ICT중소・벤처기업(법인)",
            "support_period": "최대 3년(2년+1년)",
            "support_content": "시장수요최적화 R&D + 고성장기업도약 R&D",
            "evidence": [{"field": "TARGET_AND_CONDITIONS",
                          "evidence_snapshot_id":
                              "44444444-4444-4444-8444-444444444444"}],
        },
        # report 는 비워 둔다 — intent 는 맞는데 결과가 없는 경우를 본다.
    },
}

ACTIVE = SessionState(analysis_session_id=SESSION, analysis_case_id=CASE)

_fail = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  — %s" % detail) if detail else ""))
    if not cond:
        _fail.append(name)
    return cond


class SpyLLM(ChatLLMClient):
    def __init__(self, reply="확인된 근거 범위에서 답변합니다."):
        self.reply = reply
        self.calls = []

    def generate(self, system_prompt, user_prompt, temperature=0.7):
        self.calls.append({"system": system_prompt, "user": user_prompt,
                           "temperature": temperature})
        return self.reply


class BrokenLLM(ChatLLMClient):
    def generate(self, system_prompt, user_prompt, temperature=0.0):
        raise RuntimeError("provider timeout")


def answer(question, llm=None, loader=None, session=None):
    return complete_turn(
        assistant_message_pk="99999999-9999-4999-8999-999999999999",
        analysis_case_id=CASE, question=question,
        context_loader=loader or MockChatContextLoader(MOCK),
        llm_client=llm or SpyLLM(), session=session)


def main():
    print("== 1 Intent Router")
    cases = [
        ("우리 사업 지원성격이 뭐야?", "MODEL_1"),
        ("어떤 유형으로 분류됐어?", "MODEL_1"),
        ("이 분류 결과를 얼마나 믿을 수 있어?", "MODEL_1"),
        ("우리 사업 지원규모가 높은 편이야?", "MODEL_2"),
        ("비교군에서 어느 정도 수준이야?", "MODEL_2"),
        ("모델이 예상한 기업당 지원규모는 얼마야?", "MODEL_2"),
        ("이 사업은 비교군 대비 이례적인 편이야?", "MODEL_3"),
        ("이례성 수준은 어느 정도야?", "MODEL_3"),
        ("이 사업의 분석 결과를 종합해서 알려줘.", "REPORT"),
        ("지원대상은 누구야?", "DOCUMENT"),
        ("지원기간은 얼마야?", "DOCUMENT"),
        ("지원내용은 뭐야?", "DOCUMENT"),
    ]
    bad = [(q, w, classify_intent(q)) for q, w in cases if classify_intent(q) != w]
    check("1a 기본 질문 12개 분류", not bad, "오분류 %s" % bad if bad else "12/12")
    check("1b 지원하지 않는 질문은 UNKNOWN",
          classify_intent("오늘 점심 뭐 먹지?") == "UNKNOWN")
    check("1c 좁은 의미가 이긴다 (이례 > 비교군)",
          classify_intent("비교군 대비 이례적이야?") == "MODEL_3")

    print("== 2 create_turn — conversation_message 행 모양")
    req = TurnRequest(analysis_session_id=SESSION, analysis_case_id=CASE,
                      content="지원규모가 높은 편이야?")
    created = create_turn(req, ACTIVE, next_sequence_no=1)
    u, a = created.user_message, created.assistant_message
    check("2a user 행: completed · content 있음 · reply_to 없음",
          u.role == "user" and u.message_status == "completed"
          and u.content and u.reply_to_message_pk is None)
    check("2b assistant 행: generating · content NULL · reply_to 연결",
          a.role == "assistant" and a.message_status == "generating"
          and a.content is None and a.reply_to_message_pk == u.message_pk)
    check("2c sequence_no 가 이어진다",
          u.sequence_no == 1 and a.sequence_no == 2)
    check("2d 큐 작업이 assistant 행을 가리킨다",
          created.job.assistant_message_pk == a.message_pk
          and created.job.analysis_case_pk == CASE
          and created.job.run_type == "chat" and created.job.status == "queued")
    try:
        MessageRow(analysis_session_pk=SESSION, role="assistant", sequence_no=2,
                   content=None, message_status="generating").check_table_shape()
        check("2e reply_to 없는 assistant 행은 막는다", False, "예외가 안 났다")
    except ValueError:
        check("2e reply_to 없는 assistant 행은 막는다", True)

    print("== 3 세션 가드")
    for label, sess in (
            ("closed", SessionState(analysis_session_id=SESSION,
                                    analysis_case_id=CASE, status="closed")),
            ("expired", SessionState(analysis_session_id=SESSION,
                                     analysis_case_id=CASE, expired=True)),
            ("case not ready", SessionState(analysis_session_id=SESSION,
                                            analysis_case_id=CASE,
                                            case_status="processing"))):
        try:
            create_turn(req, sess, next_sequence_no=1)
            check("3 %s 세션은 접수 거부" % label, False, "예외가 안 났다")
        except SessionNotUsableError:
            check("3 %s 세션은 접수 거부" % label, True)

    print("== 4 complete_turn — 워커 변경분")
    spy = SpyLLM()
    ok = answer("지원규모가 높은 편이야?", llm=spy)
    check("4a 성공 → completed · content 채움 · error 없음",
          ok.message_status == "completed" and ok.content
          and ok.error_code is None and ok.intent.type == "MODEL_2")
    check("4b temperature=0.0 으로 호출된다",
          spy.calls and spy.calls[0]["temperature"] == TEMPERATURE)
    check("4c 근거가 전달된다 (evidence_snapshot_id 보존)",
          ok.evidence and ok.evidence[0].evidence_snapshot_id
          == "33333333-3333-4333-8333-333333333333")

    u1 = answer("오늘 점심 뭐 먹지?")
    check("4d UNKNOWN → failed · UNSUPPORTED_QUESTION · 재시도 불가",
          u1.message_status == "failed"
          and u1.error_code == "UNSUPPORTED_QUESTION" and not u1.retryable)

    n1 = answer("이 사업의 분석 결과를 종합해서 알려줘.")
    check("4e 결과 없음 → ANALYSIS_RESULT_NOT_AVAILABLE · 재시도 불가",
          n1.message_status == "failed"
          and n1.error_code == "ANALYSIS_RESULT_NOT_AVAILABLE"
          and not n1.retryable)

    env = MockChatContextLoader({CASE: {"model_3": {
        "status": "insufficient_data", "result": None,
        "error": {"code": "INSUFFICIENT_NUMERIC_AXES"}}}})
    i1 = answer("이례적이야?", loader=env)
    check("4f 근거 부족 → ANALYSIS_RESULT_INSUFFICIENT (결과 없음과 구분)",
          i1.error_code == "ANALYSIS_RESULT_INSUFFICIENT" and not i1.retryable)

    f1 = answer("지원규모가 높은 편이야?", llm=BrokenLLM())
    check("4g LLM 장애 → CHAT_LLM_FAILED · **재시도 가능**",
          f1.error_code == "CHAT_LLM_FAILED" and f1.retryable)

    s1 = answer("지원규모가 높은 편이야?",
                session=SessionState(analysis_session_id=SESSION,
                                     analysis_case_id=CASE, status="closed"))
    check("4h 워커 실행 중 세션이 닫혔으면 SESSION_NOT_ACTIVE",
          s1.error_code == "SESSION_NOT_ACTIVE")

    w1 = answer("지원규모가 높은 편이야?",
                llm=SpyLLM("예측값은 1,234,567,890원입니다."))
    check("4i 근거 밖 수치는 경고로 남고 답변은 살린다",
          w1.message_status == "completed" and w1.warnings, str(w1.warnings))

    print("== 5 retry_turn")
    failed_row = MessageRow(analysis_session_pk=SESSION, role="assistant",
                            sequence_no=2, content=None,
                            message_status="failed",
                            reply_to_message_pk=u.message_pk,
                            error_code="CHAT_LLM_FAILED", retry_count=0)
    patch = retry_turn(failed_row)
    check("5a 재시도 가능한 실패는 generating 으로 되돌린다",
          patch["message_status"] == "generating" and patch["retry_count"] == 1
          and patch["error_code"] is None)
    for code in ("UNSUPPORTED_QUESTION", "ANALYSIS_RESULT_NOT_AVAILABLE"):
        row = failed_row.model_copy(update={"error_code": code})
        try:
            retry_turn(row)
            check("5b %s 는 재시도 막는다" % code, False, "예외가 안 났다")
        except ValueError:
            check("5b %s 는 재시도 막는다" % code, True)

    print("== 6 model_result 매핑 (envelope → 행)")
    env_ok = {"analysis_id": CASE,
              "model": {"name": "support_amount_regressor", "version": "1.0",
                        "model_type": "XGBoost"},
              "status": "success", "input": {"input_completeness": "partial"},
              "result": {"level": "high"}, "metadata": {"pred_log10": 8.7},
              "error": None}
    row = envelope_to_row(CASE, "MODEL_2", env_ok)
    check("6a envelope.result → result_data",
          row["result_data"] == {"level": "high"}
          and row["model_name"] == "MODEL_2" and row["status"] == "success")
    check("6b model·input 은 result_data 에 섞지 않고 metadata 로",
          row["metadata"]["model_info"]["model_type"] == "XGBoost"
          and row["metadata"]["input_data"]["input_completeness"] == "partial"
          and "_model" not in row["result_data"],
          "result_data 는 프롬프트에 그대로 실리는 자리")

    # 의존 실패: Model 1 이 죽으면 2·3 은 not_available 이지 failed 가 아니다.
    pipeline = {
        "model_1": {"status": "failed", "result": None,
                    "error": {"code": "MODEL1_INFERENCE_FAILED"},
                    "model": {}, "metadata": {}},
        "model_2": {"status": "not_available", "result": None,
                    "error": {"code": "DEPENDENCY_NOT_AVAILABLE"},
                    "model": {}, "metadata": {}},
        "model_3": {"status": "not_available", "result": None,
                    "error": {"code": "DEPENDENCY_NOT_AVAILABLE"},
                    "model": {}, "metadata": {}},
        "sim_r": {"status": "not_available", "result": None},
    }
    rows = to_rows(pipeline, CASE)
    check("6c 부분·의존 실패를 그대로 저장한다",
          len(rows) == 3
          and [r["status"] for r in rows]
          == ["failed", "not_available", "not_available"]
          and all(r["result_data"] is None for r in rows),
          "Model 2·3 을 failed 로 바꾸지 않는다")
    check("6d sim_r 은 model_result 에 넣지 않는다",
          all(r["model_name"] in MODEL_NAMES for r in rows))
    try:
        envelope_to_row(CASE, "MODEL_9", env_ok)
        check("6e 알 수 없는 model_name 은 막는다", False, "예외가 안 났다")
    except ValueError:
        check("6e 알 수 없는 model_name 은 막는다", True)

    print("== 7 SupabaseChatContextLoader (가짜 client)")
    db = FakeSupabase({
        "model_result": [
            {"analysis_case_pk": CASE, "model_name": "MODEL_2",
             "status": "success", "result_data": MOCK[CASE]["model_2"],
             "metadata": {"model_info": {"name": "support_amount_regressor"}},
             "error": None},
            {"analysis_case_pk": CASE, "model_name": "MODEL_3",
             "status": "insufficient_data", "result_data": None,
             "metadata": {}, "error": {"code": "INSUFFICIENT_NUMERIC_AXES"}},
        ],
        "analysis_case": [
            {"analysis_case_pk": CASE, "case_status": "ready",
             "program_name": "ICT지원사업", "original_filename": "req.hwp",
             "input_profile_snapshot": {"support_period": "최대 3년"}},
        ],
        "evidence_snapshot": [
            {"analysis_case_pk": CASE,
             "evidence_snapshot_pk": "55555555-5555-4555-8555-555555555555",
             "field_code": "TARGET_AND_CONDITIONS", "snippet_text": "ICT중소기업"},
        ],
        "axis_result": [{"analysis_case_pk": CASE, "axis_code": "CPL-01",
                         "axis_status": "PRESENT", "summary_text": "확인",
                         "result_data": {}}],
        "sim_candidate": [{"analysis_case_pk": CASE,
                           "announcement_title": "유사공고", "source_url": None,
                           "issuing_organization": None, "summary_text": None,
                           "comparable_axes": []}],
    })
    loader = SupabaseChatContextLoader(db)
    m2 = loader.get_context(CASE, "MODEL_2", "q")
    check("7a MODEL_2 를 model_result 에서 읽는다",
          m2["status"] == "success" and m2["data"]["level"] == "high",
          "쿼리 %s" % db.calls[-1])
    check("7b model_name 으로 행을 고른다",
          db.calls[-1]["filters"]["model_name"] == "MODEL_2"
          and db.calls[-1]["table"] == "model_result")
    m3 = loader.get_context(CASE, "MODEL_3", "q")
    check("7c DB status 를 그대로 승계한다 (insufficient_data)",
          m3["status"] == "insufficient_data" and m3["data"] is None
          and m3["source_status"] == "insufficient_data")
    check("7d 행이 없으면 not_available",
          loader.get_context(CASE, "MODEL_1", "q")["status"] == "not_available")
    doc = loader.get_context(CASE, "DOCUMENT", "q")
    check("7e DOCUMENT 는 input_profile_snapshot + evidence_snapshot",
          doc["status"] == "success"
          and doc["data"]["request_profile"]["support_period"] == "최대 3년"
          and doc["evidence"][0]["evidence_snapshot_id"]
          == "55555555-5555-4555-8555-555555555555")
    rep = loader.get_context(CASE, "REPORT", "q")
    check("7f REPORT 는 axis_result + sim_candidate",
          rep["status"] == "success" and rep["data"]["axis_results"]
          and rep["data"]["similar_programs"])
    check("7g UNKNOWN 은 조회하지 않는다",
          loader.get_context(CASE, "UNKNOWN", "q")["status"] == "not_available")
    not_ready = SupabaseChatContextLoader(FakeSupabase({"analysis_case": [
        {"analysis_case_pk": CASE, "case_status": "processing",
         "program_name": None, "original_filename": "x",
         "input_profile_snapshot": {}}]}))
    check("7h 분석이 아직 안 끝났으면 insufficient_data",
          not_ready.get_context(CASE, "DOCUMENT", "q")["status"]
          == "insufficient_data")

    print("== 8 근거 점검")
    ctx2 = MOCK[CASE]["model_2"]
    check("8a 단위 풀어쓰기는 오탐하지 않는다",
          not ungrounded_numbers("예측값은 약 5억 2,639만원입니다.", ctx2))
    check("8b Context 밖 수치는 잡는다",
          ungrounded_numbers("예측값은 1,234,567,890원입니다.", ctx2))
    ctx3 = MOCK[CASE]["model_3"]
    check("8c 기여도 없이 원인 단정을 잡는다",
          axis_cause_claims("지원금액이 가장 큰 원인입니다.", ctx3))
    check("8d anomaly_level null 인데 수준 단정을 잡는다",
          any("anomaly_level" in w for w in
              check_grounding("이례성은 높은 수준입니다.", "MODEL_3", ctx3)))

    print("== 9 비교군 provenance Guard")
    # 실제 envelope 모양(metadata 에 provenance)이 프롬프트까지 오는지부터 본다.
    # 이게 안 오면 아래 Guard 는 판단 근거가 없어 전부 무력해진다.
    def m2_env(uses_st, level, compat="known", unseen=None):
        return {"status": "success",
                "model": {"name": "support_amount_regressor"},
                "result": {"cohort_percentile": 75.2, "level": "high",
                           "predicted_per_recipient": {"amount": 526390144,
                                                       "unit": "KRW"}},
                "metadata": {"support_type_compatibility": compat,
                             "unseen_support_type": unseen,
                             "prediction_scope_warning":
                                 (None if compat == "known"
                                  else "Model 2 학습 시 존재하지 않았던 support_type 입니다."),
                             "percentile_cohort_level": level,
                             "percentile_uses_support_type": uses_st,
                             "percentile_caveat":
                                 (None if uses_st
                                  else "fell_back_to_cohort_without_support_type"),
                             "bucket_proba": {"High": 0.95}}}

    wide = MockChatContextLoader({CASE: {"model_2": m2_env(False, "단위x출처")}})
    ctx = wide.get_context(CASE, "MODEL_2", "q")
    prov = ctx["data"].get("_provenance") or {}
    check("9a provenance 가 프롬프트 context 까지 전달된다",
          prov.get("percentile_uses_support_type") is False
          and prov.get("percentile_cohort_level") == "단위x출처"
          and prov.get("percentile_caveat"),
          "키 %s" % sorted(prov))
    check("9b 운영 metadata 는 올라가지 않는다",
          "bucket_proba" not in prov, "provenance 화이트리스트")

    # A. known label 인데 백분위는 성격을 안 쓴 단계에서 나왔다.
    bad2 = "같은 연구개발 사업 중 75.2백분위입니다."
    good2 = ("지원단위와 출처 기준의 더 넓은 비교군에서 계산된 75.2백분위입니다.")
    check("9c(A) 성격 비교군인 척하면 warning",
          percentile_cohort_claims(bad2, ctx["data"]),
          str(percentile_cohort_claims(bad2, ctx["data"])))
    check("9d(A) 실제 비교군을 밝히면 warning 없음",
          not percentile_cohort_claims(good2, ctx["data"]))
    w = check_grounding(bad2, "MODEL_2", ctx["data"])
    check("9e(A) warning 문구에 실제 단계가 들어간다",
          any("support_type-based cohort" in x and "단위x출처" in x for x in w),
          w[0] if w else "없음")

    # C. 실제로 성격을 쓴 단계면 같은 표현이 정상이다.
    narrow = MockChatContextLoader({CASE: {
        "model_2": m2_env(True, "성격x방식x단위x출처")}})
    nctx = narrow.get_context(CASE, "MODEL_2", "q")["data"]
    check("9f(C) 성격을 쓴 단계면 같은 표현을 허용",
          not percentile_cohort_claims(bad2, nctx)
          and not check_grounding(bad2, "MODEL_2", nctx))

    # B. unseen label — 예측은 유지하고 한정만 붙인다.
    useen = MockChatContextLoader({CASE: {
        "model_2": m2_env(False, "단위x출처", "unseen_by_model2", "상담")}})
    ures = complete_turn(
        assistant_message_pk="99999999-9999-4999-8999-999999999999",
        analysis_case_id=CASE, question="지원규모가 높은 편이야?",
        context_loader=useen, llm_client=SpyLLM("예측값은 526390144원입니다."))
    check("9g(B) unseen 이어도 status=success · 예측 유지",
          ures.message_status == "completed" and ures.content)
    uprov = useen.get_context(CASE, "MODEL_2", "q")["data"]["_provenance"]
    check("9h(B) unseen 사실과 경고가 context 에 실린다",
          uprov["support_type_compatibility"] == "unseen_by_model2"
          and uprov["unseen_support_type"] == "상담"
          and uprov["prediction_scope_warning"])

    # D. Model 3 이 전체 비교군으로 물러난 경우.
    m3_l0 = {"distance_percentile": 0.91, "anomaly_level": None,
             "anomaly_level_status": "threshold_undetermined",
             "used_axes": ["log_per_recipient", "project_duration"],
             "available_axis_count": 2,
             "reference": {"cohort_level": "L0 전체", "cohort_n": 2626,
                           "min_cohort": 20}}
    bad3 = "같은 상담 사업과 비교했습니다."
    good3 = "상담 세부 비교군의 표본이 부족해 전체 비교군 기준으로 계산했습니다."
    check("9i(D) L0 fallback 인데 유형 비교군이라 하면 warning",
          model3_cohort_claims(bad3, m3_l0),
          str(model3_cohort_claims(bad3, m3_l0)))
    check("9j(D) 전체 비교군이라고 밝히면 warning 없음",
          not model3_cohort_claims(good3, m3_l0))
    w3 = check_grounding(bad3, "MODEL_3", m3_l0)
    check("9k(D) warning 문구에 실제 cohort_level 이 들어간다",
          any("type-specific cohort" in x and "L0" in x for x in w3),
          w3[0] if w3 else "없음")
    m3_l1 = dict(m3_l0, reference={"cohort_level": "L1 support_typexsupport_method",
                                   "cohort_n": 232, "min_cohort": 20})
    check("9l(D) 유형 단계에서 나왔으면 같은 표현을 허용",
          not model3_cohort_claims(bad3, m3_l1))
    check("9m top1_axis 는 provenance 로 전달된다 (원인 아님)",
          "top1_axis" in PROVENANCE_KEYS)

    print()
    if _fail:
        print("실패 %d건: %s" % (len(_fail), _fail))
        return 1
    print("CHATMESSAGE 회귀 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
