"""근거(분석 결과) 조회 계층.

여기서 꺼내는 것은 **대화 이력이 아니다.** 대화는 `result.conversation_message`
가 갖고 있고 API 도 따로 있다(`api.v_conversation_messages`). 이 계층은 답변의
**근거**가 될 분석 결과를 가져온다.

Supabase PoC 스키마에서 Intent 별로 읽을 곳:

    MODEL_1/2/3  result.model_result                       (model_name 으로 구분)
    DOCUMENT     result.analysis_case.input_profile_snapshot
                 result.evidence_snapshot                  (원문 근거)
    REPORT       result.axis_result                        (CPL 13 + FIT 7)
                 result.sim_candidate                      (상위 5개 유사공고)

Model 1·2·3 은 `axis_result.result_data` 에 넣지 않고 전용 테이블에 둔다 —
CPL·FIT 축 결과가 아니고, envelope 을 통째로 보존하는 편이 조회도 단순하다.

반환 계약(구현이 무엇이든 이 모양이어야 한다):

    {"status": "success"|"not_available"|"insufficient_data",
     "data": dict|None,
     "evidence": list[dict]}
"""
from typing import Any, Dict, List

from .model_result import provenance_of

# Intent -> 저장소 키.
INTENT_SOURCE: Dict[str, str] = {
    "MODEL_1": "model_1",
    "MODEL_2": "model_2",
    "MODEL_3": "model_3",
    "REPORT": "report",
    "DOCUMENT": "document",
}

# 실제 테이블. 로더 구현체가 참고한다.
TABLES = {
    "model_1": ("result.model_result",),
    "model_2": ("result.model_result",),
    "model_3": ("result.model_result",),
    "document": ("result.analysis_case.input_profile_snapshot",
                 "result.evidence_snapshot"),
    "report": ("result.axis_result", "result.sim_candidate"),
}


def _empty(status: str = "not_available") -> Dict[str, Any]:
    return {"status": status, "data": None, "evidence": []}


class ChatContextLoader:
    """조회 인터페이스. 구현체는 반환 계약만 지키면 된다.

    나중에 `SupabaseChatContextLoader` 가 이 자리를 대신한다. 그때 바뀌는 것은
    이 클래스 하나이고 router·prompt·responder·service 는 그대로다.
    """

    def get_context(self, analysis_case_id: str, intent: str,
                    question: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockChatContextLoader(ChatContextLoader):
    """개발·테스트용. 메모리 dict 를 저장소처럼 읽는다. 운영에 쓰지 않는다.

    ML Result envelope 모양(status/result 를 가진 dict)도 받아준다 — ML
    Orchestrator 가 내놓는 형태가 그것이라, 실제 저장이 붙어도 프롬프트가 보는
    모양이 달라지지 않게 하려는 것이다.
    """

    def __init__(self, mock_data: Dict[str, Any]):
        self.mock_data = mock_data or {}

    def get_context(self, analysis_case_id: str, intent: str,
                    question: str) -> Dict[str, Any]:
        key = INTENT_SOURCE.get(intent)
        if key is None:                          # UNKNOWN 등
            return _empty()

        analysis = self.mock_data.get(analysis_case_id)
        if analysis is None:
            return _empty()

        data = analysis.get(key)
        if data is None:
            return _empty()

        # envelope 이면 그 status 를 물려받는다. 모델이 근거 부족으로 점수를
        # 못 낸 것을 챗봇이 '결과 없음' 으로 뭉개면 안 된다.
        if isinstance(data, dict) and "status" in data and "result" in data:
            status = data.get("status")
            if status != "success":
                mapped = ("insufficient_data"
                          if status == "insufficient_data" else "not_available")
                return {"status": mapped, "data": None, "evidence": [],
                        "source_status": status}
            payload = dict(data.get("result") or {})
            payload["_model"] = data.get("model")
            # 비교군 provenance 는 metadata 에 있다. 이것 없이 result 만 넘기면
            # 챗봇이 실제로 쓰이지 않은 비교군을 지어낸다.
            prov = provenance_of(data.get("metadata"))
            if prov:
                payload["_provenance"] = prov
            return {"status": "success", "data": payload,
                    "evidence": _evidence_of(data, key)}

        return {"status": "success", "data": data,
                "evidence": _evidence_of(data, key)}


def _evidence_of(data: Any, source: str) -> List[Dict[str, Any]]:
    """근거 목록을 꺼내고 source 를 채운다. 없으면 빈 목록."""
    if not isinstance(data, dict):
        return []
    out: List[Dict[str, Any]] = []
    for e in data.get("evidence") or []:
        if isinstance(e, dict):
            item = dict(e)
            item.setdefault("source", source)
            out.append(item)
    return out
