"""기존 공고에 Model 1 지원유형 라벨을 채운다.

    python scripts/backfill_support_type.py                      # dry-run
    python scripts/backfill_support_type.py --write

기본이 dry-run 이다. `--write` 를 붙이지 않으면 DB 를 건드리지 않는다.

무엇을 채우는가
--------------
`sims.announcement_version` 의 다섯 컬럼을 한 묶음으로 채운다. 스키마 CHECK 가
전부 채워지거나 전부 비거나만 허용하므로 일부만 쓰는 경로는 없다.

    support_type
    support_type_confidence
    support_type_grade
    support_type_model_version
    support_type_classified_at

라벨을 어디서 가져오는가
----------------------
`--source existing-predictions` (기본)
    ml/data/processed/announcement_detail_with_support_type_v2.parquet 의
    공고 1,570건 예측을 그대로 쓴다. 같은 Model 1 이 이미 돌린 결과이므로
    다시 추론할 이유가 없다. DB 없이도 dry-run 이 된다.

`--source inference`
    DB 의 공고 원문으로 Model 1 을 돌린다. parquet 에 없는 공고를 채울 때
    쓴다. 읽을 원문이 DB 에 있으므로 dry-run 에도 DATABASE_URL 이 필요하다.

두 모드 모두 **전처리를 새로 만들지 않는다.** 입력 문자열은 학습과 같은
`input_builder.build_model1_text()` 로 잇고, 등급은 러너의 `TRUST_GRADE` 를
쓴다.

기존 값은 덮지 않는다
-------------------
이미 라벨이 있는 행은 건너뛴다. 재분류가 필요하면 `--overwrite`. 이 규칙이
그대로 재실행 안전장치가 된다 — 중간에 끊긴 뒤 다시 돌리면 남은 행만 채운다.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml" / "tools"))

import simr_routing_coverage as COVERAGE  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_PREDICTIONS = (
    REPO_ROOT / "ml" / "data" / "processed"
    / "announcement_detail_with_support_type_v2.parquet"
)
DEFAULT_FAILURES = REPO_ROOT / "backfill_failures.jsonl"

# ml/serving/model1/runner.py 의 MODEL_VERSION 과 같아야 한다. existing-predictions
# 모드는 torch 를 올리지 않으려고 러너를 import 하지 않으므로 여기 적어 둔다.
# inference 모드는 러너에서 직접 읽으므로 이 값을 쓰지 않는다.
DEFAULT_MODEL_VERSION = "1.0"

GRADES = ("trusted", "reference", "hold")

# 이 다섯이 한 묶음이다. 스키마 CHECK 가 전부 채워지거나 전부 비거나만 허용하므로
# 하나라도 없으면 backfill 자체가 성립하지 않는다.
LABEL_COLUMNS = (
    "support_type",
    "support_type_confidence",
    "support_type_grade",
    "support_type_model_version",
    "support_type_classified_at",
)
MIGRATION_PATH = "backend/app/db/migrations/2026-09-08_announcement_support_type.sql"


class Label:
    __slots__ = ("pblanc_id", "support_type", "confidence", "grade", "model_version")

    def __init__(self, pblanc_id, support_type, confidence, grade, model_version):
        if grade not in GRADES:
            raise ValueError("알 수 없는 grade: %r" % (grade,))
        if not (0.0 <= float(confidence) <= 1.0):
            raise ValueError("confidence 가 [0,1] 밖이다: %r" % (confidence,))
        if not (support_type or "").strip():
            raise ValueError("support_type 이 비어 있다")
        self.pblanc_id = pblanc_id
        self.support_type = support_type
        self.confidence = float(confidence)
        self.grade = grade
        self.model_version = model_version


# ------------------------------------------------------------------ 라벨 확보


def load_existing_predictions(path, model_version):
    """parquet 예측 → pblanc_id 로 색인한 라벨.

    parquet 의 `announcement_id` 는 `PBLN_...` 로, DB 의
    `sims.announcement.pblanc_id` 와 같은 값이다.
    """
    import pandas as pd

    df = pd.read_parquet(path)
    unknown = sorted(set(df["support_type_status"]) - set(COVERAGE.GRADE))
    if unknown:
        raise SystemExit("알 수 없는 support_type_status: %s" % unknown)

    labels = {}
    for row in df.itertuples(index=False):
        labels[row.announcement_id] = Label(
            pblanc_id=row.announcement_id,
            support_type=row.support_type_pred,
            confidence=row.support_type_confidence,
            grade=COVERAGE.GRADE[row.support_type_status],
            model_version=model_version,
        )
    return labels


def load_by_inference(engine, limit, batch_size):
    """DB 의 공고 원문으로 Model 1 을 돌린다.

    입력 조립은 학습 경로와 같은 함수를 쓴다. 공고의 purpose/content/target 은
    동기화가 요약문을 섹션으로 쪼개 넣은 값이라 F03 의 조각과 성격이 같다.
    그래서 `already_cleaned=True` 로 넣는다 — 러너 문서의 조건 그대로다.
    """
    sys.path.insert(0, str(REPO_ROOT / "ml" / "serving" / "model1"))
    import input_builder as IB
    import runner as RUNNER

    rows = fetch_announcement_texts(engine, limit)
    predictor = RUNNER._impl()
    labels = {}
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        texts = [
            IB.build_model1_text({
                "title": row["pblanc_nm"],
                "purpose": row["purpose"],
                "content": row["content"],
                "target_text": row["target"],
            })
            for row in chunk
        ]
        outputs = predictor.predict(texts, already_cleaned=True)
        for row, out in zip(chunk, outputs):
            labels[row["pblanc_id"]] = Label(
                pblanc_id=row["pblanc_id"],
                support_type=out["support_type_pred"],
                confidence=round(float(out["confidence"]), 4),
                grade=RUNNER.TRUST_GRADE[out["status"]],
                model_version=RUNNER.MODEL_VERSION,
            )
        print("  추론 %d/%d" % (min(start + batch_size, len(rows)), len(rows)))
    return labels


# ---------------------------------------------------------------------- DB


def create_engine_or_none(required):
    from app.core.config import Settings
    from app.db.session import create_database_engine

    if not os.getenv("DATABASE_URL"):
        if required:
            raise SystemExit(
                "preflight 실패: DATABASE_URL 이 없다.\n"
                "  --write 와 --source inference 는 DB 를 요구한다.\n"
                "  dry-run 은 --source existing-predictions 로 DB 없이 돌릴 수 있다."
            )
        return None
    settings = Settings()
    return create_database_engine(
        str(settings.database_url), settings.database_connect_timeout_seconds
    )


def check_schema(engine):
    """대상 테이블과 다섯 컬럼이 실제로 있는지 본다. 문제 목록을 돌려준다.

    마이그레이션을 적용하지 않은 DB 에 --write 를 걸면 첫 배치부터 모든 행이
    UndefinedColumn 으로 실패한다. 배치가 행 단위로 실패를 견디도록 만들어 둔
    탓에 **끝까지 돌면서** 실패 파일만 공고 수만큼 쌓인다. 그 전에 멈춘다.

    information_schema 를 보는 이유는 실패 메시지 때문이다. SELECT 를 던져 보고
    예외를 읽으면 없는 컬럼 하나만 알게 되는데, 여기서는 다섯 개 중 무엇이
    빠졌는지 한 번에 말해 줄 수 있다.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        if not connection.scalar(
            text("SELECT to_regclass('sims.announcement_version') IS NOT NULL")
        ):
            return ["sims.announcement_version 테이블이 없다"]
        present = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'sims'
                      AND table_name = 'announcement_version'
                    """
                )
            )
        }

    missing = [column for column in LABEL_COLUMNS if column not in present]
    if missing:
        return ["sims.announcement_version 에 컬럼이 없다: %s" % ", ".join(missing)]
    return []


def print_preflight_failure(problems):
    print("preflight 실패")
    for problem in problems:
        print("  - %s" % problem)
    print()
    print("기존 DB 라면 마이그레이션을 먼저 적용한다:")
    print('  psql "$DATABASE_URL" -f %s' % MIGRATION_PATH)
    print("새로 만드는 DB 라면 backend/app/db/schema.sql 에 같은 정의가 들어 있다.")
    print()


def fetch_targets(engine):
    """backfill 대상. 현재 버전 공고와 이미 붙어 있는 라벨."""
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT av.id AS announcement_version_id,
                       a.pblanc_id,
                       av.support_type,
                       av.support_type_grade
                FROM sims.announcement_version av
                JOIN sims.announcement a ON a.id = av.announcement_id
                WHERE av.is_current
                ORDER BY av.id
                """
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def fetch_announcement_texts(engine, limit):
    from sqlalchemy import text

    sql = """
        SELECT a.pblanc_id, av.pblanc_nm, av.purpose, av.content, av.target
        FROM sims.announcement_version av
        JOIN sims.announcement a ON a.id = av.announcement_id
        WHERE av.is_current
        ORDER BY av.id
    """
    if limit:
        sql += "\n        LIMIT :limit"
    with engine.connect() as connection:
        rows = connection.execute(
            text(sql), {"limit": limit} if limit else {}
        ).mappings().all()
    return [dict(row) for row in rows]


