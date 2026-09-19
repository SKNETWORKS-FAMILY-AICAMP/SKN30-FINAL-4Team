from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    PdfCoordinateManifest,
)
from common_ir_pipeline.pdf_fusion.native_capture import (
    canonical_json_bytes as canonical_native_json_bytes,
    validate_native_capture,
)
import common_ir_pipeline.pdf_fusion.primary_table_grid_gold as table_gold
from common_ir_pipeline.pdf_fusion.primary_table_grid_gold import (
    PrimaryTableGridGoldError,
    PrimaryTableGridGoldFixture,
    canonical_primary_table_grid_gold_json,
    load_primary_table_grid_gold_file,
    parse_primary_table_grid_gold_bytes,
    validate_primary_table_grid_gold_against_inputs,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPageInput,
    assemble_render_manifest,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ACTUAL_114788_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_table_grid_gold_114788_p3_p4.v1.json"
)


def _encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png_bytes(*, width: int = 200, height: int = 400) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + (b"\x00\x00\x00" * width)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanline * height))
        + _png_chunk(b"IEND", b"")
    )


def _text_item(text: str, *, item_type: str = "text") -> dict[str, object]:
    return {
        "page": 1,
        "text": text,
        "x": 10.0,
        "y": 20.0,
        "width": 30.0,
        "height": 10.0,
        "font": "Synthetic",
        "font_tag": "F1",
        "font_size": 10.0,
        "is_bold": False,
        "is_italic": False,
        "is_underline": False,
        "is_strikeout": False,
        "item_type": item_type,
        "mcid": None,
    }


def _native_capture(source_pdf: Path) -> dict[str, object]:
    source = source_pdf.read_bytes()
    return {
        "capture_schema_version": "pdf_inspector_native_capture/v1",
        "notice_id": "PBLN_000000000114788",
        "source_kind": "pdf",
        "artifact_role": "production",
        "method": "pdf_inspector",
        "version": "1.17.0",
        "extraction_scope": "full_document",
        "source_path": source_pdf.name,
        "source_sha256": sha256(source).hexdigest(),
        "source_size_bytes": len(source),
        "process_result": {
            "pdf_type": "text_based",
            "markdown": "derived markdown is not Gold input",
            "page_count": 1,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": None,
            "confidence": 1.0,
            "is_complex_layout": True,
            "pages_with_tables": [1],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        },
        "pages_markdown_result": {
            "pages": [
                {
                    "page": 0,
                    "markdown": "",
                    "needs_ocr": False,
                    "ocr_reason": None,
                }
            ],
            "pages_with_tables": [1],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": True,
        },
        "text_items": [
            _text_item("before"),
            _text_item("left header"),
            _text_item("right header"),
            _text_item("left body"),
            _text_item("after"),
        ],
        "structure_elements": [],
    }


def _coordinate_manifest(
    *, source_sha256: str, image_sha256: str
) -> PdfCoordinateManifest:
    forward = AffineTransform.from_sequence([2, 0, 0, -2, 0, 400])
    return PdfCoordinateManifest(
        source_sha256=source_sha256,
        page=1,
        page_count=1,
        media_box=(0, 0, 100, 200),
        crop_box=(0, 0, 100, 200),
        rotation=0,
        user_unit=1.0,
        canonical_width_pt=100.0,
        canonical_height_pt=200.0,
        render_scale_px_per_point=2.0,
        rendered_width_px=200,
        rendered_height_px=400,
        pdf_origin="bottom_left",
        pdf_x_axis="right",
        pdf_y_axis="up",
        pixel_origin="top_left",
        pixel_x_axis="right",
        pixel_y_axis="down",
        user_to_pixel=forward,
        pixel_to_user=forward.inverse(),
        renderer="fixture-renderer",
        renderer_version="1.0",
        renderer_config_sha256=sha256(b"fixture-render-config").hexdigest(),
        page_image_sha256=image_sha256,
    )


def _replay_artifacts(
    root: Path,
) -> tuple[Path, Path, dict[str, object], PdfRenderManifest]:
    source_dir = root / "source"
    rendered_dir = root / "rendered"
    source_dir.mkdir(parents=True)
    rendered_dir.mkdir(parents=True)
    source_pdf = source_dir / "notice.pdf"
    render = rendered_dir / "page-0001.png"
    source_pdf.write_bytes(b"%PDF-1.7\nsynthetic table-grid Gold replay\n")
    render.write_bytes(_png_bytes())
    manifest = assemble_render_manifest(
        artifact_root=root,
        source_pdf_path=source_pdf,
        pages=(
            RenderedPageInput(
                render,
                _coordinate_manifest(
                    source_sha256=sha256(source_pdf.read_bytes()).hexdigest(),
                    image_sha256=sha256(render.read_bytes()).hexdigest(),
                ),
            ),
        ),
    )
    capture = validate_native_capture(
        _native_capture(source_pdf),
        source_pdf=source_pdf,
        expected_notice_id="PBLN_000000000114788",
    )
    return source_pdf, render, capture, manifest


