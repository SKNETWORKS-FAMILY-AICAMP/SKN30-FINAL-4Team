from __future__ import annotations

from email.message import Message
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile, ZipInfo

import pytest
from urllib.error import HTTPError
from urllib.request import BaseHandler, Request, build_opener
from urllib.response import addinfourl


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from existing_kb_pack import (
    MAX_MEMBER_BYTES,
    PackValidationError,
    _verify_manifest,
    extract_validated_pack,
    validate_directory,
    validate_notice_directory,
    validate_pack,
)
import embed_existing_profiles as embedding_cli
import ingest_existing_profile as single_cli
import ingest_existing_profiles as batch_cli
from ingest_existing_profile import (
    KnowledgeBase,
    SupabaseStorage,
    _NoRedirectHandler as ImporterNoRedirectHandler,
    insert_profile_children,
    run_transaction,
    upsert_delivery_roles,
)
from local_supabase_env import load_local_supabase_settings
import verify_existing_kb as verify_cli
from verify_existing_kb import (
    EXPECTED_ARTIFACT_TYPES,
    EXPECTED_EMBEDDING_SCOPES,
    PRESERVED_COUNT_FIELDS,
    _NoRedirectHandler,
    _expected,
    evaluate_bootstrap,
    verify_storage_objects,
)


def _write_pack(root: Path) -> Path:
    notice_id = "PBLN_000000000000001"
    notice = root / notice_id
    attachment = notice / "attachments" / "body_output__fixture.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"%PDF-1.4\nfixture\n")
    source_hash = sha256(attachment.read_bytes()).hexdigest()
    text = "수행기관A"
    fact = {
        "fact_id": "delivery-1",
        "field_name": "delivery_roles",
        "value_raw": text,
        "status": "identified",
        "scope": "notice",
        "support_component_id": None,
        "applicability_component_ids": [],
        "modifies_fact_ids": [],
        "recipient_fact_ids": [],
        "basis_fact_ids": [],
        "subject_role": None,
        "semantic_role": None,
        "evidence": [],
        "context_evidence": [],
        "canonical_role": "operating_agency",
        "role_raw": "수행기관",
        "role_source_block_id": "block-1",
        "role_source": {
            "source_block_id": "block-1",
            "start_char": 0,
            "end_char": 4,
            "text_basis": "common_ir_v1_candidate_pack",
        },
        "organization_names": [
            {
                "value_raw": text,
                "value_source": {
                    "source_block_id": "block-1",
                    "start_char": 0,
                    "end_char": len(text),
                    "text_basis": "common_ir_v1_candidate_pack",
                },
            }
        ],
        "value_source": {
            "source_block_id": "block-1",
            "start_char": 0,
            "end_char": len(text),
            "text_basis": "common_ir_v1_candidate_pack",
        },
    }
    profile = {
        "schema_version": "existing_program_profile/v0.2",
        "notice_id": notice_id,
        "source_profile_id": f"pdf:{notice_id}",
        "source_documents": [
            {
                "format": "pdf",
                "common_ir": {
                    "document_id": f"pdf:{notice_id}",
                    "schema_version": "common_ir_v1",
                    "source_kind": "pdf",
                    "source_sha256": source_hash,
                },
            }
        ],
        "identity": {},
        "comparison_profile": {"delivery_roles": [fact]},
        "support_components": [],
        "derived_projections": [],
        "processing_metadata": {
            "candidate_pack": {
                "candidate_pack_id": "pack-1",
                "common_ir_document_id": f"pdf:{notice_id}",
                "common_ir_source_sha256": source_hash,
            }
        },
        "table_catalog": [],
        "unresolved_observations": [],
        "unresolved_relations": [],
    }
    metadata = {
        "notice_id": notice_id,
        "pblanc_id": notice_id,
        "title": "테스트 공고",
        "apply_period": "2026-01-01 ~ 2026-01-31",
        "ministry": "테스트 부처",
        "executing_agency": "테스트 기관",
        "registered_at": "2026-01-01",
        "detail_url": "https://example.invalid/notices/1",
    }
    selection = {
        "common_ir_identity": {
            "document_id": f"pdf:{notice_id}",
            "source_kind": "pdf",
            "source_sha256": source_hash,
        },
        "candidate_pack_lineage": {"candidate_pack_id": "pack-1"},
        "source_block_texts": {"block-1": text},
    }
    common_ir = {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"pdf:{notice_id}",
            "source_kind": "pdf",
            "provenance": {"source_sha256": source_hash},
        },
        "blocks": [],
        "relations": [],
        "conflicts": [],
    }
    analysis = {
        "input_path": attachment.name,
        "common_ir_path": f"pipeline/common_ir_v1/{notice_id}.pdf.json",
    }
    record = {
        "schema_version": "bizinfo_existing_ingestion_record/v0.1",
        "generated_at": "2026-09-10T00:00:00Z",
        "portal_metadata": metadata,
        "analysis": analysis,
        "structured_profile": profile,
    }
    outputs = {
        notice / "metadata.json": metadata,
        notice / "pipeline" / "source_selection.json": selection,
        notice / "pipeline" / "structured_profile.v0.2.json": profile,
        notice / "pipeline" / "ingestion_record.v0.1.json": record,
        notice / "pipeline" / "common_ir_v1" / f"{notice_id}.pdf.json": common_ir,
    }
    for path, value in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return notice


