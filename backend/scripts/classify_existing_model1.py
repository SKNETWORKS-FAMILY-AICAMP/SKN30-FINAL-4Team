#!/usr/bin/env python3
"""Backfill versioned Model 1 classifications for every current Existing Profile.

The command is deliberately a trusted worker-side operation: it reads only
approved ``kb.*`` data, invokes Model 1 through the existing short-lived
subprocess adapter, records ``ops.*`` audit rows, and promotes a staged
configuration only after a transactionally rechecked full-corpus backfill.
It never exposes classifications through the browser API.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker.adapters.ml_subprocess import Model1SubprocessMlModel, model1_command
from worker.existing_model1 import (
    MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION,
    ExistingModel1Backfill,
    ExistingModel1Prediction,
    ExistingModel1Profile,
)

try:
    from .local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings
except ImportError:  # direct ``python scripts/...`` execution
    from local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings


RUN_TYPE = "existing_profile_classification"
COMPONENT_NAME = "backfill_existing_model1"
MODEL_ROLE = "existing_support_type_classification"
MODEL1_SERVING_DIR_ENV = "PREREVIEW_MODEL1_SERVING_DIR"
ML_PYTHON_EXECUTABLE_ENV = "PREREVIEW_ML_PYTHON_EXECUTABLE"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(BACKEND_ROOT / ".env", override=False)


def _model1_serving_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the same two Model 1 mount layouts accepted by ``worker.main``."""

    values = os.environ if env is None else env
    raw = values.get(MODEL1_SERVING_DIR_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{MODEL1_SERVING_DIR_ENV} is not set")
    configured = Path(raw).expanduser()
    if (configured / "inference.py").is_file():
        return configured
    nested = configured / "model1"
    if (nested / "inference.py").is_file():
        return nested
    raise RuntimeError("Model 1 inference.py was not found under configured serving directory")


def _configured_ml_python(env: Mapping[str, str] | None = None) -> str:
    """Return a verified child interpreter, following the worker's policy."""

    values = os.environ if env is None else env
    configured = values.get(ML_PYTHON_EXECUTABLE_ENV, "").strip()
    if not configured:
        return sys.executable
    candidate = Path(configured).expanduser()
    if candidate.is_file():
        # ``subprocess.run`` does not perform shell tilde expansion.
        return str(candidate)
    if shutil.which(configured) is None:
        raise RuntimeError(f"{ML_PYTHON_EXECUTABLE_ENV} does not identify an executable")
    return configured


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_model1_artifact(
    configuration: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> tuple[Path, str]:
    """Fail before a mutating run if mounted Model 1 bytes differ from config."""

    root = _model1_serving_root(env)
    artifact = root / "model" / "model.safetensors"
    if not artifact.is_file():
        raise RuntimeError("Model 1 model.safetensors was not found under serving directory")
    expected = str(configuration.get("artifact_sha256") or "").strip().lower()
    actual = _sha256_file(artifact)
    if actual != expected:
        raise RuntimeError("Model 1 model.safetensors SHA-256 does not match configuration")
    return root, actual


class PostgresExistingModel1Repository:
    """SQL implementation of the narrow backfill port.

    The promotion method is the only multi-step mutation: it locks the target
    configuration, re-reads the *current* corpus and complete result set, then
    changes the active flag in that same transaction.
    """

    def __init__(self, connection: Any, *, configuration: Mapping[str, Any]) -> None:
        self._connection = connection
        self._configuration = dict(configuration)

    def current_profiles(self) -> list[ExistingModel1Profile]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT profile.profile_version_pk,
                       notice.portal_metadata,
                       COALESCE(facts.rows, '[]'::jsonb) AS facts
                FROM kb.profile_version AS profile
                JOIN kb.source_version AS source
                  ON source.source_version_pk = profile.source_version_pk
                JOIN kb.source_profile AS source_profile
                  ON source_profile.source_profile_pk = source.source_profile_pk
                JOIN kb.notice AS notice ON notice.notice_pk = source_profile.notice_pk
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'field_name', fact.field_name,
                            'value_raw', fact.value_raw,
                            'status', fact.status,
                            'ordinal', fact.ordinal
                        ) ORDER BY fact.ordinal, fact.fact_pk
                    ) AS rows
                    FROM kb.fact_occurrence AS fact
                    WHERE fact.profile_version_pk = profile.profile_version_pk
                ) AS facts ON TRUE
                WHERE profile.is_current AND source.is_current
                ORDER BY profile.profile_version_pk
                """
            )
            rows = cursor.fetchall()
        return [
            ExistingModel1Profile(
                profile_version_id=str(row["profile_version_pk"]),
                portal_metadata=_mapping(row.get("portal_metadata")),
                facts=_mapping_list(row.get("facts")),
            )
            for row in rows
        ]

    def has_result(
        self, *, profile_version_id: str, configuration_id: str, input_sha256: str
    ) -> bool:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM retrieval.existing_profile_classification
                WHERE profile_version_pk = %s
                  AND classification_config_pk = %s
                  AND lower(input_sha256) = %s
                  AND execution_status = 'OK'
                """,
                (profile_version_id, configuration_id, input_sha256.lower()),
            )
            return cursor.fetchone() is not None

    def start_run(self, *, configuration_id: str, profile_count: int) -> str:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ops.processing_run (
                    run_type, status, pipeline_version, component_name,
                    component_version, started_at, run_metadata
                ) VALUES (
                    %s, 'running', %s, %s, %s, now(), %s::jsonb
                )
                RETURNING processing_run_pk
                """,
                (
                    RUN_TYPE,
                    MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION,
                    COMPONENT_NAME,
                    MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION,
                    json.dumps(
                        {
                            "classification_config_pk": configuration_id,
                            "profile_count": profile_count,
                            "dry_run": False,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            run_id = cursor.fetchone()["processing_run_pk"]
        self._connection.commit()
        return str(run_id)

    def record_invocation(
        self,
        *,
        processing_run_id: str,
        input_sha256: str,
        output_sha256: str | None,
        status: str,
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ops.model_invocation (
                    processing_run_pk, model_role, model_id, model_version,
                    prompt_version, input_hash, output_hash, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    processing_run_id,
                    MODEL_ROLE,
                    self._configuration["model_id"],
                    self._configuration["artifact_sha256"],
                    MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION,
                    input_sha256,
                    output_sha256,
                    status,
                ),
            )
        self._connection.commit()

    def write_result(
        self,
        *,
        profile_version_id: str,
        configuration_id: str,
        input_sha256: str,
        prediction: ExistingModel1Prediction,
        processing_run_id: str,
    ) -> None:
        # ``판단보류`` is a successful model execution.  Its raw label/status
        # remain auditable; the migration's service query CASE-gates the
        # effective downstream support type to NULL.
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO retrieval.existing_profile_classification (
                    profile_version_pk, classification_config_pk, input_sha256,
                    support_type_pred, confidence, prediction_status,
                    execution_status, reason_code, processing_run_pk
                ) VALUES (%s, %s, %s, %s, %s, %s, 'OK', NULL, %s)
                ON CONFLICT (profile_version_pk, classification_config_pk)
                DO UPDATE SET
                    input_sha256 = EXCLUDED.input_sha256,
                    support_type_pred = EXCLUDED.support_type_pred,
                    confidence = EXCLUDED.confidence,
                    prediction_status = EXCLUDED.prediction_status,
                    execution_status = EXCLUDED.execution_status,
                    reason_code = EXCLUDED.reason_code,
                    processing_run_pk = EXCLUDED.processing_run_pk,
                    created_at = now()
                """,
                (
                    profile_version_id,
                    configuration_id,
                    input_sha256,
                    prediction.support_type_pred,
                    prediction.confidence,
                    prediction.prediction_status,
                    processing_run_id,
                ),
            )
        self._connection.commit()

    def write_failure(
        self,
        *,
        profile_version_id: str,
        configuration_id: str,
        input_sha256: str,
        reason_code: str,
        processing_run_id: str,
    ) -> None:
        """Persist the local failure without manufacturing a prediction."""

        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO retrieval.existing_profile_classification (
                    profile_version_pk, classification_config_pk, input_sha256,
                    support_type_pred, confidence, prediction_status,
                    execution_status, reason_code, processing_run_pk
                ) VALUES (%s, %s, %s, NULL, NULL, NULL, 'FAILED', %s, %s)
                ON CONFLICT (profile_version_pk, classification_config_pk)
                DO UPDATE SET
                    input_sha256 = EXCLUDED.input_sha256,
                    support_type_pred = NULL,
                    confidence = NULL,
                    prediction_status = NULL,
                    execution_status = 'FAILED',
                    reason_code = EXCLUDED.reason_code,
                    processing_run_pk = EXCLUDED.processing_run_pk,
                    created_at = now()
                """,
                (
                    profile_version_id,
                    configuration_id,
                    input_sha256,
                    reason_code,
                    processing_run_id,
                ),
            )
        self._connection.commit()

    def verify_and_promote(
        self,
        *,
        configuration_id: str,
        expected_inputs: Mapping[str, str],
        processing_run_id: str,
    ) -> None:
        try:
            with self._connection.cursor() as cursor:
                # Serialises config promotion with a changing current KB
                # corpus.  Migration 31 uses this same named lock if profile
                # activation starts to invalidate classifications in future.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("pre-review-existing-kb-current-and-classification-v1",),
                )
                cursor.execute(
                    """
                    SELECT classification_config_pk, model_id, artifact_sha256,
                           input_assembly_version, is_active
                    FROM retrieval.classification_configuration
                    WHERE classification_config_pk = %s
                    FOR UPDATE
                    """,
                    (configuration_id,),
                )
                config = cursor.fetchone()
                if config is None:
                    raise RuntimeError("classification configuration disappeared")
                _validate_configuration(config)
                cursor.execute(
                    """
                    SELECT profile.profile_version_pk
                    FROM kb.profile_version AS profile
                    JOIN kb.source_version AS source
                      ON source.source_version_pk = profile.source_version_pk
                    WHERE profile.is_current AND source.is_current
                    ORDER BY profile.profile_version_pk
                    """
                )
                current = {str(row["profile_version_pk"]) for row in cursor.fetchall()}
                if current != set(expected_inputs):
                    raise RuntimeError("current Existing corpus changed during Model 1 backfill")
                cursor.execute(
                    """
                    SELECT profile_version_pk, lower(input_sha256) AS input_sha256,
                           support_type_pred, confidence, prediction_status,
                           execution_status
                    FROM retrieval.existing_profile_classification AS classification
                    JOIN kb.profile_version AS profile
                      ON profile.profile_version_pk = classification.profile_version_pk
                    JOIN kb.source_version AS source
                      ON source.source_version_pk = profile.source_version_pk
                    WHERE classification.classification_config_pk = %s
                      AND profile.is_current AND source.is_current
                    """,
                    (configuration_id,),
                )
                rows = {str(row["profile_version_pk"]): row for row in cursor.fetchall()}
                if set(rows) != current:
                    raise RuntimeError("Model 1 backfill is not complete for the current Existing corpus")
                for profile_id, expected_sha in expected_inputs.items():
                    row = rows[profile_id]
                    if (
                        row["input_sha256"] != expected_sha.lower()
                        or row["execution_status"] != "OK"
                        or not isinstance(row["support_type_pred"], str)
                        or row["confidence"] is None
                        or not isinstance(row["prediction_status"], str)
                    ):
                        raise RuntimeError("Model 1 result verification failed")
                cursor.execute(
                    """
                    UPDATE retrieval.classification_configuration
                    SET is_active = FALSE
                    WHERE is_active AND classification_config_pk <> %s
                    """,
                    (configuration_id,),
                )
                cursor.execute(
                    """
                    UPDATE retrieval.classification_configuration
                    SET is_active = TRUE
                    WHERE classification_config_pk = %s
                    """,
                    (configuration_id,),
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def finish_run(
        self, *, processing_run_id: str, succeeded: bool, error_code: str | None = None
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ops.processing_run
                SET status = %s, finished_at = now(), error_code = %s,
                    error_message = CASE WHEN %s IS NULL THEN NULL ELSE 'Existing Model 1 backfill failed.' END
                WHERE processing_run_pk = %s AND status = 'running'
                """,
                ("succeeded" if succeeded else "failed", error_code, error_code, processing_run_id),
            )
        self._connection.commit()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _validate_configuration(row: Mapping[str, Any]) -> None:
    if row.get("model_id") != "model_1_support_type":
        raise RuntimeError("configuration is not for model_1_support_type")
    if row.get("input_assembly_version") != MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION:
        raise RuntimeError("classification configuration input assembly version does not match")
    artifact = row.get("artifact_sha256")
    if not isinstance(artifact, str) or len(artifact.strip()) != 64:
        raise RuntimeError("classification configuration artifact SHA-256 is invalid")