def _payload(
    source_pdf: Path,
    capture: dict[str, object],
    manifest: PdfRenderManifest,
) -> dict[str, object]:
    return {
        "schema_version": "pdf_primary_table_grid_gold/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "notice_id": "PBLN_000000000114788",
        "source": {
            "source_pdf_sha256": sha256(source_pdf.read_bytes()).hexdigest(),
            "canonical_page_renders": [
                {
                    "physical_page": 1,
                    "canonical_render_sha256": manifest.pages[0].image_sha256,
                }
            ],
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": sha256(
                    canonical_native_json_bytes(capture)
                ).hexdigest(),
                "extractor_version": "1.17.0",
            },
        },
        "page_scope": [1],
        "reviewed_scopes": [
            {
                "scope_id": "p1.table.review",
                "physical_page": 1,
                "segment_id": "p1.table.segment",
                "review_status": "pending_human_confirmation",
                "reviewer_ref": None,
                "confirmed_at": None,
                "occurrence_ids": [
                    "occ:inspector:p1:t0",
                    "occ:inspector:p1:t1",
                    "occ:inspector:p1:t2",
                    "occ:inspector:p1:t3",
                    "occ:inspector:p1:t4",
                ],
                "row_ids": ["p1.table.r1", "p1.table.r2"],
                "column_ids": ["p1.table.c1", "p1.table.c2"],
                "cells": [
                    {
                        "cell_id": "p1.table.cell.r1c1",
                        "row_ids": ["p1.table.r1"],
                        "column_ids": ["p1.table.c1", "p1.table.c2"],
                        "content_status": "populated",
                        "occurrence_ids": [
                            "occ:inspector:p1:t1",
                            "occ:inspector:p1:t2",
                        ],
                    },
                    {
                        "cell_id": "p1.table.cell.r2c1",
                        "row_ids": ["p1.table.r2"],
                        "column_ids": ["p1.table.c1"],
                        "content_status": "populated",
                        "occurrence_ids": ["occ:inspector:p1:t3"],
                    },
                    {
                        "cell_id": "p1.table.cell.r2c2",
                        "row_ids": ["p1.table.r2"],
                        "column_ids": ["p1.table.c2"],
                        "content_status": "empty",
                        "occurrence_ids": [],
                    },
                ],
                "hard_negatives": [
                    {
                        "kind": "forbidden_segment_membership",
                        "occurrence_id": "occ:inspector:p1:t0",
                        "anchor_position": "before_segment",
                    },
                    {
                        "kind": "forbidden_segment_membership",
                        "occurrence_id": "occ:inspector:p1:t4",
                        "anchor_position": "after_segment",
                    },
                ],
            }
        ],
    }


class PrimaryTableGridGoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_primary_table_grid_gold_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        cls.schema_validator = Draft202012Validator(cls.schema)

    def synthetic(
        self, root: Path
    ) -> tuple[
        dict[str, object],
        Path,
        Path,
        dict[str, object],
        PdfRenderManifest,
    ]:
        source_pdf, render, capture, manifest = _replay_artifacts(root)
        return (
            _payload(source_pdf, capture, manifest),
            source_pdf,
            render,
            capture,
            manifest,
        )

    def assert_runtime_reject(self, payload: dict[str, object]) -> None:
        with self.assertRaises(PrimaryTableGridGoldError):
            PrimaryTableGridGoldFixture.from_dict(payload)

    def test_schema_runtime_and_canonical_textless_contract(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            self.schema_validator.validate(payload)
            fixture = PrimaryTableGridGoldFixture.from_dict(payload)
            self.assertEqual(
                fixture.canonical_json(),
                canonical_primary_table_grid_gold_json(payload),
            )
            self.assertEqual(
                fixture.quality_gate_status, "not_evaluable_gold_pending"
            )
            serialized = fixture.canonical_json().decode("utf-8")
            for forbidden in (
                '"text"',
                '"bbox"',
                "surya",
                "opendataloader",
                "candidate_id",
                "region_id",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_actual_114788_confirmed_fixture_loads_and_is_trusted(self) -> None:
        raw = ACTUAL_114788_GOLD.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        self.schema_validator.validate(payload)
        fixture = load_primary_table_grid_gold_file(ACTUAL_114788_GOLD)
        self.assertEqual(fixture.canonical_json(), raw)
        self.assertEqual(
            fixture.quality_gate_status,
            "not_evaluable_input_replay_required",
        )
        self.assertTrue(fixture.has_trusted_confirmation)
        self.assertEqual(
            [scope["physical_page"] for scope in fixture.to_dict()["reviewed_scopes"]],
            [3, 4],
        )
        self.assertTrue(
            all(
                scope["review_status"] == "human_confirmed"
                and scope["reviewer_ref"] == "reviewer:project-owner"
                and scope["confirmed_at"] == "2026-09-19T15:19:38Z"
                for scope in fixture.to_dict()["reviewed_scopes"]
            )
        )

    def test_merged_and_empty_cells_form_exact_rectangular_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            fixture = PrimaryTableGridGoldFixture.from_dict(payload)
            cells = fixture.to_dict()["reviewed_scopes"][0]["cells"]
            self.assertEqual(cells[0]["column_ids"], ["p1.table.c1", "p1.table.c2"])
            self.assertEqual(cells[-1]["content_status"], "empty")

            overlap = deepcopy(payload)
            cells = overlap["reviewed_scopes"][0]["cells"]  # type: ignore[index]
            cells[1]["column_ids"] = [
                "p1.table.c1",
                "p1.table.c2",
            ]
            self.assert_runtime_reject(overlap)

            gap = deepcopy(payload)
            gap["reviewed_scopes"][0]["cells"].pop()  # type: ignore[index]
            self.assert_runtime_reject(gap)

    def test_cell_spans_must_be_contiguous_and_canonically_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            noncontiguous = deepcopy(payload)
            scope = noncontiguous["reviewed_scopes"][0]  # type: ignore[index]
            scope["column_ids"] = ["p1.table.c1", "p1.table.c2", "p1.table.c3"]
            scope["cells"][0]["column_ids"] = ["p1.table.c1", "p1.table.c3"]
            self.assert_runtime_reject(noncontiguous)

            reordered = deepcopy(payload)
            cells = reordered["reviewed_scopes"][0]["cells"]  # type: ignore[index]
            cells[0], cells[1] = cells[1], cells[0]
            self.assert_runtime_reject(reordered)

    def test_occurrences_are_exactly_partitioned_and_anchors_bound_extent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            duplicate = deepcopy(payload)
            cells = duplicate["reviewed_scopes"][0]["cells"]  # type: ignore[index]
            cells[1]["occurrence_ids"] = [
                "occ:inspector:p1:t1"
            ]
            self.assert_runtime_reject(duplicate)

            missing = deepcopy(payload)
            missing["reviewed_scopes"][0]["hard_negatives"].pop()  # type: ignore[index]
            self.assert_runtime_reject(missing)

            wrong_side = deepcopy(payload)
            negatives = wrong_side["reviewed_scopes"][0][  # type: ignore[index]
                "hard_negatives"
            ]
            negatives[0]["anchor_position"] = "after_segment"
            self.assert_runtime_reject(wrong_side)

    def test_schema_and_runtime_reject_gold_leakage_and_bad_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            mutations: list[dict[str, object]] = []
            bad = deepcopy(payload)
            cells = bad["reviewed_scopes"][0]["cells"]  # type: ignore[index]
            cells[0]["text"] = "forbidden"
            mutations.append(bad)
            bad = deepcopy(payload)
            scope = bad["reviewed_scopes"][0]  # type: ignore[index]
            scope["surya_region_id"] = "region-1"
            mutations.append(bad)
            bad = deepcopy(payload)
            scope = bad["reviewed_scopes"][0]  # type: ignore[index]
            scope["reviewer_ref"] = "reviewer:forbidden"
            mutations.append(bad)
            for mutation in mutations:
                self.assertFalse(self.schema_validator.is_valid(mutation))
                self.assert_runtime_reject(mutation)

    def test_json_node_cap_fails_closed_before_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            payload["oversized_unknown"] = [None] * 50_001
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError, "node safety cap"
            ):
                PrimaryTableGridGoldFixture.from_dict(payload)

    def test_allowlist_and_review_status_are_independent_trust_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            pending = PrimaryTableGridGoldFixture.from_dict(payload)
            with patch.object(
                table_gold,
                "TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S",
                frozenset({pending.canonical_sha256}),
            ):
                self.assertFalse(pending.has_trusted_confirmation)

            confirmed_payload = deepcopy(payload)
            scope = confirmed_payload["reviewed_scopes"][0]  # type: ignore[index]
            scope["review_status"] = "human_confirmed"
            scope["reviewer_ref"] = "reviewer:review-001"
            scope["confirmed_at"] = "2026-09-19T09:00:00Z"
            confirmed = PrimaryTableGridGoldFixture.from_dict(confirmed_payload)
            self.assertFalse(confirmed.has_trusted_confirmation)
            self.assertEqual(
                confirmed.quality_gate_status,
                "not_evaluable_untrusted_confirmation",
            )
            with patch.object(
                table_gold,
                "TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S",
                frozenset({confirmed.canonical_sha256}),
            ):
                self.assertTrue(confirmed.has_trusted_confirmation)
                self.assertEqual(
                    confirmed.quality_gate_status,
                    "not_evaluable_input_replay_required",
                )

    def test_reader_is_canonical_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, *_ = self.synthetic(root)
            raw = _encoded(payload)
            fixture = parse_primary_table_grid_gold_bytes(raw)
            self.assertEqual(fixture.canonical_json(), raw)
            with self.assertRaisesRegex(PrimaryTableGridGoldError, "not canonical"):
                parse_primary_table_grid_gold_bytes(raw + b"\n")
            duplicate = raw.replace(
                b'{"evaluation_only":true,',
                b'{"evaluation_only":true,"evaluation_only":true,',
                1,
            )
            with self.assertRaisesRegex(PrimaryTableGridGoldError, "duplicate"):
                parse_primary_table_grid_gold_bytes(duplicate)

            gold_file = root / "gold.json"
            gold_file.write_bytes(raw)
            self.assertEqual(
                load_primary_table_grid_gold_file(gold_file).canonical_json(), raw
            )
            link = root / "gold-link.json"
            link.symlink_to(gold_file)
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError, "regular non-symlink"
            ):
                load_primary_table_grid_gold_file(link)

    def test_reader_rejects_oversized_integer_float_and_nonfinite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            raw = _encoded(payload)
            oversized_integer = raw.replace(
                b'"physical_page":1', b'"physical_page":12345678', 1
            )
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError, "oversized integer"
            ):
                parse_primary_table_grid_gold_bytes(oversized_integer)

            floating_point = raw.replace(
                b'"physical_page":1', b'"physical_page":1.0', 1
            )
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError, "floating-point"
            ):
                parse_primary_table_grid_gold_bytes(floating_point)

            nonfinite = raw.replace(
                b'"physical_page":1', b'"physical_page":NaN', 1
            )
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError, "non-finite"
            ):
                parse_primary_table_grid_gold_bytes(nonfinite)

    def test_public_replay_revalidates_source_native_and_render_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, render, capture, manifest = self.synthetic(root)
            replayed = validate_primary_table_grid_gold_against_inputs(
                payload,
                source_pdf=source_pdf,
                native_capture=capture,
                render_manifest=manifest,
                render_artifact_root=root,
            )
            self.assertTrue(replayed.is_replay_receipt)
            self.assertFalse(replayed.has_trusted_confirmation)

            render.write_bytes(b"not a PNG")
            with self.assertRaisesRegex(
                PrimaryTableGridGoldError,
                "render manifest did not pass strict artifact replay",
            ):
                validate_primary_table_grid_gold_against_inputs(
                    payload,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )

    def test_replay_receipt_rejects_direct_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, *_ = self.synthetic(Path(directory))
            fixture = PrimaryTableGridGoldFixture.from_dict(payload)
            replayed_type = table_gold.ReplayedPrimaryTableGridGold
            with self.assertRaises(TypeError):
                replayed_type(fixture)
            with self.assertRaises(PrimaryTableGridGoldError):
                replayed_type(
                    fixture,
                    _construction_token=object(),
                    replayed_canonical_sha256=fixture.canonical_sha256,
                )

    def test_actual_114788_confirmed_replay_when_artifact_environment_is_configured(
        self,
    ) -> None:
        names = {
            "source_pdf": "PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF",
            "native_capture": "PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE",
            "render_manifest": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST",
            "render_artifact_root": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT",
        }
        configured = {key: os.environ.get(name) for key, name in names.items()}
        if not all(configured.values()):
            self.skipTest("actual 114788 table-grid Gold environment is not configured")

        def mapping(name: str) -> dict:
            return json.loads(Path(configured[name]).read_text(encoding="utf-8"))  # type: ignore[arg-type]

        replayed = validate_primary_table_grid_gold_against_inputs(
            load_primary_table_grid_gold_file(ACTUAL_114788_GOLD),
            source_pdf=Path(configured["source_pdf"]),  # type: ignore[arg-type]
            native_capture=mapping("native_capture"),
            render_manifest=mapping("render_manifest"),
            render_artifact_root=Path(configured["render_artifact_root"]),  # type: ignore[arg-type]
        )
        self.assertTrue(replayed.is_replay_receipt)
        self.assertTrue(replayed.has_trusted_confirmation)
        self.assertEqual(
            replayed.fixture.quality_gate_status,
            "not_evaluable_input_replay_required",
        )
        self.assertEqual(
            replayed.canonical_sha256,
            "548f3fd6ce803e15439f4c4e0e5abaa7fa295db76b6ceb71aa61dcc77bf6d725",
        )


if __name__ == "__main__":
    unittest.main()