def test_pack_validator_accepts_directory_and_zip(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)

    directory_summary = validate_directory(pack, expected_count=1)
    assert directory_summary.notices == 1
    assert directory_summary.source_formats == {"pdf": 1}
    assert directory_summary.delivery_roles == 1

    archive_path = tmp_path / "pack.zip"
    with ZipFile(archive_path, "w") as archive:
        for path in sorted(pack.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(pack).as_posix())
    archive_summary = validate_pack(archive_path, expected_count=1)
    assert archive_summary.files == 6
    assert archive_summary.archive_sha256 == sha256(archive_path.read_bytes()).hexdigest()


def test_pack_validator_rejects_source_hash_tampering(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    notice = _write_pack(pack)
    next((notice / "attachments").iterdir()).write_bytes(b"%PDF-1.4\ntampered\n")

    with pytest.raises(PackValidationError, match="source SHA-256 mismatch"):
        validate_directory(pack, expected_count=1)


def test_pack_validator_rejects_zip_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape", "unsafe")

    with pytest.raises(PackValidationError, match="unsafe ZIP member path"):
        validate_pack(archive_path)


def test_pack_validator_rejects_symlink_source(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)
    linked_pack = tmp_path / "linked-pack"
    linked_pack.symlink_to(pack, target_is_directory=True)

    with pytest.raises(PackValidationError, match="source must not be a symlink"):
        validate_pack(linked_pack)


def test_single_notice_validator_rejects_nested_symlink_and_oversize(
    tmp_path: Path,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    symlink = notice / "pipeline" / "linked"
    symlink.symlink_to(notice / "metadata.json")
    with pytest.raises(PackValidationError, match="symlink"):
        validate_notice_directory(notice)
    symlink.unlink()

    attachment = next((notice / "attachments").iterdir())
    with attachment.open("r+b") as stream:
        stream.truncate(MAX_MEMBER_BYTES + 1)
    with pytest.raises(PackValidationError, match="per-file safety limit"):
        validate_notice_directory(notice)


def test_pack_validator_rejects_unmapped_business_age(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    record_path = notice / "pipeline" / "ingestion_record.v0.1.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["derived_projections"] = [
        {
            "projection_type": "target_constraints",
            "positive_source_fact_ids": ["delivery-1"],
            "exclusion_source_fact_ids": [],
            "entity_types": [],
            "regions": [],
            "industries": [],
            "business_age": {},
            "status": "identified",
        }
    ]
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["structured_profile"] = profile
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PackValidationError, match="business_age"):
        validate_notice_directory(notice)


def test_pack_validator_rejects_boolean_organization_span(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    record_path = notice / "pipeline" / "ingestion_record.v0.1.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    organization_source = profile["comparison_profile"]["delivery_roles"][0][
        "organization_names"
    ][0]["value_source"]
    # JSON false behaves like integer zero in Python slicing, so an explicit
    # bool rejection is required even though the exact span still matches.
    organization_source["start_char"] = False
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["structured_profile"] = profile
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PackValidationError, match="invalid organization span"):
        validate_notice_directory(notice)