UPDATE_SQL = """
    UPDATE sims.announcement_version
       SET support_type = :support_type,
           support_type_confidence = :confidence,
           support_type_grade = :grade,
           support_type_model_version = :model_version,
           support_type_classified_at = :classified_at
     WHERE id = :announcement_version_id
       AND (CAST(:overwrite AS boolean) OR support_type IS NULL)
"""


def apply_updates(engine, planned, batch_size, overwrite, failures_path):
    """배치 단위로 쓴다. 한 행이 실패해도 나머지는 계속 쓴다.

    행마다 SAVEPOINT 를 잡는다. 잡지 않으면 한 행이 실패한 순간 트랜잭션 전체가
    중단되어 뒤따르는 행이 전부 함께 죽는다.

    실패는 모아 두지 않고 그때그때 파일에 흘린다. 중간에 프로세스가 죽으면
    무엇이 실패했는지가 가장 먼저 필요한데, 마지막에 한꺼번에 쓰면 그 기록이
    함께 사라진다.
    """
    from sqlalchemy import text

    statement = text(UPDATE_SQL)
    classified_at = datetime.now(timezone.utc)
    written = raced = failed = 0
    handle = None

    try:
        for start in range(0, len(planned), batch_size):
            chunk = planned[start:start + batch_size]
            with engine.begin() as connection:
                for item in chunk:
                    label = item["label"]
                    try:
                        with connection.begin_nested():
                            result = connection.execute(statement, {
                                "announcement_version_id":
                                    item["announcement_version_id"],
                                "support_type": label.support_type,
                                "confidence": label.confidence,
                                "grade": label.grade,
                                "model_version": label.model_version,
                                "classified_at": classified_at,
                                "overwrite": overwrite,
                            })
                    except Exception as error:  # noqa: BLE001 - 행 하나로 멈추지 않는다
                        failed += 1
                        if handle is None:
                            handle = open(failures_path, "a", encoding="utf-8")
                        handle.write(json.dumps({
                            "announcement_version_id":
                                item["announcement_version_id"],
                            "pblanc_id": label.pblanc_id,
                            "error": "%s: %s" % (type(error).__name__, error),
                        }, ensure_ascii=False) + "\n")
                        handle.flush()
                        continue
                    if result.rowcount:
                        written += 1
                    else:
                        # 계획을 세운 뒤 다른 쪽이 먼저 라벨을 채웠다. overwrite 가
                        # 아니면 조건에서 걸러진 것이므로 실패가 아니다.
                        raced += 1
            print("  기록 %d/%d"
                  % (min(start + batch_size, len(planned)), len(planned)))
    finally:
        if handle is not None:
            handle.close()

    return {"written": written, "raced": raced, "failed": failed,
            "failures_path": str(failures_path) if failed else None}


