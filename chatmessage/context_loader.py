"""분석 결과 조회 계층 — 지금은 인터페이스와 Mock 만.

결과 저장 스키마가 아직 없다(`backend/app/schemas/analysis_result.py` 가 빈
파일이다). 저장 계약 없이 조회 코드를 쓰면 스키마가 정해질 때 다시 뜯게 되므로,
여기서는 **경계만** 만들어 둔다. 나중에 `SupabaseChatContextLoader` 하나만
갈아끼우면 router·prompt·responder·service 는 그대로 쓴다.

반환 계약(구현이 무엇이든 이 모양이어야 한다):

    {"status": "success"|"not_available"|"insufficient_data",
     "data": dict|None,
     "evidence": list[dict]}
"""
from typing import Any, Dict, List

# Intent -> 저장소에서 꺼낼 키.
INTENT_TO_KEY: Dict[str, str] = {
    "MODEL_1": "model_1",
    "MODEL_2": "model_2",
    "MODEL_3": "model_3",
    "REPORT": "report",
    "DOCUMENT": "document",
}


def _empty(status: str = "not_available") -> Dict[str, Any]:
    return {"status": status, "data": None, "evidence": []}


class ChatContextLoader:
    """조회 인터페이스. 구현체는 이 계약만 지키면 된다."""

    def get_context(self, analysis_id: str, intent: str,
                    question: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockChatContextLoader(ChatContextLoader):
    """개발용. 메모리 dict 를 저장소처럼 읽는다.

    ML Result envelope 모양(status/result 를 가진 dict)도 그대로 받아준다 —
    실제로 저장될 형태가 그것이라, 나중에 갈아끼울 때 프롬프트가 보는 모양이
    달라지지 않게 하려는 것이다.
    """

    def __init__(self, mock_data: Dict[str, Any]):
        self.mock_data = mock_data or {}

    def get_context(self, analysis_id: str, intent: str,
                    question: str) -> Dict[str, Any]:
        analysis = self.mock_data.get(analysis_id)
        if analysis is None:
            return _empty()

        key = INTENT_TO_KEY.get(intent)
        if key is None:                      # UNKNOWN 등 조회 대상 없음
            return _empty()

        data = analysis.get(key)
        if data is None:
            return _empty()

        # envelope 이면 그 status 를 그대로 물려받는다. 모델이 근거 부족으로
        # 점수를 못 낸 것을 챗봇이 '결과 없음' 으로 뭉개면 안 된다.
        if isinstance(data, dict) and "status" in data and "result" in data:
            status = data.get("status")
            if status != "success":
                mapped = ("insufficient_data"
                          if status == "insufficient_data" else "not_available")
                return {"status": mapped, "data": None,
                        "evidence": [], "source_status": status}
            payload = dict(data.get("result") or {})
            payload["_model"] = data.get("model")
            return {"status": "success", "data": payload,
                    "evidence": _evidence_of(data, key)}

        return {"status": "success", "data": data,
                "evidence": _evidence_of(data, key)}


def _evidence_of(data: Any, source: str) -> List[Dict[str, Any]]:
    """근거 목록을 꺼내고 source 를 채운다. 없으면 빈 목록."""
    if not isinstance(data, dict):
        return []
    raw = data.get("evidence") or []
    out: List[Dict[str, Any]] = []
    for e in raw:
        if isinstance(e, dict):
            item = dict(e)
            item.setdefault("source", source)
            out.append(item)
    return out
