"""Context Router — 질문에 필요한 **저장된 분석 결과 섹션**을 고른다.

챗봇은 멀티에이전트가 아니다. CPL·FIT·Retrieval·SIM·Model 1·2·3 은 분석 단계에서
이미 실행돼 결과가 저장돼 있고, 챗봇은 그것을 **다시 실행하지 않는다.**

    [분석 단계]  요청서 → Parser → CPL/FIT/Retrieval/SIM/Model 1·2·3 → 결과 저장
    [챗봇 단계]  질문 → 필요한 결과 섹션 선택 → Chat Context → 하나의 Chat LLM

`context_scope()` 가 고르는 것은 그 저장된 결과의 섹션이다. `["fit", "model2"]`
는 "FIT 과 Model 2 를 돌린다" 가 아니라 "이미 저장된 FIT 결과와 Model 2 결과를
이번 질문의 Context 에 상세히 싣는다" 는 뜻이다.

리포트를 통째로 실으면 두 가지가 같이 망가진다.

1. **양** — CPL 13항목·FIT 7관계·유사공고 5건이 각자 근거 원문을 통째로 들고
   있다. 같은 문장이 여러 섹션에 복사돼 들어가, 질문 한 번에 리포트 전체가 몇
   번씩 중복돼 올라간다.
2. **혼동** — 어느 단계가 낸 값인지 프롬프트 안에서 구분되지 않는다. 서로 다른
   판정을 섞지 않으려면 섹션이 갈려 있어야 한다.

그래서 여기서 세 가지를 한다.

    섹션별로 자른다        report.cpl / fit / retrieval / sim / model1..3 / summary
    근거는 한 곳에 모은다   evidence[] 에 evidence_id 로 한 번만 싣고,
                          각 섹션은 그 id 만 가리킨다
    질문에 필요한 것만 상세  context_scope — 나머지는 상태·개수만.
                          **빼지는 않는다** ("전체적으로 가장 중요한 문제가
                          뭐야" 같은 질문이 특정 어휘를 안 쓰기 때문에, 안 걸린
                          섹션을 지우면 답이 반쪽이 된다.)

사용자는 CPL·FIT 같은 내부 이름을 모른다. 업무 자연어로 물어도 필요한 섹션이
골라져야 한다 — 아래 어휘 표가 그것을 맡는다.
"""
import re
from typing import Any, Iterable

from .provenance import PROVENANCE_KEYS, strip_internal_values
from .schema import SECTION_KEYS

# Context 상한. 프롬프트가 무한정 커지지 않게 하는 선이지, 정확도 기준이 아니다.
#
# 160 은 실측에서 나온 값이다. CPL 13항목 × 근거 3개 + FIT 7관계 × 2 + SIM 후보
# 5건 × 4축 × 2 = 87건이 "전체 리포트를 근거와 함께 요약해줘" 한 번에 실린다.
# 80 이면 그 질문에서 잘렸다. 두 배로 두어 여유를 남긴다.
MAX_EVIDENCE_ITEMS = 160
MAX_EXCERPT_CHARS = 400
MAX_SIM_CANDIDATES = 5


