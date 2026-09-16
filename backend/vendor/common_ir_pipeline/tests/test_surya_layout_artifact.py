from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.coordinate_manifest import AffineTransform, PdfCoordinateManifest, build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import PdfRenderManifest, RenderedPage
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    MAX_REGIONS_PER_PAGE,
    REGION_LABELS_V1,
    SuryaLayoutArtifact,
    SuryaLayoutArtifactError,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity,
    parse_surya_layout_artifact_bytes,
    validate_surya_layout_artifact,
)
import common_ir_pipeline.pdf_fusion.surya_layout_artifact as contract


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def coordinate(page: int, image_hash: str, *, page_count: int = 2) -> PdfCoordinateManifest:
    forward = AffineTransform.from_sequence([2, 0, 0, -2, 0, 400])
    return PdfCoordinateManifest(
        source_sha256=digest("source"), page=page, page_count=page_count,
        media_box=(0, 0, 100, 200), crop_box=(0, 0, 100, 200), rotation=0, user_unit=1.0,
        canonical_width_pt=100.0, canonical_height_pt=200.0, render_scale_px_per_point=2.0,
        rendered_width_px=200, rendered_height_px=400,
        pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
        pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
        user_to_pixel=forward, pixel_to_user=forward.inverse(), renderer="pdfium",
        renderer_version="153.0", renderer_config_sha256=digest("render-config"), page_image_sha256=image_hash,
    )


def trusted_render_manifest() -> PdfRenderManifest:
    pages = []
    for page in (1, 2):
        image_hash = digest(f"page-{page}")
        manifest = coordinate(page, image_hash)
        pages.append(RenderedPage(
            page=page, image_relative_path=f"rendered/page-{page}.png", image_sha256=image_hash,
            image_size_bytes=100 + page, coordinate_manifest=manifest,
            coordinate_manifest_sha256=manifest.manifest_sha256(),
        ))
    return PdfRenderManifest(
        source_pdf_relative_path="source/notice.pdf", source_pdf_sha256=digest("source"),
        source_pdf_size_bytes=99, page_count=2, renderer="pdfium", renderer_version="153.0",
        renderer_config_sha256=digest("render-config"), pages=tuple(pages),
    )


def producer() -> SuryaProducerIdentity:
    return SuryaProducerIdentity(
        engine_id="surya", engine_version="0.15.0", model_id="surya-layout", model_revision="r1",
        model_weights_sha256=digest("model"), pipeline_revision="surya-layout-v1", config_sha256=digest("config"),
        worker_image_digest="sha256:" + digest("image"),
    )


def region(page: int, reading_order: int, label: str, bbox: tuple[float, float, float, float], confidence: float | None) -> SuryaLayoutRegion:
    x0, y0, x1, y1 = bbox
    template = SuryaLayoutRegion(
        region_id="pending", label=label, bbox_px=bbox,
        polygon_px=((x0, y0), (x1, y0), (x1, y1), (x0, y1)), confidence=confidence, reading_order=reading_order,
    )
    return SuryaLayoutRegion(
        region_id=f"p{page:04d}-{label}-{reading_order + 1:04d}", label=label, bbox_px=bbox,
        polygon_px=template.polygon_px, confidence=confidence, reading_order=reading_order,
    )


def page_entry(render_manifest: PdfRenderManifest, page: int, regions: tuple[SuryaLayoutRegion, ...]) -> SuryaLayoutPage:
    coordinate_manifest = render_manifest.pages[page - 1].coordinate_manifest
    return SuryaLayoutPage(
        page=page, sidecar_binding=build_sidecar_binding(coordinate_manifest), pixel_width=200,
        pixel_height=400, rendered_page_px=(0, 0, 200, 400), regions=regions,
    )


def artifact(render_manifest: PdfRenderManifest | None = None) -> SuryaLayoutArtifact:
    render_manifest = render_manifest or trusted_render_manifest()
    return SuryaLayoutArtifact(
        logical_compute_key=digest("logical-key"), source_sha256=render_manifest.source_pdf_sha256,
        page_count=render_manifest.page_count, render_manifest_schema_version=render_manifest.schema_version,
        render_manifest_sha256=render_manifest.manifest_sha256(), producer=producer(), requested_pages=(1, 2),
        pages=(
            page_entry(render_manifest, 1, (region(1, 0, "table", (20, 20, 100, 80), 0.9),)),
            page_entry(render_manifest, 2, (region(2, 0, "text", (10, 30, 80, 130), None),)),
        ),
    )


class SuryaLayoutArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.render_manifest = trusted_render_manifest()
        self.artifact = artifact(self.render_manifest)

    def test_accepts_canonical_textless_artifact_and_exact_local_bindings(self) -> None:
        raw = self.artifact.canonical_json()
        parsed = parse_surya_layout_artifact_bytes(
            raw, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key,
            expected_producer=self.artifact.producer, expected_requested_pages=(1, 2),
        )
        self.assertEqual(parsed, self.artifact)
        rendered = parsed.to_dict()
        self.assertNotIn("ocr", json.dumps(rendered).lower())
        self.assertNotIn("html", json.dumps(rendered).lower())
        self.assertNotIn("recognized_text", json.dumps(rendered).lower())

    def test_packaged_schema_accepts_contract(self) -> None:
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath(
            "schemas/surya_layout_artifact_v1.schema.json"
        ).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.artifact.to_dict())

    def test_rejects_noncanonical_bytes_duplicate_keys_nan_and_unknown_keys(self) -> None:
        canonical = self.artifact.canonical_json()
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "canonical JSON"):
            parse_surya_layout_artifact_bytes(canonical + b"\n", render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))
        duplicate = b'{"schema_version":"surya_layout_artifact/v1","schema_version":"surya_layout_artifact/v1"}'
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "duplicate JSON key"):
            parse_surya_layout_artifact_bytes(duplicate, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))
        nan = canonical.replace(b'0.9', b'NaN', 1)
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "non-finite"):
            parse_surya_layout_artifact_bytes(nan, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "surrogate code point"):
            parse_surya_layout_artifact_bytes(b'{"x":"\\ud800"}', render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))
        payload = self.artifact.to_dict()
        payload["ocr_text"] = "forbidden"
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "unexpected keys"):
            SuryaLayoutArtifact.from_dict(payload)

    def test_rejects_wrong_manifest_page_binding_identity_and_page_coverage(self) -> None:
        payload = self.artifact.to_dict()
        payload["pages"][0]["sidecar_binding"]["page_image_sha256"] = digest("wrong")
        wrong_binding = SuryaLayoutArtifact.from_dict(payload)
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "sidecar coordinate binding mismatch"):
            validate_surya_layout_artifact(wrong_binding, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))

        payload = self.artifact.to_dict()
        payload["requested_pages"] = [2, 1]
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "strictly ascending"):
            SuryaLayoutArtifact.from_dict(payload)

        payload = self.artifact.to_dict()
        payload["pages"][1]["page"] = 1
        payload["pages"][1]["regions"][0]["region_id"] = "p0001-text-0001"
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "exactly once"):
            SuryaLayoutArtifact.from_dict(payload)

        with self.assertRaisesRegex(SuryaLayoutArtifactError, "logical_compute_key"):
            validate_surya_layout_artifact(self.artifact, render_manifest=self.render_manifest, expected_logical_compute_key=digest("other"), expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "producer identity"):
            validate_surya_layout_artifact(self.artifact, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=SuryaProducerIdentity(**{**producer().to_dict(), "engine_version": "other"}), expected_requested_pages=(1, 2))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "requested_pages"):
            validate_surya_layout_artifact(self.artifact, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1,))

    def test_rejects_noncanonical_region_order_id_label_geometry_and_bounds(self) -> None:
        # Canonical order is explicit reading order, not incidental geometry.
        first = region(1, 0, "table", (20, 20, 100, 80), 0.9)
        second = region(1, 1, "text", (10, 30, 80, 130), None)
        ordered_page = page_entry(self.render_manifest, 1, (first, second))
        self.assertEqual(ordered_page.regions, (first, second))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "canonical v1 region order"):
            page_entry(self.render_manifest, 1, (second, first))

        payload = self.artifact.to_dict()
        payload["pages"][0]["regions"][0]["region_id"] = "anything-goes"
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "deterministic canonical"):
            SuryaLayoutArtifact.from_dict(payload)

        payload = self.artifact.to_dict()
        payload["pages"][0]["regions"][0]["label"] = "formula"
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "pinned v1 vocabulary"):
            SuryaLayoutArtifact.from_dict(payload)

        payload = self.artifact.to_dict()
        payload["pages"][0]["regions"][0]["polygon_px"][2] = [101, 80]
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "envelope"):
            SuryaLayoutArtifact.from_dict(payload)

        payload = self.artifact.to_dict()
        payload["pages"][0]["regions"][0]["bbox_px"] = [-1, 20, 100, 80]
        payload["pages"][0]["regions"][0]["polygon_px"] = [[-1, 20], [100, 20], [100, 80], [-1, 80]]
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "outside rendered_page_px"):
            SuryaLayoutArtifact.from_dict(payload)

    def test_round_trip_uses_trusted_coordinate_transform(self) -> None:
        payload = self.artifact.to_dict()
        payload["pages"][0]["pixel_width"] = 201
        payload["pages"][0]["rendered_page_px"] = [0, 0, 201, 400]
        altered = SuryaLayoutArtifact.from_dict(payload)
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "pixel dimensions"):
            validate_surya_layout_artifact(altered, render_manifest=self.render_manifest, expected_logical_compute_key=self.artifact.logical_compute_key, expected_producer=self.artifact.producer, expected_requested_pages=(1, 2))

    def test_pinned_region_vocabulary_is_not_open_ended(self) -> None:
        self.assertEqual(REGION_LABELS_V1, (
            "caption", "footnote", "equation", "list_group", "page_header", "page_footer", "picture", "section_header",
            "table", "text", "figure", "code", "form", "table_of_contents", "chemical_block", "diagram", "bibliography", "blank_page",
        ))
        self.assertEqual(MAX_REGIONS_PER_PAGE, 200)

    def test_runtime_schema_parity_nullable_confidence_and_worker_digest(self) -> None:
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath(
            "schemas/surya_layout_artifact_v1.schema.json"
        ).read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        payload = self.artifact.to_dict()
        self.assertIsNone(payload["pages"][1]["regions"][0]["confidence"])
        validator.validate(payload)
        for key, value, runtime_error in (
            ("engine_id", "not-surya", "engine_id"),
            ("worker_image_digest", "sha256:not-a-digest", "worker_image_digest"),
            ("model_id", " model ", "model_id"),
            ("model_id", "X\n", "model_id"),
            ("pipeline_revision", "x" * 257, "pipeline_revision"),
        ):
            with self.subTest(key=key):
                bad = self.artifact.to_dict()
                bad["producer"][key] = value
                self.assertTrue(list(validator.iter_errors(bad)))
                with self.assertRaisesRegex(SuryaLayoutArtifactError, runtime_error):
                    SuryaLayoutArtifact.from_dict(bad)

    def test_raw_resource_caps_depth_and_immutable_binding(self) -> None:
        raw = self.artifact.canonical_json()
        kwargs = {
            "render_manifest": self.render_manifest,
            "expected_logical_compute_key": self.artifact.logical_compute_key,
            "expected_producer": self.artifact.producer,
            "expected_requested_pages": (1, 2),
        }
        with patch.object(contract, "MAX_ARTIFACT_BYTES", len(raw) - 1):
            with self.assertRaisesRegex(SuryaLayoutArtifactError, "byte cap"):
                parse_surya_layout_artifact_bytes(raw, **kwargs)
        deep = "[" * 40 + "0" + "]" * 40
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "nesting depth"):
            parse_surya_layout_artifact_bytes(deep.encode(), **kwargs)
        oversized_string = json.dumps({"x": "a" * (contract.MAX_STRING_BYTES + 1)}, separators=(",", ":")).encode()
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "string exceeds"):
            parse_surya_layout_artifact_bytes(oversized_string, **kwargs)
        too_many_pages = json.dumps({"requested_pages": list(range(1, contract.MAX_REQUESTED_PAGES + 2))}, separators=(",", ":")).encode()
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "page cap"):
            parse_surya_layout_artifact_bytes(too_many_pages, **kwargs)
        too_many_regions = json.dumps({"pages": [{"regions": [{}] * (MAX_REGIONS_PER_PAGE + 1)}]}, separators=(",", ":")).encode()
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "per-page region cap"):
            parse_surya_layout_artifact_bytes(too_many_regions, **kwargs)
        # Preflight is deliberately before json.loads: even this syntactically
        # irrelevant array must stop on containers, not after allocations.
        with patch.object(contract, "MAX_JSON_NODES", 4):
            with self.assertRaisesRegex(SuryaLayoutArtifactError, "node/token cap"):
                parse_surya_layout_artifact_bytes(b"[{},{},{},{}]", **kwargs)
        # Braces and escaped quotes within strings are not structural tokens.
        contract._lexical_json_preflight(r'{"not_structure":"{[\\\"]}"}')
        with self.assertRaises(TypeError):
            self.artifact.pages[0].sidecar_binding["page"] = 99

    def test_polygon_precision_canonicalization_and_reading_order_invariants(self) -> None:
        # This binary-float tail occurs in real Surya sidecars; v1 emits 3dp.
        normalized = region(1, 0, "diagram", (20.0, 30.0, 183.26800000000003, 80.0), None)
        self.assertEqual(normalized.bbox_px[2], 183.268)
        rotated = SuryaLayoutRegion(
            region_id="p0001-diagram-0001", label="diagram", bbox_px=(20, 30, 183.268, 80),
            polygon_px=((183.268, 80), (183.268, 30), (20, 30), (20, 80)), confidence=None, reading_order=0,
        )
        self.assertEqual(normalized.polygon_px, rotated.polygon_px)
        self.assertEqual(normalized.to_dict()["polygon_px"], rotated.to_dict()["polygon_px"])
        duplicate = SuryaLayoutRegion(
            region_id="p0001-table-0002", label="table", bbox_px=(20, 30, 183.268, 80),
            polygon_px=normalized.polygon_px, confidence=0.3, reading_order=1,
        )
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "identical geometry"):
            page_entry(self.render_manifest, 1, (normalized, duplicate))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "reading_order"):
            page_entry(self.render_manifest, 1, (region(1, 1, "table", (20, 20, 30, 30), 0.2),))
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "self-intersect"):
            SuryaLayoutRegion(
                region_id="p0001-table-0001", label="table", bbox_px=(0, 0, 2, 1),
                polygon_px=((0, 0), (2, 0), (1, 0), (1, 1)), confidence=None, reading_order=0,
            )
        with self.assertRaisesRegex(SuryaLayoutArtifactError, "exactly cover"):
            SuryaLayoutPage(
                page=1, sidecar_binding=build_sidecar_binding(self.render_manifest.pages[0].coordinate_manifest),
                pixel_width=200, pixel_height=400, rendered_page_px=(0, 0, 200.0000001, 400), regions=(),
            )

    def test_rotated_crop_userunit_fixture_round_trips_region_geometry(self) -> None:
        image_hash = digest("rotated-image")
        forward = AffineTransform.from_sequence([0, 4, 4, 0, -80, -40])
        coordinate_manifest = PdfCoordinateManifest(
            source_sha256=digest("source"), page=1, page_count=1,
            media_box=(0, 0, 120, 240), crop_box=(10, 20, 110, 220), rotation=90, user_unit=2.0,
            canonical_width_pt=400.0, canonical_height_pt=200.0, render_scale_px_per_point=2.0,
            rendered_width_px=800, rendered_height_px=400,
            pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up", pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
            user_to_pixel=forward, pixel_to_user=forward.inverse(), renderer="pdfium", renderer_version="153.0",
            renderer_config_sha256=digest("render-config"), page_image_sha256=image_hash,
        )
        render_manifest = PdfRenderManifest(
            source_pdf_relative_path="source/notice.pdf", source_pdf_sha256=digest("source"), source_pdf_size_bytes=99, page_count=1,
            renderer="pdfium", renderer_version="153.0", renderer_config_sha256=digest("render-config"),
            pages=(RenderedPage(page=1, image_relative_path="rendered/page-1.png", image_sha256=image_hash, image_size_bytes=101, coordinate_manifest=coordinate_manifest, coordinate_manifest_sha256=coordinate_manifest.manifest_sha256()),),
        )
        entry = SuryaLayoutPage(page=1, sidecar_binding=build_sidecar_binding(coordinate_manifest), pixel_width=800, pixel_height=400, rendered_page_px=(0, 0, 800, 400), regions=(region(1, 0, "table", (100, 100, 300, 200), 0.7),))
        rotated_artifact = SuryaLayoutArtifact(logical_compute_key=digest("rotated-key"), source_sha256=render_manifest.source_pdf_sha256, page_count=1, render_manifest_schema_version=render_manifest.schema_version, render_manifest_sha256=render_manifest.manifest_sha256(), producer=producer(), requested_pages=(1,), pages=(entry,))
        self.assertEqual(validate_surya_layout_artifact(rotated_artifact, render_manifest=render_manifest, expected_logical_compute_key=rotated_artifact.logical_compute_key, expected_producer=rotated_artifact.producer, expected_requested_pages=(1,)), rotated_artifact)
