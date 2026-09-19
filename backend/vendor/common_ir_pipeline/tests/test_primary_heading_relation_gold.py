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
import common_ir_pipeline.pdf_fusion.primary_heading_relation_gold as heading_gold
from common_ir_pipeline.pdf_fusion.primary_heading_relation_gold import (
    PrimaryHeadingRelationGoldError,
    PrimaryHeadingRelationGoldFixture,
    canonical_primary_heading_relation_gold_json,
    load_primary_heading_relation_gold_file,
    parse_primary_heading_relation_gold_bytes,
    validate_primary_heading_relation_gold_against_inputs,
)
import common_ir_pipeline.pdf_fusion.primary_structure_gold as structure_gold
from common_ir_pipeline.pdf_fusion.primary_structure_gold import (
    PrimaryStructureGoldFixture,
    canonical_primary_structure_gold_json,
    load_primary_structure_gold_file,
    validate_primary_structure_gold_against_inputs,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPageInput,
    assemble_render_manifest,
)


BASE_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_structure_gold_114788_p3_purpose.v1.json"
)
FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_heading_relation_gold_114788_p3_purpose.v1.json"
)
CONFIRMED_FIXTURE_SHA256 = (
    "6bc14a5bc1802fc4e5752795f875dd8b61d95fc04cca4e663923791fd8dd83d5"
)
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
        return {
            key: _replace_occurrences(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_occurrences(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


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
                {
                    "page": 0,
                    "markdown": "",
                    "needs_ocr": False,
                    "ocr_reason": None,
                }
            ],
            "pages_with_tables": [],
            "pages_with_columns": [],
            "pages_needing_ocr": [],
            "ocr_reasons_by_page": [],
            "is_complex": False,
        },
        "text_items": [
            _text_item("heading"),
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
) -> tuple[Path, Path, PdfRenderManifest]:
    source_dir = root / "source"
    rendered_dir = root / "rendered"
    source_dir.mkdir(parents=True)
    rendered_dir.mkdir(parents=True)
    source_pdf = source_dir / "notice.pdf"
    render = rendered_dir / "page-0001.png"
    source_pdf.write_bytes(b"%PDF-1.7\nsynthetic heading Gold replay\n")
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


class PrimaryHeadingRelationGoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_fixture = load_primary_structure_gold_file(BASE_FIXTURE)
        cls.raw = FIXTURE.read_bytes()
        cls.payload: dict[str, object] = json.loads(cls.raw)
        cls.base_payload: dict[str, object] = cls.base_fixture.to_dict()
        cls.schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_primary_heading_relation_gold_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        cls.schema_validator = Draft202012Validator(cls.schema)

    def parsed(
        self,
        payload: dict[str, object] | None = None,
        *,
        base: dict[str, object] | PrimaryStructureGoldFixture | None = None,
    ) -> PrimaryHeadingRelationGoldFixture:
        return parse_primary_heading_relation_gold_bytes(
            self.raw if payload is None else _encoded(payload),
            primary_structure_gold=self.base_fixture if base is None else base,
        )

    def assert_schema_and_runtime_reject(
        self, payload: dict[str, object]
    ) -> None:
        self.assertFalse(self.schema_validator.is_valid(payload))
        with self.assertRaises(PrimaryHeadingRelationGoldError):
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
        PrimaryStructureGoldFixture,
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
        replaced_base = _replace_occurrences(
            deepcopy(self.base_payload), replacements
        )
        self.assertIsInstance(replaced_base, dict)
        base: dict[str, object] = replaced_base
        base["page_scope"] = [1]
        base_scope = base["reviewed_scopes"][0]  # type: ignore[index]
        base_scope["scope_id"] = "p1.purpose"
        base_scope["physical_page"] = 1
        base_scope["review_status"] = "pending_human_confirmation"
        base_scope["reviewer_ref"] = None
        base_scope["confirmed_at"] = None
        base_scope["ordered_groups"][0]["group_id"] = "p1.purpose.paragraph"
        base_scope["hard_negatives"][0]["target_group_id"] = (
            "p1.purpose.paragraph"
        )
        base_source = base["source"]  # type: ignore[assignment]
        base_source["source_pdf_sha256"] = sha256(source_pdf.read_bytes()).hexdigest()
        base_source["native_capture"]["canonical_sha256"] = sha256(
            canonical_native_json_bytes(capture)
        ).hexdigest()
        base_source["canonical_page_renders"] = [
            {
                "physical_page": 1,
                "canonical_render_sha256": manifest.pages[0].image_sha256,
            }
        ]
        base_fixture = PrimaryStructureGoldFixture.from_dict(base)

        replaced_heading = _replace_occurrences(
            self.pending_payload(), replacements
        )
        self.assertIsInstance(replaced_heading, dict)
        payload: dict[str, object] = replaced_heading
        payload["page_scope"] = [1]
        scope = payload["reviewed_scopes"][0]  # type: ignore[index]
        scope["scope_id"] = "p1.purpose.heading"
        scope["physical_page"] = 1
        scope["base_structure_scope_id"] = "p1.purpose"
        scope["expected_relations"][0]["body_group_id"] = (
            "p1.purpose.paragraph"
        )
        payload["source"] = {
            **deepcopy(base_fixture.to_dict()["source"]),
            "primary_structure_gold": {
                "schema_version": "pdf_primary_structure_gold/v1",
                "canonical_sha256": base_fixture.canonical_sha256,
            },
        }
        return payload, base_fixture, source_pdf, render, capture, manifest

    def test_114788_fixture_schema_digest_and_relation_are_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        fixture = load_primary_heading_relation_gold_file(
            FIXTURE, primary_structure_gold=self.base_fixture
        )
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
        self.assertEqual(
            payload["reviewed_scopes"][0]["expected_relations"],
            [
                {
                    "heading_occurrence_id": "occ:inspector:p3:t17",
                    "body_group_id": "p3.purpose.paragraph",
                }
            ],
        )
        serialized = fixture.canonical_json().decode("utf-8")
        for forbidden in (
            "leaf-",
            '"text"',
            '"bbox"',
            "surya",
            "opendataloader",
            "reconstruction_plan",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_pending_and_allowlist_are_independent_trust_gates(self) -> None:
        pending = self.parsed(self.pending_payload())
        self.assertFalse(pending.has_trusted_confirmation)
        self.assertEqual(pending.quality_gate_status, "not_evaluable_gold_pending")

        confirmed = deepcopy(self.payload)
        confirmed["reviewed_scopes"][0]["reviewer_ref"] = "reviewer:other-review"  # type: ignore[index]
        fixture = self.parsed(confirmed)
        self.assertFalse(fixture.has_trusted_confirmation)
        self.assertEqual(
            fixture.quality_gate_status,
            "not_evaluable_untrusted_confirmation",
        )
        with patch.object(
            heading_gold,
            "TRUSTED_CONFIRMED_HEADING_RELATION_GOLD_SHA256S",
            frozenset({fixture.canonical_sha256}),
        ):
            self.assertTrue(fixture.has_trusted_confirmation)
            self.assertEqual(
                fixture.quality_gate_status,
                "not_evaluable_input_replay_required",
            )

    def test_schema_runtime_parity_rejects_unknown_fields_and_bad_metadata(self) -> None:
        mutations: list[dict[str, object]] = []
        bad = deepcopy(self.payload)
        bad["unknown"] = True
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["source"]["candidate_leaf_id"] = "leaf-" + "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["expected_relations"][0]["kind"] = "heading_to_body"  # type: ignore[index]
        mutations.append(bad)
        bad = self.pending_payload()
        bad["reviewed_scopes"][0]["reviewer_ref"] = "reviewer:forbidden"  # type: ignore[index]
        mutations.append(bad)
        for payload in mutations:
            self.assert_schema_and_runtime_reject(payload)

    def test_base_binding_and_relation_references_fail_closed(self) -> None:
        mutations: list[dict[str, object]] = []
        bad = deepcopy(self.payload)
        bad["source"]["source_pdf_sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["source"]["native_capture"]["canonical_sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["source"]["canonical_page_renders"][0]["canonical_render_sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["source"]["primary_structure_gold"]["canonical_sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["base_structure_scope_id"] = "missing.scope"  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["expected_relations"][0]["heading_occurrence_id"] = "occ:inspector:p3:t18"  # type: ignore[index]
        mutations.append(bad)
        bad = deepcopy(self.payload)
        bad["reviewed_scopes"][0]["expected_relations"][0]["body_group_id"] = "missing.group"  # type: ignore[index]
        mutations.append(bad)
        for payload in mutations:
            with self.assertRaises(PrimaryHeadingRelationGoldError):
                self.parsed(payload)

    def test_relation_requires_matching_base_forbidden_same_leaf(self) -> None:
        base = deepcopy(self.base_payload)
        scope = base["reviewed_scopes"][0]  # type: ignore[index]
        scope["occurrence_ids"].append("occ:inspector:p3:t21")
        scope["ordered_groups"].append(
            {
                "group_id": "p3.secondary.paragraph",
                "kind": "paragraph",
                "occurrence_ids": ["occ:inspector:p3:t21"],
                "boundaries": [],
            }
        )
        scope["hard_negatives"][0]["target_group_id"] = "p3.secondary.paragraph"
        base_fixture = PrimaryStructureGoldFixture.from_dict(base)
        payload = deepcopy(self.payload)
        payload["source"]["primary_structure_gold"]["canonical_sha256"] = (  # type: ignore[index]
            base_fixture.canonical_sha256
        )
        with self.assertRaisesRegex(
            PrimaryHeadingRelationGoldError,
            "requires a forbidden_same_leaf",
        ):
            self.parsed(payload, base=base_fixture)

    def test_reader_rejects_duplicate_nonfinite_surrogate_and_noncanonical(self) -> None:
        duplicate = self.raw.replace(
            b'{"evaluation_only":true,',
            b'{"evaluation_only":true,"evaluation_only":true,',
            1,
        )
        with self.assertRaisesRegex(PrimaryHeadingRelationGoldError, "duplicate"):
            parse_primary_heading_relation_gold_bytes(
                duplicate, primary_structure_gold=self.base_fixture
            )
        nonfinite = self.raw.replace(b'"physical_page":3', b'"physical_page":NaN', 1)
        with self.assertRaisesRegex(PrimaryHeadingRelationGoldError, "non-finite"):
            parse_primary_heading_relation_gold_bytes(
                nonfinite, primary_structure_gold=self.base_fixture
            )
        surrogate = self.raw.replace(
            b'"notice_id":"PBLN_000000000114788"',
            b'"notice_id":"\\ud800"',
            1,
        )
        with self.assertRaises(PrimaryHeadingRelationGoldError):
            parse_primary_heading_relation_gold_bytes(
                surrogate, primary_structure_gold=self.base_fixture
            )
        with self.assertRaisesRegex(PrimaryHeadingRelationGoldError, "BOM"):
            parse_primary_heading_relation_gold_bytes(
                b"\xef\xbb\xbf" + self.raw,
                primary_structure_gold=self.base_fixture,
            )
        with self.assertRaisesRegex(PrimaryHeadingRelationGoldError, "not canonical"):
            parse_primary_heading_relation_gold_bytes(
                self.raw + b"\n", primary_structure_gold=self.base_fixture
            )
        with self.assertRaisesRegex(PrimaryHeadingRelationGoldError, "valid UTF-8"):
            parse_primary_heading_relation_gold_bytes(
                b'{"oversized_integer":' + b"1" * 5_000 + b"}",
                primary_structure_gold=self.base_fixture,
            )

    def test_canonical_json_is_deterministic_and_immutable(self) -> None:
        fixture = self.parsed()
        reordered = {key: self.payload[key] for key in reversed(list(self.payload))}
        self.assertEqual(
            canonical_primary_heading_relation_gold_json(
                reordered, primary_structure_gold=self.base_fixture
            ),
            fixture.canonical_json(),
        )
        self.assertEqual(fixture.canonical_sha256, sha256(self.raw).hexdigest())
        with self.assertRaises(TypeError):
            fixture.payload["notice_id"] = "changed"  # type: ignore[index]

    def test_file_reader_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "gold.json"
            link.symlink_to(FIXTURE)
            with self.assertRaisesRegex(
                PrimaryHeadingRelationGoldError, "regular non-symlink"
            ):
                load_primary_heading_relation_gold_file(
                    link, primary_structure_gold=self.base_fixture
                )

    def test_complete_replay_replays_base_source_native_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, base, source_pdf, render, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            replayed = validate_primary_heading_relation_gold_against_inputs(
                payload,
                primary_structure_gold=base,
                source_pdf=source_pdf,
                native_capture=capture,
                render_manifest=manifest,
                render_artifact_root=root,
            )
            self.assertTrue(replayed.is_replay_receipt)
            self.assertFalse(replayed.has_trusted_confirmation)
            self.assertEqual(
                replayed.fixture.quality_gate_status,
                "not_evaluable_gold_pending",
            )

            render.write_bytes(b"not a PNG")
            with self.assertRaisesRegex(
                PrimaryHeadingRelationGoldError,
                "base Gold did not pass complete source replay",
            ):
                validate_primary_heading_relation_gold_against_inputs(
                    payload,
                    primary_structure_gold=base,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )

    def test_actual_114788_gold_replay_when_artifact_environment_is_configured(
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
            self.skipTest("actual 114788 Gold artifact environment is not configured")

        def mapping(name: str) -> dict:
            return json.loads(
                Path(configured[name]).read_text(encoding="utf-8")
            )

        replay_arguments = {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
        }
        replayed_base = validate_primary_structure_gold_against_inputs(
            self.base_fixture, **replay_arguments
        )
        self.assertTrue(replayed_base.is_replay_receipt)
        self.assertTrue(replayed_base.has_trusted_confirmation)

        replayed_heading = validate_primary_heading_relation_gold_against_inputs(
            self.payload,
            primary_structure_gold=replayed_base.fixture,
            **replay_arguments,
        )
        self.assertTrue(replayed_heading.is_replay_receipt)
        self.assertTrue(replayed_heading.has_trusted_confirmation)
        self.assertEqual(replayed_heading.canonical_sha256, CONFIRMED_FIXTURE_SHA256)

    def test_confirmed_allowlisted_gold_is_trusted_only_after_full_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, base, source_pdf, _, capture, manifest = (
                self.synthetic_replay_inputs(root)
            )
            base_payload = base.to_dict()
            base_scope = base_payload["reviewed_scopes"][0]
            base_scope["review_status"] = "human_confirmed"
            base_scope["reviewer_ref"] = "reviewer:review-001"
            base_scope["confirmed_at"] = "2026-09-19T09:00:00Z"
            confirmed_base = PrimaryStructureGoldFixture.from_dict(base_payload)

            scope = payload["reviewed_scopes"][0]  # type: ignore[index]
            scope["review_status"] = "human_confirmed"
            scope["reviewer_ref"] = "reviewer:review-001"
            scope["confirmed_at"] = "2026-09-19T09:00:00Z"
            payload["source"]["primary_structure_gold"]["canonical_sha256"] = (  # type: ignore[index]
                confirmed_base.canonical_sha256
            )
            fixture = PrimaryHeadingRelationGoldFixture.from_dict(
                payload, primary_structure_gold=confirmed_base
            )
            with (
                patch.object(
                    structure_gold,
                    "TRUSTED_CONFIRMED_GOLD_SHA256S",
                    frozenset({confirmed_base.canonical_sha256}),
                ),
                patch.object(
                    heading_gold,
                    "TRUSTED_CONFIRMED_HEADING_RELATION_GOLD_SHA256S",
                    frozenset({fixture.canonical_sha256}),
                ),
            ):
                self.assertTrue(fixture.has_trusted_confirmation)
                self.assertFalse(fixture.is_evaluable)
                replayed = validate_primary_heading_relation_gold_against_inputs(
                    fixture,
                    primary_structure_gold=confirmed_base,
                    source_pdf=source_pdf,
                    native_capture=capture,
                    render_manifest=manifest,
                    render_artifact_root=root,
                )
                self.assertTrue(replayed.has_trusted_confirmation)

    def test_receipt_rejects_direct_construction(self) -> None:
        fixture = self.parsed(self.pending_payload())
        replayed_type = heading_gold.ReplayedPrimaryHeadingRelationGold
        with self.assertRaises(TypeError):
            replayed_type(fixture)
        with self.assertRaises(PrimaryHeadingRelationGoldError):
            replayed_type(
                fixture,
                _construction_token=object(),
                replayed_canonical_sha256=fixture.canonical_sha256,
            )

    def test_base_canonical_reference_matches_independent_serializer(self) -> None:
        self.assertEqual(
            self.payload["source"]["primary_structure_gold"]["canonical_sha256"],  # type: ignore[index]
            sha256(
                canonical_primary_structure_gold_json(self.base_payload)
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
