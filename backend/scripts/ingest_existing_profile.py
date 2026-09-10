#!/usr/bin/env python3
"""Upload one completed Existing ingestion record into Supabase Storage and kb.*.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SERVICE_ROLE_KEY).
The script is idempotent for the same source/profile SHA-256 pair.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL
from psycopg.types.json import Jsonb

try:
    from .existing_kb_pack import PackValidationError, validate_notice_directory
except ImportError:  # direct ``python scripts/...`` execution
    from existing_kb_pack import PackValidationError, validate_notice_directory


EXISTING_SPECIFIC = {
    "payment_terms", "duplicate_support_conditions", "applicable_entity", "delivery_roles",
}


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects so privileged Storage headers never reach another URL."""

    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


class SupabaseStorage:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(f"{self.url}{path}", data=body, headers=request_headers, method=method)
        try:
            # urllib follows redirects by default and can carry Authorization
            # and apikey headers into the redirected request.  The dedicated
            # opener is the stdlib equivalent of follow_redirects=False.
            with _NO_REDIRECT_OPENER.open(request, timeout=60) as response:
                raw = response.read()
        except HTTPError as error:
            error.close()
            raise RuntimeError(
                f"Storage API request failed with HTTP {error.code}"
            ) from error
        return json.loads(raw.decode("utf-8")) if raw else None

    def upload(self, key: str, path: Path) -> None:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        request = Request(
            f"{self.url}/storage/v1/object/existing-kb/{quote(key, safe='/')}",
            data=path.read_bytes(),
            headers={
                "apikey": self.key, "Authorization": f"Bearer {self.key}",
                "Content-Type": content_type, "x-upsert": "true",
            },
            method="POST",
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=120) as response:
                response.read()
        except HTTPError as error:
            error.close()
            raise RuntimeError(
                f"Storage upload failed with HTTP {error.code}"
            ) from error


class KnowledgeBase:
    """Trusted-worker writer. kb is never exposed through PostgREST."""

    def __init__(self, database_url: str) -> None:
        self.connection = psycopg.connect(database_url, row_factory=dict_row)

    @staticmethod
    def _value(value: Any) -> Any:
        return Jsonb(value) if isinstance(value, dict) else value

    def one(self, table: str, **filters: Any) -> dict[str, Any] | None:
        where = SQL(" AND ").join(SQL("{} = %s").format(Identifier(key)) for key in filters)
        query = SQL("SELECT * FROM kb.{} WHERE ").format(Identifier(table)) + where
        with self.connection.cursor() as cursor:
            cursor.execute(query, list(filters.values()))
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"expected one {table} row for {filters}, got {len(rows)}")
        return rows[0] if rows else None

    def insert(self, table: str, row: dict[str, Any], *, on_conflict_ignore: bool = False) -> dict[str, Any] | None:
        columns = list(row)
        query = SQL("INSERT INTO kb.{} ({}) VALUES ({})").format(
            Identifier(table), SQL(", ").join(map(Identifier, columns)), SQL(", ").join(SQL("%s") for _ in columns),
        )
        if on_conflict_ignore:
            query += SQL(" ON CONFLICT DO NOTHING")
        query += SQL(" RETURNING *")
        with self.connection.cursor() as cursor:
            cursor.execute(query, [self._value(row[column]) for column in columns])
            result = cursor.fetchone()
        return result

    def patch(self, table: str, filters: dict[str, Any], row: dict[str, Any]) -> None:
        assignments = SQL(", ").join(SQL("{} = %s").format(Identifier(key)) for key in row)
        where = SQL(" AND ").join(SQL("{} = %s").format(Identifier(key)) for key in filters)
        query = SQL("UPDATE kb.{} SET ").format(Identifier(table)) + assignments + SQL(" WHERE ") + where
        with self.connection.cursor() as cursor:
            cursor.execute(query, [self._value(value) for value in row.values()] + list(filters.values()))

    def delete(self, table: str, **filters: Any) -> None:
        where = SQL(" AND ").join(SQL("{} = %s").format(Identifier(key)) for key in filters)
        with self.connection.cursor() as cursor:
            cursor.execute(SQL("DELETE FROM kb.{} WHERE ").format(Identifier(table)) + where, list(filters.values()))

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def artifact(
    storage: SupabaseStorage, database: KnowledgeBase, *, source_version_pk: str, source_sha256: str, notice_id: str,
    source_profile_id: str, artifact_type: str, logical_id: str | None, path: Path,
    schema_version: str | None,
) -> dict[str, Any]:
    content_sha256 = digest(path)
    suffix = path.suffix.lower().lstrip(".") or "bin"
    key = "/".join((notice_id, source_profile_id, source_sha256, artifact_type, f"{content_sha256}.{suffix}"))
    storage.upload(key, path)
    existing = database.one("artifact", storage_bucket="existing-kb", storage_object_key=key)
    if existing:
        return existing
    return database.insert("artifact", {
        "source_version_pk": source_version_pk, "artifact_type": artifact_type,
        "artifact_logical_id": logical_id, "storage_bucket": "existing-kb",
        "storage_object_key": key, "content_sha256": content_sha256,
        "mime_type": mimetypes.guess_type(path.name)[0], "size_bytes": path.stat().st_size,
        "schema_version": schema_version,
    })


