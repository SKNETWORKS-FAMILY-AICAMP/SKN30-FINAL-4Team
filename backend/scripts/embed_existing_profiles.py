#!/usr/bin/env python3
"""Embed Existing Profile JSON files and upsert the four retrieval scopes.

The trusted ingestion script connects directly to PostgreSQL, as does the
Existing KB importer. It never prints the OpenAI key or database credentials.
Every local Profile byte snapshot must match the current DB Profile SHA-256,
structured artifact, source identity, and schema before any OpenAI request.
Identical (profile version, configuration, scope, input SHA-256) rows are
skipped, so rerunning the command is safe.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.retrieval.embedding_inputs import (
    DEFAULT_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_INPUT_TOKENS,
    EmbeddingInput,
    assemble_embedding_inputs,
    mean_pool_embeddings,
)
try:
    from .local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings
except ImportError:  # direct ``python scripts/...`` execution
    from local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings


ASSEMBLY_VERSION = "approved-facts-role-aware-v1"
CHUNKING_STRATEGY = "fact-boundary-token-weighted-mean-v1"
MAX_BATCH_TOKENS = 300_000
ALL_SCOPES = ("purpose", "target", "support", "combined")
PROFILE_SCHEMA = "existing_program_profile/v0.2"
SOURCE_KINDS = frozenset({"hwp", "hwpx", "pdf", "markdown_fixture"})
SHA256_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")


@dataclass(slots=True)
class PendingInput:
    profile_version_pk: Any
    notice_id: str
    source_profile_id: str
    embedding_input: EmbeddingInput
    chunk_vectors: list[list[float]] = field(default_factory=list)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    backend_env = BACKEND_ROOT / ".env"
    load_dotenv(
        backend_env if backend_env.is_file() else REPOSITORY_ROOT / ".env",
        override=False,
    )


def _discover_profiles(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    direct = root / "pipeline" / "structured_profile.v0.2.json"
    if direct.is_file():
        return [direct]
    return sorted(root.glob("*/pipeline/structured_profile.v0.2.json"))


def _load_profile(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"profile root must be an object: {path}")
    return value


def _profile_identity(profile: dict[str, Any], path: Path) -> dict[str, str]:
    schema_version = profile.get("schema_version")
    if schema_version != PROFILE_SCHEMA:
        raise ValueError(f"unsupported Existing Profile schema: {path}")
    notice_id = str(profile.get("notice_id") or "").strip()
    source_profile_id = str(profile.get("source_profile_id") or "").strip()
    if not notice_id or not source_profile_id:
        raise ValueError(f"Existing Profile identity is incomplete: {path}")
    documents = profile.get("source_documents")
    if (
        not isinstance(documents, list)
        or len(documents) != 1
        or not isinstance(documents[0], dict)
    ):
        raise ValueError(f"Existing Profile must contain one source document: {path}")
    source_kind = str(documents[0].get("format") or "").strip().lower()
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unsupported Existing Profile source kind: {path}")
    common_ir = documents[0].get("common_ir")
    if not isinstance(common_ir, dict):
        raise ValueError(f"Existing Profile Common IR identity is missing: {path}")
    common_ir_document_id = common_ir.get("document_id")
    if common_ir_document_id is not None and common_ir_document_id != source_profile_id:
        raise ValueError(f"Existing Profile/Common IR identity mismatch: {path}")
    common_ir_source_kind = common_ir.get("source_kind")
    if common_ir_source_kind is not None and common_ir_source_kind != source_kind:
        raise ValueError(f"Existing Profile/Common IR source kind mismatch: {path}")
    source_sha256 = str(common_ir.get("source_sha256") or "").strip().lower()
    if SHA256_PATTERN.fullmatch(source_sha256) is None:
        raise ValueError(f"Existing Profile source SHA-256 is invalid: {path}")
    return {
        "schema_version": str(schema_version),
        "notice_id": notice_id,
        "source_profile_id": source_profile_id,
        "source_kind": source_kind,
        "source_sha256": source_sha256,
    }


def _load_profile_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    """Parse and hash the exact same byte snapshot used for embeddings."""

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"profile root must be an object: {path}")
    _profile_identity(value, path)
    return value, sha256(raw).hexdigest()


def _batched_chunks(
    pending: Sequence[PendingInput], *, max_inputs: int, max_tokens: int
) -> Iterable[list[tuple[PendingInput, Any]]]:
    batch: list[tuple[PendingInput, Any]] = []
    tokens = 0
    for item in pending:
        for chunk in item.embedding_input.chunks:
            if batch and (
                len(batch) >= max_inputs or tokens + chunk.token_count > max_tokens
            ):
                yield batch
                batch = []
                tokens = 0
            batch.append((item, chunk))
            tokens += chunk.token_count
    if batch:
        yield batch


def _vector_literal(vector: Sequence[float]) -> str:
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"expected {EMBEDDING_DIMENSIONS} dimensions, got {len(vector)}"
        )
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def _configuration(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT embedding_config_pk, provider, model_id, dimensions,
                   assembly_version, max_input_tokens, chunking_strategy
            FROM retrieval.embedding_configuration
            WHERE is_active
            """
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected one active embedding configuration, got {len(rows)}")
    config = dict(rows[0])
    expected = {
        "provider": "openai",
        "model_id": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "assembly_version": ASSEMBLY_VERSION,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "chunking_strategy": CHUNKING_STRATEGY,
    }
    mismatches = {
        key: (config.get(key), value)
        for key, value in expected.items()
        if config.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"active embedding configuration mismatch: {mismatches}")
    return config