def _configuration(connection: Any, configuration_id: str) -> Mapping[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT classification_config_pk, model_id, artifact_sha256,
                   input_assembly_version, producer_version, is_active
            FROM retrieval.classification_configuration
            WHERE classification_config_pk = %s
            """,
            (configuration_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("classification configuration was not found")
    _validate_configuration(row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration-id", required=True, help="staged Model 1 classification configuration UUID")
    parser.add_argument("--supabase-compose-env", type=Path, help="self-hosted Supabase .env; only DB values are loaded")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--dry-run", action="store_true", help="read and assemble current KB inputs without inference or writes")
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    _load_dotenv()
    if args.supabase_compose_env:
        try:
            os.environ.update(load_local_supabase_settings(args.supabase_compose_env))
        except LocalSupabaseEnvError as error:
            parser.error(str(error))
    database_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("set SUPABASE_DB_URL/DATABASE_URL or use --supabase-compose-env")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        parser.error(f"install backend runtime dependencies: {error}")

    connection = psycopg.connect(database_url, row_factory=dict_row)
    try:
        configuration = _configuration(connection, args.configuration_id)
        # A dry run is strictly read-only and intentionally does not require
        # either a mounted artifact or an ML interpreter.  A real backfill
        # verifies both before it can create a processing run or promote data.
        python_executable = (
            _configured_ml_python() if not args.dry_run else os.environ.get(
                ML_PYTHON_EXECUTABLE_ENV, ""
            ).strip() or sys.executable
        )
        if not args.dry_run:
            _verify_model1_artifact(configuration)
        repository = PostgresExistingModel1Repository(connection, configuration=configuration)
        model = Model1SubprocessMlModel(
            model1_command(python_executable=python_executable),
            timeout_seconds=args.timeout_seconds,
            artifact_version=str(configuration["artifact_sha256"]),
        )
        summary = ExistingModel1Backfill(
            repository, model, configuration_id=args.configuration_id
        ).run(dry_run=args.dry_run)
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "status": "dry_run" if summary.dry_run else "completed",
                "current_profiles": summary.current_profiles,
                "predicted": summary.predicted,
                "skipped": summary.skipped,
                "promoted": summary.promoted,
                "configuration_id": args.configuration_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