def all_facts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for facts in profile.get("comparison_profile", {}).values():
        values.extend(facts or [])
    for component in profile.get("support_components", []):
        values.extend(component.get("facts", []))
    seen: set[str] = set()
    result = []
    for fact in values:
        if fact["fact_id"] not in seen:
            seen.add(fact["fact_id"])
            result.append(fact)
    return result


def upsert_delivery_roles(
    database: KnowledgeBase,
    profile_version_pk: str,
    profile: dict[str, Any],
) -> dict[str, int]:
    """Materialise the Existing-only delivery subtype, including old rows.

    Early versions of this importer stored ``delivery_roles`` only as generic
    ``fact_occurrence`` rows.  An otherwise identical Profile therefore takes
    the existing-profile early-return path.  This repair is intentionally run
    for both new and already-present Profile versions and uses only natural
    keys from the DDL, so repeated execution is idempotent.
    """

    role_count = organization_count = inserted_roles = inserted_organizations = 0
    for fact in all_facts(profile):
        if fact["field_name"] != "delivery_roles":
            continue
        role_count += 1
        occurrence = database.one(
            "fact_occurrence",
            profile_version_pk=profile_version_pk,
            fact_id=fact["fact_id"],
        )
        if not occurrence:
            raise RuntimeError(
                "cannot materialise delivery role because its fact_occurrence "
                f"is missing: {fact['fact_id']}"
            )
        fact_pk = occurrence["fact_pk"]
        role_values = {
            "canonical_role": fact.get("canonical_role"),
            "relation_container_type": "existing_program_profile.delivery_roles",
            "relation_container_data": {
                "role_raw": fact.get("role_raw"),
                "role_source_block_id": fact.get("role_source_block_id"),
                "role_source": fact.get("role_source"),
            },
        }
        existing_role = database.one("delivery_role", fact_pk=fact_pk)
        if existing_role:
            database.patch("delivery_role", {"fact_pk": fact_pk}, role_values)
        else:
            database.insert("delivery_role", {"fact_pk": fact_pk, **role_values})
            inserted_roles += 1

        for ordinal, organization in enumerate(fact.get("organization_names", [])):
            organization_count += 1
            source = organization["value_source"]
            natural_key = {
                "fact_pk": fact_pk,
                "source_block_id": source["source_block_id"],
                "start_char": source["start_char"],
                "end_char": source["end_char"],
            }
            values = {
                "value_raw": organization["value_raw"],
                "text_basis": source["text_basis"],
                "ordinal": ordinal,
            }
            if database.one("delivery_role_organization", **natural_key):
                database.patch("delivery_role_organization", natural_key, values)
            else:
                database.insert(
                    "delivery_role_organization", {**natural_key, **values}
                )
                inserted_organizations += 1
    return {
        "delivery_roles": role_count,
        "delivery_role_organizations": organization_count,
        "delivery_roles_inserted": inserted_roles,
        "delivery_role_organizations_inserted": inserted_organizations,
    }