def _profile_version(
    connection: Any,
    profile: dict[str, Any],
    path: Path,
    *,
    profile_sha256: str,
) -> Any:
    identity = _profile_identity(profile, path)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT profile.profile_version_pk,
                   lower(profile.profile_sha256) AS profile_sha256,
                   profile.schema_version,
                   notice.notice_id,
                   source_profile.source_profile_id,
                   source_profile.source_kind,
                   lower(source.source_sha256) AS source_sha256,
                   structured.artifact_type AS structured_artifact_type,
                   lower(structured.content_sha256) AS structured_content_sha256
            FROM kb.profile_version AS profile
            JOIN kb.source_version AS source
              ON source.source_version_pk = profile.source_version_pk
            JOIN kb.source_profile AS source_profile
              ON source_profile.source_profile_pk = source.source_profile_pk
            JOIN kb.notice AS notice
              ON notice.notice_pk = source_profile.notice_pk
            JOIN kb.artifact AS structured
              ON structured.artifact_pk = profile.structured_artifact_pk
            WHERE source_profile.source_profile_id = %s
              AND lower(profile.profile_sha256) = %s
              AND profile.is_current
              AND source.is_current
            """,
            (identity["source_profile_id"], profile_sha256.lower()),
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            "expected one exact current DB Profile matching source_profile_id "
            f"and local file SHA-256, got {len(rows)}: {path}"
        )
    row = rows[0]
    expected = {
        "profile_sha256": profile_sha256.lower(),
        "schema_version": identity["schema_version"],
        "notice_id": identity["notice_id"],
        "source_profile_id": identity["source_profile_id"],
        "source_kind": identity["source_kind"],
        "source_sha256": identity["source_sha256"],
        "structured_artifact_type": "structured_profile",
        "structured_content_sha256": profile_sha256.lower(),
    }
    if any(str(row.get(key) or "") != value for key, value in expected.items()):
        raise RuntimeError(f"current DB Profile identity does not match local bytes: {path}")
    return row["profile_version_pk"]


def _existing_hashes(connection: Any, config_pk: Any) -> dict[tuple[Any, str], str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT profile_version_pk, scope, lower(input_sha256) AS input_sha256
            FROM retrieval.existing_profile_embedding
            WHERE embedding_config_pk = %s
            """,
            (config_pk,),
        )
        return {
            (row["profile_version_pk"], row["scope"]): row["input_sha256"]
            for row in cursor.fetchall()
        }