# --------------------------------------------------------------- 질문 → 초점
#
# 한 질문이 여러 Agent 를 가리킬 수 있다. Intent 하나를 고르지 않는다 —
# "지원규모가 적정해?" 는 FIT(지원내용-지원규모 정합)과 Model 2(예측금액)를
# 동시에 묻는 질문이고, 둘 중 하나만 실으면 답이 틀린다.
_SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 요청자료 완전성·기초구조 점검. 사용자는 "확인이 필요한 부분", "빠진 것"
    # 이라고 묻지 CPL 이라고 하지 않는다.
    # `요청서` 는 넣지 않는다 — 이 서비스의 질문은 거의 다 요청서 이야기라,
    # "이 요청서 어때?" 같은 범위 없는 질문까지 한 섹션으로 좁혀 버린다.
    "cpl": (
        "cpl", "요청자료", "누락", "빠진", "빠뜨린", "확인이 필요", "확인 필요",
        "확인필요", "확인해야", "확인할", "미확인", "완전성", "기재", "적혀",
        "적혀 있", "항목", "체크", "서식", "보완이 필요", "보완해야", "채워",
        "작성이 안", "미비", "부족한", "모자란",
    ),
    # 내부 정합성. "내용이 서로 맞지 않는", "앞뒤가 안 맞" 처럼 묻는다.
    "fit": (
        "fit", "정합", "일치", "연결", "맞지 않", "안 맞", "안맞", "부적합",
        "서로 맞", "앞뒤", "모순", "충돌", "어긋",
        "목적", "지원대상", "지원 대상", "지원내용", "지원 내용", "관계",
        "논리", "구조",
    ),
    # 유사공고 검색. "비슷한 사업", "기존 사업" 이 사용자 어휘다.
    "retrieval": (
        "유사사업", "유사 사업", "비슷한", "유사한", "닮은", "검색", "후보",
        "공고", "기존 사업", "기존사업", "다른 사업", "기존에 있",
    ),
    # 유사공고 비교. 맨 `차이` 는 넣지 않는다 — "요청금액과 예측금액의 차이"
    # (Model 2)까지 끌어온다.
    "sim": (
        "sim", "중복", "차이점", "비교", "공통점", "유사도", "유사", "겹치",
        "어떤 차이", "무슨 차이", "다른 점", "다른점",
    ),
    "model1": (
        "지원유형", "지원 유형", "지원성격", "지원 성격", "분류", "유형",
        "어떤 사업으로", "성격이", "model 1", "model1", "모델 1",
    ),
    "model2": (
        "금액", "예측금액", "예측 금액", "지원규모", "지원 규모", "한도",
        "예산", "기업당", "과제당", "얼마", "규모가",
        "model 2", "model2", "모델 2",
    ),
    "model3": (
        "이례", "특이", "이상치", "튀는", "비정형", "드문", "희귀",
        "anomaly", "dif", "일반적인 사업", "보통과", "남다른",
        "model 3", "model3", "모델 3",
    ),
    # 전체를 묻는 어휘. 혼자 걸렸을 때만 전 섹션으로 넓힌다.
    "summary": (
        "종합", "전체", "전반", "요약", "정리해", "총평", "한눈에",
        "가장 중요", "중요한", "핵심", "다섯 줄", "5줄", "세 가지", "3가지",
    ),
}

# 수정·보완 제안을 요청하는 질문(명세 7.4). 답변에 `suggested_revision` 을
# 채울지 말지를 여기서 정한다 — 물어보지도 않았는데 수정안을 붙이면 사용자가
# 그것을 확정된 조치로 읽는다(명세 8).
_REVISION_KEYWORDS: tuple[str, ...] = (
    "수정", "보완", "고치", "고쳐", "다시 작성", "재작성", "바꾸", "바꿔",
    "제안", "문구", "어떻게 쓰", "어떻게 적", "어떻게 하면", "개선",
    "다듬", "표현", "작성해",
    # 후속 질문에서 실제로 나온 표현. "다시 작성" 만으로는 "다시 써줘" 를
    # 놓친다 — 사용자는 그렇게 줄여 말한다.
    "다시 써", "다시 쓰", "새로 써", "문장으로",
)

_WS = re.compile(r"\s+")


def _normalize(question: str) -> str:
    return _WS.sub(" ", (question or "").strip().lower())


def _keyword_hits(text: str) -> set[str]:
    return {
        section
        for section, keywords in _SECTION_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    }