# ---------------------------------------------------------------------- 계획


def build_plan(targets, labels, overwrite):
    planned, skipped, unlabelled = [], [], []
    for target in targets:
        label = labels.get(target["pblanc_id"])
        if label is None:
            unlabelled.append(target)
            continue
        if target["support_type"] is not None and not overwrite:
            skipped.append(target)
            continue
        planned.append({
            "announcement_version_id": target["announcement_version_id"],
            "label": label,
        })
    matched = {target["pblanc_id"] for target in targets}
    unmatched = [pblanc_id for pblanc_id in labels if pblanc_id not in matched]
    return {"planned": planned, "skipped": skipped,
            "unlabelled": unlabelled, "unmatched": unmatched}


def projected_pairs(targets, plan):
    """backfill 이 끝난 뒤 코퍼스가 갖게 될 (support_type, grade) 목록."""
    updates = {
        item["announcement_version_id"]: item["label"] for item in plan["planned"]
    }
    pairs = []
    for target in targets:
        label = updates.get(target["announcement_version_id"])
        if label is not None:
            pairs.append((label.support_type, label.grade))
        elif target["support_type"] is not None:
            pairs.append((target["support_type"], target["support_type_grade"]))
    return pairs


# ---------------------------------------------------------------------- 보고


def print_distribution(pairs):
    from collections import Counter

    by_class = Counter(support_type for support_type, _grade in pairs)
    by_grade = Counter(grade for _support_type, grade in pairs)
    cross = Counter(pairs)

    print("출현 class %d개 / 총 %d건" % (len(by_class), len(pairs)))
    print()
    print("%-12s %8s %8s %8s %8s" % ("support_type", "trusted", "reference",
                                     "hold", "합계"))
    print("-" * 50)
    for support_type, total in by_class.most_common():
        print("%-12s %8d %8d %8d %8d"
              % (support_type, cross[(support_type, "trusted")],
                 cross[(support_type, "reference")],
                 cross[(support_type, "hold")], total))
    print("-" * 50)
    print("%-12s %8d %8d %8d %8d"
          % ("합계", by_grade["trusted"], by_grade["reference"],
             by_grade["hold"], len(pairs)))
    print()


