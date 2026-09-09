import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Connection, Engine, text

from app.ports.embedding_client import EmbeddingClient
from app.services.retrieval.corpus_embedding import (
    CORPUS_FIELD_CODES,
    CORPUS_INPUT_TEMPLATE,
    QUERY_FIELD_CODES,
    QUERY_INPUT_TEMPLATE,
    vector_literal,
)


TOP_K = 5

# Model 1 이 내는 신뢰 등급. 값은 ml/serving/model1/runner.py 의 TRUST_GRADE 와
# 같은 문자열을 쓴다 — 두 곳이 어긋나면 라우팅이 조용히 unfiltered 로 떨어진다.
TRUST_GRADES = ("trusted", "reference", "hold")

# 라우팅 모드. 요청서 등급 하나가 아니라 '어디까지 넓혀서 K 를 채웠는가' 까지
# 반영한다 — hard 로 시작해도 후보가 모자라면 넓히게 되고, 그 둘은 결과의 성격이
# 다르다.
ROUTING_HARD = "hard"              # 1단계에서 K 를 채움
ROUTING_EXPANDED = "expanded"      # 2~3단계까지 넓혀서 채움
ROUTING_SOFT = "soft"              # 자르지 않고 우선순위만 준다
ROUTING_UNFILTERED = "unfiltered"  # 좁히지 않는다

GRADE_POLICY_TRUSTED = "trusted"
GRADE_POLICY_TRUSTED_REFERENCE = "trusted+reference"
GRADE_POLICY_ANY = "any"

# 요청서가 trusted 일 때 내려가는 사다리. (단계, 허용 공고 등급, 스냅샷 표기).
# 등급이 None 인 단계는 등급을 가리지 않는다.
#
# 왜 공고 등급까지 보는가: 공고 라벨의 28% 만 trusted 다(1,570건 실측,
# confidence 중앙값 0.25). 요청서가 trusted 여도 저신뢰 공고 라벨로 코퍼스를
# 자르면 노이즈로 자르는 셈이 된다. 반대로 1단계로 좁히면 실측상 사업화가
# 457건에서 57건으로 줄어 좁히기가 실제 의미를 갖는다.
#
# 단계별 정착 비율(요청이 공고와 같은 분포라고 볼 때):
#   1단계 96.1% · 2단계 3.2% · 3단계 0.3% · 4단계 0.3%
# 재현: ml/tools/simr_routing_coverage.py
_LADDER = (
    (1, ("trusted",), GRADE_POLICY_TRUSTED),
    (2, ("trusted", "reference"), GRADE_POLICY_TRUSTED_REFERENCE),
    (3, None, GRADE_POLICY_ANY),
)
_UNFILTERED_STAGE = 4

# 검색 대상 말뭉치를 고르는 조건. 개수를 세는 곳과 실제로 검색하는 곳이 같은
# 조건을 봐야 filter_snapshot 의 before/after 가 결과와 맞는다.
_CORPUS_PREDICATE = """
    ae.embedding_profile_id = :profile_id
    AND a.source_code = 'BIZINFO_OPEN_API'
    AND av.is_current
    AND av.search_status IN ('OPEN', 'UNKNOWN')
"""


class RetrievalNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """이번 검색이 말뭉치를 어떻게 좁혔는지.

    검색 결과만 남기면 왜 그 5건이 나왔는지 되짚을 수 없다. 같은 요청서를 다시
    돌려도 그 사이 backfill 로 라벨이 채워지면 다른 결과가 나오기 때문이다.
    어느 단계까지 내려갔는지(fallback_stage)와 필터 전/후 후보 수까지 남겨야
    "라벨이 없어서 넓게 잡힌 것" 과 "라벨이 있는데 후보가 적은 것" 을 구분할 수
    있다.
    """

    request_support_type: str | None
    request_trust_grade: str
    mode: str
    candidate_grade_policy: str | None
    fallback_stage: int | None
    fallback_used: bool
    before_count: int
    after_count: int | None

    def as_snapshot(self) -> dict:
        return {
            "request_support_type": self.request_support_type,
            "request_trust_grade": self.request_trust_grade,
            "candidate_grade_policy": self.candidate_grade_policy,
            "routing_mode": self.mode,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "fallback_stage": self.fallback_stage,
            "fallback_used": self.fallback_used,
        }


@dataclass(frozen=True, slots=True)
class RetrievalCandidateResult:
    rank: int
    announcement_version_id: int
    title: str
    url: str
    search_status: str
    semantic_similarity: float
    semantic_similarity_display: int


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    retrieval_run_id: int
    embedding_profile_id: int
    top_k_used: int
    candidates: list[RetrievalCandidateResult]
    routing: RoutingDecision


