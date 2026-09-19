from __future__ import annotations

import hashlib
from importlib.resources import files
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.opendataloader_artifact import (
    OdlPageToSourcePage,
    OpenDataLoaderArtifact,
    OpenDataLoaderArtifactError,
    OpenDataLoaderParser,
    canonical_json_sha256,
    validate_opendataloader_artifact,
)
from common_ir_pipeline.pdf_fusion.legacy_opendataloader_evaluation import (
    LegacyOdlPageToSourcePage,
    build_legacy_opendataloader_evaluation_bytes,
)
from common_ir_pipeline.pdf_fusion.structure_candidates import (
    PdfStructureCandidatesError,
    canonical_structure_candidates_json,
    project_structure_candidates,
)


FINGERPRINT_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/legacy_odl_121019_p5.structure_candidates.fingerprint.v1.json"
)
DERIVED_TEXTLESS_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/derived_textless_odl_121019_p5.evaluation.v1.json"
)
FINGERPRINT_CANONICAL_SHA256 = "046465b9f413a6f8177eaf755a5ce8f28abd8116759034839eaaa1685d8078cb"
DERIVED_FIXTURE_CANONICAL_SHA256 = "7acf008bb4407939244b399ddaf027e4698315e4746209a972685ce20424d89b"
DERIVED_RAW_CANONICAL_SHA256 = "793c5ac96a9c7caf1cb682168e9c151a900519df6e4675cabb637fdb69c98bc0"
PROJECTED_CANDIDATES_CANONICAL_SHA256 = "f0de9f98e5b39b2150271f4f733c8c88f247eca75be8743df2afa65f541889c9"
DERIVED_PROJECTED_CANDIDATES_CANONICAL_SHA256 = "1ad981e3ea045cf0d58a59c92c317c21b193908ea79eebe1df630df4ac1288fd"


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def nested_document() -> dict[str, object]:
    return {
        "kids": [
            {"type": "heading", "id": 1, "page number": 1, "bounding box": [1, 2, 30, 10], "content": "ignored"},
            {
                "type": "list", "id": 2, "page number": 1, "bounding box": [1, 12, 50, 50],
                "list items": [
                    {
                        "type": "list item", "id": 3, "page number": 1, "bounding box": [3, 15, 45, 24],
                        "content": "never emitted",
                        "kids": [{"type": "paragraph", "id": 4, "page number": 1, "bbox": [5, 16, 40, 23], "content": "also ignored"}],
                    }
                ],
            },
            {
                "type": "table", "id": 5, "page number": 2, "bounding box": [10, 10, 90, 50],
                "number of rows": 1, "number of columns": 2,
                "rows": [{
                    "type": "table row", "id": 6, "row number": 1, "bounding box": [10, 10, 90, 50],
                    "cells": [
                        {"type": "table cell", "id": 7, "page number": 2, "bounding box": [10, 10, 50, 50], "row number": 1, "column number": 1, "row span": 1, "column span": 1, "content": "left"},
                        {"type": "table cell", "id": 8, "page number": 2, "bounding box": [50, 10, 90, 50], "row number": 1, "column number": 2, "row span": 1, "column span": 1, "content": "right"},
                    ],
                }],
            },
        ]
    }


def artifact(raw: dict[str, object]) -> tuple[OpenDataLoaderArtifact, bytes]:
    raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    value = OpenDataLoaderArtifact(
        notice_id="PBLN_000000000121019", source_pdf_sha256=digest("pdf"), raw_json_sha256=digest(raw_bytes),
        canonical_json_sha256=canonical_json_sha256(raw),
        parser=OpenDataLoaderParser("opendataloader-pdf", "2.5.7", digest("config"), False),
        source_page_count=5, odl_page_count=2, page_scope=(1, 5),
        odl_page_to_source_page=(OdlPageToSourcePage(1, 1), OdlPageToSourcePage(2, 5)),
    )
    return value, raw_bytes


def legacy_structural_document() -> dict[str, object]:
    """A minimal cached-ODL shape: deliberately separate from strict input."""
    return {
        "number of pages": 5,
        "kids": [{
            "type": "table", "id": 10, "page number": 5, "number of rows": 1, "number of columns": 1,
            "rows": [{
                "type": "table row", "id": 11, "row number": 1,
                "cells": [{
                    "type": "table cell", "id": 12, "page number": 5, "row number": 1,
                    "column number": 1, "row span": 1, "column span": 1, "content": "opaque legacy text",
                }],
            }],
        }],
    }


class StructureCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = nested_document()
        self.artifact, self.raw_bytes = artifact(self.raw)
        # The projection contract requires the prior ODL binding.  This also
        # demonstrates the required raw bytes and actual ODL page count.
        validate_opendataloader_artifact(
            self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=2,
        )

    def test_projects_nested_list_and_table_without_text_or_row_geometry(self) -> None:
        result = project_structure_candidates(
            self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=2,
        )
        candidates = result["candidates"]
        self.assertEqual(result["ordering_status"], "odl_traversal_unverified")
        self.assertEqual([item["normalized_kind"] for item in candidates], [
            "heading", "list", "list_item", "paragraph", "table", "table_row", "table_cell", "table_cell",
        ])
        self.assertEqual(candidates[4]["source_page"], 5)
        self.assertEqual(candidates[5]["source_page"], 5)
        self.assertNotIn("bbox_odl_pdf_points_unverified", candidates[5])
        self.assertEqual(candidates[6]["table_metadata"], {"row_number": 1, "column_number": 1, "row_span": 1, "column_span": 1})
        self.assertEqual(candidates[4]["table_metadata"], {"declared_row_count": 1, "declared_column_count": 2})
        encoded = canonical_structure_candidates_json(result)
        self.assertNotIn(b"ignored", encoded)
        self.assertNotIn(b"never emitted", encoded)
        self.assertNotIn(b"content", encoded)
        self.assertNotIn(b"reading_order", encoded)

    def test_schema_and_canonical_determinism(self) -> None:
        first = project_structure_candidates(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=2)
        second = project_structure_candidates(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=2)
        self.assertEqual(canonical_structure_candidates_json(first), canonical_structure_candidates_json(second))
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/pdf_structure_candidates_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(first)

    def test_external_legacy_shape_is_frozen_as_a_textless_hermetic_fingerprint(self) -> None:
        """Re-run the projector on derived structure without copying external ODL text."""
        fingerprint = json.loads(FINGERPRINT_FIXTURE.read_text(encoding="utf-8"))
        canonical_fingerprint = json.dumps(
            fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(digest(canonical_fingerprint), FINGERPRINT_CANONICAL_SHA256)
        self.assertEqual(fingerprint["schema_version"], "pdf_structure_candidates_fingerprint/v1")
        self.assertEqual(fingerprint["fixture_role"], "hermetic_textless_fingerprint")
        self.assertEqual(fingerprint["input"], {
            "binding_schema_version": "legacy_opendataloader_evaluation/v1",
            "binding_sha256": "36fd14646f0cdb3f36b82ab5fe53842fc74ab11b6f2dc6a9b3ac01feda01fd8f",
            "observed_external_raw_json_sha256": "f60515489fa1ad338b9c39d3e9173ec03e2c4400872fa097c09c800c5fce29e4",
            "observed_external_canonical_json_sha256": "0c8964181495909245e6f4a2a62acae95c0141f6be82e36330767b31f5816110",
            "external_raw_payload_in_repository": False,
            "provenance_status": "legacy_evaluation_only_non_promotable",
            "derived_textless_fixture": "derived_textless_odl_121019_p5.evaluation.v1.json",
            "derived_textless_canonical_json_sha256": DERIVED_RAW_CANONICAL_SHA256,
        })
        projection = fingerprint["projection"]
        self.assertEqual(projection["candidate_count"], 63)
        self.assertEqual(projection["normalized_kind_counts"], {
            "heading": 1, "list": 8, "list_item": 51, "paragraph": 3,
        })
        self.assertEqual(projection["ordering_status"], "odl_traversal_unverified")
        self.assertIs(projection["evaluation_only"], True)
        self.assertIs(projection["non_promotable"], True)
        self.assertEqual(projection["excluded_payload_fields"], ["content", "reading_order", "text"])
        self.assertEqual(
            projection["observed_external_projection_canonical_json_sha256"],
            PROJECTED_CANDIDATES_CANONICAL_SHA256,
        )
        self.assertEqual(
            projection["derived_projection_canonical_json_sha256"],
            DERIVED_PROJECTED_CANDIDATES_CANONICAL_SHA256,
        )

        derived_fixture = json.loads(DERIVED_TEXTLESS_FIXTURE.read_text(encoding="utf-8"))
        canonical_derived_fixture = json.dumps(
            derived_fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(digest(canonical_derived_fixture), DERIVED_FIXTURE_CANONICAL_SHA256)
        self.assertEqual(derived_fixture["schema_version"], "derived_textless_odl_structure_fixture/v1")
        self.assertIs(derived_fixture["evaluation_only"], True)
        self.assertIs(derived_fixture["non_promotable"], True)

        raw_structure = derived_fixture["raw_structure"]
        derived_raw_bytes = json.dumps(
            raw_structure, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(digest(derived_raw_bytes), DERIVED_RAW_CANONICAL_SHA256)
        self.assertEqual(
            derived_fixture["derivation"]["derived_raw_canonical_json_sha256"],
            DERIVED_RAW_CANONICAL_SHA256,
        )

        derived_binding = build_legacy_opendataloader_evaluation_bytes(
            derived_raw_bytes,
            notice_id="PBLN_000000000121019",
            source_pdf_sha256="7f5cb27e0e0894ecf919e73a4290e2b6e186df0802ed80b2928d70eea365865a",
            source_page_count=5,
            page_scope=(5,),
            odl_page_to_source_page=(LegacyOdlPageToSourcePage(5, 5),),
        )
        projected = project_structure_candidates(
            derived_binding, raw_structure, raw_bytes=derived_raw_bytes, actual_odl_page_count=5,
        )
        self.assertEqual(projected["ordering_status"], "odl_traversal_unverified")
        self.assertEqual(len(projected["candidates"]), 63)
        kind_counts: dict[str, int] = {}
        for candidate in projected["candidates"]:
            kind = candidate["normalized_kind"]
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        self.assertEqual(kind_counts, {"heading": 1, "list": 8, "list_item": 51, "paragraph": 3})
        self.assertEqual(
            digest(json.dumps([item["candidate_id"] for item in projected["candidates"]], separators=(",", ":"))),
            projection["candidate_ids_sha256"],
        )
        self.assertEqual(
            digest(json.dumps([item["normalized_kind"] for item in projected["candidates"]], separators=(",", ":"))),
            projection["normalized_kind_sequence_sha256"],
        )
        self.assertEqual(
            digest(canonical_structure_candidates_json(projected)),
            DERIVED_PROJECTED_CANDIDATES_CANONICAL_SHA256,
        )
        self.assertNotEqual(
            DERIVED_PROJECTED_CANDIDATES_CANONICAL_SHA256,
            PROJECTED_CANDIDATES_CANONICAL_SHA256,
        )

        schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_structure_candidates_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        candidate_properties = schema["$defs"]["candidate"]["properties"]
        self.assertTrue({"content", "reading_order", "text"}.isdisjoint(candidate_properties))
        def collect_keys(value: object, target: set[str]) -> None:
            if isinstance(value, dict):
                target.update(value)
                for nested in value.values():
                    collect_keys(nested, target)
            elif isinstance(value, list):
                for nested in value:
                    collect_keys(nested, target)

        raw_structure_keys: set[str] = set()
        collect_keys(raw_structure, raw_structure_keys)
        self.assertLessEqual(raw_structure_keys, set(derived_fixture["derivation"]["retained_keys"]))
        self.assertTrue({"content", "reading_order", "text"}.isdisjoint(raw_structure_keys))

    def test_legacy_evaluation_projects_without_masquerading_as_strict(self) -> None:
        raw = legacy_structural_document()
        raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        legacy = build_legacy_opendataloader_evaluation_bytes(
            raw_bytes, notice_id="PBLN_000000000121019", source_pdf_sha256=digest("curated pdf"),
            source_page_count=5, page_scope=(5,),
            odl_page_to_source_page=(LegacyOdlPageToSourcePage(5, 5),),
        )
        with self.assertRaises(OpenDataLoaderArtifactError):
            OpenDataLoaderArtifact.from_dict(legacy.to_dict())
        result = project_structure_candidates(legacy, raw, raw_bytes=raw_bytes, actual_odl_page_count=5)
        self.assertEqual(result["input_binding_schema_version"], "legacy_opendataloader_evaluation/v1")
        self.assertNotIn("opendataloader_artifact_sha256", result)
        self.assertEqual([item["normalized_kind"] for item in result["candidates"]], ["table", "table_row", "table_cell"])
        self.assertNotIn("content", canonical_structure_candidates_json(result).decode("utf-8"))

    def test_serializer_rejects_extra_or_tampered_sidecar_fields(self) -> None:
        clean = project_structure_candidates(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=2)
        mutations: list[tuple[dict[str, object], str]] = []
        extra_root = copy.deepcopy(clean)
        extra_root["untrusted"] = True
        mutations.append((extra_root, "unexpected keys"))
        extra_candidate = copy.deepcopy(clean)
        extra_candidate["candidates"][0]["content"] = "must never be serializable"
        mutations.append((extra_candidate, "unexpected keys"))
        wrong_parent = copy.deepcopy(clean)
        wrong_parent["candidates"][0]["parent_candidate_id"] = wrong_parent["candidates"][1]["candidate_id"]
        mutations.append((wrong_parent, "earlier candidate"))
        bad_bbox = copy.deepcopy(clean)
        bad_bbox["candidates"][0]["bbox_odl_pdf_points_unverified"] = [1, 1, 1, 2]
        mutations.append((bad_bbox, "strictly increasing"))
        bad_page_map = copy.deepcopy(clean)
        bad_page_map["candidates"][0]["source_page"] = 5
        mutations.append((bad_page_map, "page mapping"))
        bad_metadata = copy.deepcopy(clean)
        bad_metadata["candidates"][0]["table_metadata"] = {"row_span": 1}
        mutations.append((bad_metadata, "non-table"))
        for payload, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(PdfStructureCandidatesError, message):
                canonical_structure_candidates_json(payload)

    def test_hard_negative_table_and_list_remain_distinct(self) -> None:
        raw = nested_document()
        raw["kids"] = [
            {"type": "list", "id": 40, "page number": 1, "list items": []},
            {"type": "table", "id": 41, "page number": 1, "number of rows": 1, "number of columns": 1, "rows": []},
        ]
        bound, raw_bytes = artifact(raw)
        result = project_structure_candidates(bound, raw, raw_bytes=raw_bytes, actual_odl_page_count=2)
        self.assertEqual([item["raw_type"] for item in result["candidates"]], ["list", "table"])
        self.assertNotEqual(result["candidates"][0]["candidate_id"], result["candidates"][1]["candidate_id"])

    def test_rejects_out_of_scope_page_bad_bbox_bad_span_and_child_shape(self) -> None:
        cases: list[tuple[dict[str, object], str]] = []
        bad_page = nested_document()
        bad_page["kids"][0]["page number"] = 3  # type: ignore[index]
        cases.append((bad_page, "integer"))
        bad_bbox = nested_document()
        bad_bbox["kids"][0]["bounding box"] = [1, 2, 1, 3]  # type: ignore[index]
        cases.append((bad_bbox, "strictly increasing"))
        bad_span = nested_document()
        bad_span["kids"][2]["rows"][0]["cells"][0]["row span"] = 0  # type: ignore[index]
        cases.append((bad_span, "row span"))
        bad_shape = nested_document()
        bad_shape["kids"][1]["list items"] = {}  # type: ignore[index]
        cases.append((bad_shape, "must be an array"))
        for raw, message in cases:
            with self.subTest(message=message):
                bound, raw_bytes = artifact(raw)
                with self.assertRaisesRegex(PdfStructureCandidatesError, message):
                    project_structure_candidates(bound, raw, raw_bytes=raw_bytes, actual_odl_page_count=2)

    def test_rejects_duplicate_identity_alias_reuse_and_unknown_child_type(self) -> None:
        duplicate = nested_document()
        duplicate["kids"][1]["id"] = 1  # type: ignore[index]
        bound, raw_bytes = artifact(duplicate)
        with self.assertRaisesRegex(PdfStructureCandidatesError, "duplicate"):
            project_structure_candidates(bound, duplicate, raw_bytes=raw_bytes, actual_odl_page_count=2)

        shared = {"type": "paragraph", "id": 99, "page number": 1}
        alias = {"kids": [shared, shared]}
        bound, raw_bytes = artifact(alias)
        with self.assertRaisesRegex(PdfStructureCandidatesError, "alias reuse"):
            project_structure_candidates(bound, alias, raw_bytes=raw_bytes, actual_odl_page_count=2)

        wrong_child = {"kids": [{"type": "list", "id": 1, "page number": 1, "list items": [{"type": "paragraph", "id": 2, "page number": 1}]}]}
        bound, raw_bytes = artifact(wrong_child)
        with self.assertRaisesRegex(PdfStructureCandidatesError, "list item"):
            project_structure_candidates(bound, wrong_child, raw_bytes=raw_bytes, actual_odl_page_count=2)