def print_rollback_hint():
    print("되돌리기 (다섯 컬럼을 함께 비운다 — CHECK 가 부분 NULL 을 막는다)")
    print("  UPDATE sims.announcement_version")
    print("     SET support_type = NULL, support_type_confidence = NULL,")
    print("         support_type_grade = NULL, support_type_model_version = NULL,")
    print("         support_type_classified_at = NULL")
    print("   WHERE support_type_classified_at >= '<이번 실행 시각>';")


# ---------------------------------------------------------------------- 진입


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="기존 공고에 Model 1 support_type 라벨을 채운다.")
    parser.add_argument("--source", choices=("existing-predictions", "inference"),
                        default="existing-predictions",
                        help="라벨 출처 (기본: existing-predictions)")
    parser.add_argument("--write", action="store_true",
                        help="실제로 DB 에 쓴다 (기본은 dry-run)")
    parser.add_argument("--overwrite", action="store_true",
                        help="이미 라벨이 있는 공고도 덮어쓴다")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--failures", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION,
                        help="existing-predictions 모드에서 기록할 모델 버전")
    parser.add_argument("--limit", type=int, default=0,
                        help="inference 모드에서 처리할 공고 수 상한 (0=전체)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.batch_size < 1:
        raise SystemExit("--batch-size 는 1 이상이어야 한다")

    mode = "write" if args.write else "dry-run"
    print("=" * 60)
    print("공고 support_type backfill · %s · source=%s%s"
          % (mode, args.source, " · overwrite" if args.overwrite else ""))
    print("=" * 60)
    print()

    needs_db = args.write or args.source == "inference"
    engine = create_engine_or_none(required=needs_db)

    try:
        # 라벨을 만들기 전에 본다. inference 는 Model 1 가중치 442MB 를 올리는데,
        # 컬럼이 없어 어차피 못 쓸 것이라면 그 시간을 쓸 이유가 없다.
        schema_ready = True
        if engine is not None:
            problems = check_schema(engine)
            if problems:
                print_preflight_failure(problems)
                if args.write:
                    return 1
                schema_ready = False
                print("dry-run 이라 계속한다. 라벨 분포와 정착률까지만 본다.")
                print()

        if args.source == "inference":
            labels = load_by_inference(engine, args.limit, args.batch_size)
        else:
            if not args.predictions.exists():
                raise SystemExit("예측 파일이 없다: %s" % args.predictions)
            labels = load_existing_predictions(args.predictions, args.model_version)
        print("라벨 %d건 확보 (%s)" % (len(labels), args.source))
        print()

        if engine is None or not schema_ready:
            # 라벨 컬럼을 읽을 수 없는 경로. announcement_version_id 를 붙일 수
            # 없으므로 분포와 사다리 정착률만 본다. 정책 검증에는 충분하다.
            pairs = [(label.support_type, label.grade) for label in labels.values()]
            print_distribution(pairs)
            print(COVERAGE.format_coverage(COVERAGE.coverage(pairs), label="라벨"))
            print()
            if engine is None:
                print("DATABASE_URL 이 없어 announcement_version_id 는 확인하지 못했다.")
            else:
                print("라벨 컬럼이 없어 announcement_version_id 는 확인하지 못했다.")
            print("컬럼을 갖춘 DB 를 붙이면 매칭·skip 건수와 실제 코퍼스 기준")
            print("정착률이 나온다.")
            return 0

        targets = fetch_targets(engine)
        plan = build_plan(targets, labels, args.overwrite)
        already = sum(1 for t in targets if t["support_type"] is not None)

        print("DB 공고 %d건 (현재 버전)" % len(targets))
        print("  이미 라벨 있음   %d" % already)
        print("  이번에 채울 대상 %d" % len(plan["planned"]))
        print("  건너뜀(기존 값)  %d%s"
              % (len(plan["skipped"]),
                 "  ← --overwrite 로 덮을 수 있다" if plan["skipped"] else ""))
        print("  라벨 없는 공고   %d" % len(plan["unlabelled"]))
        print("  DB 에 없는 라벨  %d" % len(plan["unmatched"]))
        print()

        pairs = projected_pairs(targets, plan)
        print_distribution(pairs)
        print(COVERAGE.format_coverage(COVERAGE.coverage(pairs), label="공고"))
        print()

        if not args.write:
            print("dry-run 이므로 아무것도 쓰지 않았다. --write 로 반영한다.")
            return 0

        if not plan["planned"]:
            print("채울 대상이 없다.")
            return 0

        print("쓰는 중 (batch=%d)" % args.batch_size)
        result = apply_updates(engine, plan["planned"], args.batch_size,
                               args.overwrite, args.failures)
        print()
        print("기록 %d · 경합으로 건너뜀 %d · 실패 %d"
              % (result["written"], result["raced"], result["failed"]))
        if result["failures_path"]:
            print("실패 목록: %s" % result["failures_path"])

        remaining = [
            row for row in fetch_targets(engine) if row["support_type"] is None
        ]
        print("남은 NULL 라벨 %d건" % len(remaining))
        print()
        print_rollback_hint()
        return 1 if result["failed"] else 0
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