def _upsert(connection: Any, config_pk: Any, pending: Sequence[PendingInput]) -> None:
    rows = []
    for item in pending:
        embedding_input = item.embedding_input
        pooled = mean_pool_embeddings(
            item.chunk_vectors,
            [chunk.token_count for chunk in embedding_input.chunks],
        )
        rows.append(
            (
                item.profile_version_pk,
                config_pk,
                embedding_input.scope,
                embedding_input.input_sha256,
                _vector_literal(pooled),
            )
        )
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO retrieval.existing_profile_embedding (
                profile_version_pk, embedding_config_pk, scope,
                input_sha256, embedding
            )
            VALUES (%s, %s, %s, %s, %s::extensions.vector)
            ON CONFLICT (profile_version_pk, embedding_config_pk, scope)
            DO UPDATE SET
                input_sha256 = EXCLUDED.input_sha256,
                embedding = EXCLUDED.embedding,
                created_at = now()
            """,
            rows,
        )
    connection.commit()


def _dry_run(
    paths: Sequence[Path],
    scopes: Sequence[str],
    limit: int | None,
    batch_size: int,
) -> int:
    counts: Counter[str] = Counter()
    maxima: Counter[str] = Counter()
    chunks: Counter[str] = Counter()
    selected = paths[:limit] if limit is not None else paths
    for path in selected:
        inputs = assemble_embedding_inputs(_load_profile(path))
        for scope in scopes:
            item = inputs[scope]
            counts[scope] += item.token_count
            maxima[scope] = max(maxima[scope], item.token_count)
            chunks[scope] += len(item.chunks)
    print(
        json.dumps(
            {
                "status": "dry_run",
                "profiles": len(selected),
                "scopes": list(scopes),
                "total_tokens": dict(counts),
                "max_tokens": dict(maxima),
                "chunks": dict(chunks),
                "max_input_tokens": MAX_INPUT_TOKENS,
                "batch_size": batch_size,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile_root",
        type=Path,
        help="100건 추출 루트, 공고 디렉터리, 또는 structured_profile JSON",
    )
    parser.add_argument(
        "--supabase-compose-env",
        type=Path,
        help="self-hosted Supabase .env; only DB values are loaded",
    )
    parser.add_argument(
        "--model",
        help=f"must remain {EMBEDDING_MODEL}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        help=f"OpenAI input count per request (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--strategy",
        choices=("all", "combined", "axes"),
        default="all",
        help="all=A+B, combined=A, axes=B",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dry_model = args.model or EMBEDDING_MODEL
    dry_batch_size = args.batch_size or DEFAULT_BATCH_SIZE
    if not 1 <= dry_batch_size <= 2048:
        parser.error("--batch-size must be between 1 and 2048 inputs")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if dry_model != EMBEDDING_MODEL:
        parser.error(f"this DB contract requires --model {EMBEDDING_MODEL}")
    scopes = {
        "all": ALL_SCOPES,
        "combined": ("combined",),
        "axes": ("purpose", "target", "support"),
    }[args.strategy]
    paths = _discover_profiles(args.profile_root.resolve())
    if not paths:
        parser.error(f"no structured profiles found under {args.profile_root}")
    if args.dry_run:
        return _dry_run(paths, scopes, args.limit, dry_batch_size)

    _load_dotenv()
    if args.supabase_compose_env:
        try:
            os.environ.update(load_local_supabase_settings(args.supabase_compose_env))
        except LocalSupabaseEnvError as error:
            parser.error(str(error))
    database_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    model = args.model or os.environ.get("OPENAI_EMBEDDING_MODEL") or EMBEDDING_MODEL
    try:
        batch_size = args.batch_size or int(
            os.environ.get("OPENAI_EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        )
    except ValueError:
        parser.error("OPENAI_EMBEDDING_BATCH_SIZE must be an integer")
    if not 1 <= batch_size <= 2048:
        parser.error("--batch-size must be between 1 and 2048 inputs")
    if model != EMBEDDING_MODEL:
        parser.error(f"this DB contract requires --model {EMBEDDING_MODEL}")
    if not database_url:
        parser.error("set SUPABASE_DB_URL/DATABASE_URL or use --supabase-compose-env")
    try:
        import psycopg
        from psycopg.rows import dict_row
        from openai import OpenAI, OpenAIError
    except ImportError as exc:
        parser.error(f"install backend runtime dependencies: {exc}")

    selected = paths[: args.limit] if args.limit is not None else paths
    # An empty conninfo intentionally lets libpq use PGHOST/PGPORT/PGUSER/
    # PGPASSWORD/PGDATABASE, which keeps passwords out of command arguments.
    connection = psycopg.connect(database_url, row_factory=dict_row)
    try:
        config = _configuration(connection)
        existing = _existing_hashes(connection, config["embedding_config_pk"])
        pending: list[PendingInput] = []
        skipped = 0
        for path in selected:
            profile, profile_sha256 = _load_profile_snapshot(path)
            profile_pk = _profile_version(
                connection,
                profile,
                path,
                profile_sha256=profile_sha256,
            )
            inputs = assemble_embedding_inputs(profile)
            for scope in scopes:
                embedding_input = inputs[scope]
                if existing.get((profile_pk, scope)) == embedding_input.input_sha256:
                    skipped += 1
                    continue
                pending.append(
                    PendingInput(
                        profile_version_pk=profile_pk,
                        notice_id=str(profile.get("notice_id") or ""),
                        source_profile_id=str(profile.get("source_profile_id") or ""),
                        embedding_input=embedding_input,
                    )
                )

        request_count = 0
        prompt_tokens = 0
        if pending:
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                parser.error("set OPENAI_API_KEY for changed or missing embeddings")
            client = OpenAI(timeout=60.0, max_retries=2)
            for batch in _batched_chunks(
                pending, max_inputs=batch_size, max_tokens=MAX_BATCH_TOKENS
            ):
                try:
                    response = client.embeddings.create(
                        model=model,
                        input=[chunk.text for _, chunk in batch],
                        dimensions=EMBEDDING_DIMENSIONS,
                        encoding_format="float",
                    )
                except OpenAIError as exc:
                    raise RuntimeError(
                        f"OpenAI embeddings request failed: {type(exc).__name__}"
                    ) from None
                ordered = sorted(response.data, key=lambda item: item.index)
                if len(ordered) != len(batch):
                    raise RuntimeError("OpenAI returned an incomplete embedding batch")
                for (pending_item, _), result in zip(batch, ordered):
                    vector = [float(value) for value in result.embedding]
                    if len(vector) != EMBEDDING_DIMENSIONS:
                        raise RuntimeError(
                            f"OpenAI returned {len(vector)} dimensions; expected {EMBEDDING_DIMENSIONS}"
                        )
                    pending_item.chunk_vectors.append(vector)
                request_count += 1
                prompt_tokens += int(
                    getattr(response.usage, "prompt_tokens", 0) or 0
                )

        if pending:
            _upsert(connection, config["embedding_config_pk"], pending)
        print(
            json.dumps(
                {
                    "status": "completed",
                    "profiles": len(selected),
                    "scopes": list(scopes),
                    "upserted": len(pending),
                    "skipped": skipped,
                    "openai_requests": request_count,
                    "prompt_tokens": prompt_tokens,
                    "embedding_config_pk": str(config["embedding_config_pk"]),
                },
                ensure_ascii=False,
            )
        )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
