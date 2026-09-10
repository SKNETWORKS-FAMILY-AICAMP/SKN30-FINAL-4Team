#!/usr/bin/env python3
"""Read-only verification of an Existing KB bootstrap against its data pack."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, BinaryIO, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.retrieval.embedding_inputs import assemble_embedding_inputs
try:
    from .existing_kb_pack import PackValidationError, validate_directory
    from .ingest_existing_profile import all_facts
    from .local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings
except ImportError:  # direct ``python scripts/...`` execution
    from existing_kb_pack import PackValidationError, validate_directory
    from ingest_existing_profile import all_facts
    from local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings


EXPECTED_ARTIFACT_TYPES = {
    "source",
    "common_ir",
    "candidate_pack",
    "structured_profile",
    "format_ir",
}
EXPECTED_EMBEDDING_SCOPES = {"purpose", "target", "support", "combined"}
EXPECTED_LINEAGE_EDGES = {
    ("source", "common_ir", "input_to"),
    ("common_ir", "candidate_pack", "input_to"),
    ("candidate_pack", "structured_profile", "input_to"),
    ("structured_profile", "format_ir", "input_to"),
}
PRESERVED_COUNT_FIELDS = (
    "facts",
    "support_components",
    "fact_evidence",
    "fact_context",
    "fact_relations",
    "fact_component_links",
    "delivery_roles",
    "delivery_role_organizations",
    "support_facets",
    "support_facet_sources",
    "support_facet_values",
    "support_scale_projections",
    "support_scale_measures",
    "target_constraints",
    "target_constraint_sources",
    "target_constraint_dimensions",
)


def _load_backend_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(BACKEND_ROOT / ".env", override=False)


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _expected(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record_path in sorted(root.glob("*/pipeline/ingestion_record.v0.1.json")):
        notice_dir = record_path.parent.parent
        record = json.loads(record_path.read_text(encoding="utf-8"))
        profile = record["structured_profile"]
        profile_path = notice_dir / "pipeline" / "structured_profile.v0.2.json"
        source_profile_id = profile["source_profile_id"]
        source_document = profile["source_documents"][0]
        source_sha256 = source_document["common_ir"]["source_sha256"]
        paths = {
            "source": notice_dir / "attachments" / record["analysis"]["input_path"],
            "common_ir": notice_dir / record["analysis"]["common_ir_path"],
            "candidate_pack": notice_dir / "pipeline" / "source_selection.json",
            "structured_profile": profile_path,
            "format_ir": record_path,
        }
        artifacts: dict[str, dict[str, Any]] = {}
        for artifact_type, artifact_path in paths.items():
            content_sha256 = _digest(artifact_path)
            suffix = artifact_path.suffix.lower().lstrip(".") or "bin"
            storage_object_key = "/".join(
                (
                    profile["notice_id"],
                    source_profile_id,
                    source_sha256,
                    artifact_type,
                    f"{content_sha256}.{suffix}",
                )
            )
            artifacts[artifact_type] = {
                "storage_bucket": "existing-kb",
                "storage_object_key": storage_object_key,
                "content_sha256": content_sha256,
                "size_bytes": artifact_path.stat().st_size,
            }
        facts = all_facts(profile)
        components = profile.get("support_components", [])
        projections = Counter(
            projection["projection_type"]
            for projection in profile.get("derived_projections", [])
        )
        inputs = assemble_embedding_inputs(profile)
        result[source_profile_id] = {
            "profile_sha256": _digest(profile_path),
            "artifacts": artifacts,
            "lineage_edges": {
                (
                    artifacts[parent_type]["storage_object_key"],
                    artifacts[child_type]["storage_object_key"],
                    relation_type,
                )
                for parent_type, child_type, relation_type in EXPECTED_LINEAGE_EDGES
            },
            "facts": len(facts),
            "support_components": len(components),
            "fact_evidence": sum(len(fact.get("evidence", [])) for fact in facts),
            "fact_context": sum(len(fact.get("context_evidence", [])) for fact in facts),
            "fact_relations": sum(
                len(fact.get(key, []))
                for fact in facts
                for key in (
                    "modifies_fact_ids",
                    "recipient_fact_ids",
                    "basis_fact_ids",
                )
            ),
            "fact_component_links": sum(
                len(fact.get("applicability_component_ids", [])) for fact in facts
            ),
            "delivery_roles": sum(
                fact["field_name"] == "delivery_roles" for fact in facts
            ),
            "delivery_role_organizations": sum(
                len(fact.get("organization_names", []))
                for fact in facts
                if fact["field_name"] == "delivery_roles"
            ),
            "support_facets": projections["support_facets"],
            "support_facet_sources": sum(
                len(projection.get("source_fact_ids", []))
                for projection in profile.get("derived_projections", [])
                if projection["projection_type"] == "support_facets"
            ),
            "support_facet_values": sum(
                sum(
                    len(projection.get(key, []))
                    for key in ("activities", "methods", "items")
                )
                for projection in profile.get("derived_projections", [])
                if projection["projection_type"] == "support_facets"
            ),
            "support_scale_projections": projections["support_scale_measures"],
            "support_scale_measures": sum(
                len(projection.get("measures", []))
                for projection in profile.get("derived_projections", [])
                if projection["projection_type"] == "support_scale_measures"
            ),
            "target_constraints": projections["target_constraints"],
            "target_constraint_sources": sum(
                len(projection.get(key, []))
                for projection in profile.get("derived_projections", [])
                if projection["projection_type"] == "target_constraints"
                for key in (
                    "positive_source_fact_ids",
                    "exclusion_source_fact_ids",
                )
            ),
            "target_constraint_dimensions": sum(
                len(projection.get(key, []))
                for projection in profile.get("derived_projections", [])
                if projection["projection_type"] == "target_constraints"
                for key in ("entity_types", "regions", "industries")
            ),
            "embedding_hashes": {
                scope: item.input_sha256 for scope, item in inputs.items()
            },
        }
    return result


def _artifact_contract_matches(
    expected_artifacts: dict[str, dict[str, Any]],
    actual_artifacts: list[dict[str, Any]],
) -> bool:
    """Compare every content-addressed artifact without masking duplicates."""

    if len(actual_artifacts) != len(expected_artifacts):
        return False
    by_type: dict[str, list[dict[str, Any]]] = {}
    for artifact in actual_artifacts:
        by_type.setdefault(str(artifact["artifact_type"]), []).append(artifact)
    if set(by_type) != set(expected_artifacts):
        return False
    for artifact_type, wanted in expected_artifacts.items():
        rows = by_type[artifact_type]
        if len(rows) != 1:
            return False
        actual = rows[0]
        try:
            values_match = all(
                (
                    str(actual[key]).lower()
                    if key == "content_sha256"
                    else int(actual[key])
                    if key == "size_bytes"
                    else actual[key]
                )
                == wanted[key]
                for key in (
                    "storage_bucket",
                    "storage_object_key",
                    "content_sha256",
                    "size_bytes",
                )
            )
        except (KeyError, TypeError, ValueError):
            return False
        if not values_match:
            return False
    return True


def _profile_artifact_refs_match(row: dict[str, Any]) -> bool:
    by_type = {
        artifact["artifact_type"]: artifact
        for artifact in row.get("artifacts", [])
    }
    candidate = by_type.get("candidate_pack")
    structured = by_type.get("structured_profile")
    return bool(
        candidate
        and structured
        and str(candidate["artifact_pk"]) == str(row["candidate_pack_artifact_pk"])
        and str(structured["artifact_pk"])
        == str(row["structured_artifact_pk"])
    )


def _bounded(values: list[str]) -> dict[str, Any]:
    return {"count": len(values), "examples": values[:20]}


def evaluate_bootstrap(
    expected: dict[str, dict[str, Any]],
    profile_rows: dict[str, dict[str, Any]],
    embeddings: dict[str, dict[str, str]],
    *,
    require_embeddings: bool,
) -> dict[str, Any]:
    """Compare DB projections with pack-derived counts without doing I/O."""

    missing: list[str] = []
    mismatched: list[str] = []
    artifact_mismatched: list[str] = []
    lineage_mismatched: list[str] = []
    profile_artifact_ref_mismatched: list[str] = []
    embedding_mismatched: list[str] = []
    fully_embedded = 0
    for source_profile_id, wanted in expected.items():
        row = profile_rows.get(source_profile_id)
        if row is None:
            missing.append(source_profile_id)
            continue
        if row["profile_sha256"] != wanted["profile_sha256"]:
            mismatched.append(source_profile_id)
        if any(int(row[key]) != wanted[key] for key in PRESERVED_COUNT_FIELDS):
            mismatched.append(source_profile_id)
        if "artifacts" in wanted:
            if not _artifact_contract_matches(
                wanted["artifacts"], row.get("artifacts", [])
            ):
                artifact_mismatched.append(source_profile_id)
            elif not _profile_artifact_refs_match(row):
                profile_artifact_ref_mismatched.append(source_profile_id)
        elif set(row["artifact_types"]) != EXPECTED_ARTIFACT_TYPES:
            artifact_mismatched.append(source_profile_id)
        if "lineage_edges" in wanted and set(row.get("lineage_edges", [])) != set(
            wanted["lineage_edges"]
        ):
            lineage_mismatched.append(source_profile_id)
        actual_embeddings = embeddings.get(source_profile_id, {})
        expected_hashes = wanted["embedding_hashes"]
        if set(actual_embeddings) == EXPECTED_EMBEDDING_SCOPES and all(
            actual_embeddings[scope] == expected_hashes[scope]
            for scope in EXPECTED_EMBEDDING_SCOPES
        ):
            fully_embedded += 1
        elif actual_embeddings or require_embeddings:
            embedding_mismatched.append(source_profile_id)

    invalid = bool(
        missing
        or mismatched
        or artifact_mismatched
        or lineage_mismatched
        or profile_artifact_ref_mismatched
        or embedding_mismatched
    )
    return {
        "status": "invalid" if invalid else "valid",
        "database_profiles": len(profile_rows),
        "fully_embedded_profiles": fully_embedded,
        "embeddings_required": require_embeddings,
        "missing_profiles": _bounded(sorted(missing)),
        "mismatched_profiles": _bounded(sorted(set(mismatched))),
        "mismatched_artifacts": _bounded(sorted(artifact_mismatched)),
        "mismatched_lineage": _bounded(sorted(lineage_mismatched)),
        "mismatched_profile_artifact_refs": _bounded(
            sorted(profile_artifact_ref_mismatched)
        ),
        "mismatched_embeddings": _bounded(sorted(embedding_mismatched)),
    }


class StorageVerificationError(RuntimeError):
    """A private object cannot be proven equal to the validated pack."""


def _stream_digest(stream: BinaryIO, *, expected_size: int) -> tuple[str, int]:
    digest = sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(chunk)
        if size > expected_size:
            raise StorageVerificationError("downloaded object exceeds expected size")
        digest.update(chunk)
    return digest.hexdigest(), size


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def verify_storage_objects(
    expected: dict[str, dict[str, Any]],
    *,
    supabase_url: str,
    service_role_key: str,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Download private objects and compare bytes; never expose response bodies."""

    failures: list[str] = []
    verified = 0
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Accept-Encoding": "identity",
    }
    open_request = opener or _NO_REDIRECT_OPENER.open
    for source_profile_id, profile in expected.items():
        for artifact_type, artifact in profile["artifacts"].items():
            bucket = quote(str(artifact["storage_bucket"]), safe="")
            object_key = quote(str(artifact["storage_object_key"]), safe="/")
            request = Request(
                f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_key}",
                headers=headers,
                method="GET",
            )
            try:
                with open_request(request, timeout=60) as response:
                    actual_sha256, actual_size = _stream_digest(
                        response, expected_size=int(artifact["size_bytes"])
                    )
            except HTTPError as error:
                error.close()
                failures.append(f"{source_profile_id}:{artifact_type}")
                continue
            except (URLError, OSError, StorageVerificationError):
                failures.append(f"{source_profile_id}:{artifact_type}")
                continue
            if (
                actual_size != int(artifact["size_bytes"])
                or actual_sha256 != artifact["content_sha256"]
            ):
                failures.append(f"{source_profile_id}:{artifact_type}")
                continue
            verified += 1
    return {
        "status": "invalid" if failures else "valid",
        "verified_artifacts": verified,
        "failed_artifacts": _bounded(sorted(failures)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_root", type=Path, help="검증을 마친 추출 루트")
    parser.add_argument("--expected-count", type=int, help="전체 팩의 예상 공고 수")
    parser.add_argument("--supabase-compose-env", type=Path, help="호스트 실행용 공식 Supabase Compose .env")
    parser.add_argument("--require-embeddings", action="store_true", help="네 retrieval scope까지 모두 요구")
    parser.add_argument(
        "--verify-storage",
        action="store_true",
        help="private Storage 객체 전체의 실제 byte SHA-256/크기도 확인",
    )
    args = parser.parse_args()
    _load_backend_env()
    if args.supabase_compose_env:
        try:
            os.environ.update(load_local_supabase_settings(args.supabase_compose_env))
        except LocalSupabaseEnvError as error:
            parser.error(str(error))
    database_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("set SUPABASE_DB_URL or DATABASE_URL in the environment or backend/.env")
    supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get("API_EXTERNAL_URL")
    service_role_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SECRET_KEY")
    )
    if args.verify_storage and (not supabase_url or not service_role_key):
        parser.error(
            "--verify-storage requires SUPABASE_URL and a service-role key in the environment"
        )
    try:
        pack_summary = validate_directory(
            args.pack_root, expected_count=args.expected_count
        )
    except PackValidationError as error:
        print(json.dumps({"status": "invalid_pack", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        parser.error(f"install backend runtime dependencies: {error}")

    expected = _expected(args.pack_root.resolve())
    source_profile_ids = sorted(expected)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    source_profile.source_profile_id,
                    profile.profile_sha256,
                    profile.profile_version_pk,
                    profile.candidate_pack_artifact_pk,
                    profile.structured_artifact_pk,
                    ARRAY(
                        SELECT DISTINCT artifact.artifact_type
                        FROM kb.artifact AS artifact
                        WHERE artifact.source_version_pk = source.source_version_pk
                        ORDER BY artifact.artifact_type
                    ) AS artifact_types,
                    (SELECT count(*) FROM kb.fact_occurrence AS fact
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS facts,
                    (SELECT count(*) FROM kb.support_component AS component
                     WHERE component.profile_version_pk = profile.profile_version_pk) AS support_components,
                    (SELECT count(*) FROM kb.fact_evidence AS evidence
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = evidence.fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS fact_evidence,
                    (SELECT count(*) FROM kb.fact_context AS context
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = context.fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS fact_context,
                    (SELECT count(*) FROM kb.fact_relation AS relation
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = relation.source_fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS fact_relations,
                    (SELECT count(*) FROM kb.fact_component_link AS link
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = link.fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS fact_component_links,
                    (SELECT count(*) FROM kb.delivery_role AS role
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = role.fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS delivery_roles,
                    (SELECT count(*) FROM kb.delivery_role_organization AS organization
                     JOIN kb.fact_occurrence AS fact ON fact.fact_pk = organization.fact_pk
                     WHERE fact.profile_version_pk = profile.profile_version_pk) AS delivery_role_organizations,
                    (SELECT count(*) FROM kb.support_facet AS facet
                     WHERE facet.profile_version_pk = profile.profile_version_pk) AS support_facets,
                    (SELECT count(*) FROM kb.support_facet_source AS source_row
                     JOIN kb.support_facet AS facet
                       ON facet.support_facet_pk = source_row.support_facet_pk
                     WHERE facet.profile_version_pk = profile.profile_version_pk) AS support_facet_sources,
                    (SELECT count(*) FROM kb.support_facet_value AS value_row
                     JOIN kb.support_facet AS facet
                       ON facet.support_facet_pk = value_row.support_facet_pk
                     WHERE facet.profile_version_pk = profile.profile_version_pk) AS support_facet_values,
                    (SELECT count(*) FROM kb.support_scale_projection AS scale
                     WHERE scale.profile_version_pk = profile.profile_version_pk) AS support_scale_projections,
                    (SELECT count(*) FROM kb.support_scale_measure AS measure
                     JOIN kb.support_scale_projection AS scale
                       ON scale.support_scale_projection_pk = measure.support_scale_projection_pk
                     WHERE scale.profile_version_pk = profile.profile_version_pk) AS support_scale_measures,
                    (SELECT count(*) FROM kb.target_constraint AS target
                     WHERE target.profile_version_pk = profile.profile_version_pk) AS target_constraints,
                    (SELECT count(*) FROM kb.target_constraint_source AS target_source
                     JOIN kb.target_constraint AS target
                       ON target.target_constraint_pk = target_source.target_constraint_pk
                     WHERE target.profile_version_pk = profile.profile_version_pk) AS target_constraint_sources,
                    (SELECT count(*) FROM kb.target_constraint_dimension AS target_dimension
                     JOIN kb.target_constraint AS target
                       ON target.target_constraint_pk = target_dimension.target_constraint_pk
                     WHERE target.profile_version_pk = profile.profile_version_pk) AS target_constraint_dimensions
                FROM kb.source_profile AS source_profile
                JOIN kb.source_version AS source
                  ON source.source_profile_pk = source_profile.source_profile_pk
                 AND source.is_current
                JOIN kb.profile_version AS profile
                  ON profile.source_version_pk = source.source_version_pk
                 AND profile.is_current
                WHERE source_profile.source_profile_id = ANY(%s)
                """,
                (source_profile_ids,),
            )
            profile_rows = {
                row["source_profile_id"]: dict(row) for row in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT source_profile.source_profile_id, artifact.artifact_pk,
                       artifact.artifact_type,
                       lower(artifact.content_sha256) AS content_sha256,
                       artifact.size_bytes, artifact.storage_bucket,
                       artifact.storage_object_key
                FROM kb.source_profile AS source_profile
                JOIN kb.source_version AS source
                  ON source.source_profile_pk = source_profile.source_profile_pk
                 AND source.is_current
                JOIN kb.artifact AS artifact
                  ON artifact.source_version_pk = source.source_version_pk
                WHERE source_profile.source_profile_id = ANY(%s)
                ORDER BY source_profile.source_profile_id, artifact.artifact_type,
                         artifact.artifact_pk
                """,
                (source_profile_ids,),
            )
            for row in cursor.fetchall():
                profile_rows.get(row["source_profile_id"], {}).setdefault(
                    "artifacts", []
                ).append(dict(row))
            cursor.execute(
                """
                SELECT DISTINCT source_profile.source_profile_id,
                       parent.storage_object_key AS parent_key,
                       child.storage_object_key AS child_key,
                       lineage.relation_type
                FROM kb.source_profile AS source_profile
                JOIN kb.source_version AS source
                  ON source.source_profile_pk = source_profile.source_profile_pk
                 AND source.is_current
                JOIN kb.artifact AS anchor
                  ON anchor.source_version_pk = source.source_version_pk
                JOIN kb.artifact_lineage AS lineage
                  ON lineage.parent_artifact_pk = anchor.artifact_pk
                  OR lineage.child_artifact_pk = anchor.artifact_pk
                JOIN kb.artifact AS parent
                  ON parent.artifact_pk = lineage.parent_artifact_pk
                JOIN kb.artifact AS child
                  ON child.artifact_pk = lineage.child_artifact_pk
                WHERE source_profile.source_profile_id = ANY(%s)
                ORDER BY source_profile.source_profile_id,
                         parent.storage_object_key, child.storage_object_key,
                         lineage.relation_type
                """,
                (source_profile_ids,),
            )
            for row in cursor.fetchall():
                profile_rows.get(row["source_profile_id"], {}).setdefault(
                    "lineage_edges", []
                ).append(
                    (row["parent_key"], row["child_key"], row["relation_type"])
                )
            cursor.execute(
                """
                SELECT source_profile.source_profile_id, embedding.scope,
                       lower(embedding.input_sha256) AS input_sha256
                FROM kb.source_profile AS source_profile
                JOIN kb.source_version AS source
                  ON source.source_profile_pk = source_profile.source_profile_pk
                 AND source.is_current
                JOIN kb.profile_version AS profile
                  ON profile.source_version_pk = source.source_version_pk
                 AND profile.is_current
                JOIN retrieval.existing_profile_embedding AS embedding
                  ON embedding.profile_version_pk = profile.profile_version_pk
                JOIN retrieval.embedding_configuration AS configuration
                  ON configuration.embedding_config_pk = embedding.embedding_config_pk
                 AND configuration.is_active
                WHERE source_profile.source_profile_id = ANY(%s)
                """,
                (source_profile_ids,),
            )
            embedding_rows = cursor.fetchall()

    embeddings: dict[str, dict[str, str]] = {}
    for row in embedding_rows:
        embeddings.setdefault(row["source_profile_id"], {})[row["scope"]] = row[
            "input_sha256"
        ]

    output = evaluate_bootstrap(
        expected,
        profile_rows,
        embeddings,
        require_embeddings=args.require_embeddings,
    )
    output["pack_notices"] = pack_summary.notices
    if args.verify_storage:
        storage_result = verify_storage_objects(
            expected,
            supabase_url=str(supabase_url),
            service_role_key=str(service_role_key),
        )
        output["storage"] = storage_result
        if storage_result["status"] != "valid":
            output["status"] = "invalid"
    invalid = output["status"] != "valid"
    target = sys.stderr if invalid else sys.stdout
    print(json.dumps(output, ensure_ascii=False, sort_keys=True), file=target)
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
