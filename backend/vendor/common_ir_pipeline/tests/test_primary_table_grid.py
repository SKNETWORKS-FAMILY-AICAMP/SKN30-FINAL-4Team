from __future__ import annotations

import ast
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_primary_document_view as view_fixtures
    import test_reconstruction_plan as reconstruction_fixtures
except ModuleNotFoundError as error:
    if error.name not in {"test_primary_document_view", "test_reconstruction_plan"}:
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_document_view as view_fixtures,
        test_reconstruction_plan as reconstruction_fixtures,
    )

import common_ir_pipeline.pdf_fusion.primary_table_grid as contract
from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.native_capture import capture_pdf_to_native
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_table_grid import (
    PdfPrimaryTableGridError,
    build_pdf_primary_table_grid,
    canonical_pdf_primary_table_grid_json,
    load_pdf_primary_table_grid_file,
    parse_pdf_primary_table_grid_bytes,
    validate_pdf_primary_table_grid,
    validate_pdf_primary_table_grid_against_inputs,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (
    build_pdf_reconstruction_plan,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPage,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/pdf_primary_table_grid_v1.schema.json"
)

# Independently reviewed strict-Surya identity for the frozen 114788 fixture.
# Tests must never derive trusted expectations from the artifact under test.
ACTUAL_114788_EXPECTED_SURYA_LOGICAL_COMPUTE_KEY = (
    "300fdc8a7e71e0a560a6ffa11b9bb47471ca74b42b468a1614a601722bd94dce"
)
ACTUAL_114788_EXPECTED_SURYA_REQUESTED_PAGES = (1, 2, 3, 4, 5, 6, 7)
ACTUAL_114788_EXPECTED_SURYA_PRODUCER = SuryaProducerIdentity(
    engine_id="surya",
    engine_version="0.22.1",
    model_id="datalab-to/surya-ocr-2",
    model_revision="3b3d4cdf88d6928b0acdc75181b13206ea67c4a3",
    model_weights_sha256=(
        "5755f82a997dd0b111964fa8b31cc2daef7aeb7a706bbd17d73d6a93ef3f723e"
    ),
    pipeline_revision="cc1dcd8c3b5205f985af69b1ca24b70525311bb1",
    config_sha256=(
        "cc1dcd8c3b5205f985af69b1ca24b70525311bb1ab474c4a7b65df29f364539d"
    ),
    worker_image_digest=(
        "sha256:cc1dcd8c3b5205f985af69b1ca24b70525311bb1ab474c4a7b65df29f364539d"
    ),
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def table_document(*, row_span: int = 1, duplicate_membership: bool = False) -> dict:
    first_right_bbox = [9, 50, 71, 62] if duplicate_membership else [40, 50, 71, 62]
    return {
        "number of pages": 1,
        "kids": [
            {
                "type": "table",
                "id": 100,
                "page number": 1,
                "bounding box": [9, 30, 71, 62],
                "number of rows": 2,
                "number of columns": 2,
                "rows": [
                    {
                        "type": "table row",
                        "id": 101,
                        "row number": 1,
                        "cells": [
                            {
                                "type": "table cell",
                                "id": 102,
                                "page number": 1,
                                "bounding box": [9, 50, 40, 62],
                                "row number": 1,
                                "column number": 1,
                                "row span": row_span,
                                "column span": 1,
                                "kids": [],
                            },
                            {
                                "type": "table cell",
                                "id": 103,
                                "page number": 1,
                                "bounding box": first_right_bbox,
                                "row number": 1,
                                "column number": 2,
                                "row span": 1,
                                "column span": 1,
                                "kids": [],
                            },
                        ],
                    },
                    {
                        "type": "table row",
                        "id": 104,
                        "row number": 2,
                        "cells": [
                            {
                                "type": "table cell",
                                "id": 105,
                                "page number": 1,
                                "bounding box": [9, 30, 14, 42],
                                "row number": 2,
                                "column number": 1,
                                "row span": 1,
                                "column span": 1,
                                "kids": [],
                            },
                            {
                                "type": "table cell",
                                "id": 106,
                                "page number": 1,
                                "bounding box": [14, 30, 71, 42],
                                "row number": 2,
                                "column number": 2,
                                "row span": 1,
                                "column span": 1,
                                "kids": [],
                            },
                        ],
                    },
                ],
            }
        ],
    }


class PrimaryTableGridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
        self.fixture.setUp()
        self.render = self.render_with_real_files()
        self.producer = SuryaProducerIdentity(
            engine_id="surya",
            engine_version="fixture-1",
            model_id="fixture-layout",
            model_revision="r1",
            model_weights_sha256=digest("weights"),
            pipeline_revision="fixture-pipeline",
            config_sha256=digest("config"),
            worker_image_digest="sha256:" + digest("image"),
        )
        self.logical_compute_key = digest("table-layout")
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self.schema)
        self.schema_validator = Draft202012Validator(self.schema)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def render_with_real_files(self) -> PdfRenderManifest:
        base = self.fixture.render()
        encoded = view_fixtures.rgb_png(200, 200)
        image_path = self.fixture.root / "rendered/page-0001.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(encoded)
        image_sha256 = digest(encoded)
        coordinate = replace(
            base.pages[0].coordinate_manifest,
            page_image_sha256=image_sha256,
        )
        page = RenderedPage(
            page=1,
            image_relative_path="rendered/page-0001.png",
            image_sha256=image_sha256,
            image_size_bytes=len(encoded),
            coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        )
        return PdfRenderManifest(
            source_pdf_relative_path="source.pdf",
            source_pdf_sha256=base.source_pdf_sha256,
            source_pdf_size_bytes=base.source_pdf_size_bytes,
            page_count=1,
            renderer=base.renderer,
            renderer_version=base.renderer_version,
            renderer_config_sha256=base.renderer_config_sha256,
            pages=(page,),
        )

    def region(
        self,
        order: int,
        bbox: tuple[float, float, float, float] = (18.0, 76.0, 142.0, 140.0),
    ) -> SuryaLayoutRegion:
        x0, y0, x1, y1 = bbox
        return SuryaLayoutRegion(
            region_id=f"p0001-table-{order + 1:04d}",
            label="table",
            bbox_px=bbox,
            polygon_px=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
            confidence=None,
            reading_order=order,
        )

    def artifact(
        self, regions: list[SuryaLayoutRegion] | None = None
    ) -> SuryaLayoutArtifact:
        page = SuryaLayoutPage(
            page=1,
            sidecar_binding=build_sidecar_binding(
                self.render.pages[0].coordinate_manifest
            ),
            pixel_width=200,
            pixel_height=200,
            rendered_page_px=(0.0, 0.0, 200.0, 200.0),
            regions=tuple([self.region(0)] if regions is None else regions),
        )
        return SuryaLayoutArtifact(
            logical_compute_key=self.logical_compute_key,
            source_sha256=self.render.source_pdf_sha256,
            page_count=1,
            render_manifest_schema_version=self.render.schema_version,
            render_manifest_sha256=self.render.manifest_sha256(),
            producer=self.producer,
            requested_pages=(1,),
            pages=(page,),
        )

    def arguments(
        self,
        *,
        raw: dict | None = None,
        artifact: SuryaLayoutArtifact | None = None,
        inspector_module: object | None = None,
    ) -> dict:
        native = capture_pdf_to_native(
            self.fixture.source,
            notice_id="PBLN_fixture",
            source_relative_path="attachments/source.pdf",
            inspector_module=(
                inspector_module or reconstruction_fixtures.FakeInspector()
            ),
            inspector_version="1.17.0",
        )
        candidates = self.fixture.candidates(raw or table_document())
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source,
            native_capture=native,
            structure_candidates=candidates,
            render_manifest=self.render,
            calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )
        selected_artifact = artifact or self.artifact()
        return {
            "source_pdf": self.fixture.source,
            "native_capture": native,
            "structure_candidates": candidates,
            "render_manifest": self.render,
            "render_artifact_root": self.fixture.root,
            "calibration_proof": self.fixture.proof,
            "expected_calibration_proof_sha256": self.fixture.proof_sha256,
            "reconstruction_plan": plan,
            "surya_layout_artifact": selected_artifact.to_dict(),
            "expected_surya_producer": self.producer,
            "expected_surya_logical_compute_key": self.logical_compute_key,
            "expected_surya_pages": (1,),
        }

    def test_builds_textless_page_local_grid_without_changing_native_ownership(self) -> None:
        arguments = self.arguments()
        before_owners = {
            item["occurrence_id"]: item["primary_owner_unit_id"]
            for item in arguments["reconstruction_plan"]["native_occurrence_ledger"]
        }
        result = build_pdf_primary_table_grid(**arguments)
        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual((table["page"], table["row_count"], table["column_count"]), (1, 2, 2))
        self.assertEqual(
            [[cell["occurrence_ids"] for cell in row["cells"]] for row in table["rows"]],
            [
                [["occ:inspector:p1:t1"], ["occ:inspector:p1:t2"]],
                [["occ:inspector:p1:t3"], ["occ:inspector:p1:t4"]],
            ],
        )
        self.assertEqual(
            before_owners,
            {
                item["occurrence_id"]: item["primary_owner_unit_id"]
                for item in arguments["reconstruction_plan"]["native_occurrence_ledger"]
            },
        )
        encoded = canonical_pdf_primary_table_grid_json(result).decode("utf-8")
        for forbidden in ("왼쪽", "오른쪽", "본문", '"bbox', '"confidence'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(validate_pdf_primary_table_grid(result), result)
        self.schema_validator.validate(result)
        self.assertEqual(
            validate_pdf_primary_table_grid_against_inputs(result, **arguments),
            result,
        )

    def test_unique_surya_outer_region_is_required_and_ambiguity_fails_closed(self) -> None:
        no_region = self.artifact([])
        self.assertEqual(
            build_pdf_primary_table_grid(**self.arguments(artifact=no_region))["tables"],
            [],
        )
        ambiguous = self.artifact(
            [self.region(0), self.region(1, (19.0, 77.0, 141.0, 139.0))]
        )
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "ambiguous Surya"):
            build_pdf_primary_table_grid(**self.arguments(artifact=ambiguous))

    def test_unsupported_spans_are_not_proposed(self) -> None:
        result = build_pdf_primary_table_grid(
            **self.arguments(raw=table_document(row_span=2))
        )
        self.assertEqual(result["tables"], [])
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "outside the declared"):
            build_pdf_primary_table_grid(
                **self.arguments(raw=table_document(row_span=3))
            )

    def test_occurrence_centers_require_both_odl_and_surya_outer_bounds(self) -> None:
        # The shifted Surya left edge keeps outer IoU above 0.90 but excludes
        # the center of the narrow bullet occurrence in row 2, column 1.
        shifted = self.artifact([self.region(0, (24.0, 76.0, 142.0, 140.0))])
        with self.assertRaisesRegex(
            PdfPrimaryTableGridError, "outside the corroborating Surya table"
        ):
            build_pdf_primary_table_grid(**self.arguments(artifact=shifted))

    def test_fully_textless_table_is_skipped_but_partial_partition_fails(self) -> None:
        empty_arguments = self.arguments(
            inspector_module=reconstruction_fixtures.EmptyInspector()
        )
        self.assertEqual(
            build_pdf_primary_table_grid(**empty_arguments)["tables"],
            [],
        )

        partial = table_document()
        empty_cell_boxes = (
            [20, 44, 30, 48],
            [45, 44, 55, 48],
            [20, 36, 30, 40],
            [45, 36, 55, 40],
        )
        cells = [
            cell
            for row in partial["kids"][0]["rows"]
            for cell in row["cells"]
        ]
        for cell, bbox in zip(cells, empty_cell_boxes, strict=True):
            cell["bounding box"] = bbox
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "exactly partition"):
            build_pdf_primary_table_grid(**self.arguments(raw=partial))

        emitted = build_pdf_primary_table_grid(**self.arguments())
        handcrafted_empty = copy.deepcopy(emitted)
        for row in handcrafted_empty["tables"][0]["rows"]:
            for cell in row["cells"]:
                cell["occurrence_ids"] = []
                cell["cell_id"] = contract._cell_id(
                    table_grid_id=handcrafted_empty["tables"][0]["table_grid_id"],
                    row_index=cell["row_index"],
                    column_index=cell["column_index"],
                    row_span=cell["row_span"],
                    column_span=cell["column_span"],
                    source_cell_candidate_id=cell["source_cell_candidate_id"],
                    occurrence_ids=[],
                )
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "Native occurrence"):
            validate_pdf_primary_table_grid(handcrafted_empty)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(handcrafted_empty)

    def test_table_occurrence_cap_matches_evaluator_and_schema(self) -> None:
        self.assertEqual(contract.MAX_OCCURRENCES_PER_CELL, 4_096)
        self.assertEqual(contract.MAX_OCCURRENCES_PER_TABLE, 4_096)
        self.assertEqual(
            self.schema["$defs"]["cell"]["properties"]["occurrence_ids"][
                "maxItems"
            ],
            4_096,
        )
        result = build_pdf_primary_table_grid(**self.arguments())
        with patch.object(contract, "MAX_OCCURRENCES_PER_TABLE", 3):
            with self.assertRaisesRegex(
                PdfPrimaryTableGridError, "evaluation safety cap"
            ):
                validate_pdf_primary_table_grid(result)
            with self.assertRaisesRegex(
                PdfPrimaryTableGridError, "evaluation safety cap"
            ):
                build_pdf_primary_table_grid(**self.arguments())

    def test_gold_presence_cannot_change_candidate_bytes_or_import_graph(self) -> None:
        arguments = self.arguments()
        absent = canonical_pdf_primary_table_grid_json(
            build_pdf_primary_table_grid(**arguments)
        )
        unrelated_gold = self.fixture.root / "pdf_primary_table_grid_gold.v1.json"
        unrelated_gold.write_bytes(b"candidate builder must not read this file")
        present = canonical_pdf_primary_table_grid_json(
            build_pdf_primary_table_grid(**arguments)
        )
        self.assertEqual(absent, present)

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

    def test_duplicate_cell_membership_and_standalone_mutation_fail_closed(self) -> None:
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "duplicate cells"):
            build_pdf_primary_table_grid(
                **self.arguments(raw=table_document(duplicate_membership=True))
            )

        result = build_pdf_primary_table_grid(**self.arguments())
        mutated = copy.deepcopy(result)
        first = mutated["tables"][0]["rows"][0]["cells"][0]
        second = mutated["tables"][0]["rows"][0]["cells"][1]
        second["occurrence_ids"] = first["occurrence_ids"][:]
        second["cell_id"] = contract._cell_id(
            table_grid_id=mutated["tables"][0]["table_grid_id"],
            row_index=second["row_index"],
            column_index=second["column_index"],
            row_span=second["row_span"],
            column_span=second["column_span"],
            source_cell_candidate_id=second["source_cell_candidate_id"],
            occurrence_ids=second["occurrence_ids"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "duplicate table-cell"):
            validate_pdf_primary_table_grid(mutated)

    def test_span_types_ids_and_missing_metadata_fail_closed(self) -> None:
        result = build_pdf_primary_table_grid(**self.arguments())
        cell = result["tables"][0]["rows"][0]["cells"][0]
        one_by_one = contract._cell_id(
            table_grid_id=result["tables"][0]["table_grid_id"],
            row_index=cell["row_index"],
            column_index=cell["column_index"],
            row_span=1,
            column_span=1,
            source_cell_candidate_id=cell["source_cell_candidate_id"],
            occurrence_ids=cell["occurrence_ids"],
        )
        two_by_one = contract._cell_id(
            table_grid_id=result["tables"][0]["table_grid_id"],
            row_index=cell["row_index"],
            column_index=cell["column_index"],
            row_span=2,
            column_span=1,
            source_cell_candidate_id=cell["source_cell_candidate_id"],
            occurrence_ids=cell["occurrence_ids"],
        )
        self.assertNotEqual(one_by_one, two_by_one)

        boolean_span = copy.deepcopy(result)
        boolean_cell = boolean_span["tables"][0]["rows"][0]["cells"][0]
        boolean_cell["row_span"] = True
        boolean_cell["cell_id"] = contract._cell_id(
            table_grid_id=boolean_span["tables"][0]["table_grid_id"],
            row_index=boolean_cell["row_index"],
            column_index=boolean_cell["column_index"],
            row_span=boolean_cell["row_span"],
            column_span=boolean_cell["column_span"],
            source_cell_candidate_id=boolean_cell["source_cell_candidate_id"],
            occurrence_ids=boolean_cell["occurrence_ids"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "row_span"):
            validate_pdf_primary_table_grid(boolean_span)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(boolean_span)

        for missing_name in ("row span", "column span"):
            with self.subTest(missing_name=missing_name):
                raw = table_document()
                del raw["kids"][0]["rows"][0]["cells"][0][missing_name]
                with self.assertRaisesRegex(
                    PdfPrimaryTableGridError, "span metadata is missing or malformed"
                ):
                    build_pdf_primary_table_grid(**self.arguments(raw=raw))

    def test_occurrence_index_contract_stops_at_499999(self) -> None:
        result = build_pdf_primary_table_grid(**self.arguments())
        boundary = copy.deepcopy(result)
        cell = boundary["tables"][0]["rows"][0]["cells"][0]
        cell["occurrence_ids"] = ["occ:inspector:p1:t499999"]
        cell["cell_id"] = contract._cell_id(
            table_grid_id=boundary["tables"][0]["table_grid_id"],
            row_index=cell["row_index"],
            column_index=cell["column_index"],
            row_span=cell["row_span"],
            column_span=cell["column_span"],
            source_cell_candidate_id=cell["source_cell_candidate_id"],
            occurrence_ids=cell["occurrence_ids"],
        )
        self.assertEqual(validate_pdf_primary_table_grid(boundary), boundary)
        self.schema_validator.validate(boundary)

        outside = copy.deepcopy(boundary)
        outside_cell = outside["tables"][0]["rows"][0]["cells"][0]
        outside_cell["occurrence_ids"] = ["occ:inspector:p1:t500000"]
        outside_cell["cell_id"] = contract._cell_id(
            table_grid_id=outside["tables"][0]["table_grid_id"],
            row_index=outside_cell["row_index"],
            column_index=outside_cell["column_index"],
            row_span=outside_cell["row_span"],
            column_span=outside_cell["column_span"],
            source_cell_candidate_id=outside_cell["source_cell_candidate_id"],
            occurrence_ids=outside_cell["occurrence_ids"],
        )
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "occurrence ID"):
            validate_pdf_primary_table_grid(outside)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(outside)

    def test_schema_parser_loader_and_replay_reject_tampering(self) -> None:
        arguments = self.arguments()
        result = build_pdf_primary_table_grid(**arguments)
        encoded = canonical_pdf_primary_table_grid_json(result)
        parsed = parse_pdf_primary_table_grid_bytes(encoded)
        self.assertEqual(parsed.to_dict(), result)
        self.assertFalse(list(self.schema_validator.iter_errors(result)))

        with self.assertRaisesRegex(PdfPrimaryTableGridError, "canonical"):
            parse_pdf_primary_table_grid_bytes(encoded + b"\n")
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "duplicate"):
            parse_pdf_primary_table_grid_bytes(
                b'{"schema_version":"x","schema_version":"y"}'
            )

        path = self.fixture.root / "primary-table-grid.json"
        path.write_bytes(encoded)
        self.assertEqual(load_pdf_primary_table_grid_file(path).to_dict(), result)
        link = self.fixture.root / "primary-table-grid-link.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "non-symlink"):
            load_pdf_primary_table_grid_file(link)

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
            with self.assertRaisesRegex(PdfPrimaryTableGridError, "changed while"):
                load_pdf_primary_table_grid_file(path)

        extra = copy.deepcopy(result)
        extra["tables"][0]["text"] = "forbidden"
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "keys"):
            validate_pdf_primary_table_grid(extra)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(extra)

        tampered = copy.deepcopy(result)
        tampered["input_artifacts"]["surya_layout_artifact_sha256"] = "0" * 64
        table = tampered["tables"][0]
        table["table_grid_id"] = contract._table_grid_id(
            source_pdf_sha256=tampered["source_pdf_sha256"],
            input_artifacts=tampered["input_artifacts"],
            page=table["page"],
            source_table_candidate_id=table["source_table_candidate_id"],
            surya_region_id=table["surya_region_id"],
        )
        for row in table["rows"]:
            row["row_id"] = contract._row_id(
                table_grid_id=table["table_grid_id"],
                row_index=row["row_index"],
            )
            for cell in row["cells"]:
                cell["cell_id"] = contract._cell_id(
                    table_grid_id=table["table_grid_id"],
                    row_index=cell["row_index"],
                    column_index=cell["column_index"],
                    row_span=cell["row_span"],
                    column_span=cell["column_span"],
                    source_cell_candidate_id=cell["source_cell_candidate_id"],
                    occurrence_ids=cell["occurrence_ids"],
                )
        validate_pdf_primary_table_grid(tampered)
        with self.assertRaisesRegex(PdfPrimaryTableGridError, "deterministic replay"):
            validate_pdf_primary_table_grid_against_inputs(tampered, **arguments)

    def test_actual_114788_replay_when_artifact_environment_is_configured(self) -> None:
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
            self.skipTest("actual 114788 table-grid artifact environment is not configured")

        def mapping(name: str) -> dict:
            return json.loads(Path(configured[name]).read_text(encoding="utf-8"))

        surya = mapping("surya_layout_artifact")
        arguments = {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
            "surya_layout_artifact": surya,
            "expected_surya_producer": ACTUAL_114788_EXPECTED_SURYA_PRODUCER,
            "expected_surya_logical_compute_key": (
                ACTUAL_114788_EXPECTED_SURYA_LOGICAL_COMPUTE_KEY
            ),
            "expected_surya_pages": ACTUAL_114788_EXPECTED_SURYA_REQUESTED_PAGES,
        }
        result = build_pdf_primary_table_grid(**arguments)
        self.assertEqual(len(result["tables"]), 3)
        by_region = {table["surya_region_id"]: table for table in result["tables"]}
        self.assertEqual((by_region["p0003-table-0008"]["row_count"], by_region["p0003-table-0008"]["column_count"]), (3, 2))
        self.assertEqual((by_region["p0004-table-0001"]["row_count"], by_region["p0004-table-0001"]["column_count"]), (3, 2))
        self.assertEqual((by_region["p0004-table-0010"]["row_count"], by_region["p0004-table-0010"]["column_count"]), (2, 2))
        self.assertEqual(
            validate_pdf_primary_table_grid_against_inputs(result, **arguments),
            result,
        )


if __name__ == "__main__":
    unittest.main()
