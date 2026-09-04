import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from app.ports.embedding_client import EmbeddingBatch, EmbeddingClient
from app.services.retrieval.announcement_sync import compose_embedding_text


CORPUS_FIELD_CODES = ["pblanc_nm", "purpose", "target", "content", "hashtags"]
CORPUS_INPUT_TEMPLATE = "공고명/사업목적/지원대상/지원내용/해시태그의 비어 있지 않은 축"
QUERY_FIELD_CODES = ["purpose", "target", "content"]
QUERY_INPUT_TEMPLATE = "사업목적/지원대상/지원내용의 비어 있지 않은 축"


@dataclass(frozen=True, slots=True)
class CorpusEmbeddingResult:
    embedding_profile_id: int | None
    embedded_count: int


async def embed_current_announcements(
    engine: Engine,
    client: EmbeddingClient,
    *,
    requested_model_name: str,
    profile_name: str,
    profile_version: int,
    preprocessing_version: str,
    batch_size: int = 100,
) -> CorpusEmbeddingResult:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    profile = _load_profile(engine, profile_name, profile_version)
    if profile is not None:
        if (
            profile["provider"] != "openai"
            or profile["preprocessing_version"] != preprocessing_version
            or list(profile["field_codes"]) != CORPUS_FIELD_CODES
            or profile["input_template"] != CORPUS_INPUT_TEMPLATE
            or profile["configuration"].get("requested_model_name")
            != requested_model_name
            or profile["configuration"].get("query_field_codes")
            != QUERY_FIELD_CODES
            or profile["configuration"].get("query_input_template")
            != QUERY_INPUT_TEMPLATE
        ):
            raise ValueError("Existing embedding profile is incompatible")
    pending = _load_pending(engine, profile["id"] if profile else None)
    if not pending:
        if profile is not None:
            _activate_profile(engine, profile["id"])
        return CorpusEmbeddingResult(
            embedding_profile_id=profile["id"] if profile else None,
            embedded_count=0,
        )

    embedded_count = 0
    for offset in range(0, len(pending), batch_size):
        rows = pending[offset : offset + batch_size]
        inputs = [row["input_text"] for row in rows]
        batch = await client.embed(inputs)
        _validate_batch(batch, len(rows))
        if profile is None:
            profile = _create_or_load_profile(
                engine,
                provider="openai",
                model_name=batch.model_name,
                dimension=len(batch.vectors[0]),
                requested_model_name=requested_model_name,
                profile_name=profile_name,
                profile_version=profile_version,
                preprocessing_version=preprocessing_version,
            )
        if (
            batch.model_name != profile["model_name"]
            or len(batch.vectors[0]) != profile["dimension"]
        ):
            raise ValueError("Embedding response does not match the active profile")
        _store_embeddings(engine, profile["id"], rows, batch.vectors)
        embedded_count += len(rows)
    _activate_profile(engine, profile["id"])
    return CorpusEmbeddingResult(
        embedding_profile_id=profile["id"],
        embedded_count=embedded_count,
    )


def _load_profile(
    engine: Engine,
    profile_name: str,
    profile_version: int,
) -> dict | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT p.id, p.preprocessing_version, p.configuration,
                       p.field_codes, p.input_template, m.provider,
                       m.model_name, m.dimension
                FROM sims.embedding_profile p
                JOIN sims.embedding_model m ON m.id = p.embedding_model_id
                WHERE p.profile_name = :profile_name
                  AND p.version_no = :profile_version
                  AND p.profile_kind = 'SUMMARY'
                """
            ),
            {"profile_name": profile_name, "profile_version": profile_version},
        ).mappings().one_or_none()
    return dict(row) if row else None


def _activate_profile(engine: Engine, profile_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE sims.embedding_profile
                SET is_active = (id = :profile_id)
                WHERE profile_kind = 'SUMMARY'
                """
            ),
            {"profile_id": profile_id},
        )