def test_manifest_mismatch_never_publishes_extraction(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)
    archive_path = tmp_path / "pack.zip"
    with ZipFile(archive_path, "w") as archive:
        for path in sorted(pack.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(pack).as_posix())
    summary = validate_pack(archive_path, expected_count=1)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "prereview_existing_kb_pack_manifest/v0.1",
                "archive": {
                    "file_name": archive_path.name,
                    "sha256": "0" * 64,
                    "size_bytes": archive_path.stat().st_size,
                },
                "expected": {
                    "notices": summary.notices,
                    "files": summary.files,
                    "bytes": summary.bytes,
                    "source_formats": summary.source_formats,
                    "facts": summary.facts,
                    "delivery_roles": summary.delivery_roles,
                    "projections": summary.projections,
                    "relational_rows": summary.relational_rows,
                    "notice_ids_sha256": summary.notice_ids_sha256,
                },
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "published"

    with pytest.raises(PackValidationError, match="SHA-256"):
        extract_validated_pack(
            archive_path,
            target,
            expected_count=1,
            manifest_path=manifest_path,
        )
    assert not target.exists()


def test_manifest_archive_checks_do_not_depend_on_live_path(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)
    archive_path = tmp_path / "pack.zip"
    with ZipFile(archive_path, "w") as archive:
        for path in sorted(pack.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(pack).as_posix())
    archive_size = archive_path.stat().st_size
    summary = validate_pack(archive_path, expected_count=1)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "prereview_existing_kb_pack_manifest/v0.1",
                "archive": {
                    "file_name": archive_path.name,
                    "sha256": "0" * 64,
                    "size_bytes": archive_size,
                },
                "expected": {
                    "notices": summary.notices,
                    "files": summary.files,
                    "bytes": summary.bytes,
                    "source_formats": summary.source_formats,
                    "facts": summary.facts,
                    "delivery_roles": summary.delivery_roles,
                    "projections": summary.projections,
                    "relational_rows": summary.relational_rows,
                    "notice_ids_sha256": summary.notice_ids_sha256,
                },
            }
        ),
        encoding="utf-8",
    )
    archive_path.unlink()

    with pytest.raises(PackValidationError, match="SHA-256"):
        _verify_manifest(
            summary,
            manifest_path,
            archive_path,
            archive_size=archive_size,
        )


def test_batch_import_is_non_mutating_without_execute(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "ingest_existing_profiles.py"),
            str(pack),
            "--expected-count",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={},
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "validated_only"


class FakeDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {
            "fact_occurrence": [
                {
                    "profile_version_pk": "profile-1",
                    "fact_id": "delivery-1",
                    "fact_pk": "fact-1",
                }
            ],
            "delivery_role": [],
            "delivery_role_organization": [],
        }

    def one(self, table: str, **filters: Any) -> dict[str, Any] | None:
        matches = [
            row
            for row in self.rows[table]
            if all(row.get(key) == value for key, value in filters.items())
        ]
        assert len(matches) <= 1
        return matches[0] if matches else None

    def insert(self, table: str, row: dict[str, Any], **_: Any) -> dict[str, Any]:
        stored = dict(row)
        self.rows[table].append(stored)
        return stored

    def patch(self, table: str, filters: dict[str, Any], row: dict[str, Any]) -> None:
        target = self.one(table, **filters)
        assert target is not None
        target.update(row)


class ProjectionDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.sequence = 0

    def one(self, table: str, **filters: Any) -> dict[str, Any] | None:
        matches = [
            row
            for row in self.rows.get(table, [])
            if all(row.get(key) == value for key, value in filters.items())
        ]
        assert len(matches) <= 1
        return matches[0] if matches else None

    def insert(self, table: str, row: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.sequence += 1
        primary_keys = {
            "fact_occurrence": "fact_pk",
            "target_constraint": "target_constraint_pk",
        }
        stored = dict(row)
        if table in primary_keys:
            stored[primary_keys[table]] = f"pk-{self.sequence}"
        self.rows.setdefault(table, []).append(stored)
        return stored

    def patch(self, table: str, filters: dict[str, Any], row: dict[str, Any]) -> None:
        target = self.one(table, **filters)
        assert target is not None
        target.update(row)


def test_delivery_role_backfill_is_idempotent(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile = json.loads(
        (notice / "pipeline" / "structured_profile.v0.2.json").read_text(encoding="utf-8")
    )
    database = FakeDatabase()

    first = upsert_delivery_roles(database, "profile-1", profile)  # type: ignore[arg-type]
    second = upsert_delivery_roles(database, "profile-1", profile)  # type: ignore[arg-type]

    assert first["delivery_roles_inserted"] == 1
    assert first["delivery_role_organizations_inserted"] == 1
    assert second["delivery_roles_inserted"] == 0
    assert second["delivery_role_organizations_inserted"] == 0
    assert len(database.rows["delivery_role"]) == 1
    assert len(database.rows["delivery_role_organization"]) == 1


def test_target_constraint_projection_is_materialized_without_loss(
    tmp_path: Path,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile = json.loads(
        (notice / "pipeline" / "structured_profile.v0.2.json").read_text(
            encoding="utf-8"
        )
    )
    profile["derived_projections"] = [
        {
            "projection_type": "target_constraints",
            "positive_source_fact_ids": ["delivery-1"],
            "exclusion_source_fact_ids": [],
            "entity_types": ["중소기업"],
            "regions": ["서울"],
            "industries": ["제조업"],
            "business_age": None,
            "status": "identified",
        }
    ]
    database = ProjectionDatabase()

    counts = insert_profile_children(database, "profile-1", profile)  # type: ignore[arg-type]

    assert counts["target_constraints"] == 1
    assert counts["target_constraint_sources"] == 1
    assert counts["target_constraint_dimensions"] == 3
    assert {
        row["dimension_key"]
        for row in database.rows["target_constraint_dimension"]
    } == {"entity_types", "regions", "industries"}


def test_post_verifier_covers_every_preserved_child_count() -> None:
    source_profile_id = "pdf:PBLN_1"
    expected = {
        source_profile_id: {
            "profile_sha256": "a" * 64,
            **{field: 1 for field in PRESERVED_COUNT_FIELDS},
            "embedding_hashes": {
                scope: f"hash-{scope}" for scope in EXPECTED_EMBEDDING_SCOPES
            },
        }
    }
    row = {
        "profile_sha256": "a" * 64,
        "artifact_types": sorted(EXPECTED_ARTIFACT_TYPES),
        **{field: 1 for field in PRESERVED_COUNT_FIELDS},
    }
    embeddings = {
        source_profile_id: {
            scope: f"hash-{scope}" for scope in EXPECTED_EMBEDDING_SCOPES
        }
    }

    valid = evaluate_bootstrap(
        expected,
        {source_profile_id: row},
        embeddings,
        require_embeddings=True,
    )
    assert valid["status"] == "valid"

    row["fact_evidence"] = 0
    invalid = evaluate_bootstrap(
        expected,
        {source_profile_id: row},
        embeddings,
        require_embeddings=True,
    )
    assert invalid["status"] == "invalid"
    assert invalid["mismatched_profiles"]["examples"] == [source_profile_id]


def _verified_profile_row(wanted: dict[str, Any]) -> dict[str, Any]:
    artifacts = []
    artifact_pks: dict[str, str] = {}
    for ordinal, (artifact_type, descriptor) in enumerate(
        wanted["artifacts"].items(), start=1
    ):
        artifact_pk = f"artifact-{ordinal}"
        artifact_pks[artifact_type] = artifact_pk
        artifacts.append(
            {"artifact_pk": artifact_pk, "artifact_type": artifact_type, **descriptor}
        )
    return {
        "profile_sha256": wanted["profile_sha256"],
        "artifact_types": sorted(EXPECTED_ARTIFACT_TYPES),
        "artifacts": artifacts,
        "lineage_edges": list(wanted["lineage_edges"]),
        "candidate_pack_artifact_pk": artifact_pks["candidate_pack"],
        "structured_artifact_pk": artifact_pks["structured_profile"],
        **{field: wanted[field] for field in PRESERVED_COUNT_FIELDS},
    }


def test_post_verifier_checks_artifact_bytes_lineage_and_profile_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)
    monkeypatch.setattr(
        verify_cli,
        "assemble_embedding_inputs",
        lambda _profile: {
            scope: SimpleNamespace(input_sha256=f"hash-{scope}")
            for scope in EXPECTED_EMBEDDING_SCOPES
        },
    )
    expected = _expected(pack)
    source_profile_id, wanted = next(iter(expected.items()))
    row = _verified_profile_row(wanted)
    embeddings: dict[str, dict[str, str]] = {}

    valid = evaluate_bootstrap(
        expected, {source_profile_id: row}, embeddings, require_embeddings=False
    )
    assert valid["status"] == "valid"

    row["artifacts"][0]["content_sha256"] = "f" * 64
    invalid_artifact = evaluate_bootstrap(
        expected, {source_profile_id: row}, embeddings, require_embeddings=False
    )
    assert invalid_artifact["mismatched_artifacts"]["examples"] == [source_profile_id]
    row = _verified_profile_row(wanted)

    row["lineage_edges"] = row["lineage_edges"][:-1]
    invalid_lineage = evaluate_bootstrap(
        expected, {source_profile_id: row}, embeddings, require_embeddings=False
    )
    assert invalid_lineage["mismatched_lineage"]["examples"] == [source_profile_id]
    row = _verified_profile_row(wanted)

    row["candidate_pack_artifact_pk"] = "wrong-artifact"
    invalid_reference = evaluate_bootstrap(
        expected, {source_profile_id: row}, embeddings, require_embeddings=False
    )
    assert invalid_reference["mismatched_profile_artifact_refs"]["examples"] == [
        source_profile_id
    ]


class FakeDownload:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0

    def __enter__(self) -> "FakeDownload":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.content):
            return b""
        end = len(self.content) if size < 0 else self.offset + size
        chunk = self.content[self.offset : end]
        self.offset += len(chunk)
        return chunk


def test_optional_storage_verifier_streams_canonical_object() -> None:
    content = b"canonical bytes"
    expected = {
        "pdf:PBLN_1": {
            "artifacts": {
                "source": {
                    "storage_bucket": "existing-kb",
                    "storage_object_key": "PBLN_1/pdf:PBLN_1/hash/source/hash.pdf",
                    "content_sha256": sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            }
        }
    }
    requests = []

    def open_success(request: Any, *, timeout: int) -> FakeDownload:
        requests.append(request)
        assert timeout == 60
        return FakeDownload(content)

    valid = verify_storage_objects(
        expected,
        supabase_url="http://storage.invalid",
        service_role_key="not-printed-secret",
        opener=open_success,
    )
    assert valid["status"] == "valid"
    assert valid["verified_artifacts"] == 1
    assert "pdf%3APBLN_1" in requests[0].full_url
    assert requests[0].get_header("Accept-encoding") == "identity"

    invalid = verify_storage_objects(
        expected,
        supabase_url="http://storage.invalid",
        service_role_key="not-printed-secret",
        opener=lambda *_args, **_kwargs: FakeDownload(b"corrupted bytes"),
    )
    assert invalid["status"] == "invalid"
    assert "not-printed-secret" not in json.dumps(invalid)

    assert _NoRedirectHandler().redirect_request(None, None, 302, "", {}, "") is None


def test_non_mutating_batch_preflight_does_not_load_backend_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = tmp_path / "pack"
    _write_pack(pack)

    def forbidden() -> None:
        raise AssertionError("validated-only mode must not load backend/.env")

    monkeypatch.setattr(batch_cli, "_load_backend_env", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["ingest_existing_profiles.py", str(pack), "--expected-count", "1"]
    )
    assert batch_cli.main() == 0


def test_embedding_dry_run_does_not_load_secret_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    template = profile["comparison_profile"]["delivery_roles"][0]
    profile["comparison_profile"] = {
        "purpose_goal": [
            {
                **template,
                "fact_id": "purpose-1",
                "field_name": "purpose_goal",
                "value_raw": "사업 목적",
            }
        ],
        "support_target": [
            {
                **template,
                "fact_id": "target-1",
                "field_name": "support_target",
                "value_raw": "지원 대상",
            }
        ],
        "support_activities": [
            {
                **template,
                "fact_id": "support-1",
                "field_name": "support_activities",
                "value_raw": "지원 내용",
            }
        ],
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("embedding dry-run must not load secret env")

    monkeypatch.setattr(embedding_cli, "_load_dotenv", forbidden)
    monkeypatch.setattr(embedding_cli, "load_local_supabase_settings", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "embed_existing_profiles.py",
            str(profile_path),
            "--supabase-compose-env",
            "/must/not/be/read",
            "--dry-run",
        ],
    )
    assert embedding_cli.main() == 0


class EmbeddingProfileCursor:
    def __init__(self, connection: "EmbeddingProfileConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "EmbeddingProfileCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, _query: Any, params: tuple[str, str]) -> None:
        self.connection.query_params = params

    def fetchall(self) -> list[dict[str, Any]]:
        if self.connection.query_params is None:
            raise AssertionError("profile query was not executed")
        if self.connection.query_params[1] != self.connection.database_profile_sha256:
            return []
        raise AssertionError("the tampered local Profile must not match the DB hash")


class EmbeddingProfileConnection:
    def __init__(self, database_profile_sha256: str) -> None:
        self.database_profile_sha256 = database_profile_sha256
        self.query_params: tuple[str, str] | None = None
        self.closed = False

    def cursor(self) -> EmbeddingProfileCursor:
        return EmbeddingProfileCursor(self)

    def close(self) -> None:
        self.closed = True


def test_embedding_rejects_nonidentical_profile_bytes_before_openai_or_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    database_profile_sha256 = sha256(profile_path.read_bytes()).hexdigest()
    # Whitespace alone changes the immutable artifact bytes while preserving
    # the JSON values and source_profile_id that the old lookup trusted.
    profile_path.write_bytes(profile_path.read_bytes() + b"\n")
    local_profile_sha256 = sha256(profile_path.read_bytes()).hexdigest()
    connection = EmbeddingProfileConnection(database_profile_sha256)
    calls = {"openai": 0, "upsert": 0, "assembly": 0}

    def forbidden_openai(*_: Any, **__: Any) -> Any:
        calls["openai"] += 1
        raise AssertionError("OpenAI must not be called for a mismatched Profile")

    def forbidden_upsert(*_: Any, **__: Any) -> None:
        calls["upsert"] += 1
        raise AssertionError("upsert must not run for a mismatched Profile")

    def forbidden_assembly(*_: Any, **__: Any) -> Any:
        calls["assembly"] += 1
        raise AssertionError("embedding input must not be assembled before DB identity")

    import openai

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.invalid/database")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setattr(embedding_cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        embedding_cli,
        "_configuration",
        lambda _connection: {"embedding_config_pk": "config-1"},
    )
    monkeypatch.setattr(
        embedding_cli, "_existing_hashes", lambda _connection, _config: {}
    )
    monkeypatch.setattr(embedding_cli, "assemble_embedding_inputs", forbidden_assembly)
    monkeypatch.setattr(embedding_cli, "_upsert", forbidden_upsert)
    monkeypatch.setattr(openai, "OpenAI", forbidden_openai)
    monkeypatch.setattr(single_cli.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(sys, "argv", ["embed_existing_profiles.py", str(profile_path)])

    with pytest.raises(RuntimeError, match="exact current DB Profile"):
        embedding_cli.main()

    assert connection.query_params == (
        "pdf:PBLN_000000000000001",
        local_profile_sha256,
    )
    assert local_profile_sha256 != database_profile_sha256
    assert calls == {"openai": 0, "upsert": 0, "assembly": 0}
    assert connection.closed


def test_embedding_live_profile_requires_v02_identity(tmp_path: Path) -> None:
    notice = _write_pack(tmp_path / "pack")
    profile_path = notice / "pipeline" / "structured_profile.v0.2.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["schema_version"] = "existing_program_profile/v0.1"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported Existing Profile schema"):
        embedding_cli._load_profile_snapshot(profile_path)


def test_verify_help_does_not_load_backend_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        raise AssertionError("--help must not load backend/.env")

    monkeypatch.setattr(verify_cli, "_load_backend_env", forbidden)
    monkeypatch.setattr(sys, "argv", ["verify_existing_kb.py", "--help"])
    with pytest.raises(SystemExit) as stopped:
        verify_cli.main()
    assert stopped.value.code == 0


def test_local_supabase_env_builds_loopback_urls_without_printing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compose_env = tmp_path / ".env"
    compose_env.write_text(
        "\n".join(
            (
                "POSTGRES_PASSWORD=p@ss:/word",
                "POOLER_TENANT_ID=tenant-one",
                "POSTGRES_DB=postgres",
                "POSTGRES_PORT=55432",
                "API_GW_HTTP_PORT=18000",
                "SERVICE_ROLE_KEY=private-service-key",
            )
        ),
        encoding="utf-8",
    )

    settings = load_local_supabase_settings(compose_env)

    assert settings["SUPABASE_URL"] == "http://127.0.0.1:18000"
    assert "host.docker.internal" not in settings["DATABASE_URL"]
    assert "p@ss:/word" not in settings["DATABASE_URL"]
    assert "p%40ss%3A%2Fword" in settings["DATABASE_URL"]
    assert settings["SUPABASE_SERVICE_ROLE_KEY"] == "private-service-key"
    assert capsys.readouterr().out == ""


class FakeCursor:
    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, *_: Any) -> None:
        return None

    def fetchone(self) -> dict[str, str]:
        return {"notice_pk": "notice-1"}


class FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_single_record_db_writes_commit_only_at_transaction_boundary() -> None:
    connection = FakeConnection()
    database = object.__new__(KnowledgeBase)
    database.connection = connection  # type: ignore[assignment]

    database.insert("notice", {"notice_id": "PBLN_1"})
    database.patch("notice", {"notice_id": "PBLN_1"}, {"portal_metadata": {}})
    assert connection.commits == 0

    with pytest.raises(RuntimeError, match="middle child failed"):
        run_transaction(
            database,
            lambda: (_ for _ in ()).throw(RuntimeError("middle child failed")),
        )
    assert connection.commits == 0
    assert connection.rollbacks == 1

    result = run_transaction(database, lambda: {"status": "ingested"})
    assert result == {"status": "ingested"}
    assert connection.commits == 1


def test_single_importer_has_no_secret_cli_and_redacts_storage_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    help_result = subprocess.run(
        [sys.executable, str(SCRIPTS / "ingest_existing_profile.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--service-role-key" not in help_result.stdout
    assert "--database-url" not in help_result.stdout

    source = tmp_path / "source.pdf"
    source.write_bytes(b"payload")
    secret_body = "provider-secret-response-body"

    def failed_upload(*_: Any, **__: Any) -> Any:
        raise HTTPError(
            "http://storage.invalid/private/key",
            500,
            "failed",
            {},
            BytesIO(secret_body.encode()),
        )

    monkeypatch.setattr(
        single_cli,
        "_NO_REDIRECT_OPENER",
        SimpleNamespace(open=failed_upload),
    )
    with pytest.raises(RuntimeError) as raised:
        SupabaseStorage("http://storage.invalid", "service-secret").upload(
            "private/key", source
        )
    assert secret_body not in str(raised.value)
    assert "service-secret" not in str(raised.value)
    assert "private/key" not in str(raised.value)


class RedirectingTransport(BaseHandler):
    """Return one synthetic redirect without making a network request."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    def default_open(self, request: Request) -> Any:
        self.requests.append(request)
        headers = Message()
        headers["Location"] = "https://attacker.invalid/capture"
        response = addinfourl(BytesIO(b""), headers, request.full_url, 302)
        response.msg = "Found"
        return response


def test_storage_redirect_is_rejected_without_a_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"payload")
    transport = RedirectingTransport()
    opener = build_opener(ImporterNoRedirectHandler(), transport)
    monkeypatch.setattr(single_cli, "_NO_REDIRECT_OPENER", opener)

    with pytest.raises(RuntimeError, match="HTTP 302"):
        SupabaseStorage("http://storage.invalid", "service-secret").upload(
            "private/key", source
        )

    assert [request.full_url for request in transport.requests] == [
        "http://storage.invalid/storage/v1/object/existing-kb/private/key"
    ]
