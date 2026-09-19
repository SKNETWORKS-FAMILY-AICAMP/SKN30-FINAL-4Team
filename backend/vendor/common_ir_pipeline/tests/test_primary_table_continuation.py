from __future__ import annotations

import ast
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_primary_table_grid as grid_fixtures
except ModuleNotFoundError as error:
    if error.name != "test_primary_table_grid":
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_table_grid as grid_fixtures,
    )

import common_ir_pipeline.pdf_fusion.primary_table_continuation as contract
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_table_continuation import (
    PdfPrimaryTableContinuationError,
    build_pdf_primary_table_continuation,
    canonical_pdf_primary_table_continuation_json,
    load_pdf_primary_table_continuation_file,
    parse_pdf_primary_table_continuation_bytes,
    validate_pdf_primary_table_continuation,
    validate_pdf_primary_table_continuation_against_inputs,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid import (
    build_pdf_primary_table_grid,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_continuation_v1.schema.json"
)

ACTUAL_114788_EXPECTED_CONTINUATION_ID = (
    "table-continuation-aa202b94d946f15b9d3f341f87735187ce19ee04d2a75e092bf532f09c0877dd"
)
ACTUAL_114788_EXPECTED_PREDECESSOR_TABLE_ID = (
    "table-grid-ee8c39c6c3a41e055ea7809f056ab3b0ec772d03c71ad6afbbabd715127dc35c"
)
ACTUAL_114788_EXPECTED_SUCCESSOR_TABLE_ID = (
    "table-grid-cecf31de181236c12c64277b9f64a373cbd16cbc5b7447a9e461a96bdce1b8b1"
)
ACTUAL_114788_LOWER_HARD_NEGATIVE_TABLE_ID = (
    "table-grid-bb480cef50560b1e4e048ce76006d7238f1b0123b81985a04887643c052d122e"
)
ACTUAL_114788_EXPECTED_GEOMETRY_METRICS = {
    "predecessor_bottom_gap_ppm": 97_834,
    "successor_top_gap_ppm": 68_674,
    "outer_x_interval_iou_ppm": 1_000_000,
    "max_column_edge_drift_ppm": 0,
    "min_column_interval_iou_ppm": 1_000_000,
}


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class PrimaryTableContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self.schema)
        self.schema_validator = Draft202012Validator(self.schema)

    def inputs(self) -> dict[str, str]:
        return {
            "primary_table_grid_schema_version": "pdf_primary_table_grid/v1",
            "primary_table_grid_sha256": digest("grid"),
            "native_capture_schema_version": "pdf_inspector_native_capture/v1",
            "native_capture_sha256": digest("native"),
            "render_manifest_schema_version": "pdf_render_manifest/v1",
            "render_manifest_sha256": digest("render"),
            "structure_candidates_schema_version": "pdf_structure_candidates/v1",
            "structure_candidates_sha256": digest("candidates"),
            "reconstruction_plan_schema_version": "pdf_reconstruction_plan/v1",
            "reconstruction_plan_sha256": digest("plan"),
            "surya_layout_artifact_schema_version": "surya_layout_artifact/v1",
            "surya_layout_artifact_sha256": digest("surya"),
        }

    def relation(
        self,
        *,
        predecessor_id: str | None = None,
        successor_id: str | None = None,
        predecessor_page: int = 1,
        successor_page: int = 2,
    ) -> dict:
        inputs = self.inputs()
        value = {
            "relation_kind": "between_rows",
            "predecessor_table_grid_id": predecessor_id
            or "table-grid-" + digest("predecessor"),
            "predecessor_page": predecessor_page,
            "successor_table_grid_id": successor_id
            or "table-grid-" + digest("successor"),
            "successor_page": successor_page,
            "column_mapping": [
                {
                    "predecessor_column_index": 1,
                    "successor_column_index": 1,
                },
                {
                    "predecessor_column_index": 2,
                    "successor_column_index": 2,
                },
            ],
            "geometry_metrics": {
                "predecessor_bottom_gap_ppm": 25_000,
                "successor_top_gap_ppm": 25_000,
                "outer_x_interval_iou_ppm": 1_000_000,
                "max_column_edge_drift_ppm": 0,
                "min_column_interval_iou_ppm": 1_000_000,
            },
        }
        value["continuation_id"] = contract._continuation_id(
            source_pdf_sha256=digest("source"),
            input_artifacts=inputs,
            **value,
        )
        return value

    def payload(self) -> dict:
        return {
            "schema_version": contract.SCHEMA_VERSION,
            "evaluation_only": True,
            "non_promotable": True,
            "standalone_validation_scope": contract.STANDALONE_VALIDATION_SCOPE,
            "policy": {"policy_version": contract.POLICY_VERSION},
            "notice_id": "PBLN_fixture",
            "source_pdf_sha256": digest("source"),
            "page_scope": [1, 2, 3],
            "input_artifacts": self.inputs(),
            "continuations": [self.relation()],
        }

    def test_standalone_schema_canonical_and_between_rows_only(self) -> None:
        payload = self.payload()
        self.assertEqual(validate_pdf_primary_table_continuation(payload), payload)
        self.schema_validator.validate(payload)
        encoded = canonical_pdf_primary_table_continuation_json(payload)
        self.assertEqual(parse_pdf_primary_table_continuation_bytes(encoded).to_dict(), payload)
        for forbidden in (b'"text"', b'"bbox"', b'"occurrence_ids"', b'"gold"'):
            self.assertNotIn(forbidden, encoded.lower())

        same_row = copy.deepcopy(payload)
        relation = same_row["continuations"][0]
        relation["relation_kind"] = "same_row"
        relation["continuation_id"] = contract._continuation_id(
            source_pdf_sha256=same_row["source_pdf_sha256"],
            input_artifacts=same_row["input_artifacts"],
            relation_kind=relation["relation_kind"],
            predecessor_table_grid_id=relation["predecessor_table_grid_id"],
            predecessor_page=relation["predecessor_page"],
            successor_table_grid_id=relation["successor_table_grid_id"],
            successor_page=relation["successor_page"],
            column_mapping=relation["column_mapping"],
            geometry_metrics=relation["geometry_metrics"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "between_rows"):
            validate_pdf_primary_table_continuation(same_row)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(same_row)

    def test_geometry_rule_selects_unique_boundaries_and_excludes_lower_table(self) -> None:
        predecessor = contract._TableGeometry(
            table_grid_id="table-grid-" + digest("p1-bottom"),
            page=1,
            column_count=2,
            outer_bbox=(10.0, 5.0, 90.0, 50.0),
            page_width=100.0,
            page_height=200.0,
            column_intervals=((10.0, 40.0), (40.0, 90.0)),
        )
        successor = contract._TableGeometry(
            table_grid_id="table-grid-" + digest("p2-top"),
            page=2,
            column_count=2,
            outer_bbox=(10.0, 150.0, 90.0, 195.0),
            page_width=100.0,
            page_height=200.0,
            column_intervals=((10.0, 40.0), (40.0, 90.0)),
        )
        lower = contract._TableGeometry(
            table_grid_id="table-grid-" + digest("p2-lower"),
            page=2,
            column_count=2,
            outer_bbox=(10.0, 10.0, 90.0, 50.0),
            page_width=100.0,
            page_height=200.0,
            column_intervals=((10.0, 80.0), (80.0, 90.0)),
        )
        self.assertIs(
            contract._unique_boundary_table([predecessor], boundary="bottom"),
            predecessor,
        )
        self.assertIs(
            contract._unique_boundary_table([successor, lower], boundary="top"),
            successor,
        )
        mapping = contract._column_mapping(predecessor, successor)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping[0], [
            {"predecessor_column_index": 1, "successor_column_index": 1},
            {"predecessor_column_index": 2, "successor_column_index": 2},
        ])
        self.assertIsNone(contract._column_mapping(predecessor, lower))

        competing = replace(
            predecessor,
            table_grid_id="table-grid-" + digest("competing"),
        )
        self.assertIsNone(
            contract._unique_boundary_table([predecessor, competing], boundary="bottom")
        )

    def test_adjacency_mapping_caps_and_fan_out_fail_closed(self) -> None:
        payload = self.payload()
        nonadjacent = copy.deepcopy(payload)
        relation = nonadjacent["continuations"][0]
        relation["successor_page"] = 3
        relation["continuation_id"] = contract._continuation_id(
            source_pdf_sha256=nonadjacent["source_pdf_sha256"],
            input_artifacts=nonadjacent["input_artifacts"],
            relation_kind=relation["relation_kind"],
            predecessor_table_grid_id=relation["predecessor_table_grid_id"],
            predecessor_page=relation["predecessor_page"],
            successor_table_grid_id=relation["successor_table_grid_id"],
            successor_page=relation["successor_page"],
            column_mapping=relation["column_mapping"],
            geometry_metrics=relation["geometry_metrics"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "adjacent"):
            validate_pdf_primary_table_continuation(nonadjacent)

        weak_geometry = copy.deepcopy(payload)
        relation = weak_geometry["continuations"][0]
        relation["geometry_metrics"]["outer_x_interval_iou_ppm"] = 899_999
        relation["continuation_id"] = contract._continuation_id(
            source_pdf_sha256=weak_geometry["source_pdf_sha256"],
            input_artifacts=weak_geometry["input_artifacts"],
            relation_kind=relation["relation_kind"],
            predecessor_table_grid_id=relation["predecessor_table_grid_id"],
            predecessor_page=relation["predecessor_page"],
            successor_table_grid_id=relation["successor_table_grid_id"],
            successor_page=relation["successor_page"],
            column_mapping=relation["column_mapping"],
            geometry_metrics=relation["geometry_metrics"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "policy gates"):
            validate_pdf_primary_table_continuation(weak_geometry)

        fan_out = self.payload()
        first = fan_out["continuations"][0]
        second = self.relation(
            predecessor_id=first["predecessor_table_grid_id"],
            successor_id="table-grid-" + digest("zz-successor"),
        )
        fan_out["continuations"] = sorted(
            [first, second],
            key=lambda item: (
                item["predecessor_page"],
                item["predecessor_table_grid_id"],
                item["successor_page"],
                item["successor_table_grid_id"],
                item["relation_kind"],
            ),
        )
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "fan out"):
            validate_pdf_primary_table_continuation(fan_out)

        with patch.object(contract, "MAX_MAPPING_WORK", 1):
            with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "work cap"):
                validate_pdf_primary_table_continuation(payload)

    def test_parser_loader_toctou_and_gold_import_firewall(self) -> None:
        encoded = canonical_pdf_primary_table_continuation_json(self.payload())
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "canonical"):
            parse_pdf_primary_table_continuation_bytes(encoded + b"\n")
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "duplicate"):
            parse_pdf_primary_table_continuation_bytes(
                b'{"schema_version":"x","schema_version":"y"}'
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuation.json"
            path.write_bytes(encoded)
            self.assertEqual(
                load_pdf_primary_table_continuation_file(path).to_dict(),
                self.payload(),
            )
            link = Path(directory) / "continuation.link.json"
            link.symlink_to(path.name)
            with self.assertRaisesRegex(
                PdfPrimaryTableContinuationError, "non-symlink"
            ):
                load_pdf_primary_table_continuation_file(link)

            opened = os.stat(path)
            changed = type(
                "ChangedStat",
                (),
                {
                    "st_mode": opened.st_mode,
                    "st_dev": opened.st_dev,
                    "st_ino": opened.st_ino,
                    "st_size": opened.st_size,
                    "st_mtime_ns": opened.st_mtime_ns,
                    "st_ctime_ns": opened.st_ctime_ns + 1,
                },
            )()
            with patch.object(contract.os, "fstat", side_effect=(opened, changed)):
                with self.assertRaisesRegex(
                    PdfPrimaryTableContinuationError, "changed while"
                ):
                    load_pdf_primary_table_continuation_file(path)

        source = Path(contract.__file__).read_text(encoding="utf-8")
        imported = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }
        imported.update(
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertFalse(any("gold" in module for module in imported))
        self.assertFalse(any("evaluation" in module for module in imported))

        fixture = grid_fixtures.PrimaryTableGridTests()
        fixture.setUp()
        try:
            arguments = fixture.arguments(raw={"number of pages": 1, "kids": []})
            grid = build_pdf_primary_table_grid(**arguments)
            absent = canonical_pdf_primary_table_continuation_json(
                build_pdf_primary_table_continuation(
                    primary_table_grid=grid, **arguments
                )
            )
            unrelated_gold = (
                fixture.fixture.root / "pdf_primary_table_continuation_gold.v1.json"
            )
            unrelated_gold.write_bytes(b"candidate builder must not read this file")
            present = canonical_pdf_primary_table_continuation_json(
                build_pdf_primary_table_continuation(
                    primary_table_grid=grid, **arguments
                )
            )
            self.assertEqual(absent, present)
        finally:
            fixture.tearDown()

    def actual_arguments(self) -> dict:
        names = {
            "source_pdf": "PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF",
            "native_capture": "PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE",
            "structure_candidates": "PRIMARY_DOCUMENT_VIEW_114788_STRUCTURE_CANDIDATES",
            "render_manifest": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST",
            "render_artifact_root": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT",
            "reconstruction_plan": "PRIMARY_DOCUMENT_VIEW_114788_RECONSTRUCTION_PLAN",
            "calibration_proof": "PRIMARY_DOCUMENT_VIEW_114788_CALIBRATION_PROOF",
            "surya_layout_artifact": "PRIMARY_HEADING_RELATIONS_114788_SURYA_LAYOUT_ARTIFACT",
        }
        configured = {key: os.environ.get(name) for key, name in names.items()}
        if not all(configured.values()):
            self.skipTest("actual 114788 continuation artifact environment is not configured")

        def mapping(name: str) -> dict:
            return json.loads(Path(configured[name]).read_text(encoding="utf-8"))

        return {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
            "surya_layout_artifact": mapping("surya_layout_artifact"),
            "expected_surya_producer": grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_PRODUCER,
            "expected_surya_logical_compute_key": (
                grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_LOGICAL_COMPUTE_KEY
            ),
            "expected_surya_pages": (
                grid_fixtures.ACTUAL_114788_EXPECTED_SURYA_REQUESTED_PAGES
            ),
        }

    def test_actual_114788_replays_one_between_rows_relation(self) -> None:
        arguments = self.actual_arguments()
        grid = build_pdf_primary_table_grid(**arguments)
        self.assertIn(
            ACTUAL_114788_LOWER_HARD_NEGATIVE_TABLE_ID,
            {table["table_grid_id"] for table in grid["tables"]},
        )
        result = build_pdf_primary_table_continuation(
            primary_table_grid=grid, **arguments
        )
        self.assertEqual(len(result["continuations"]), 1)
        relation = result["continuations"][0]
        self.assertEqual(
            relation,
            {
                "continuation_id": ACTUAL_114788_EXPECTED_CONTINUATION_ID,
                "relation_kind": "between_rows",
                "predecessor_table_grid_id": ACTUAL_114788_EXPECTED_PREDECESSOR_TABLE_ID,
                "predecessor_page": 3,
                "successor_table_grid_id": ACTUAL_114788_EXPECTED_SUCCESSOR_TABLE_ID,
                "successor_page": 4,
                "column_mapping": [
                    {
                        "predecessor_column_index": 1,
                        "successor_column_index": 1,
                    },
                    {
                        "predecessor_column_index": 2,
                        "successor_column_index": 2,
                    },
                ],
                "geometry_metrics": ACTUAL_114788_EXPECTED_GEOMETRY_METRICS,
            },
        )
        self.assertNotIn(
            ACTUAL_114788_LOWER_HARD_NEGATIVE_TABLE_ID,
            {
                relation["predecessor_table_grid_id"],
                relation["successor_table_grid_id"],
            },
        )
        self.schema_validator.validate(result)
        self.assertEqual(
            validate_pdf_primary_table_continuation_against_inputs(
                result, primary_table_grid=grid, **arguments
            ),
            result,
        )

        tampered = copy.deepcopy(result)
        mutated = tampered["continuations"][0]
        mutated["geometry_metrics"]["predecessor_bottom_gap_ppm"] += 1
        mutated["continuation_id"] = contract._continuation_id(
            source_pdf_sha256=tampered["source_pdf_sha256"],
            input_artifacts=tampered["input_artifacts"],
            relation_kind=mutated["relation_kind"],
            predecessor_table_grid_id=mutated["predecessor_table_grid_id"],
            predecessor_page=mutated["predecessor_page"],
            successor_table_grid_id=mutated["successor_table_grid_id"],
            successor_page=mutated["successor_page"],
            column_mapping=mutated["column_mapping"],
            geometry_metrics=mutated["geometry_metrics"],
        )
        validate_pdf_primary_table_continuation(tampered)
        with self.assertRaisesRegex(PdfPrimaryTableContinuationError, "deterministic"):
            validate_pdf_primary_table_continuation_against_inputs(
                tampered, primary_table_grid=grid, **arguments
            )


if __name__ == "__main__":
    unittest.main()
