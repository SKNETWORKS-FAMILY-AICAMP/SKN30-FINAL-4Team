"""챗봇 회귀 검사 — Router · Context Loader · Responder · Response.

    python chatmessage/test_chatmessage.py

LLM 은 부르지 않는다. 대신 호출 인자를 기록하는 Spy 와 일부러 근거 밖으로
나가는 Bad 구현으로 **점검 장치가 실제로 잡는지**를 본다. 진짜 LLM 을 붙이면
답변 문장은 달라져도 이 계약은 그대로여야 한다.
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

from chatmessage import (ChatRequest, MockChatContextLoader,          # noqa: E402
                     TEMPERATURE, check_grounding, classify_intent,
                     handle_chat)
from chatmessage.responder import (ChatLLMClient, axis_cause_claims,  # noqa: E402
                               ungrounded_numbers)

ANALYSIS_ID = "AN-2026-0001"

MOCK = {
    ANALYSIS_ID: {
        "model_1": {
            "support_type": {"code": "TYPE_01", "label": "연구개발"},
            "confidence": 0.996,
            "trust_grade": "trusted",
            "evidence": [{"field": "result.support_type", "value": "연구개발"}],
        },
        "model_2": {
            "observed_per_recipient": {"amount": 1500000000, "unit": "KRW"},
            "predicted_per_recipient": {"amount": 526390144, "unit": "KRW"},
            "cohort_percentile": 78.4,
            "level": "high",
            "reference": {"cohort_level": "L1 support_typexsupport_method",
                          "sample_count": 232, "min_cohort": 30},
            "evidence": [
                {"field": "result.predicted_per_recipient.amount",
                 "value": 526390144},
                {"field": "result.level", "value": "high"},
            ],
        },
        "model_3": {
            "distance_percentile": 1.0,
            "anomaly_level": None,
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
            "evidence": [{"field": "TARGET_AND_CONDITIONS", "value": "blk-02"}],
        },
        # report 는 일부러 비워 둔다 — intent 는 맞는데 결과가 없는 경우를 본다.
    },
    "AN-EMPTY": {},
}


class SpyLLM(ChatLLMClient):
    """호출 인자를 기록하고 정해진 답을 준다."""

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


_fail = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  — %s" % detail) if detail else ""))
    if not cond:
        _fail.append(name)
    return cond


def ask(message, loader=None, llm=None, analysis_id=ANALYSIS_ID):
    return handle_chat(
        ChatRequest(conversation_id="conv_001", analysis_id=analysis_id,
                    message=message),
        loader or MockChatContextLoader(MOCK),
        llm or SpyLLM())


def main():
    print("== 1 Intent Router (요청서 24절 기본 질문 12개)")
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
    bad = [(q, want, classify_intent(q)) for q, want in cases
           if classify_intent(q) != want]
    check("1a 기본 질문 12개가 모두 의도대로 분류된다", not bad,
          "오분류 %s" % ["%s → %s(기대 %s)" % (q, got, want)
                        for q, want, got in bad] if bad else "12/12")
    check("1b 지원하지 않는 질문은 UNKNOWN",
          classify_intent("오늘 점심 뭐 먹지?") == "UNKNOWN")
    check("1c 좁은 의미가 넓은 의미를 이긴다 (이례 > 비교군)",
          classify_intent("비교군 대비 이례적이야?") == "MODEL_3",
          "비교군(MODEL_2 어휘)이 함께 있어도 MODEL_3")
    check("1d 빈 질문도 안전하게 UNKNOWN", classify_intent("   ") == "UNKNOWN")

    print("== 2 Context Loader")
    loader = MockChatContextLoader(MOCK)
    ok = loader.get_context(ANALYSIS_ID, "MODEL_2", "q")
    check("2a analysis_id 존재 → success",
          ok["status"] == "success" and ok["data"]["level"] == "high")
    check("2b analysis_id 없음 → not_available",
          loader.get_context("NOPE", "MODEL_2", "q")["status"] == "not_available")
    check("2c 해당 intent 결과 없음 → not_available",
          loader.get_context(ANALYSIS_ID, "REPORT", "q")["status"]
          == "not_available", "report 미저장")
    check("2d UNKNOWN 은 조회 대상 없음",
          loader.get_context(ANALYSIS_ID, "UNKNOWN", "q")["status"]
          == "not_available")
    env_loader = MockChatContextLoader({ANALYSIS_ID: {"model_3": {
        "status": "insufficient_data", "result": None,
        "error": {"code": "INSUFFICIENT_NUMERIC_AXES"}}}})
    check("2e envelope 의 insufficient_data 를 물려받는다",
          env_loader.get_context(ANALYSIS_ID, "MODEL_3", "q")["status"]
          == "insufficient_data",
          "모델이 근거부족으로 안 낸 것을 '결과없음'으로 뭉개지 않는다")

    print("== 3 Responder 근거 점검")
    spy = SpyLLM()
    ask("우리 사업 지원규모가 높은 편이야?", llm=spy)
    check("3a temperature=0.0 으로 호출된다",
          spy.calls and spy.calls[0]["temperature"] == TEMPERATURE,
          "temperature=%s" % (spy.calls[0]["temperature"] if spy.calls else "?"))
    check("3b 프롬프트에 Context 가 실린다",
          "526390144" in spy.calls[0]["user"] and "MODEL_2" in spy.calls[0]["user"])
    ctx2 = MOCK[ANALYSIS_ID]["model_2"]
    check("3c 단위를 풀어 쓴 표현은 오탐하지 않는다",
          not ungrounded_numbers("예측값은 약 5억 2,639만원입니다.", ctx2),
          "526390144 의 부분일치 허용")
    check("3d Context 에 없는 수치를 잡는다",
          ungrounded_numbers("예측값은 1,234,567,890원입니다.", ctx2),
          str(ungrounded_numbers("예측값은 1,234,567,890원입니다.", ctx2)))
    ctx3 = MOCK[ANALYSIS_ID]["model_3"]
    check("3e 축별 기여도 없이 원인을 단정하면 잡는다",
          axis_cause_claims("지원금액이 가장 큰 원인입니다.", ctx3))
    check("3f 기여도가 있으면 잡지 않는다",
          not axis_cause_claims("지원금액이 가장 큰 원인입니다.",
                                {**ctx3, "axis_contribution": {"x": 1}}))
    check("3g anomaly_level 이 null 인데 수준을 단정하면 잡는다",
          any("anomaly_level" in w for w in
              check_grounding("이례성은 높은 수준입니다.", "MODEL_3", ctx3)))

    print("== 4 Chat Response")
    r = ask("우리 사업 지원규모가 높은 편이야?")
    check("4a success 면 answer 있고 error 는 null",
          r.status == "success" and r.answer is not None and r.error is None
          and r.intent.type == "MODEL_2")
    check("4b evidence 가 전달된다",
          len(r.answer.evidence) == 2
          and r.answer.evidence[0].source == "model_2",
          "%d건 · source=%s" % (len(r.answer.evidence),
                               r.answer.evidence[0].source))
    check("4c message_id 가 매번 새로 발급된다",
          ask("지원대상은 누구야?").message_id != r.message_id)

    u = ask("오늘 점심 뭐 먹지?")
    check("4d UNKNOWN → not_available + answer null",
          u.status == "not_available" and u.answer is None
          and u.error.code == "UNSUPPORTED_QUESTION"
          and u.intent.type == "UNKNOWN")

    n = ask("이 사업의 분석 결과를 종합해서 알려줘.")
    check("4e 결과 미저장 → not_available",
          n.status == "not_available" and n.answer is None
          and n.error.code == "ANALYSIS_RESULT_NOT_AVAILABLE"
          and n.intent.type == "REPORT")

    i = handle_chat(
        ChatRequest(conversation_id="c", analysis_id=ANALYSIS_ID,
                    message="이례적이야?"),
        env_loader, SpyLLM())
    check("4f 근거 부족 → insufficient_data (not_available 과 구분)",
          i.status == "insufficient_data"
          and i.error.code == "ANALYSIS_RESULT_INSUFFICIENT")

    f = ask("지원규모가 높은 편이야?", llm=BrokenLLM())
    check("4g LLM 장애 → failed (분석 결과 부재와 구분)",
          f.status == "failed" and f.error.code == "CHAT_LLM_FAILED")

    w = ask("지원규모가 높은 편이야?",
            llm=SpyLLM("예측값은 1,234,567,890원입니다."))
    check("4h 근거 밖 수치는 경고로 남는다",
          w.status == "success" and w.warnings,
          str(w.warnings))

    print("== 5 요청 검증")
    try:
        ChatRequest(conversation_id="c", analysis_id="a", message="  ")
        check("5a 빈 질문은 거부된다", False, "예외가 안 났다")
    except Exception as e:                                  # noqa: BLE001
        check("5a 빈 질문은 거부된다", True, type(e).__name__)

    print()
    if _fail:
        print("실패 %d건: %s" % (len(_fail), _fail))
        return 1
    print("CHATBOT 회귀 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