def context_scope(question: str, previous_question: str | None = None) -> set[str]:
    """질문에 필요한 **저장된 결과 섹션**들. 하나도 안 걸리면 전부를 싣는다.

    Agent 를 실행하지 않는다. 이미 저장된 결과 중 어느 것을 이번 Context 에
    상세히 실을지를 고를 뿐이다.

    안 걸렸다는 것은 "이 요청서 어때?" 처럼 범위를 안 준 질문이라는 뜻이지,
    아무것도 필요 없다는 뜻이 아니다.

    `previous_question` 은 직전 사용자 질문이다. "왜 그렇게 판단했어?",
    "근거를 보여줘" 처럼 **이어지는 질문에는 범위 어휘가 없다.** 그것을 범위
    없는 질문으로 보면 매 후속 질문마다 리포트 전체가 다시 올라간다.
    """
    text = _normalize(question)
    if not text:
        return set(SECTION_KEYS)

    hit = _keyword_hits(text)
    if not hit and previous_question:
        # 앞 질문의 범위를 물려받는다. summary 승격은 하지 않는다 —
        # 앞이 요약 요청이었다면 어차피 전부가 걸린다.
        hit = _keyword_hits(_normalize(previous_question))
    if not hit:
        return set(SECTION_KEYS)

    # 요약 어휘가 **혼자** 걸렸을 때만 전부를 본다. "확인이 필요한 부분만
    # 요약해줘" 는 요약 요청이지 전체 요청이 아니다.
    if hit == {"summary"}:
        return set(SECTION_KEYS)
    hit.discard("summary")
    # 유사사업 질문은 검색 결과와 비교 결과가 한 쌍이다. 한쪽만 실으면
    # "어떤 사업과 비슷해?" 에 제목만 있고 비교 내용이 없는 답이 나온다.
    if hit & {"retrieval", "sim"}:
        hit |= {"retrieval", "sim"}
    return hit


def wants_revision(question: str) -> bool:
    text = _normalize(question)
    return any(keyword in text for keyword in _REVISION_KEYWORDS)


# --------------------------------------------------------------- 근거 모음
class _EvidenceBook:
    """근거를 한 번만 싣고 각 섹션은 id 로 가리키게 한다.

    같은 문장이 CPL 항목·FIT 좌변·SIM 축에 동시에 근거로 붙는다. 그대로 실으면
    한 문장이 프롬프트에 세 번 들어가고, LLM 은 서로 다른 근거 셋으로 읽는다.
    """

    def __init__(self, limit: int = MAX_EVIDENCE_ITEMS) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._limit = limit
        # 상한에 걸려 싣지 못한 근거 수. 0 이 아니면 답변이 리포트의 모든
        # 근거를 본 것이 아니다 — 그 사실을 프롬프트에 알려야 한다.
        self._dropped = 0

    def add(self, evidence: dict[str, Any] | None, agent: str) -> str | None:
        if not isinstance(evidence, dict):
            return None
        reference = evidence.get("evidence_ref")
        if not isinstance(reference, str) or not reference:
            return None

        stored = self._items.get(reference)
        if stored is not None:
            if agent not in stored["agents"]:
                stored["agents"].append(agent)
            return reference
        if len(self._items) >= self._limit:
            # 상한을 넘으면 새 근거를 싣지 않는다. 이미 실린 것을 밀어내면
            # 앞쪽 Agent 의 근거가 조용히 사라진다. 대신 몇 건을 못 실었는지
            # 세어 둔다 — 조용히 빠지면 챗봇이 전부 본 것처럼 답한다.
            self._dropped += 1
            return None

        excerpt = str(evidence.get("excerpt") or "")
        if len(excerpt) > MAX_EXCERPT_CHARS:
            excerpt = excerpt[:MAX_EXCERPT_CHARS] + "…"
        self._items[reference] = {
            "evidence_id": reference,
            "agents": [agent],
            "source_side": evidence.get("source_side"),
            "field_code": evidence.get("field_code"),
            "axis_code": evidence.get("axis_code"),
            "page_no": evidence.get("page_no"),
            "excerpt": excerpt,
        }
        return reference

    def add_all(self, items: Iterable[Any], agent: str) -> list[str]:
        refs = []
        for item in items or ():
            reference = self.add(item, agent)
            if reference is not None:
                refs.append(reference)
        return refs

    def as_list(self) -> list[dict[str, Any]]:
        return list(self._items.values())

    def ids(self) -> set[str]:
        return set(self._items)

    @property
    def dropped(self) -> int:
        return self._dropped


