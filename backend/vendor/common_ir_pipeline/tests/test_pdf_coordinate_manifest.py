from __future__ import annotations

import copy
import hashlib
from importlib.resources import files
import json
import math
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.coordinate_manifest import (
    AffineTransform,
    CoordinateManifestError,
    PdfCoordinateManifest,
    build_sidecar_binding,
    pixel_to_user_bbox,
    user_to_pixel_bbox,
    validate_sidecar_binding,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def transform_for_rotation(rotation: int) -> tuple[list[float], int, int]:
    """A 2 px/user-unit top-left renderer for crop [10, 20, 110, 220]."""
    transforms = {
        0: ([2, 0, 0, -2, -20, 440], 200, 400),
        90: ([0, 2, 2, 0, -40, -20], 400, 200),
        180: ([-2, 0, 0, 2, 220, -40], 200, 400),
        270: ([0, -2, -2, 0, 440, 220], 400, 200),
    }
    return transforms[rotation]


def make_manifest(rotation: int = 0, *, crop_box: tuple[float, float, float, float] = (10, 20, 110, 220)) -> PdfCoordinateManifest:
    matrix, width, height = transform_for_rotation(rotation)
    forward = AffineTransform.from_sequence(matrix, name="user_to_pixel")
    crop_width = crop_box[2] - crop_box[0]
    crop_height = crop_box[3] - crop_box[1]
    canonical_width, canonical_height = (
        (crop_height, crop_width) if rotation in {90, 270} else (crop_width, crop_height)
    )
    return PdfCoordinateManifest(
        source_sha256=digest("source"),
        page=2,
        page_count=4,
        media_box=(0, 0, 120, 240),
        crop_box=crop_box,
        rotation=rotation,
        user_unit=1.0,
        canonical_width_pt=canonical_width,
        canonical_height_pt=canonical_height,
        render_scale_px_per_point=2.0,
        rendered_width_px=width,
        rendered_height_px=height,
        pdf_origin="bottom_left",
        pdf_x_axis="right",
        pdf_y_axis="up",
        pixel_origin="top_left",
        pixel_x_axis="right",
        pixel_y_axis="down",
        user_to_pixel=forward,
        pixel_to_user=forward.inverse(),
        renderer="pymupdf",
        renderer_version="1.26.0",
        renderer_config_sha256=digest("renderer-config"),
        page_image_sha256=digest("page-2.png"),
    )


class PdfCoordinateManifestTests(unittest.TestCase):
    def assert_bbox_close(self, actual, expected) -> None:
        self.assertEqual(len(actual), len(expected))
        for left, right in zip(actual, expected):
            self.assertTrue(math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9), (actual, expected))

    def test_all_right_angle_rotations_round_trip_with_non_media_crop(self) -> None:
        """Crop-origin translation and 0/90/180/270 rotation are explicit."""
        original = (20, 30, 70, 100)
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                manifest = make_manifest(rotation)
                pixel = user_to_pixel_bbox(manifest, original)
                self.assertTrue(0 <= pixel[0] < pixel[2] <= manifest.rendered_width_px)
                self.assertTrue(0 <= pixel[1] < pixel[3] <= manifest.rendered_height_px)
                self.assert_bbox_close(pixel_to_user_bbox(manifest, pixel), original)

    def test_user_unit_above_one_with_clockwise_90_degree_rotation(self) -> None:
        # PDF /Rotate is clockwise.  A producer normalizes it modulo 360
        # before manifest construction; here 90 and UserUnit=2 give a
        # 400x200-point canonical page rendered at 2 px/point.
        forward = AffineTransform.from_sequence([0, 4, 4, 0, -80, -40])
        manifest = PdfCoordinateManifest(
            source_sha256=digest("source"), page=1, page_count=1,
            media_box=(0, 0, 120, 240), crop_box=(10, 20, 110, 220), rotation=90,
            user_unit=2.0, canonical_width_pt=400.0, canonical_height_pt=200.0,
            render_scale_px_per_point=2.0, rendered_width_px=800, rendered_height_px=400,
            pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
            pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
            user_to_pixel=forward, pixel_to_user=forward.inverse(), renderer="pymupdf",
            renderer_version="1.26.0", renderer_config_sha256=digest("renderer-config"),
            page_image_sha256=digest("page-1.png"),
        )
        self.assert_bbox_close(pixel_to_user_bbox(manifest, user_to_pixel_bbox(manifest, (20, 30, 70, 100))), (20, 30, 70, 100))

    def test_canonical_json_and_hash_are_deterministic_across_input_order(self) -> None:
        manifest = make_manifest(90)
        encoded = manifest.canonical_json()
        scrambled = dict(reversed(list(manifest.to_dict().items())))
        rebuilt = PdfCoordinateManifest.from_dict(scrambled)
        self.assertEqual(encoded, rebuilt.canonical_json())
        self.assertEqual(manifest.manifest_sha256(), rebuilt.manifest_sha256())
        self.assertEqual(
            encoded,
            json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        )

    def test_canonical_hash_normalizes_signed_zero(self) -> None:
        payload = make_manifest().to_dict()
        payload["user_to_pixel"][1] = -0.0
        rebuilt = PdfCoordinateManifest.from_dict(payload)
        self.assertEqual(make_manifest().manifest_sha256(), rebuilt.manifest_sha256())

    def test_packaged_cross_language_json_schema_accepts_manifest(self) -> None:
        schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_coordinate_manifest_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(make_manifest().to_dict())

    def test_from_dict_rejects_extra_or_missing_keys_fail_closed(self) -> None:
        payload = make_manifest().to_dict()
        payload["unknown"] = "not ignored"
        with self.assertRaisesRegex(CoordinateManifestError, "unexpected keys"):
            PdfCoordinateManifest.from_dict(payload)
        payload = make_manifest().to_dict()
        del payload["page_image_sha256"]
        with self.assertRaisesRegex(CoordinateManifestError, "missing keys"):
            PdfCoordinateManifest.from_dict(payload)

    def test_sidecar_binding_rejects_tampered_source_page_image_and_manifest_hash(self) -> None:
        manifest = make_manifest()
        binding = build_sidecar_binding(manifest)
        validate_sidecar_binding(binding, manifest)
        for key, replacement in (
            ("source_sha256", digest("other-source")),
            ("page", 3),
            ("page_image_sha256", digest("different-render")),
            ("coordinate_manifest_sha256", digest("manifest-tamper")),
        ):
            with self.subTest(key=key):
                tampered = copy.deepcopy(binding)
                tampered[key] = replacement
                with self.assertRaisesRegex(CoordinateManifestError, key):
                    validate_sidecar_binding(tampered, manifest)

    def test_sidecar_binding_rejects_wrong_manifest_even_if_source_and_page_match(self) -> None:
        manifest = make_manifest()
        alternate = make_manifest(180)
        binding = build_sidecar_binding(manifest)
        with self.assertRaisesRegex(CoordinateManifestError, "coordinate_manifest_sha256"):
            validate_sidecar_binding(binding, alternate)

    def test_sidecar_binding_rejects_missing_and_extra_fields(self) -> None:
        manifest = make_manifest()
        binding = build_sidecar_binding(manifest)
        del binding["page"]
        with self.assertRaisesRegex(CoordinateManifestError, "missing keys"):
            validate_sidecar_binding(binding, manifest)
        binding = build_sidecar_binding(manifest)
        binding["optional_but_unsafe"] = True
        with self.assertRaisesRegex(CoordinateManifestError, "unexpected keys"):
            validate_sidecar_binding(binding, manifest)

    def test_rejects_nan_infinity_singular_matrix_and_bad_rotation(self) -> None:
        payload = make_manifest().to_dict()
        payload["user_unit"] = float("nan")
        with self.assertRaisesRegex(CoordinateManifestError, "finite"):
            PdfCoordinateManifest.from_dict(payload)
        payload = make_manifest().to_dict()
        payload["user_to_pixel"] = [1, 0, 0, 0, 0, 0]
        payload["pixel_to_user"] = [1, 0, 0, 1, 0, 0]
        with self.assertRaisesRegex(CoordinateManifestError, "non-singular"):
            PdfCoordinateManifest.from_dict(payload)
        payload = make_manifest().to_dict()
        payload["rotation"] = 45
        with self.assertRaisesRegex(CoordinateManifestError, "rotation"):
            PdfCoordinateManifest.from_dict(payload)

    def test_page_count_user_unit_scale_and_axis_fields_are_bound(self) -> None:
        payload = make_manifest().to_dict()
        payload["page_count"] = 1
        with self.assertRaisesRegex(CoordinateManifestError, "page_count"):
            PdfCoordinateManifest.from_dict(payload)

        payload = make_manifest().to_dict()
        payload["user_unit"] = 2.0
        with self.assertRaisesRegex(CoordinateManifestError, "canonical page dimensions"):
            PdfCoordinateManifest.from_dict(payload)

        payload = make_manifest().to_dict()
        payload["render_scale_px_per_point"] = 1.0
        with self.assertRaisesRegex(CoordinateManifestError, "rendered dimensions"):
            PdfCoordinateManifest.from_dict(payload)

        payload = make_manifest().to_dict()
        payload["pixel_x_axis"] = "left"
        with self.assertRaisesRegex(CoordinateManifestError, "pixel_x_axis"):
            PdfCoordinateManifest.from_dict(payload)

    def test_rejects_crop_outside_media_and_render_mapping_outside_page(self) -> None:
        payload = make_manifest().to_dict()
        payload["crop_box"] = [-1, 20, 110, 220]
        with self.assertRaisesRegex(CoordinateManifestError, "crop_box must be contained"):
            PdfCoordinateManifest.from_dict(payload)
        payload = make_manifest().to_dict()
        payload["user_to_pixel"] = [2, 0, 0, -2, 1000, 440]
        payload["pixel_to_user"] = AffineTransform.from_sequence(payload["user_to_pixel"]).inverse().as_list()
        with self.assertRaisesRegex(CoordinateManifestError, "outside rendered page bounds"):
            PdfCoordinateManifest.from_dict(payload)

    def test_rejects_transform_that_only_covers_part_of_page(self) -> None:
        payload = make_manifest().to_dict()
        payload["user_to_pixel"] = [1, 0, 0, -1, -10, 220]
        payload["pixel_to_user"] = AffineTransform.from_sequence(payload["user_to_pixel"]).inverse().as_list()
        with self.assertRaisesRegex(CoordinateManifestError, "complete crop_box"):
            PdfCoordinateManifest.from_dict(payload)

    def test_rejects_rotation_metadata_that_disagrees_with_transform(self) -> None:
        payload = make_manifest(0).to_dict()
        payload["rotation"] = 180
        with self.assertRaisesRegex(CoordinateManifestError, "declared rotation"):
            PdfCoordinateManifest.from_dict(payload)

    def test_bbox_conversion_rejects_out_of_bounds_source_or_pixel(self) -> None:
        manifest = make_manifest()
        with self.assertRaisesRegex(CoordinateManifestError, "outside crop_box"):
            user_to_pixel_bbox(manifest, (0, 20, 10, 30))
        with self.assertRaisesRegex(CoordinateManifestError, "outside rendered page"):
            pixel_to_user_bbox(manifest, (0, 0, 201, 10))

    def test_rejects_non_rectilinear_matrix_even_with_valid_inverse(self) -> None:
        # A diagonal rotation is invertible, but an axis-aligned bbox cannot
        # round-trip through it without expansion. PDF page rotation v1 is
        # deliberately restricted to the right-angle manifest contract.
        forward = AffineTransform(1, 1, -1, 1, 200, 0)
        with self.assertRaisesRegex(CoordinateManifestError, "rectilinear"):
            PdfCoordinateManifest(
                source_sha256=digest("source"), page=1,
                page_count=1, media_box=(0, 0, 120, 240), crop_box=(10, 20, 110, 220), rotation=0,
                user_unit=1, canonical_width_pt=100, canonical_height_pt=200,
                render_scale_px_per_point=2, rendered_width_px=200, rendered_height_px=400,
                pdf_origin="bottom_left", pdf_x_axis="right", pdf_y_axis="up",
                pixel_origin="top_left", pixel_x_axis="right", pixel_y_axis="down",
                user_to_pixel=forward, pixel_to_user=forward.inverse(),
                renderer="test", renderer_version="1", renderer_config_sha256=digest("config"), page_image_sha256=digest("image"),
            )


if __name__ == "__main__":
    unittest.main()