def _load_pending(engine: Engine, profile_id: int | None) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT av.id AS announcement_version_id, av.pblanc_nm,
                       av.purpose, av.target, av.content, av.hashtags
                FROM sims.announcement_version av
                JOIN sims.announcement a ON a.id = av.announcement_id
                LEFT JOIN sims.announcement_embedding ae
                  ON ae.announcement_version_id = av.id
                 AND ae.embedding_profile_id = :profile_id
                WHERE a.source_code = 'BIZINFO_OPEN_API'
                  AND av.is_current AND ae.id IS NULL
                ORDER BY av.id
                """
            ),
            {"profile_id": profile_id},
        ).mappings().all()
    return [
        {
            "announcement_version_id": row["announcement_version_id"],
            "input_text": compose_embedding_text(
                title=row["pblanc_nm"],
                purpose=row["purpose"],
                target=row["target"],
                content=row["content"],
                hashtags=list(row["hashtags"]),
            ),
        }
        for row in rows
    ]


def _create_or_load_profile(
    engine: Engine,
    *,
    provider: str,
    model_name: str,
    dimension: int,
    requested_model_name: str,
    profile_name: str,
    profile_version: int,
    preprocessing_version: str,
) -> dict:
    with engine.begin() as connection:
        model_id = connection.scalar(
            text(
                """
                INSERT INTO sims.embedding_model (
                    provider, model_name, model_version, dimension, distance_metric
                ) VALUES (
                    :provider, :model_name, '', :dimension, 'COSINE'
                )
                ON CONFLICT (provider, model_name, model_version, dimension)
                DO UPDATE SET is_enabled = true
                RETURNING id
                """
            ),
            {
                "provider": provider,
                "model_name": model_name,
                "dimension": dimension,
            },
        )
        profile_id = connection.scalar(
            text(
                """
                INSERT INTO sims.embedding_profile (
                    embedding_model_id, profile_name, version_no, profile_kind,
                    field_codes, input_template, configuration,
                    preprocessing_version, is_active
                ) VALUES (
                    :model_id, :profile_name, :profile_version, 'SUMMARY',
                    :field_codes, :input_template, CAST(:configuration AS jsonb),
                    :preprocessing_version, false
                )
                ON CONFLICT (profile_name, version_no) DO NOTHING
                RETURNING id
                """
            ),
            {
                "model_id": model_id,
                "profile_name": profile_name,
                "profile_version": profile_version,
                "field_codes": CORPUS_FIELD_CODES,
                "input_template": CORPUS_INPUT_TEMPLATE,
                "configuration": json.dumps(
                    {
                        "requested_model_name": requested_model_name,
                        "query_field_codes": QUERY_FIELD_CODES,
                        "query_input_template": QUERY_INPUT_TEMPLATE,
                    },
                    ensure_ascii=False,
                ),
                "preprocessing_version": preprocessing_version,
            },
        )
        if profile_id is None:
            row = connection.execute(
                text(
                    """
                    SELECT p.id, p.preprocessing_version, p.configuration,
                           p.field_codes, p.input_template, p.profile_kind,
                           m.provider, m.model_name, m.dimension
                    FROM sims.embedding_profile p
                    JOIN sims.embedding_model m ON m.id = p.embedding_model_id
                    WHERE p.profile_name = :profile_name
                      AND p.version_no = :profile_version
                    """
                ),
                {"profile_name": profile_name, "profile_version": profile_version},
            ).mappings().one()
            if (
                row["provider"] != provider
                or row["model_name"] != model_name
                or row["dimension"] != dimension
                or row["preprocessing_version"] != preprocessing_version
                or row["configuration"].get("requested_model_name")
                != requested_model_name
                or row["configuration"].get("query_field_codes")
                != QUERY_FIELD_CODES
                or row["configuration"].get("query_input_template")
                != QUERY_INPUT_TEMPLATE
                or list(row["field_codes"]) != CORPUS_FIELD_CODES
                or row["input_template"] != CORPUS_INPUT_TEMPLATE
                or row["profile_kind"] != "SUMMARY"
            ):
                raise ValueError("Existing embedding profile is incompatible")
            profile_id = row["id"]
    return {
        "id": int(profile_id),
        "provider": provider,
        "model_name": model_name,
        "dimension": dimension,
        "preprocessing_version": preprocessing_version,
    }


def _store_embeddings(
    engine: Engine,
    profile_id: int,
    rows: list[dict],
    vectors: list[list[float]],
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO sims.announcement_embedding (
                    announcement_version_id, embedding_profile_id,
                    input_text, input_sha256_hex, embedding
                ) VALUES (
                    :announcement_version_id, :profile_id,
                    :input_text, :input_sha256_hex, CAST(:embedding AS vector)
                )
                ON CONFLICT (announcement_version_id, embedding_profile_id)
                DO NOTHING
                """
            ),
            [
                {
                    "announcement_version_id": row["announcement_version_id"],
                    "profile_id": profile_id,
                    "input_text": row["input_text"],
                    "input_sha256_hex": hashlib.sha256(
                        row["input_text"].encode("utf-8")
                    ).hexdigest(),
                    "embedding": vector_literal(vector),
                }
                for row, vector in zip(rows, vectors, strict=True)
            ],
        )


def _validate_batch(batch: EmbeddingBatch, expected_count: int) -> None:
    if len(batch.vectors) != expected_count or not batch.vectors:
        raise ValueError("Embedding response count does not match inputs")
    dimensions = {len(vector) for vector in batch.vectors}
    if len(dimensions) != 1 or 0 in dimensions:
        raise ValueError("Embedding response dimensions are inconsistent")


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in vector) + "]"