def _counts(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------- Agent 별 투영
def _cpl_section(
    report: dict[str, Any], book: _EvidenceBook, full: bool
) -> dict[str, Any]:
    self_check = report.get("self_check") or {}
    items = self_check.get("items") or []
    if not full:
        return {
            "detail": "overview",
            "confirmed_count": self_check.get("confirmed_count"),
            "total_count": self_check.get("total_count"),
            "status_counts": _counts(item.get("status") for item in items),
            # 확인된 것은 PRESENT·NOT_APPLICABLE 둘이다(CplResult 의 셈법과
            # 같아야 한다). 나머지가 사용자가 보완해야 할 항목이다.
            "unconfirmed_field_codes": [
                item.get("field_code")
                for item in items
                if item.get("status") not in ("PRESENT", "NOT_APPLICABLE")
            ],
        }
    return {
        "detail": "full",
        "confirmed_count": self_check.get("confirmed_count"),
        "total_count": self_check.get("total_count"),
        "items": [
            {
                "field_code": item.get("field_code"),
                "status": item.get("status"),
                "reason_code": item.get("reason_code"),
                "explanation": item.get("explanation"),
                "evidence_ids": book.add_all(item.get("occurrences"), "CPL"),
            }
            for item in items
        ],
        "warnings": self_check.get("warnings") or [],
    }


def _fit_section(
    report: dict[str, Any], book: _EvidenceBook, full: bool
) -> dict[str, Any]:
    structural = report.get("structural_consistency") or {}
    relations = structural.get("relations") or []
    score = structural.get("score") or {}
    if not full:
        return {
            "detail": "overview",
            "module_status": structural.get("module_status"),
            "assessable_count": score.get("assessable_count"),
            "total_count": score.get("total_count"),
            "status_counts": _counts(item.get("status") for item in relations),
            "relations": [
                {
                    "relation_id": item.get("relation_id"),
                    "status": item.get("status"),
                }
                for item in relations
            ],
        }
    return {
        "detail": "full",
        "module_status": structural.get("module_status"),
        "assessable_count": score.get("assessable_count"),
        "total_count": score.get("total_count"),
        "relations": [
            {
                "relation_id": item.get("relation_id"),
                "status": item.get("status"),
                "summary": item.get("summary"),
                "reason_code": item.get("reason_code"),
                "left_evidence_ids": book.add_all(item.get("left_evidence"), "FIT"),
                "right_evidence_ids": book.add_all(item.get("right_evidence"), "FIT"),
            }
            for item in relations
        ],
        "warnings": structural.get("warnings") or [],
    }


def _retrieval_section(report: dict[str, Any], full: bool) -> dict[str, Any]:
    """검색 결과(어떤 공고가 후보로 뽑혔나). 축별 비교는 SIM 쪽이다.

    명세 5.3 은 Retrieval 과 SIM 을 함께 쓰지만 답변에서 둘을 구분해야 한다 —
    "가장 유사한 사업" 은 검색 순위이고 "가장 큰 차이점" 은 비교 결과다.
    """
    candidates = (report.get("similar_candidates") or [])[:MAX_SIM_CANDIDATES]
    return {
        "detail": "full" if full else "overview",
        "candidate_count": len(report.get("similar_candidates") or []),
        "candidates": [
            {
                "rank": item.get("rank"),
                "announcement_id": item.get("announcement_id"),
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                # 유사도 점수는 담지 않는다. 결과 화면도 쓰지 않는 값이라
                # (ReportSimCandidateDisplay), 챗봇만 숫자를 말하면 화면과
                # 답변이 어긋난다. 순위는 남긴다 — 점수가 아니라 순서다.
                "review_grade": item.get("review_grade"),
            }
            for item in candidates
        ],
    }


_SIM_AXES = ("purpose", "target", "content", "delivery")


def _sim_section(
    report: dict[str, Any], book: _EvidenceBook, full: bool
) -> dict[str, Any]:
    candidates = (report.get("similar_candidates") or [])[:MAX_SIM_CANDIDATES]
    if not candidates:
        return {"detail": "overview", "status": "not_available", "candidates": []}
    if not full:
        return {
            "detail": "overview",
            "candidates": [
                {
                    "rank": item.get("rank"),
                    "title": item.get("title"),
                    "review_grade": item.get("review_grade"),
                    "comparison_summary": item.get("comparison_summary"),
                    "axis_statuses": {
                        axis: ((item.get("axes") or {}).get(axis) or {}).get("status")
                        for axis in _SIM_AXES
                    },
                }
                for item in candidates
            ],
        }
    return {
        "detail": "full",
        "candidates": [
            {
                "rank": item.get("rank"),
                "title": item.get("title"),
                "review_grade": item.get("review_grade"),
                "comparison_summary": item.get("comparison_summary"),
                "axes": {
                    axis: _sim_axis(
                        (item.get("axes") or {}).get(axis) or {}, book
                    )
                    for axis in _SIM_AXES
                },
                "warnings": item.get("warnings") or [],
            }
            for item in candidates
        ],
    }


def _sim_axis(axis: dict[str, Any], book: _EvidenceBook) -> dict[str, Any]:
    return {
        "axis_id": axis.get("axis_id"),
        "status": axis.get("status"),
        "summary": axis.get("summary"),
        "common_points": axis.get("common_points") or [],
        "differences": axis.get("differences") or [],
        "reason_code": axis.get("reason_code"),
        "request_evidence_ids": book.add_all(axis.get("request_evidence"), "SIM"),
        "candidate_evidence_ids": book.add_all(axis.get("candidate_evidence"), "SIM"),
    }


# --------------------------------------------------------------- ML 모델
# `models` / `dif` 는 ML Orchestrator 의 envelope 을 그대로 담고 있다.
# status 가 success 가 아니면 result 는 None 이고, 그 이유(failed /
# not_available / insufficient_data)를 뭉개면 "모델이 고장났다" 와 "선행 결과가
# 없어 못 돌렸다" 가 같은 문구로 안내된다.


# Loader 가 넘기는 status 별칭. 저장 구조가 아직 확정되지 않아 어느 이름으로
# 올지 모른다. 챗봇이 읽는 이름은 하나로 고정하고 입구에서 흡수한다.
_MODEL_STATUS_ALIASES = {
    "success": "success",
    "available": "success",
    "ok": "success",
    "insufficient_data": "insufficient_data",
    "insufficient_evidence": "insufficient_data",
}


def _model_section(envelope: Any, full: bool) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        return {"detail": "overview", "status": "not_available", "result": None}

    raw_status = envelope.get("status")
    status = _MODEL_STATUS_ALIASES.get(str(raw_status), "not_available")
    if status != "success":
        # '실행 못 했다' 와 '실행은 됐지만 근거가 모자랐다' 를 구분해 둔다.
        # 뭉개면 챗봇이 두 경우를 같은 문구로 안내한다.
        return {
            "detail": "overview",
            "status": status,
            "source_status": raw_status,
            "result": None,
            "error": envelope.get("error"),
        }

    result = envelope.get("result") or {}
    metadata = envelope.get("metadata") or {}
    provenance = {
        key: metadata[key] for key in PROVENANCE_KEYS if key in metadata
    }
    if not full:
        # 상세가 아닐 때는 스칼라 값만 남긴다. 중첩 구조(축별 거리·확률 분포)는
        # 그 자체로 설명이 필요해서, 초점이 아닌 질문에 실으면 답이 옆길로 샌다.
        result = {
            key: value
            for key, value in result.items()
            if not isinstance(value, (dict, list))
        }
    return {
        "detail": "full" if full else "overview",
        "status": "success",
        "result": result,
        "provenance": provenance,
    }


def _summary_section(report: dict[str, Any]) -> dict[str, Any]:
    """항상 싣는다. 어떤 질문이든 "전체 중 어디쯤인가" 를 알아야 답이 선다."""
    issues = report.get("review_issues") or []
    return {
        "detail": "full",
        "quality": report.get("quality"),
        "module_summary": report.get("module_summary"),
        "review_issue_counts": _counts(item.get("source") for item in issues),
        "review_issues": [
            {
                "issue_id": item.get("issue_id"),
                "source": item.get("source"),
                "reference_id": item.get("reference_id"),
                "status": item.get("status"),
                "summary": item.get("summary"),
            }
            for item in issues
        ],
        "warnings": report.get("warnings") or [],
    }


# --------------------------------------------------------------- 진입점
def build_chat_context(
    report_json: dict[str, Any],
    question: str,
    conversation: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """분석 리포트 + 질문 → 프롬프트에 실을 Context 한 벌.

    `report_json` 은 **JSON 으로 직렬화된 dict** 다. backend 의 pydantic 모델을
    받지 않는다 — 이 패키지가 backend 스키마를 import 하면 의존 방향이 거꾸로
    선다. 호출부가 `model_dump(mode="json")` 해서 넘긴다.

    `report` 아래 Agent 키는 명세에 적힌 이름(cpl/fit/retrieval/sim/model1..3/
    summary)을 그대로 쓴다. 저장 스키마의 이름(self_check·structural_consistency·
    dif)을 그대로 노출하면 LLM 이 그 이름으로 답한다.
    """
    raw = report_json or {}
    scope = context_scope(question, _last_user_question(conversation))
    book = _EvidenceBook()

    models = raw.get("models") or {}
    sections = {
        "cpl": _cpl_section(raw, book, "cpl" in scope),
        "fit": _fit_section(raw, book, "fit" in scope),
        "retrieval": _retrieval_section(raw, "retrieval" in scope),
        "sim": _sim_section(raw, book, "sim" in scope),
        "model1": _model_section(models.get("model_1"), "model1" in scope),
        "model2": _model_section(models.get("model_2"), "model2" in scope),
        "model3": _model_section(raw.get("dif"), "model3" in scope),
        "summary": _summary_section(raw),
    }

    context = {
        "analysis_id": str((raw.get("case") or {}).get("case_id")),
        "case": {
            "title": (raw.get("case") or {}).get("title"),
            "completed_at": (raw.get("case") or {}).get("completed_at"),
        },
        "question": question,
        # 어떤 결과 섹션을 상세로 실었는지 밝힌다. 상세가 아닌 섹션을 근거로
        # 단정하지 말라고 프롬프트가 이 값을 참조한다. Agent 를 실행한 목록이
        # 아니다 — 저장된 결과 중 고른 것이다.
        "context_scope": sorted(scope),
        "revision_requested": wants_revision(question),
        "report": sections,
        "evidence": book.as_list(),
        # 근거를 전부 싣지 못했으면 밝힌다. 답변이 "확인된 근거는 이것뿐" 이라고
        # 말해도 되는지가 여기서 갈린다.
        "evidence_truncated": book.dropped > 0,
        "evidence_omitted_count": book.dropped,
        "conversation": conversation or [],
    }
    # 내부 진단값(percentile·confidence·probability·raw_score)은 여기서 지운다.
    # 프롬프트로만 막으면 값은 이미 실려 있고, 모델이 규칙을 어기는 순간 그대로
    # 새어 나간다. Context 에 없으면 샐 값 자체가 없다.
    return strip_internal_values(context)


def _last_user_question(conversation: list[dict[str, str]] | None) -> str | None:
    for item in reversed(conversation or []):
        if str(item.get("role", "")).upper() == "USER":
            return item.get("content")
    return None


def context_evidence_ids(context: dict[str, Any]) -> set[str]:
    """답변이 인용해도 되는 근거 id. 이 밖을 인용하면 지어낸 것이다."""
    return {
        str(item.get("evidence_id"))
        for item in context.get("evidence") or []
        if item.get("evidence_id")
    }
