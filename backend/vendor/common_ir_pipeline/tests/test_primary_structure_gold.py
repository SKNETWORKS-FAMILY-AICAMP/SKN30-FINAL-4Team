from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
import common_ir_pipeline.pdf_fusion.primary_structure_gold as primary_structure_gold
from common_ir_pipeline.pdf_fusion.primary_structure_gold import (
    PrimaryStructureGoldError,
    PrimaryStructureGoldFixture,
    canonical_primary_structure_gold_json,
    load_primary_structure_gold_file,
    parse_primary_structure_gold_bytes,
    validate_primary_structure_gold_against_inputs,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPageInput,
    assemble_render_manifest,
)


FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_structure_gold_114788_p3_purpose.v1.json"
)
CONFIRMED_FIXTURE_SHA256 = (
    "2e80529bb500c3947a1121a592483164ce50365e321f352891fdf660d1f362ea"
)
ACTUAL_REPLAY_ENV = {
    "source_pdf": "PRIMARY_STRUCTURE_GOLD_114788_SOURCE_PDF",
    "native_capture": "PRIMARY_STRUCTURE_GOLD_114788_NATIVE_CAPTURE",
    "render_manifest": "PRIMARY_STRUCTURE_GOLD_114788_RENDER_MANIFEST",
    "render_root": "PRIMARY_STRUCTURE_GOLD_114788_RENDER_ROOT",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _replace_occurrences(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _replace_occurrences(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_occurrences(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def _text_item(text: str, *, page: int = 1, item_type: str = "text") -> dict[str, object]:
    return {
        "page": page,
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


def _native_capture(
    source_pdf: Path,
    *,
    first_item_type: str = "text",
    notice_id: str = "PBLN_000000000114788",
) -> dict[str, object]:
    source = source_pdf.read_bytes()
    return {
        "capture_schema_version": "pdf_inspector_native_capture/v1",
        "notice_id": notice_id,
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
            "markdown": "derived markdown must not be a Gold input",
            "page_count": 1,
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "title": None,
            "confidence": 1.0,
            "is_complex_layout": False,
            "pages_with_tables": [],
            "pages_with_columns": [],
            "has_encoding_issues": False,
        },
        "pages_markdown_result": {
            "pages": [
                {"page": page, "markdown": "", "needs_ocr": False, "ocr_reason": None}
                for page in range(1)
            ],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": False,
        },
        "text_items": [
            _text_item("heading", item_type=first_item_type),
            _text_item("first"),
            _text_item("second"),
        ],
        "structure_elements": [],
    }


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


def _one_page_render_artifacts(
    root: Path,
    *,
    source_bytes: bytes = b"%PDF-1.7\nsynthetic primary Gold replay\n",
) -> tuple[Path, Path, PdfRenderManifest]:
    source_dir = root / "source"
    rendered_dir = root / "rendered"
    source_dir.mkdir(parents=True)
    rendered_dir.mkdir(parents=True)
    source_pdf = source_dir / "notice.pdf"
    render = rendered_dir / "page-0001.png"
    source_pdf.write_bytes(source_bytes)
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
    return source_pdf, render, manifest


class PrimaryStructureGoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = FIXTURE.read_bytes()
        self.payload: dict[str, object] = json.loads(self.raw)
        self.schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_primary_structure_gold_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.schema_validator = Draft202012Validator(self.schema)

    def parsed(self, payload: dict[str, object] | None = None) -> PrimaryStructureGoldFixture:
        return parse_primary_structure_gold_bytes(
            self.raw if payload is None else _encoded(payload)
        )

    def assert_schema_and_runtime_reject(self, payload: dict[str, object]) -> None:
        self.assertFalse(self.schema_validator.is_valid(payload))
        with self.assertRaises(PrimaryStructureGoldError):
            self.parsed(payload)

    def pending_payload(self) -> dict[str, object]:
        payload = deepcopy(self.payload)
        for scope in payload["reviewed_scopes"]:  # type: ignore[union-attr]
            scope["review_status"] = "pending_human_confirmation"
            scope["reviewer_ref"] = None
            scope["confirmed_at"] = None
        return payload

    def synthetic_replay_inputs(
        self, root: Path
    ) -> tuple[
        dict[str, object],
        Path,
        Path,
        dict[str, object],
        PdfRenderManifest,
    ]:
        source_pdf, render, manifest = _one_page_render_artifacts(root)
        capture = validate_native_capture(
            _native_capture(source_pdf),
            source_pdf=source_pdf,
            expected_notice_id="PBLN_000000000114788",
        )
        replacements = {
            "occ:inspector:p3:t17": "occ:inspector:p1:t0",
            "occ:inspector:p3:t19": "occ:inspector:p1:t1",
            "occ:inspector:p3:t20": "occ:inspector:p1:t2",
        }
        replaced = _replace_occurrences(self.pending_payload(), replacements)
        self.assertIsInstance(replaced, dict)
        payload: dict[str, object] = replaced
        payload["page_scope"] = [1]
        scope = payload["reviewed_scopes"][0]  # type: ignore[index]
        scope["scope_id"] = "p1.purpose"
        scope["physical_page"] = 1
        group = scope["ordered_groups"][0]
        group["group_id"] = "p1.purpose.paragraph"
        scope["hard_negatives"][0]["target_group_id"] = "p1.purpose.paragraph"
        source = payload["source"]  # type: ignore[assignment]
        source["source_pdf_sha256"] = sha256(source_pdf.read_bytes()).hexdigest()
        source["native_capture"]["canonical_sha256"] = sha256(
            canonical_native_json_bytes(capture)
        ).hexdigest()
        source["canonical_page_renders"] = [
            {
                "physical_page": 1,
                "canonical_render_sha256": manifest.pages[0].image_sha256,
            }
        ]
        return payload, source_pdf, render, capture, manifest

    def test_114788_confirmed_p3_purpose_fixture_and_schema_are_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        fixture = load_primary_structure_gold_file(FIXTURE)
        payload = fixture.to_dict()
        self.schema_validator.validate(payload)
        self.assertEqual(self.raw, fixture.canonical_json())
        self.assertEqual(fixture.canonical_sha256, CONFIRMED_FIXTURE_SHA256)
        self.assertTrue(fixture.has_trusted_confirmation)
        self.assertFalse(fixture.is_evaluable)
        self.assertEqual(
            fixture.quality_gate_status,
            "not_evaluable_input_replay_required",
        )
        self.assertEqual(payload["page_scope"], [3])
        scope = payload["reviewed_scopes"][0]
        self.assertEqual(scope["occurrence_ids"], [
            "occ:inspector:p3:t17",
            "occ:inspector:p3:t19",
            "occ:inspector:p3:t20",
        ])
        group = scope["ordered_groups"][0]
        self.assertEqual(group["occurrence_ids"], [
            "occ:inspector:p3:t19",
            "occ:inspector:p3:t20",
        ])
        self.assertEqual(group["boundaries"][0]["join_class"], "intra_word_wrap")
        self.assertEqual(scope["hard_negatives"], [{
            "kind": "forbidden_same_leaf",
            "occurrence_id": "occ:inspector:p3:t17",
            "target_group_id": "p3.purpose.paragraph",
        }])
        serialized = fixture.canonical_json().decode("utf-8")
        for forbidden in (
            "추진목적",
            "기술경쟁력을",
            "원하여 성공적인",
            '"text"',
            '"separator"',
            "reconstruction_plan_sha256",
            "opendataloader_sha256",
            "surya_layout_artifact_sha256",
            "fragment_groups_sha256",
            "context_groups_sha256",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_confirmation_metadata_is_not_a_trust_anchor(self) -> None:
        pending_payload = self.pending_payload()
        pending = self.parsed(pending_payload)
        self.assertFalse(pending.has_trusted_confirmation)
        self.assertFalse(pending.is_evaluable)
        self.assertEqual(pending.quality_gate_status, "not_evaluable_gold_pending")
        self.assertNotIn(
            "quality_gate_status",
            self.payload["reviewed_scopes"][0],  # type: ignore[index]
        )

        confirmed = deepcopy(self.payload)
        scope = confirmed["reviewed_scopes"][0]  # type: ignore[index]
        scope["review_status"] = "human_confirmed"
        scope["reviewer_ref"] = "reviewer:review-001"
        scope["confirmed_at"] = "2026-09-19T09:00:00Z"
        parsed = self.parsed(confirmed)
        self.assertFalse(parsed.has_trusted_confirmation)
        self.assertFalse(parsed.is_evaluable)
        self.assertEqual(
            parsed.quality_gate_status,
            "not_evaluable_untrusted_confirmation",
        )
        with patch.object(
            primary_structure_gold,
            "TRUSTED_CONFIRMED_GOLD_SHA256S",
            frozenset({parsed.canonical_sha256}),
        ):
            self.assertTrue(parsed.has_trusted_confirmation)
            self.assertFalse(parsed.is_evaluable)
            self.assertEqual(
                parsed.quality_gate_status,
                "not_evaluable_input_replay_required",
            )

        for field, value in (
            ("reviewer_ref", "reviewer:review-001"),
            ("confirmed_at", "2026-09-19T09:00:00Z"),
        ):
            bad = deepcopy(pending_payload)
            bad["reviewed_scopes"][0][field] = value  # type: ignore[index]
            self.assert_schema_and_runtime_reject(bad)

        for field, value in (
            ("reviewer_ref", None),
            ("confirmed_at", None),
        ):
            bad = deepcopy(confirmed)
            bad["reviewed_scopes"][0][field] = value  # type: ignore[index]
            self.assert_schema_and_runtime_reject(bad)

        persisted_gate = deepcopy(confirmed)
        persisted_gate["reviewed_scopes"][0]["quality_gate_status"] = "evaluable"  # type: ignore[index]
        self.assert_schema_and_runtime_reject(persisted_gate)

    def test_schema_runtime_parity_rejects_unknown_fields_enums_and_labels(self) -> None:
        mutations: list[dict[str, object]] = []
        bad = deepcopy(self.payload)
        bad["unknown"] = True
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["source"]["surya_layout_artifact_sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["printed_page_label"] = "page one"  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["ordered_groups"][0]["kind"] = "table"  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["ordered_groups"][0]["boundaries"][0]["join_class"] = "newline"  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["hard_negatives"][0]["kind"] = "exclude_document"  # type: ignore[index]
        mutations.append(bad)
        for payload in mutations:
            self.assert_schema_and_runtime_reject(payload)

    def test_notice_id_rejects_non_ascii_digits_and_korean_text(self) -> None:
        for notice_id in (
            "PBLN_０００００００００１１４７８８",
            "PBLN_공고번호일이삼사오육칠팔구십",
        ):
            with self.subTest(notice_id=notice_id):
                bad = deepcopy(self.payload)
                bad["notice_id"] = notice_id
                self.assert_schema_and_runtime_reject(bad)

    def test_reader_rejects_duplicate_nonfinite_surrogate_and_noncanonical_json(self) -> None:
        duplicate = self.raw.replace(
            b'{"evaluation_only":true,',
            b'{"evaluation_only":true,"evaluation_only":true,',
            1,
        )
        with self.assertRaisesRegex(PrimaryStructureGoldError, "duplicate"):
            parse_primary_structure_gold_bytes(duplicate)
        nonfinite = self.raw.replace(b'"physical_page":3', b'"physical_page":NaN', 1)
        with self.assertRaisesRegex(PrimaryStructureGoldError, "non-finite"):
            parse_primary_structure_gold_bytes(nonfinite)
        surrogate = self.raw.replace(
            b'"notice_id":"PBLN_000000000114788"',
            b'"notice_id":"\\ud800"',
            1,
        )
        with self.assertRaises(PrimaryStructureGoldError):
            parse_primary_structure_gold_bytes(surrogate)
        with self.assertRaisesRegex(PrimaryStructureGoldError, "BOM"):
            parse_primary_structure_gold_bytes(b"\xef\xbb\xbf" + self.raw)
        with self.assertRaisesRegex(PrimaryStructureGoldError, "not canonical"):
            parse_primary_structure_gold_bytes(self.raw + b"\n")
        with self.assertRaisesRegex(PrimaryStructureGoldError, "valid UTF-8 JSON"):
            parse_primary_structure_gold_bytes(
                b'{"oversized_integer":' + b"1" * 5_000 + b"}"
            )

    def test_semantic_text_and_raw_separator_cannot_be_smuggled_in(self) -> None:
        mutations: list[dict[str, object]] = []
        for target_path, key in (
            (("reviewed_scopes", 0, "ordered_groups", 0), "text"),
            (("reviewed_scopes", 0, "ordered_groups", 0, "boundaries", 0), "separator"),
            (("reviewed_scopes", 0, "hard_negatives", 0), "reason_text"),
        ):
            bad = deepcopy(self.payload)
            target: object = bad
            for segment in target_path:
                target = target[segment]  # type: ignore[index]
            target[key] = "semantic payload"  # type: ignore[index]
            mutations.append(bad)
        for payload in mutations:
            self.assert_schema_and_runtime_reject(payload)

    def test_canonical_json_and_sha_are_deterministic(self) -> None:
        fixture = self.parsed()
        reordered = {key: self.payload[key] for key in reversed(list(self.payload))}
        self.assertEqual(
            canonical_primary_structure_gold_json(reordered),
            fixture.canonical_json(),
        )
        self.assertEqual(fixture.canonical_sha256, sha256(self.raw).hexdigest())
        round_trip = parse_primary_structure_gold_bytes(fixture.canonical_json())
        self.assertEqual(round_trip.canonical_json(), fixture.canonical_json())
        with self.assertRaises(TypeError):
            fixture.payload["notice_id"] = "changed"  # type: ignore[index]

    def test_occurrence_order_boundary_and_hard_negative_fail_closed(self) -> None:
        mutations: list[dict[str, object]] = []
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["occurrence_ids"] = [  # type: ignore[index]
            "occ:inspector:p3:t19",
            "occ:inspector:p3:t17",
            "occ:inspector:p3:t20",
        ]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        group = bad["reviewed_scopes"][0]["ordered_groups"][0]  # type: ignore[index]
        group["occurrence_ids"] = list(reversed(group["occurrence_ids"]))
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["ordered_groups"][0]["boundaries"] = []  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["ordered_groups"][0]["occurrence_ids"].insert(0, "occ:inspector:p3:t17")  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["hard_negatives"][0]["target_group_id"] = "unknown.group"  # type: ignore[index]
        mutations.append(bad)
        for payload in mutations:
            with self.assertRaises(PrimaryStructureGoldError):
                self.parsed(payload)

    def test_occurrence_cannot_be_owned_by_two_reviewed_scopes(self) -> None:
        bad = deepcopy(self.payload)
        duplicate_scope = deepcopy(bad["reviewed_scopes"][0])  # type: ignore[index]
        duplicate_scope["scope_id"] = "p3.secondary"
        duplicate_scope["ordered_groups"][0]["group_id"] = "p3.secondary.paragraph"
        duplicate_scope["hard_negatives"][0]["target_group_id"] = "p3.secondary.paragraph"
        bad["reviewed_scopes"].append(duplicate_scope)  # type: ignore[union-attr]
        self.assertTrue(self.schema_validator.is_valid(bad))
        with self.assertRaisesRegex(
            PrimaryStructureGoldError,
            "cannot be adjudicated by two reviewed scopes",
        ):
            self.parsed(bad)

    def test_artifact_bound_replay_uses_full_render_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, render, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )

            fixture = validate_primary_structure_gold_against_inputs(
                payload,
                source_pdf=source_pdf,
                native_capture=capture,
                render_manifest=manifest,
                render_artifact_root=root,
            )
            self.assertTrue(fixture.is_replay_receipt)
            self.assertFalse(fixture.has_trusted_confirmation)
            self.assertEqual(
                fixture.fixture.quality_gate_status,
                "not_evaluable_gold_pending",
            )

            render.write_bytes(b"not a PNG")
            with self.assertRaisesRegex(
                PrimaryStructureGoldError,
                "render manifest did not pass strict artifact replay",
            ):
                validate_primary_structure_gold_against_inputs(
                    payload,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )
            render.write_bytes(_png_bytes())

            missing = deepcopy(payload)
            missing["reviewed_scopes"][0]["occurrence_ids"][2] = "occ:inspector:p1:t99"  # type: ignore[index]
            missing["reviewed_scopes"][0]["ordered_groups"][0]["occurrence_ids"][1] = "occ:inspector:p1:t99"  # type: ignore[index]
            missing["reviewed_scopes"][0]["ordered_groups"][0]["boundaries"][0]["right_occurrence_id"] = "occ:inspector:p1:t99"  # type: ignore[index]
            with self.assertRaisesRegex(PrimaryStructureGoldError, "not present"):
                validate_primary_structure_gold_against_inputs(
                    missing,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )

            image_capture = validate_native_capture(
                _native_capture(source_pdf, first_item_type="image"),
                source_pdf=source_pdf,
                expected_notice_id="PBLN_000000000114788",
            )
            image_bound = deepcopy(payload)
            image_bound["source"]["native_capture"]["canonical_sha256"] = sha256(  # type: ignore[index]
                canonical_native_json_bytes(image_capture)
            ).hexdigest()
            with self.assertRaisesRegex(
                PrimaryStructureGoldError,
                "only substantive native text occurrences",
            ):
                validate_primary_structure_gold_against_inputs(
                    image_bound,
                    source_pdf=source_pdf,
                    native_capture=image_capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )

    def test_confirmed_allowlisted_gold_is_evaluable_only_after_artifact_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, _, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            scope = payload["reviewed_scopes"][0]  # type: ignore[index]
            scope["review_status"] = "human_confirmed"
            scope["reviewer_ref"] = "reviewer:review-001"
            scope["confirmed_at"] = "2026-09-19T09:00:00Z"
            raw_fixture = PrimaryStructureGoldFixture.from_dict(payload)

            with patch.object(
                primary_structure_gold,
                "TRUSTED_CONFIRMED_GOLD_SHA256S",
                frozenset({raw_fixture.canonical_sha256}),
            ):
                self.assertTrue(raw_fixture.has_trusted_confirmation)
                self.assertFalse(raw_fixture.is_evaluable)
                self.assertEqual(
                    raw_fixture.quality_gate_status,
                    "not_evaluable_input_replay_required",
                )
                replayed = validate_primary_structure_gold_against_inputs(
                    raw_fixture,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )
                self.assertTrue(replayed.has_trusted_confirmation)
                self.assertTrue(replayed.is_replay_receipt)
                self.assertTrue(replayed.has_trusted_confirmation)

    def test_replay_rejects_native_and_render_page_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, _, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            capture["process_result"]["page_count"] = 2  # type: ignore[index]
            capture["pages_markdown_result"]["pages"].append(  # type: ignore[index]
                {
                    "page": 1,
                    "markdown": "",
                    "needs_ocr": False,
                    "ocr_reason": None,
                }
            )
            mismatched_capture = validate_native_capture(
                capture,
                source_pdf=source_pdf,
                expected_notice_id="PBLN_000000000114788",
            )
            payload["source"]["native_capture"]["canonical_sha256"] = sha256(  # type: ignore[index]
                canonical_native_json_bytes(mismatched_capture)
            ).hexdigest()
            with self.assertRaisesRegex(
                PrimaryStructureGoldError,
                "page counts disagree",
            ):
                validate_primary_structure_gold_against_inputs(
                    payload,
                    source_pdf=source_pdf,
                    native_capture=mismatched_capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )

    def test_replay_receipt_rejects_accidental_direct_construction_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, _, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            raw_fixture = PrimaryStructureGoldFixture.from_dict(payload)
            replayed_type = primary_structure_gold.ReplayedPrimaryStructureGold

            with self.assertRaises(TypeError):
                replayed_type(raw_fixture)

            with self.assertRaises(PrimaryStructureGoldError):
                replayed_type(
                    raw_fixture,
                    _construction_token=object(),
                    replayed_canonical_sha256=raw_fixture.canonical_sha256,
                )

            class DuckTypedFixture:
                canonical_sha256 = raw_fixture.canonical_sha256

            with self.assertRaises(PrimaryStructureGoldError):
                replayed_type(
                    DuckTypedFixture(),  # type: ignore[arg-type]
                    _construction_token=object(),
                    replayed_canonical_sha256=raw_fixture.canonical_sha256,
                )

            valid_replayed = validate_primary_structure_gold_against_inputs(
                raw_fixture,
                source_pdf=source_pdf,
                native_capture=capture,
                render_manifest=manifest,
                render_artifact_root=root,
            )
            with self.assertRaises((TypeError, PrimaryStructureGoldError)):
                replace(valid_replayed, fixture=raw_fixture)

    def test_render_manifest_mapping_preflight_rejects_more_than_256_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, _, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            cases = {
                "page-count": {
                    **manifest.to_dict(),
                    "page_count": 257,
                },
                "pages-array": {
                    **manifest.to_dict(),
                    "pages": [{} for _ in range(257)],
                },
            }
            for name, raw_manifest in cases.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                    PrimaryStructureGoldError,
                    "render manifest did not pass strict artifact replay",
                ):
                    validate_primary_structure_gold_against_inputs(
                        payload,
                        source_pdf=source_pdf,
                        native_capture=capture,
                        render_manifest=raw_manifest,
                        render_artifact_root=root,
                    )

    def test_artifact_bound_replay_rejects_manifest_from_another_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, source_pdf, _, capture, _ = self.synthetic_replay_inputs(
                root / "expected"
            )
            _, _, foreign_manifest = _one_page_render_artifacts(
                root / "foreign",
                source_bytes=b"%PDF-1.7\na different source document\n",
            )
            with self.assertRaisesRegex(
                PrimaryStructureGoldError,
                "render manifest source binding mismatch",
            ):
                validate_primary_structure_gold_against_inputs(
                    payload,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=foreign_manifest,
                    render_artifact_root=root / "foreign",
                )

    def test_reviewed_occurrence_requires_substantive_text_and_positive_geometry(self) -> None:
        mutations = (
            ("whitespace", "text", " \t\n"),
            ("zero-width", "width", 0.0),
            ("negative-width", "width", -1.0),
            ("zero-height", "height", 0.0),
            ("negative-height", "height", -1.0),
            ("outside-coordinate-crop", "x", 101.0),
        )
        for name, field, value in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                payload, source_pdf, _, capture, manifest = (
                    self.synthetic_replay_inputs(root)
                )
                capture["text_items"][1][field] = value  # type: ignore[index]
                mutated_capture = validate_native_capture(
                    capture,
                    source_pdf=source_pdf,
                    expected_notice_id="PBLN_000000000114788",
                )
                payload["source"]["native_capture"]["canonical_sha256"] = sha256(  # type: ignore[index]
                    canonical_native_json_bytes(mutated_capture)
                ).hexdigest()
                with self.assertRaisesRegex(
                    PrimaryStructureGoldError,
                    "only substantive native text occurrences",
                ):
                    validate_primary_structure_gold_against_inputs(
                        payload,
                        source_pdf=source_pdf,
                        native_capture=mutated_capture,
                        render_manifest=manifest,
                        render_artifact_root=root,
                    )

    def test_actual_114788_replay_when_artifact_environment_is_configured(self) -> None:
        configured = {
            name: os.environ.get(variable)
            for name, variable in ACTUAL_REPLAY_ENV.items()
        }
        missing = [
            ACTUAL_REPLAY_ENV[name]
            for name, value in configured.items()
            if not value
        ]
        if missing:
            self.skipTest(
                "actual 114788 replay requires: " + ", ".join(sorted(missing))
            )
        source_pdf = Path(configured["source_pdf"])  # type: ignore[arg-type]
        native_capture = json.loads(
            Path(configured["native_capture"]).read_text(encoding="utf-8")  # type: ignore[arg-type]
        )
        render_manifest = json.loads(
            Path(configured["render_manifest"]).read_text(encoding="utf-8")  # type: ignore[arg-type]
        )
        fixture = load_primary_structure_gold_file(FIXTURE)
        replayed = validate_primary_structure_gold_against_inputs(
            fixture,
            source_pdf=source_pdf,
            native_capture=native_capture,
            render_manifest=render_manifest,
            render_artifact_root=Path(configured["render_root"]),  # type: ignore[arg-type]
        )
        self.assertEqual(replayed.canonical_json(), fixture.canonical_json())
        self.assertTrue(replayed.has_trusted_confirmation)
        self.assertTrue(replayed.is_replay_receipt)


if __name__ == "__main__":
    unittest.main()