def insert_profile_children(database: KnowledgeBase, profile_version_pk: str, profile: dict[str, Any]) -> dict[str, int]:
    component_pks: dict[str, str] = {}
    for ordinal, component in enumerate(profile.get("support_components", [])):
        row = database.insert("support_component", {
            "profile_version_pk": profile_version_pk,
            "support_component_id": component["support_component_id"],
            "component_kind": component["component_kind"], "name_raw": component.get("name_raw"),
            "name_status": component["name_status"], "name_source_block_id": component.get("name_source_block_id"),
            "source_block_ids": component.get("source_block_ids", []),
            "table_block_ids": component.get("table_block_ids", []), "ordinal": ordinal,
        })
        component_pks[component["support_component_id"]] = row["component_pk"]

    fact_pks: dict[str, str] = {}
    facts = all_facts(profile)
    for ordinal, fact in enumerate(facts):
        value_source = fact["value_source"]
        row = database.insert("fact_occurrence", {
            "profile_version_pk": profile_version_pk, "fact_id": fact["fact_id"],
            "fact_scope": "existing_specific" if fact["field_name"] in EXISTING_SPECIFIC else "comparison",
            "field_name": fact["field_name"], "value_raw": fact["value_raw"], "status": fact["status"],
            "scope": fact["scope"], "support_component_pk": component_pks.get(fact.get("support_component_id")),
            "subject_role": fact.get("subject_role"), "semantic_role": fact.get("semantic_role"),
            "source_block_id": value_source["source_block_id"], "start_char": value_source["start_char"],
            "end_char": value_source["end_char"], "text_basis": value_source["text_basis"], "ordinal": ordinal,
        })
        fact_pks[fact["fact_id"]] = row["fact_pk"]
        for evidence_ordinal, evidence in enumerate(fact.get("evidence", [])):
            database.insert("fact_evidence", {
                "fact_pk": row["fact_pk"], "source_block_id": evidence["source_block_id"],
                "section_id": evidence.get("section_id"), "common_ir_document_id": evidence["common_ir_document_id"],
                "common_ir_block_id": evidence["common_ir_block_id"], "common_ir_cell_id": evidence.get("common_ir_cell_id"),
                "common_ir_occurrence_ids": evidence.get("common_ir_occurrence_ids"), "ordinal": evidence_ordinal,
            })
        for context_ordinal, context in enumerate(fact.get("context_evidence", [])):
            database.insert("fact_context", {
                "fact_pk": row["fact_pk"], "source_block_id": context["source_block_id"],
                "section_id": context.get("section_id"), "context_text": context.get("text"),
                "common_ir_document_id": context.get("common_ir_document_id"),
                "common_ir_block_id": context.get("common_ir_block_id"), "common_ir_cell_id": context.get("common_ir_cell_id"),
                "common_ir_occurrence_ids": context.get("common_ir_occurrence_ids"), "ordinal": context_ordinal,
            })

    for fact in facts:
        source_fact_pk = fact_pks[fact["fact_id"]]
        for relation_type, key in (("modifies", "modifies_fact_ids"), ("recipient", "recipient_fact_ids"), ("basis", "basis_fact_ids")):
            for target_id in fact.get(key, []):
                database.insert("fact_relation", {"source_fact_pk": source_fact_pk, "target_fact_pk": fact_pks[target_id], "relation_type": relation_type})
        for component_id in fact.get("applicability_component_ids", []):
            database.insert("fact_component_link", {"fact_pk": source_fact_pk, "component_pk": component_pks[component_id], "link_type": "applicability"})

    facet_count = 0
    scale_count = 0
    target_count = target_source_count = target_dimension_count = 0
    for projection in profile.get("derived_projections", []):
        if projection["projection_type"] == "target_constraints":
            if projection.get("business_age") is not None:
                raise ValueError(
                    "target_constraints.business_age is not supported by the Existing importer"
                )
            target = database.insert("target_constraint", {
                "profile_version_pk": profile_version_pk,
                "status": projection["status"],
            })
            target_count += 1
            source_ordinal = 0
            for source_role, key in (
                ("positive", "positive_source_fact_ids"),
                ("exclusion", "exclusion_source_fact_ids"),
            ):
                for fact_id in projection.get(key, []):
                    database.insert("target_constraint_source", {
                        "target_constraint_pk": target["target_constraint_pk"],
                        "fact_pk": fact_pks[fact_id], "source_role": source_role,
                        "ordinal": source_ordinal,
                    })
                    source_ordinal += 1
                    target_source_count += 1
            dimension_ordinal = 0
            for dimension_key in ("entity_types", "regions", "industries"):
                for value in projection.get(dimension_key, []):
                    database.insert("target_constraint_dimension", {
                        "target_constraint_pk": target["target_constraint_pk"],
                        "dimension_key": dimension_key, "value_kind": "categorical",
                        "value_text": value, "ordinal": dimension_ordinal,
                    })
                    dimension_ordinal += 1
                    target_dimension_count += 1
        elif projection["projection_type"] == "support_facets":
            facet = database.insert("support_facet", {"profile_version_pk": profile_version_pk, "status": projection["status"], "ordinal": facet_count})
            facet_count += 1
            for ordinal, fact_id in enumerate(projection.get("source_fact_ids", [])):
                database.insert("support_facet_source", {"support_facet_pk": facet["support_facet_pk"], "fact_pk": fact_pks[fact_id], "ordinal": ordinal})
            for source_key, facet_type in (("activities", "activity"), ("methods", "method"), ("items", "item")):
                for ordinal, value in enumerate(projection.get(source_key, [])):
                    database.insert("support_facet_value", {"support_facet_pk": facet["support_facet_pk"], "facet_type": facet_type, "value_text": value, "ordinal": ordinal})
        elif projection["projection_type"] == "support_scale_measures":
            scale = database.insert("support_scale_projection", {"profile_version_pk": profile_version_pk, "status": projection["status"]})
            scale_count += 1
            for ordinal, measure in enumerate(projection.get("measures", [])):
                measure_row = dict(measure)
                source_fact_id = measure_row.pop("source_fact_id")
                database.insert("support_scale_measure", {
                    **measure_row, "support_scale_projection_pk": scale["support_scale_projection_pk"],
                    "source_fact_pk": fact_pks[source_fact_id], "ordinal": ordinal,
                })
        else:
            raise ValueError(f"unsupported Existing projection type: {projection['projection_type']!r}")
    delivery_counts = upsert_delivery_roles(database, profile_version_pk, profile)
    return {
        "components": len(component_pks), "facts": len(fact_pks),
        **delivery_counts, "support_facets": facet_count,
        "support_scale_projections": scale_count,
        "target_constraints": target_count,
        "target_constraint_sources": target_source_count,
        "target_constraint_dimensions": target_dimension_count,
    }


