"""SIM-R support_type 라우팅.

DB 를 요구하지 않는다. 라우팅은 어느 단계를 골랐는가, 어떤 SQL 을 만들었는가,
무엇을 기록했는가로 판정할 수 있고, 그 셋이 실제로 틀리기 쉬운 지점이다.

단계별 후보 수의 실측 근거는 ml/tools/simr_routing_coverage.py 에 있다.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.ml import pipeline_bridge
from app.services.retrieval.retrieval import (
    GRADE_POLICY_ANY,
    GRADE_POLICY_TRUSTED,
    GRADE_POLICY_TRUSTED_REFERENCE,
    ROUTING_EXPANDED,
    ROUTING_HARD,
    ROUTING_SOFT,
    ROUTING_UNFILTERED,
    TOP_K,
    RoutingDecision,
    _corpus_counts,
    _filter_snapshot,
    _plan_routing,
    _search_candidates,
    _search_filter,
    retrieve_top_five,
)


class RecordingConnection:
    """실행한 SQL 과 바인드 파라미터만 붙잡는다."""

    def __init__(self, row=None):
        self.sql = ""
        self.params = {}
        self._row = row or {}

    def execute(self, clause, params=None):
        self.sql = " ".join(str(clause).split())
        self.params = params or {}
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(all=lambda: [], one=lambda: self._row)
        )


def counts(stage_1, stage_2, stage_3, corpus=1570):
    return {
        "corpus_count": corpus,
        "stage_1": stage_1,
        "stage_2": stage_2,
        "stage_3": stage_3,
    }


def plan(support_type, trust_grade, stage_counts):
    return _plan_routing(
        support_type=support_type, trust_grade=trust_grade, counts=stage_counts
    )


def search_sql(**filter_kwargs):
    connection = RecordingConnection()
    _search_candidates(
        connection, inspection_embedding_id=1, profile_id=2, **filter_kwargs
    )
    return connection.sql


def sql_for(support_type, trust_grade, stage_counts):
    return search_sql(**_search_filter(plan(support_type, trust_grade, stage_counts)))


SUPPORT_TYPE_PREDICATE = "AND av.support_type = :support_type"
GRADE_PREDICATE = "AND av.support_type_grade = ANY(CAST(:grades AS text[]))"
SUPPORT_TYPE_ORDER = "ORDER BY (av.support_type IS DISTINCT FROM :support_type)"


# --------------------------------------------------------------- 단계 선택


def test_trusted_request_settles_at_stage_one_when_trusted_labels_suffice():
    """실측 사업화: trusted 57건. 1단계에서 끝나고 코퍼스가 457에서 57 로 줄어든다."""
    routing = plan("사업화", "trusted", counts(57, 250, 457))
    assert routing.mode == ROUTING_HARD
    assert routing.candidate_grade_policy == GRADE_POLICY_TRUSTED
    assert routing.fallback_stage == 1
    assert routing.fallback_used is False
    assert routing.after_count == 57


def test_trusted_request_expands_when_trusted_labels_are_too_few():
    """실측 성능인증: trusted 1건이라 2단계(trusted+reference 7건)로 넓힌다."""
    routing = plan("성능인증", "trusted", counts(1, 7, 11))
    assert routing.mode == ROUTING_EXPANDED
    assert routing.candidate_grade_policy == GRADE_POLICY_TRUSTED_REFERENCE
    assert routing.fallback_stage == 2
    assert routing.fallback_used is True
    assert routing.after_count == 7


def test_trusted_request_expands_to_every_grade_before_giving_up():
    """실측 SW·솔루션: 1건 / 3건 / 5건. 3단계에서 겨우 K 를 채운다."""
    routing = plan("SW·솔루션", "trusted", counts(1, 3, 5))
    assert routing.mode == ROUTING_EXPANDED
    assert routing.candidate_grade_policy == GRADE_POLICY_ANY
    assert routing.fallback_stage == 3
    assert routing.after_count == 5


def test_trusted_request_gives_up_when_the_label_has_too_few_announcements():
    """실측 보증 3건 / 상담 2건. 등급을 다 열어도 K 를 못 채운다."""
    routing = plan("보증", "trusted", counts(0, 1, 3))
    assert routing.mode == ROUTING_UNFILTERED
    assert routing.fallback_stage == 4
    assert routing.fallback_used is True
    # 좁히기를 포기했으므로 필터 후라는 수가 없다. 0 은 후보가 없었다는 뜻이다.
    assert routing.after_count is None


def test_stage_boundary_is_at_top_k():
    """K 를 정확히 채우면 그 단계에서 멈춘다. 경계에서 한 단계 더 내려가지 않는다."""
    assert plan("교육훈련", "trusted", counts(TOP_K, 15, 20)).fallback_stage == 1
    assert plan("교육훈련", "trusted", counts(TOP_K - 1, 15, 20)).fallback_stage == 2


def test_reference_request_never_climbs_the_ladder():
    """soft 는 자르지 않으므로 후보가 모자랄 일이 없다."""
    routing = plan("보증", "reference", counts(0, 1, 3))
    assert routing.mode == ROUTING_SOFT
    assert routing.candidate_grade_policy == GRADE_POLICY_ANY
    assert routing.fallback_stage is None
    assert routing.fallback_used is False
    assert routing.after_count == 3


def test_hold_request_is_unfiltered():
    routing = plan("연구개발", "hold", counts(19, 66, 91))
    assert routing.mode == ROUTING_UNFILTERED
    assert routing.candidate_grade_policy is None
    assert routing.after_count is None


@pytest.mark.parametrize("support_type", [None, "", "   "])
def test_missing_label_is_unfiltered_even_for_a_trusted_request(support_type):
    routing = plan(support_type, "trusted", counts(0, 0, 0))
    assert routing.mode == ROUTING_UNFILTERED
    assert routing.request_support_type is None


def test_unknown_trust_grade_is_rejected():
    """모르는 등급을 unfiltered 로 흘리면 라우팅이 꺼진 것을 아무도 모른다.

    engine 자리에 object() 를 넘긴다. 등급 검사가 DB 를 건드리기 전에 걸리지
    않으면 이 테스트는 ValueError 가 아닌 다른 예외로 실패한다.
    """
    with pytest.raises(ValueError, match="trust grade"):
        asyncio.run(
            retrieve_top_five(
                object(),
                object(),
                case_id=1,
                input_text="검색 입력",
                support_type="연구개발",
                trust_grade="TRUSTED",
            )
        )


# ----------------------------------------------------------------- SQL 생성


def test_stage_one_filters_by_label_and_grade():
    sql = sql_for("사업화", "trusted", counts(57, 250, 457))
    assert SUPPORT_TYPE_PREDICATE in sql
    assert GRADE_PREDICATE in sql
    assert SUPPORT_TYPE_ORDER not in sql


def test_stage_three_filters_by_label_only():
    """전체 등급 단계에서는 등급 조건을 걸지 않는다. 걸면 같은 뜻의 조건이 는다."""
    sql = sql_for("SW·솔루션", "trusted", counts(1, 3, 5))
    assert SUPPORT_TYPE_PREDICATE in sql
    assert GRADE_PREDICATE not in sql


def test_soft_routing_orders_without_filtering():
    """soft 는 잘라내지 않는다. 후보가 적은 라벨에서도 Top-5 가 채워져야 한다."""
    sql = sql_for("연구개발", "reference", counts(19, 66, 91))
    assert SUPPORT_TYPE_PREDICATE not in sql
    assert SUPPORT_TYPE_ORDER in sql


def test_unfiltered_routing_touches_neither():
    sql = sql_for("연구개발", "hold", counts(19, 66, 91))
    assert SUPPORT_TYPE_PREDICATE not in sql
    assert SUPPORT_TYPE_ORDER not in sql


def test_soft_routing_uses_is_distinct_from_for_unlabelled_rows():
    """`<>` 를 쓰면 라벨이 NULL 인 행의 정렬 키가 NULL 이 된다.

    backfill 전에는 거의 모든 행이 NULL 이라 그 차이가 결과 전체를 바꾼다.
    """
    sql = sql_for("연구개발", "reference", counts(19, 66, 91))
    assert "IS DISTINCT FROM" in sql
    assert "av.support_type <> :support_type" not in sql


def test_corpus_counts_measures_every_stage_in_one_scan():
    """단계마다 따로 물으면 그 사이에 라벨이 채워져 단계와 검색 대상이 어긋난다."""
    connection = RecordingConnection(
        {"corpus_count": 1570, "stage_1": 57, "stage_2": 250, "stage_3": 457}
    )
    result = _corpus_counts(connection, profile_id=1, support_type="사업화")
    assert result == {
        "corpus_count": 1570,
        "stage_1": 57,
        "stage_2": 250,
        "stage_3": 457,
    }
    assert connection.sql.count("count(*)") == 4


# -------------------------------------------------------------- 스냅샷 기록


def test_filter_snapshot_records_the_routing():
    routing = plan("성능인증", "trusted", counts(1, 7, 11))
    snapshot = json.loads(_filter_snapshot(7, routing))
    assert snapshot["routing"] == {
        "request_support_type": "성능인증",
        "request_trust_grade": "trusted",
        "candidate_grade_policy": "trusted+reference",
        "routing_mode": "expanded",
        "before_count": 1570,
        "after_count": 7,
        "fallback_stage": 2,
        "fallback_used": True,
    }
    # 기존 필터 조건도 그대로 남아야 한다. 라우팅은 덧붙는 것이지 대체가 아니다.
    assert snapshot["embedding_profile_id"] == 7
    assert snapshot["search_status"] == ["OPEN", "UNKNOWN"]


def test_snapshot_keeps_korean_labels_readable():
    routing = RoutingDecision(
        request_support_type="기술·IP평가",
        request_trust_grade="trusted",
        mode=ROUTING_EXPANDED,
        candidate_grade_policy=GRADE_POLICY_TRUSTED_REFERENCE,
        fallback_stage=2,
        fallback_used=True,
        before_count=1570,
        after_count=7,
    )
    raw = _filter_snapshot(1, routing)
    assert "기술·IP평가" in raw
    assert json.loads(raw)["routing"]["request_support_type"] == "기술·IP평가"


# ------------------------------------------------------- Model 1 에서 라우팅 입력


@pytest.mark.parametrize(
    ("ml_results", "expected"),
    [
        (None, (None, "hold")),
        ({}, (None, "hold")),
        # Model 1 이 실패하면 라우팅 입력이 없다.
        ({"model_1": {"status": "failed", "error": {}}}, (None, "hold")),
        (
            {
                "model_1": {
                    "status": "success",
                    "result": {
                        "support_type": "연구개발",
                        "confidence": 0.9955,
                        "trust_grade": "trusted",
                    },
                }
            },
            ("연구개발", "trusted"),
        ),
        # 성공인데 라벨이 비면 좁히지 않는다.
        (
            {"model_1": {"status": "success", "result": {"support_type": ""}}},
            (None, "hold"),
        ),
    ],
)
def test_routing_inputs(ml_results, expected):
    assert pipeline_bridge.routing_inputs(ml_results) == expected
