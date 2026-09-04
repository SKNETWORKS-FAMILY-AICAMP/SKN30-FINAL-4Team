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


class RetrievalNotReadyError(RuntimeError):
    pass


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
) -> RetrievalResult:
    if not input_text.strip():
        raise ValueError("Retrieval input text must not be blank")
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
                "filter_snapshot": json.dumps(
                    {
                        "source_code": "BIZINFO_OPEN_API",
                        "is_current": True,
                        "search_status": ["OPEN", "UNKNOWN"],
                        "embedding_profile_id": profile["id"],
                    }
                ),
            },
        )
        if retrieval_run_id is None:
            raise RuntimeError("Failed to create retrieval run")
        rows = _search_candidates(
            connection,
            inspection_embedding_id=inspection_embedding_id,
            profile_id=profile["id"],
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
    )


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
) -> list[dict]:
    rows = connection.execute(
        text(
            """
            SELECT av.id AS announcement_version_id, av.pblanc_nm,
                   av.pblanc_url, av.search_status,
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
            WHERE ae.embedding_profile_id = :profile_id
              AND a.source_code = 'BIZINFO_OPEN_API'
              AND av.is_current
              AND av.search_status IN ('OPEN', 'UNKNOWN')
            ORDER BY ae.embedding <=> (
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
            "top_k": TOP_K,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _display_score(similarity: float) -> int:
    clamped = min(1.0, max(0.0, similarity)) * 100
    return int(Decimal(str(clamped)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