def ingest_record(
    record_path: Path,
    storage: SupabaseStorage,
    database: KnowledgeBase,
) -> dict[str, Any]:
    """Ingest one already-validated record into the caller's transaction."""

    record_path = record_path.resolve()
    notice_dir = record_path.parent.parent
    expected_record_path = notice_dir / "pipeline" / "ingestion_record.v0.1.json"
    if record_path != expected_record_path:
        raise PackValidationError(
            "ingestion record must use pipeline/ingestion_record.v0.1.json"
        )
    validate_notice_directory(notice_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    metadata, profile = record["portal_metadata"], record["structured_profile"]
    notice_id, source_profile_id = profile["notice_id"], profile["source_profile_id"]
    source_document = profile["source_documents"][0]
    source_sha256 = source_document["common_ir"]["source_sha256"]
    analysis_input = record["analysis"]["input_path"]
    source_path = notice_dir / "attachments" / analysis_input
    common_ir_path = notice_dir / "pipeline" / "common_ir_v1" / f"{metadata['notice_id']}.{source_document['format']}.json"
    selection_path = notice_dir / "pipeline" / "source_selection.json"
    profile_path = notice_dir / "pipeline" / "structured_profile.v0.2.json"
    for path in (source_path, common_ir_path, selection_path, profile_path, record_path):
        if not path.is_file():
            raise SystemExit(f"required artifact is missing: {path}")

    notice = database.one("notice", notice_id=notice_id)
    if notice:
        database.patch("notice", {"notice_pk": notice["notice_pk"]}, {"portal_metadata": metadata})
    else:
        notice = database.insert("notice", {"notice_id": notice_id, "portal_metadata": metadata})
    source_profile = database.one("source_profile", source_profile_id=source_profile_id)
    if not source_profile:
        source_profile = database.insert("source_profile", {"notice_pk": notice["notice_pk"], "source_profile_id": source_profile_id, "source_kind": source_document["format"]})
    elif source_profile["notice_pk"] != notice["notice_pk"]:
        raise RuntimeError(
            f"source_profile_id belongs to another notice: {source_profile_id}"
        )
    source_version = database.one("source_version", source_profile_pk=source_profile["source_profile_pk"], source_sha256=source_sha256)
    if not source_version:
        database.patch("source_version", {"source_profile_pk": source_profile["source_profile_pk"], "is_current": True}, {"is_current": False})
        source_version = database.insert("source_version", {
            "source_profile_pk": source_profile["source_profile_pk"], "source_sha256": source_sha256,
            "source_location": str(source_path), "source_url": source_document.get("source_url"),
            "notice_detail_url": source_document.get("notice_detail_url"), "is_current": True,
        })
    source = artifact(storage, database, source_version_pk=source_version["source_version_pk"], source_sha256=source_sha256, notice_id=notice_id, source_profile_id=source_profile_id, artifact_type="source", logical_id=None, path=source_path, schema_version=None)
    common_ir = artifact(storage, database, source_version_pk=source_version["source_version_pk"], source_sha256=source_sha256, notice_id=notice_id, source_profile_id=source_profile_id, artifact_type="common_ir", logical_id=source_document["common_ir"]["document_id"], path=common_ir_path, schema_version="common_ir_v1")
    candidate = artifact(storage, database, source_version_pk=source_version["source_version_pk"], source_sha256=source_sha256, notice_id=notice_id, source_profile_id=source_profile_id, artifact_type="candidate_pack", logical_id=profile["processing_metadata"]["candidate_pack"]["candidate_pack_id"], path=selection_path, schema_version="source_selection/v0.2")
    structured = artifact(storage, database, source_version_pk=source_version["source_version_pk"], source_sha256=source_sha256, notice_id=notice_id, source_profile_id=source_profile_id, artifact_type="structured_profile", logical_id=source_profile_id, path=profile_path, schema_version=profile["schema_version"])
    ingestion = artifact(storage, database, source_version_pk=source_version["source_version_pk"], source_sha256=source_sha256, notice_id=notice_id, source_profile_id=source_profile_id, artifact_type="format_ir", logical_id="ingestion_record", path=record_path, schema_version=record["schema_version"])
    for parent, child in ((source, common_ir), (common_ir, candidate), (candidate, structured), (structured, ingestion)):
        database.insert("artifact_lineage", {"parent_artifact_pk": parent["artifact_pk"], "child_artifact_pk": child["artifact_pk"], "relation_type": "input_to"}, on_conflict_ignore=True)

    profile_sha256 = digest(profile_path)
    existing = database.one("profile_version", source_version_pk=source_version["source_version_pk"], profile_sha256=profile_sha256)
    if existing:
        delivery_counts = upsert_delivery_roles(
            database, existing["profile_version_pk"], profile
        )
        return {"status": "already_ingested", "notice_id": notice_id, "profile_version_pk": str(existing["profile_version_pk"]), **delivery_counts}
    database.patch("profile_version", {"source_version_pk": source_version["source_version_pk"], "is_current": True}, {"is_current": False})
    profile_version = database.insert("profile_version", {
        "source_version_pk": source_version["source_version_pk"], "schema_version": profile["schema_version"],
        "profile_sha256": profile_sha256, "candidate_pack_artifact_pk": candidate["artifact_pk"],
        "structured_artifact_pk": structured["artifact_pk"], "is_current": True,
    })
    counts = insert_profile_children(
        database, profile_version["profile_version_pk"], profile
    )
    return {"status": "ingested", "notice_id": notice_id, "profile_version_pk": str(profile_version["profile_version_pk"]), **counts}


def run_transaction(
    database: KnowledgeBase,
    operation: Any,
) -> dict[str, Any]:
    """Commit one complete notice or roll every DB row back on failure."""

    try:
        result = operation()
        database.commit()
        return result
    except BaseException:
        database.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ingestion_record", type=Path)
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL") or os.environ.get("API_EXTERNAL_URL") or "http://127.0.0.1:8000")
    args = parser.parse_args()
    service_role_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SECRET_KEY")
    )
    database_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not service_role_key:
        parser.error("set SUPABASE_SERVICE_ROLE_KEY, SERVICE_ROLE_KEY, or SUPABASE_SECRET_KEY")
    if not database_url:
        parser.error("set SUPABASE_DB_URL or DATABASE_URL for the trusted kb writer")

    storage = SupabaseStorage(args.supabase_url, service_role_key)
    database = KnowledgeBase(database_url)
    try:
        result = run_transaction(
            database,
            lambda: ingest_record(args.ingestion_record, storage, database),
        )
    finally:
        database.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
