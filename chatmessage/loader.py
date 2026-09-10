"""분석 결과를 챗봇 입력 dict 로 가져오는 경계.

    실제 저장소            Loader              chatmessage
    ─────────────   →   load()   →   build_chat_context()

**이 파일이 존재하는 이유는 저장 구조가 아직 정해지지 않았기 때문이다.**
Model 1·2·3 의 영속 저장 위치가 확정되지 않았고, 나중에 백엔드가 바뀌면
읽는 곳도 바뀐다. 그때 갈아 끼우는 것은 Loader 구현 하나여야 하고
`context.py`·`prompt.py`·`grounding.py` 는 그대로여야 한다.

그래서 Loader 는 **입력 계약(dict)만** 지키면 된다. 어디서 어떻게 읽었는지는
챗봇이 알 필요가 없다.

입력 계약
--------
`build_chat_context()` 가 읽는 키는 다음과 같다. 없는 키는 그 Agent 가
`not_available` 로 내려앉을 뿐 오류가 아니다.

    case                     {case_id, title, completed_at}
    self_check               CPL — {confirmed_count, total_count, items[]}
    structural_consistency   FIT — {module_status, score, relations[]}
    similar_candidates       Retrieval/SIM — [{rank, title, source_url, axes{}}]
    models.model_1           Model 1 envelope
    models.model_2           Model 2 envelope
    dif                      Model 3 envelope
    module_summary           집계
    review_issues            종합 이슈 목록
    quality, warnings

ML envelope 은 `{"status": ..., "result": {...}, "metadata": {...}}` 다.
status 는 `success`/`available`/`ok`, `insufficient_data`/`insufficient_evidence`,
그 밖(=`not_available`)을 받는다. **저장이 아직 없으면 그냥 넣지 않으면 된다** —
챗봇이 `not_available` 로 답한다.

지금 있는 구현은 파일 두 개뿐이다. DB 를 읽는 Loader 는 백엔드가 저장 구조를
확정한 뒤에 백엔드 쪽에 붙인다(이 패키지는 DB 를 모른다).
"""
import json
from pathlib import Path
from typing import Any, Protocol


class AnalysisResultLoader(Protocol):
    """분석 결과 하나를 챗봇 입력 dict 로 돌려준다."""

    def load(self) -> dict[str, Any]: ...


class JsonFileLoader:
    """파일 하나를 그대로 읽는다. fixture 와 실제 분석 결과 JSON 이 같은 길이다.

    fixture 라고 다른 경로를 두지 않는다 — 검증에 쓰는 입력과 운영에서 오는
    입력의 모양이 갈리면, fixture 로 통과한 것이 실제로는 안 되는 일이 생긴다.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LoaderError(f"분석 결과 파일이 없다: {self.path}") from None
        except json.JSONDecodeError as error:
            raise LoaderError(f"JSON 을 읽지 못했다: {self.path} ({error})") from None
        if not isinstance(payload, dict):
            raise LoaderError(f"최상위가 객체여야 한다: {self.path}")
        return payload


class DictLoader:
    """이미 dict 로 들고 있을 때. 테스트와 호출부가 쓴다."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def load(self) -> dict[str, Any]:
        return self.payload


class LoaderError(RuntimeError):
    """분석 결과를 가져오지 못했다. 원인은 메시지에 담는다."""


# 입력에 무엇이 들어 있는지 한눈에 보기 위한 점검. Loader 를 새로 만들 때
# "이 dict 로 챗봇이 무엇을 답할 수 있나" 를 먼저 확인하는 용도다.
def describe(payload: dict[str, Any]) -> dict[str, str]:
    """Agent 별로 결과가 실렸는지 요약한다. 값 판단은 하지 않는다."""
    models = payload.get("models") or {}

    def ml(envelope: Any) -> str:
        if not isinstance(envelope, dict):
            return "not_available"
        return str(envelope.get("status") or "not_available")

    return {
        "cpl": "있음" if payload.get("self_check") else "없음",
        "fit": "있음" if payload.get("structural_consistency") else "없음",
        "retrieval/sim": (
            f"후보 {len(payload.get('similar_candidates') or [])}건"
        ),
        "model1": ml(models.get("model_1")),
        "model2": ml(models.get("model_2")),
        "model3": ml(payload.get("dif")),
        "summary": "있음" if payload.get("module_summary") else "없음",
    }
