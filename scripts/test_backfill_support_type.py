"""backfill 계획 수립부. DB 없이 검증할 수 있는 만큼만 본다.

실제 UPDATE 는 Postgres 가 있어야 확인된다. 대신 '무엇을 쓸지 정하는' 부분은
전부 순수 함수라 여기서 볼 수 있고, 틀리면 조용히 남의 라벨을 덮는 쪽이라
가장 위험한 지점이기도 하다.

    python -m pytest scripts/test_backfill_support_type.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill_support_type import (  # noqa: E402
    LABEL_COLUMNS,
    Label,
    build_plan,
    check_schema,
    projected_pairs,
)


class FakeConnection:
    def __init__(self, table_exists, columns):
        self._table_exists = table_exists
        self._columns = columns

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def scalar(self, _clause):
        return self._table_exists

    def execute(self, _clause):
        return [(column,) for column in self._columns]


class FakeEngine:
    """check_schema 가 information_schema 에 무엇을 묻는지가 아니라, 답을 받고
    무엇을 판단하는지를 본다."""

    def __init__(self, table_exists=True, columns=()):
        self._table_exists = table_exists
        self._columns = columns

    def connect(self):
        return FakeConnection(self._table_exists, self._columns)


# --------------------------------------------------------------- preflight


def test_preflight_passes_when_every_label_column_exists():
    engine = FakeEngine(columns=("id", "pblanc_nm", *LABEL_COLUMNS))
    assert check_schema(engine) == []


def test_preflight_reports_a_missing_table():
    assert check_schema(FakeEngine(table_exists=False)) == [
        "sims.announcement_version 테이블이 없다"
    ]


def test_preflight_names_every_missing_column_at_once():
    """하나만 알려 주면 마이그레이션을 안 돌린 것을 컬럼 다섯 번에 걸쳐 알게 된다."""
    engine = FakeEngine(columns=("id", "support_type", "support_type_grade"))
    problems = check_schema(engine)
    assert len(problems) == 1
    assert "support_type_confidence" in problems[0]
    assert "support_type_model_version" in problems[0]
    assert "support_type_classified_at" in problems[0]
    # 있는 컬럼은 부르지 않는다.
    assert "support_type_grade," not in problems[0]


def test_preflight_treats_the_five_columns_as_one_unit():
    """스키마 CHECK 가 부분 NULL 을 막으므로 하나만 없어도 backfill 이 성립하지 않는다."""
    for column in LABEL_COLUMNS:
        present = [c for c in LABEL_COLUMNS if c != column]
        assert check_schema(FakeEngine(columns=present)), column


def label(pblanc_id="PBLN_1", support_type="연구개발", confidence=0.99,
          grade="trusted", model_version="1.0"):
    return Label(pblanc_id, support_type, confidence, grade, model_version)


def target(announcement_version_id, pblanc_id, support_type=None, grade=None):
    return {
        "announcement_version_id": announcement_version_id,
        "pblanc_id": pblanc_id,
        "support_type": support_type,
        "support_type_grade": grade,
    }


# ------------------------------------------------------------------ 라벨 검증


def test_label_rejects_an_unknown_grade():
    """스키마 CHECK 가 막기 전에 여기서 걸려야 배치 중간에 죽지 않는다."""
    with pytest.raises(ValueError):
        label(grade="신뢰")


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_label_rejects_confidence_outside_the_unit_interval(confidence):
    with pytest.raises(ValueError):
        label(confidence=confidence)


def test_label_rejects_a_blank_support_type():
    with pytest.raises(ValueError):
        label(support_type="   ")


# ------------------------------------------------------------------ 계획 수립


def test_existing_labels_are_left_alone_by_default():
    targets = [target(1, "PBLN_1", "컨설팅", "reference")]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=False)
    assert plan["planned"] == []
    assert [row["announcement_version_id"] for row in plan["skipped"]] == [1]


def test_overwrite_replaces_existing_labels():
    targets = [target(1, "PBLN_1", "컨설팅", "reference")]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=True)
    assert [row["announcement_version_id"] for row in plan["planned"]] == [1]
    assert plan["skipped"] == []


def test_unlabelled_rows_are_planned():
    targets = [target(1, "PBLN_1")]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=False)
    assert plan["planned"][0]["announcement_version_id"] == 1
    assert plan["planned"][0]["label"].support_type == "연구개발"


def test_announcements_without_a_prediction_are_reported_not_guessed():
    """라벨이 없는 공고는 비워 둔다. 지어내면 사다리가 잘못된 후보를 채운다."""
    targets = [target(1, "PBLN_1"), target(2, "PBLN_2")]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=False)
    assert [row["pblanc_id"] for row in plan["unlabelled"]] == ["PBLN_2"]


def test_predictions_with_no_matching_announcement_are_reported():
    """parquet 은 1,570건인데 DB 공고와 겹치지 않을 수 있다."""
    targets = [target(1, "PBLN_1")]
    labels = {"PBLN_1": label(), "PBLN_9": label(pblanc_id="PBLN_9")}
    plan = build_plan(targets, labels, overwrite=False)
    assert plan["unmatched"] == ["PBLN_9"]


def test_rerunning_after_a_partial_write_plans_only_what_is_left():
    """중간에 끊긴 뒤 다시 돌리면 이미 채워진 행은 계획에서 빠진다."""
    targets = [target(1, "PBLN_1", "연구개발", "trusted"), target(2, "PBLN_2")]
    labels = {"PBLN_1": label(), "PBLN_2": label(pblanc_id="PBLN_2")}
    plan = build_plan(targets, labels, overwrite=False)
    assert [row["announcement_version_id"] for row in plan["planned"]] == [2]


# --------------------------------------------------------------- 사후 코퍼스


def test_projection_mixes_new_labels_with_the_ones_already_stored():
    """dry-run 이 보여 주는 정착률은 backfill 이 끝난 뒤의 코퍼스 기준이어야 한다."""
    targets = [
        target(1, "PBLN_1"),                          # 이번에 채운다
        target(2, "PBLN_2", "컨설팅", "hold"),          # 이미 있다
        target(3, "PBLN_3"),                          # 라벨이 없다
    ]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=False)
    assert sorted(projected_pairs(targets, plan)) == [
        ("연구개발", "trusted"),
        ("컨설팅", "hold"),
    ]


def test_projection_excludes_rows_that_stay_unlabelled():
    """라벨이 없는 행은 사다리에 걸리지 않으므로 분모에서도 빠져야 한다."""
    targets = [target(1, "PBLN_1"), target(2, "PBLN_2")]
    plan = build_plan(targets, {"PBLN_1": label()}, overwrite=False)
    assert projected_pairs(targets, plan) == [("연구개발", "trusted")]