def compose_inspection_embedding_text(
    *,
    purpose: str,
    target: str,
    content: str,
) -> str:
    values = [
        ("사업목적", purpose.strip()),
        ("지원대상", target.strip()),
        ("지원내용", content.strip()),
    ]
    result = "\n".join(f"{label}: {value}" for label, value in values if value)
    if not result:
        raise ValueError("At least one retrieval input axis is required")
    return result


async def retrieve_top_five(
    engine: Engine,
    client: EmbeddingClient,
    *,
    case_id: int,
    input_text: str,
    support_type: str | None = None,
    trust_grade: str = "hold",
) -> RetrievalResult:
    """유사 공고 Top-5. support_type 이 주어지면 신뢰 등급에 따라 말뭉치를 좁힌다.

    두 값은 요청서에 대한 Model 1 결과다. 호출부가 이미 메모리에 갖고 있으므로
    여기서 DB 를 다시 뒤지지 않는다(§12). 기본값은 좁히지 않는 쪽이다 — 값을
    넘기지 않는 기존 호출은 동작이 바뀌지 않는다.

        trusted    사다리를 내려가며 좁힌다(아래)
        reference  soft        같은 support_type 을 앞에 세우되 잘라내지는 않는다
        hold       unfiltered  좁히지 않는다

    요청서 등급만으로는 부족하다. 자르는 대상은 **공고 쪽 라벨** 이고 그쪽의
    28% 만 trusted 이기 때문이다. 그래서 요청서가 trusted 일 때는 공고 등급까지
    함께 보고 단계적으로 넓힌다.

        1  같은 support_type + 공고 trusted
        2  같은 support_type + 공고 trusted/reference
        3  같은 support_type 전체 등급
        4  좁히지 않음

    각 단계의 후보 수를 먼저 세고, K 를 채우는 첫 단계에서 한 번만 검색한다 —
    검색을 여러 번 돌려 보고 되돌리지 않는다. 그래서 filter_snapshot 은 처음
    기록될 때 이미 확정값이다.
    """
    if not input_text.strip():
        raise ValueError("Retrieval input text must not be blank")
    if trust_grade not in TRUST_GRADES:
        raise ValueError(
            "Unknown trust grade %r (expected one of %s)"
            % (trust_grade, ", ".join(TRUST_GRADES))
        )
    profile = _active_summary_profile(engine)
    _require_checking_case(engine, case_id)
    batch = await client.embed([input_text])
    if (
        len(batch.vectors) != 1
        or batch.model_name != profile["model_name"]
        or len(batch.vectors[0]) != profile["dimension"]
    ):
        raise ValueError("Inspection embedding does not match the active profile")

    with engine.begin() as connection:
        case = connection.execute(
            text(
                """
                SELECT status, top_k_used
                FROM sims.inspection_case
                WHERE id = :case_id
                FOR UPDATE
                """
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
        if case is None or case["status"] != "CHECKING":
            raise RetrievalNotReadyError("Inspection case is not ready for retrieval")
        if case["top_k_used"] != TOP_K:
            raise RetrievalNotReadyError("Inspection case does not use the Top-5 contract")

        inspection_embedding_id = _store_inspection_embedding(
            connection,
            case_id=case_id,
            profile_id=profile["id"],
            input_text=input_text,
            vector=batch.vectors[0],
        )
        source_sync_run_id = connection.scalar(
            text(
                """
                SELECT id FROM sims.api_sync_run
                WHERE source_code = 'BIZINFO_OPEN_API' AND status = 'SUCCEEDED'
                ORDER BY sync_date_kst DESC, id DESC
                LIMIT 1
                """
            )
        )
        counts = _corpus_counts(
            connection, profile_id=profile["id"], support_type=support_type
        )
        routing = _plan_routing(
            support_type=support_type, trust_grade=trust_grade, counts=counts
        )
        retrieval_run_id = connection.scalar(
            text(
                """
                INSERT INTO sims.retrieval_run (
                    inspection_case_id, inspection_embedding_id,
                    source_sync_run_id, status, top_k_used,
                    corpus_snapshot_at, filter_snapshot, started_at
                ) VALUES (
                    :case_id, :inspection_embedding_id,
                    :source_sync_run_id, 'RUNNING', :top_k,
                    statement_timestamp(), CAST(:filter_snapshot AS jsonb),
                    statement_timestamp()
                ) RETURNING id
                """
            ),
            {
                "case_id": case_id,
                "inspection_embedding_id": inspection_embedding_id,
                "source_sync_run_id": source_sync_run_id,
                "top_k": TOP_K,
                "filter_snapshot": _filter_snapshot(profile["id"], routing),
            },
        )
        if retrieval_run_id is None:
            raise RuntimeError("Failed to create retrieval run")
        rows = _search_candidates(
            connection,
            inspection_embedding_id=inspection_embedding_id,
            profile_id=profile["id"],
            **_search_filter(routing),
        )
        candidate_results: list[RetrievalCandidateResult] = []
        for rank, row in enumerate(rows, start=1):
            similarity = float(row["similarity"])
            connection.execute(
                text(
                    """
                    INSERT INTO sims.retrieval_candidate (
                        retrieval_run_id, announcement_version_id, rank_no,
                        vector_distance, vector_similarity, status_verification
                    ) VALUES (
                        :retrieval_run_id, :announcement_version_id, :rank,
                        :distance, :similarity, :status_verification
                    )
                    """
                ),
                {
                    "retrieval_run_id": retrieval_run_id,
                    "announcement_version_id": row["announcement_version_id"],
                    "rank": rank,
                    "distance": float(row["distance"]),
                    "similarity": similarity,
                    "status_verification": (
                        "VERIFIED_OPEN"
                        if row["search_status"] == "OPEN"
                        else "NEEDS_CONFIRMATION"
                    ),
                },
            )
            candidate_results.append(
                RetrievalCandidateResult(
                    rank=rank,
                    announcement_version_id=row["announcement_version_id"],
                    title=row["pblanc_nm"],
                    url=row["pblanc_url"],
                    search_status=row["search_status"],
                    semantic_similarity=similarity,
                    semantic_similarity_display=_display_score(similarity),
                )
            )
        connection.execute(
            text(
                """
                UPDATE sims.retrieval_run
                SET status = 'SUCCESS', completed_at = statement_timestamp()
                WHERE id = :retrieval_run_id AND status = 'RUNNING'
                """
            ),
            {"retrieval_run_id": retrieval_run_id},
        )
        connection.execute(
            text(
                """
                UPDATE sims.inspection_case
                SET status = 'RETRIEVING'
                WHERE id = :case_id AND status = 'CHECKING'
                """
            ),
            {"case_id": case_id},
        )
    return RetrievalResult(
        retrieval_run_id=int(retrieval_run_id),
        embedding_profile_id=profile["id"],
        top_k_used=TOP_K,
        candidates=candidate_results,
        routing=routing,
    )


def _plan_routing(
    *,
    support_type: str | None,
    trust_grade: str,
    counts: dict,
) -> RoutingDecision:
    """후보 수를 보고 이번 검색의 라우팅을 확정한다.

    사다리를 내려가는 판단이 여기 한 곳에 모여 있다. 검색을 돌려 보고 부족하면
    되돌리는 방식이 아니라, 세어 놓은 수로 먼저 단계를 고르고 그 단계로 한 번만
    검색한다. 결정이 검색보다 앞서므로 filter_snapshot 이 처음부터 확정값이다.
    """
    corpus_count = counts["corpus_count"]
    labelled = support_type is not None and bool(support_type.strip())

    if not labelled or trust_grade == "hold":
        # 좁히지 않았으면 '필터 후' 라는 수 자체가 없다. 0 으로 적으면 후보가
        # 없었다는 뜻으로 읽힌다.
        return RoutingDecision(
            request_support_type=None if not labelled else support_type,
            request_trust_grade=trust_grade,
            mode=ROUTING_UNFILTERED,
            candidate_grade_policy=None,
            fallback_stage=None,
            fallback_used=False,
            before_count=corpus_count,
            after_count=None,
        )

    if trust_grade == "reference":
        # 자르지 않으므로 후보가 모자랄 일이 없다 — 사다리를 타지 않는다.
        return RoutingDecision(
            request_support_type=support_type,
            request_trust_grade=trust_grade,
            mode=ROUTING_SOFT,
            candidate_grade_policy=GRADE_POLICY_ANY,
            fallback_stage=None,
            fallback_used=False,
            before_count=corpus_count,
            after_count=counts["stage_3"],
        )

    for stage, _grades, policy in _LADDER:
        available = counts["stage_%d" % stage]
        if available >= TOP_K:
            return RoutingDecision(
                request_support_type=support_type,
                request_trust_grade=trust_grade,
                mode=ROUTING_HARD if stage == 1 else ROUTING_EXPANDED,
                candidate_grade_policy=policy,
                fallback_stage=stage,
                fallback_used=stage > 1,
                before_count=corpus_count,
                after_count=available,
            )

    # 같은 라벨을 가진 공고가 등급을 다 열어도 K 에 못 미친다(실측: 보증 3건,
    # 상담 2건). 빈 자리를 남기느니 좁히기를 포기한다.
    return RoutingDecision(
        request_support_type=support_type,
        request_trust_grade=trust_grade,
        mode=ROUTING_UNFILTERED,
        candidate_grade_policy=None,
        fallback_stage=_UNFILTERED_STAGE,
        fallback_used=True,
        before_count=corpus_count,
        after_count=None,
    )


def _search_filter(routing: RoutingDecision) -> dict:
    """라우팅 결정 → `_search_candidates` 의 검색 조건."""
    if routing.mode == ROUTING_SOFT:
        return {
            "support_type": routing.request_support_type,
            "grades": None,
            "prefer": True,
        }
    if routing.mode == ROUTING_UNFILTERED:
        return {"support_type": None, "grades": None, "prefer": False}
    grades = next(
        grades for stage, grades, _p in _LADDER if stage == routing.fallback_stage
    )
    return {
        "support_type": routing.request_support_type,
        "grades": None if grades is None else list(grades),
        "prefer": False,
    }


def _filter_snapshot(profile_id: int, routing: RoutingDecision) -> str:
    return json.dumps(
        {
            "source_code": "BIZINFO_OPEN_API",
            "is_current": True,
            "search_status": ["OPEN", "UNKNOWN"],
            "embedding_profile_id": profile_id,
            "routing": routing.as_snapshot(),
        },
        ensure_ascii=False,
    )


def _corpus_counts(
    connection: Connection,
    *,
    profile_id: int,
    support_type: str | None,
) -> dict:
    """말뭉치 크기와 사다리 단계별 후보 수.

    한 번의 스캔으로 세 단계를 함께 센다. 단계마다 따로 물으면 그 사이에 다른
    트랜잭션이 라벨을 채울 수 있고, 그러면 고른 단계와 실제 검색 대상이 어긋난다.
    """
    row = connection.execute(
        text(
            """
            SELECT count(*) AS corpus_count,
                   count(*) FILTER (
                       WHERE av.support_type = :support_type
                         AND av.support_type_grade = 'trusted'
                   ) AS stage_1,
                   count(*) FILTER (
                       WHERE av.support_type = :support_type
                         AND av.support_type_grade IN ('trusted', 'reference')
                   ) AS stage_2,
                   count(*) FILTER (
                       WHERE av.support_type = :support_type
                   ) AS stage_3
            FROM sims.announcement_embedding ae
            JOIN sims.announcement_version av
              ON av.id = ae.announcement_version_id
            JOIN sims.announcement a
              ON a.id = av.announcement_id
            WHERE """
            + _CORPUS_PREDICATE
        ),
        {"profile_id": profile_id, "support_type": support_type},
    ).mappings().one()
    return {key: int(row[key]) for key in
            ("corpus_count", "stage_1", "stage_2", "stage_3")}


def _active_summary_profile(engine: Engine) -> dict:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT p.id, p.preprocessing_version, p.field_codes,
                       p.input_template, p.configuration,
                       m.model_name, m.dimension
                FROM sims.embedding_profile p
                JOIN sims.embedding_model m ON m.id = p.embedding_model_id
                WHERE p.profile_kind = 'SUMMARY' AND p.is_active AND m.is_enabled
                ORDER BY p.id
                """
            )
        ).mappings().all()
    if len(rows) != 1:
        raise RetrievalNotReadyError("Exactly one active summary profile is required")
    profile = dict(rows[0])
    if (
        list(profile["field_codes"]) != CORPUS_FIELD_CODES
        or profile["input_template"] != CORPUS_INPUT_TEMPLATE
        or profile["configuration"].get("query_field_codes")
        != QUERY_FIELD_CODES
        or profile["configuration"].get("query_input_template")
        != QUERY_INPUT_TEMPLATE
    ):
        raise RetrievalNotReadyError("Active summary profile is incompatible")
    return profile


def _require_checking_case(engine: Engine, case_id: int) -> None:
    with engine.connect() as connection:
        case = connection.execute(
            text(
                "SELECT status, top_k_used "
                "FROM sims.inspection_case WHERE id = :case_id"
            ),
            {"case_id": case_id},
        ).mappings().one_or_none()
    if case is None or case["status"] != "CHECKING":
        raise RetrievalNotReadyError("Inspection case is not ready for retrieval")
    if case["top_k_used"] != TOP_K:
        raise RetrievalNotReadyError("Inspection case does not use the Top-5 contract")


def _store_inspection_embedding(
    connection: Connection,
    *,
    case_id: int,
    profile_id: int,
    input_text: str,
    vector: list[float],
) -> int:
    digest = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
    row = connection.execute(
        text(
            """
            INSERT INTO sims.inspection_embedding (
                inspection_case_id, embedding_profile_id,
                input_text, input_sha256_hex, embedding
            ) VALUES (
                :case_id, :profile_id, :input_text, :digest,
                CAST(:embedding AS vector)
            )
            ON CONFLICT (inspection_case_id, embedding_profile_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "case_id": case_id,
            "profile_id": profile_id,
            "input_text": input_text,
            "digest": digest,
            "embedding": vector_literal(vector),
        },
    ).scalar_one_or_none()
    if row is not None:
        return int(row)
    existing = connection.execute(
        text(
            """
            SELECT id, input_sha256_hex
            FROM sims.inspection_embedding
            WHERE inspection_case_id = :case_id
              AND embedding_profile_id = :profile_id
            """
        ),
        {"case_id": case_id, "profile_id": profile_id},
    ).mappings().one()
    if existing["input_sha256_hex"] != digest:
        raise RetrievalNotReadyError("Inspection embedding input is immutable")
    return int(existing["id"])


def _search_candidates(
    connection: Connection,
    *,
    inspection_embedding_id: int,
    profile_id: int,
    support_type: str | None = None,
    grades: list[str] | None = None,
    prefer: bool = False,
) -> list[dict]:
    """말뭉치를 좁히거나 순서를 바꾼다.

    support_type 이 있고 prefer 가 False 면 WHERE 로 자른다. prefer 가 True 면
    자르지 않고 정렬 우선키로만 쓴다(soft). grades 는 자를 때만 의미가 있고,
    None 이면 등급을 가리지 않는다.

    soft 에 유사도 가산점을 주는 방식은 쓰지 않았다 — 가산점 크기를 정할 근거가
    아직 없고, 임의로 정하면 그 숫자가 결과를 지배하면서도 어디서 왔는지 설명할
    수 없다. 정렬 키는 상수를 요구하지 않는다.

    IS DISTINCT FROM 을 쓰는 이유는 라벨이 NULL 인 행 때문이다. `<>` 로 쓰면
    NULL 비교가 NULL 이 되어 정렬 키가 셋(참/거짓/NULL)으로 갈린다. backfill
    전에는 거의 모든 행이 NULL 이므로 그 차이가 결과 전체를 좌우한다.
    """
    conditions = ""
    order_prefix = ""
    if support_type is not None:
        if prefer:
            order_prefix = "(av.support_type IS DISTINCT FROM :support_type), "
        else:
            conditions = " AND av.support_type = :support_type"
            if grades is not None:
                conditions += (
                    " AND av.support_type_grade"
                    " = ANY(CAST(:grades AS text[]))"
                )

    rows = connection.execute(
        text(
            """
            SELECT av.id AS announcement_version_id, av.pblanc_nm,
                   av.pblanc_url, av.search_status, av.support_type,
                   av.support_type_grade,
                   ae.embedding <=> (
                       SELECT ie.embedding
                       FROM sims.inspection_embedding ie
                       WHERE ie.id = :inspection_embedding_id
                   ) AS distance,
                   LEAST(1.0, GREATEST(-1.0, 1.0 - (
                       ae.embedding <=> (
                           SELECT ie.embedding
                           FROM sims.inspection_embedding ie
                           WHERE ie.id = :inspection_embedding_id
                       )
                   ))) AS similarity
            FROM sims.announcement_embedding ae
            JOIN sims.announcement_version av
              ON av.id = ae.announcement_version_id
            JOIN sims.announcement a
              ON a.id = av.announcement_id
            WHERE """
            + _CORPUS_PREDICATE
            + conditions
            + """
            ORDER BY """
            + order_prefix
            + """ae.embedding <=> (
                SELECT ie.embedding
                FROM sims.inspection_embedding ie
                WHERE ie.id = :inspection_embedding_id
            ), av.id
            LIMIT :top_k
            """
        ),
        {
            "inspection_embedding_id": inspection_embedding_id,
            "profile_id": profile_id,
            "support_type": support_type,
            "grades": grades,
            "top_k": TOP_K,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _display_score(similarity: float) -> int:
    clamped = min(1.0, max(0.0, similarity)) * 100
    return int(Decimal(str(clamped)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
