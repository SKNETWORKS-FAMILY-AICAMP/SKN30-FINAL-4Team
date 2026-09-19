from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from common_ir_pipeline.pdf_fusion.render_manifest import validate_render_manifest_files
from common_ir_pipeline.workers.pdfium_renderer import (
    PdfiumRenderError,
    PdfiumRenderLimits,
    RENDER_SCALE_PX_PER_POINT,
    _pillow_runtime_identity,
    render_canonical_pdf,
    renderer_config_sha256,
    resolve_pdf_page_geometries,
)


def _pdf(objects: list[bytes]) -> bytes:
    chunks = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    for index, object_data in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii") + object_data + b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    chunks.extend(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def fixture_pdf(
    *,
    rotation: int,
    user_unit: float = 2,
    media_box: tuple[float, float, float, float] = (-20, -10, 280, 190),
    crop_box: tuple[float, float, float, float] = (10, 20, 250, 170),
    content: bytes = b"",
) -> bytes:
    # Only the page-tree-inheritable attributes intentionally live on /Pages.
    # /UserUnit is a leaf /Page entry under the PDF specification.
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 "
            + f"/MediaBox [{' '.join(str(value) for value in media_box)}] ".encode("ascii")
            + f"/CropBox [{' '.join(str(value) for value in crop_box)}] ".encode("ascii")
            + f"/Rotate {rotation} >>".encode("ascii")
        ),
        (
            b"<< /Type /Page /Parent 2 0 R "
            + f"/UserUnit {user_unit} ".encode("ascii")
            + b"/Resources << >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
    ])


def many_page_pdf(page_count: int) -> bytes:
    page_object_numbers = range(3, page_count + 3)
    return _pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
            + f"] /Count {page_count} /MediaBox [0 0 100 100] >>".encode("ascii")
        ),
        *(b"<< /Type /Page /Parent 2 0 R >>" for _ in range(page_count)),
    ])


CORNER_RECTANGLES = (
    # (user-space centre, RGB) -- each is sufficiently inside a 20x20pt box
    # to avoid antialiased rectangle edges at every page rotation.
    ((30.0, 40.0), (255, 0, 0)),
    ((230.0, 40.0), (0, 255, 0)),
    ((30.0, 150.0), (0, 0, 255)),
    ((230.0, 150.0), (255, 255, 0)),
)


def colored_corner_content() -> bytes:
    return b"\n".join((
        b"q 1 0 0 rg 20 30 20 20 re f Q",
        b"q 0 1 0 rg 220 30 20 20 re f Q",
        b"q 0 0 1 rg 20 140 20 20 re f Q",
        b"q 1 1 0 rg 220 140 20 20 re f Q",
    ))


class PdfiumRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "source").mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_source(self, *, rotation: int = 0, user_unit: int = 2) -> Path:
        source = self.root / "source" / "notice.pdf"
        source.write_bytes(fixture_pdf(rotation=rotation, user_unit=user_unit))
        return source

    def test_inherited_media_crop_rotation_and_direct_user_unit_are_resolved(self) -> None:
        geometry = resolve_pdf_page_geometries(fixture_pdf(rotation=270, user_unit=2))
        self.assertEqual(len(geometry), 1)
        page = geometry[0]
        self.assertEqual(page.media_box, (-20.0, -10.0, 280.0, 190.0))
        self.assertEqual(page.crop_box, (10.0, 20.0, 250.0, 170.0))
        self.assertEqual(page.rotation, 270)
        self.assertEqual(page.user_unit, 2.0)

    def test_parent_user_unit_is_not_inherited(self) -> None:
        source = _pdf([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 100] /UserUnit 9 >>",
            b"<< /Type /Page /Parent 2 0 R >>",
        ])

        geometry = resolve_pdf_page_geometries(source)

        self.assertEqual(geometry[0].user_unit, 1.0)

    def test_decimal_a4_geometry_keeps_raw_manifest_values_and_renders(self) -> None:
        media_box = (0.0, 0.0, 595.28, 842.123456)
        source = self.root / "source" / "decimal-a4.pdf"
        source.write_bytes(fixture_pdf(rotation=0, user_unit=1, media_box=media_box, crop_box=media_box))

        manifest = render_canonical_pdf(artifact_root=self.root, source_pdf_path=source)

        coordinate = manifest.pages[0].coordinate_manifest
        # pypdf is the manifest authority: no float32 roundtrip leaks into the
        # evidence coordinate contract just because PDFium has float32 APIs.
        self.assertEqual(coordinate.media_box, media_box)
        self.assertEqual(coordinate.crop_box, media_box)
        self.assertEqual((coordinate.rendered_width_px, coordinate.rendered_height_px), (1654, 2340))

    def test_ceil_boundary_uses_pdfium_float32_bbox_not_raw_python_subtraction(self) -> None:
        # 360.00001 * (200 / 72) is just over 1000 when calculated from raw
        # Python doubles.  It rounds to exactly 360 in PDFium's float32 page
        # geometry, for which the real bitmap is exactly 1000 pixels wide.
        media_box = (0.0, 0.0, 360.00001, 100.0)
        source = self.root / "source" / "ceil-boundary.pdf"
        source.write_bytes(fixture_pdf(rotation=0, user_unit=1, media_box=media_box, crop_box=media_box))

        manifest = render_canonical_pdf(artifact_root=self.root, source_pdf_path=source)

        coordinate = manifest.pages[0].coordinate_manifest
        self.assertEqual(coordinate.rendered_width_px, 1000)
        self.assertEqual(coordinate.rendered_height_px, 278)
        self.assertEqual(coordinate.crop_box, media_box)

    def test_page_tree_limit_is_applied_before_pages_materialize(self) -> None:
        source = _pdf([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
            b"<< /Type /Page /Parent 2 0 R >>",
            b"<< /Type /Page /Parent 2 0 R >>",
        ])
        with self.assertRaisesRegex(PdfiumRenderError, "page count exceeds"):
            resolve_pdf_page_geometries(source, max_pages=1)

    def test_page_tree_internal_nodes_do_not_reduce_valid_leaf_page_limit(self) -> None:
        # pypdf counts internal /Pages nodes toward its parser entry limit. A
        # document with exactly the renderer's 256 leaves must still be valid.
        geometry = resolve_pdf_page_geometries(many_page_pdf(256), max_pages=256)
        self.assertEqual(len(geometry), 256)

    def test_all_rotations_render_full_crop_in_rgb_and_bind_affine(self) -> None:
        expected_dimensions = {
            0: (1334, 834),
            90: (834, 1334),
            180: (1334, 834),
            270: (834, 1334),
        }
        for rotation, expected in expected_dimensions.items():
            with self.subTest(rotation=rotation):
                root = self.root / f"r{rotation}"
                (root / "source").mkdir(parents=True)
                (root / "source" / "notice.pdf").write_bytes(fixture_pdf(rotation=rotation))
                manifest = render_canonical_pdf(artifact_root=root, source_pdf_path="source/notice.pdf")
                coordinate = manifest.pages[0].coordinate_manifest
                self.assertEqual((coordinate.rendered_width_px, coordinate.rendered_height_px), expected)
                self.assertEqual(coordinate.render_scale_px_per_point, RENDER_SCALE_PX_PER_POINT)
                self.assertEqual(coordinate.user_unit, 2.0)
                png = (root / manifest.pages[0].image_relative_path).read_bytes()
                self.assertEqual(png[12:16], b"IHDR")
                self.assertEqual(png[24], 8)   # bit depth
                self.assertEqual(png[25], 2)   # RGB, not RGBA/palette/gray
                self.assertEqual(png[28], 0)   # non-interlaced
                self.assertEqual(
                    coordinate.page_image_sha256,
                    hashlib.sha256(png).hexdigest(),
                )
                self.assertEqual(manifest, validate_render_manifest_files(manifest, artifact_root=root))

    def test_colored_crop_corners_follow_manifest_affine_for_all_rotations(self) -> None:
        # Exercise actual painted pixels, not only PDFium's coordinate helper:
        # the crop origin is non-zero and each corner has a distinct RGB mark.
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                root = self.root / f"corner-r{rotation}"
                (root / "source").mkdir(parents=True)
                source = root / "source" / "notice.pdf"
                source.write_bytes(fixture_pdf(rotation=rotation, content=colored_corner_content()))
                manifest = render_canonical_pdf(artifact_root=root, source_pdf_path=source)
                coordinate = manifest.pages[0].coordinate_manifest
                png = (root / manifest.pages[0].image_relative_path).read_bytes()
                with Image.open(BytesIO(png)) as image:
                    image.load()
                    self.assertEqual(image.mode, "RGB")
                    for (user_x, user_y), expected_rgb in CORNER_RECTANGLES:
                        pixel_x, pixel_y = coordinate.user_to_pixel.apply_point(user_x, user_y)
                        center_x, center_y = round(pixel_x), round(pixel_y)
                        # Centre samples are at least ~25 pixels from every
                        # painted edge, so they are unaffected by PDF anti-
                        # aliasing and ceil-rounding at the image boundary.
                        for delta_x, delta_y in ((0, 0), (-2, 0), (2, 0), (0, -2), (0, 2)):
                            self.assertEqual(
                                image.getpixel((center_x + delta_x, center_y + delta_y)),
                                expected_rgb,
                                msg=(rotation, user_x, user_y, delta_x, delta_y),
                            )

    def test_repeated_private_workspaces_are_byte_deterministic_and_same_workspace_refuses_overwrite(self) -> None:
        source = self.write_source(rotation=90)
        first = render_canonical_pdf(artifact_root=self.root, source_pdf_path=source)
        with self.assertRaisesRegex(PdfiumRenderError, "already exists"):
            render_canonical_pdf(artifact_root=self.root, source_pdf_path=source)

        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other))
        (other / "source").mkdir()
        (other / "source" / "notice.pdf").write_bytes(source.read_bytes())
        second = render_canonical_pdf(artifact_root=other, source_pdf_path="source/notice.pdf")
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(
            (self.root / first.pages[0].image_relative_path).read_bytes(),
            (other / second.pages[0].image_relative_path).read_bytes(),
        )
        self.assertEqual(
            (self.root / "render_manifest.json").read_bytes(),
            (other / "render_manifest.json").read_bytes(),
        )

    def test_pixel_cap_fails_before_output_directory_or_manifest_exists(self) -> None:
        source = self.write_source(rotation=0)
        limits = PdfiumRenderLimits(max_png_total_pixels=1)
        with self.assertRaisesRegex(PdfiumRenderError, "pixel count"):
            render_canonical_pdf(artifact_root=self.root, source_pdf_path=source, limits=limits)
        self.assertFalse((self.root / "rendered").exists())
        self.assertFalse((self.root / "render_manifest.json").exists())

    def test_document_pixel_cap_fails_before_output_directory_or_manifest_exists(self) -> None:
        source = self.write_source(rotation=0)
        limits = PdfiumRenderLimits(max_document_total_pixels=1)
        with self.assertRaisesRegex(PdfiumRenderError, "document pixel count"):
            render_canonical_pdf(artifact_root=self.root, source_pdf_path=source, limits=limits)
        self.assertFalse((self.root / "rendered").exists())
        self.assertFalse((self.root / "render_manifest.json").exists())

    def test_manifest_is_canonical_json(self) -> None:
        source = self.write_source()
        manifest = render_canonical_pdf(artifact_root=self.root, source_pdf_path=source)
        stored = (self.root / "render_manifest.json").read_bytes()
        self.assertEqual(stored, manifest.canonical_json())
        self.assertEqual(json.loads(stored), manifest.to_dict())

    def test_pillow_version_mismatch_fails_closed(self) -> None:
        import PIL
        with patch.object(PIL, "__version__", "0.0.0"):
            with self.assertRaisesRegex(PdfiumRenderError, "Pillow==12.3.0"):
                _pillow_runtime_identity()

    def test_renderer_config_hash_binds_runtime_identity(self) -> None:
        # The public manifest exposes a hash rather than host paths. Calling
        # it twice verifies runtime identity collection is stable per host.
        first = renderer_config_sha256()
        second = renderer_config_sha256()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, second)
