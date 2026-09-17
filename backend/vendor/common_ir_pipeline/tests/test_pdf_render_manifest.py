from __future__ import annotations

import copy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from common_ir_pipeline.pdf_fusion.coordinate_manifest import AffineTransform, PdfCoordinateManifest
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    PdfRenderManifestError,
    RenderedPageInput,
    assemble_render_manifest,
    inspect_png_bytes,
    validate_render_manifest_files,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_bytes(payload: bytes, *, width: int = 200, height: int = 400) -> bytes:
    """A valid RGB PNG; payload only differentiates otherwise-valid fixtures."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # One filtered RGB scanline per row.  A zero-width payload marker cannot
    # live outside chunks, so vary the ancillary text chunk instead.
    pixels = b"\x00" + (b"\x00\x00\x00" * width)
    image_data = zlib.compress(pixels * height)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"tEXt", b"fixture\x00" + payload) + _chunk(b"IDAT", image_data) + _chunk(b"IEND", b"")


def png_with_idat(
    image_data: bytes,
    *,
    width: int = 200,
    height: int = 400,
    bit_depth: int = 8,
    color_type: int = 2,
    interlace: int = 0,
) -> bytes:
    """Build a deliberately minimal PNG for validator rejection fixtures."""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", image_data) + _chunk(b"IEND", b"")


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def coordinate_manifest(
    *,
    source_sha256: str,
    page: int,
    image_sha256: str,
    page_count: int = 2,
    renderer: str = "pymupdf",
) -> PdfCoordinateManifest:
    # The crop is 100 x 200 PDF user units, rendered 2 px/unit with a
    # top-left pixel origin.  This gives every test a valid full-page mapping.
    forward = AffineTransform.from_sequence([2, 0, 0, -2, 0, 400])
    return PdfCoordinateManifest(
        source_sha256=source_sha256,
        page=page,
        page_count=page_count,
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
        renderer=renderer,
        renderer_version="1.26.0",
        renderer_config_sha256=digest("render-config"),
        page_image_sha256=image_sha256,
    )


class PdfRenderManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "source").mkdir()
        (self.root / "rendered").mkdir()
        self.source = self.root / "source" / "notice.pdf"
        self.source.write_bytes(b"%PDF-1.7\nexample source bytes\n")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_page(
        self,
        page: int,
        payload: bytes | None = None,
        *,
        page_count: int | None = None,
    ) -> tuple[Path, PdfCoordinateManifest]:
        image = self.root / "rendered" / f"page-{page}.png"
        image.write_bytes(png_bytes(payload or f"page-{page}".encode("utf-8")))
        return image, coordinate_manifest(
            source_sha256=digest(self.source.read_bytes()),
            page=page,
            page_count=page_count or max(2, page),
            image_sha256=digest(image.read_bytes()),
        )

    def build_two_page_manifest(self) -> PdfRenderManifest:
        page_one_path, page_one_coordinate = self.make_page(1)
        page_two_path, page_two_coordinate = self.make_page(2)
        return assemble_render_manifest(
            artifact_root=self.root,
            source_pdf_path=self.source,
            # Deliberately reverse input traversal order: output must stay
            # canonical by page number.
            pages=(
                RenderedPageInput(page_two_path, page_two_coordinate),
                RenderedPageInput(page_one_path, page_one_coordinate),
            ),
        )

    def test_inspect_png_bytes_matches_file_path_validation(self) -> None:
        """Remote bytes and the established local file boundary agree exactly."""
        page_path, _ = self.make_page(1, page_count=1)
        image_bytes = page_path.read_bytes()
        self.assertEqual(
            (digest(image_bytes), len(image_bytes), 200, 400),
            inspect_png_bytes(image_bytes),
        )
        # The existing file-path route remains the public manifest regression.
        manifest = assemble_render_manifest(
            artifact_root=self.root,
            source_pdf_path=self.source,
            pages=(
                RenderedPageInput(
                    page_path,
                    coordinate_manifest(
                        source_sha256=digest(self.source.read_bytes()),
                        page=1,
                        page_count=1,
                        image_sha256=digest(image_bytes),
                    ),
                ),
            ),
        )
        self.assertEqual(manifest, validate_render_manifest_files(manifest, artifact_root=self.root))

    def test_inspect_png_bytes_rejects_remote_tamper_oversize_trailing_and_corruption(self) -> None:
        valid = png_bytes(b"remote-page")
        tampered = valid[:-1] + bytes([valid[-1] ^ 0x01])
        cases: tuple[tuple[str, bytes, str], ...] = (
            ("tampered-crc", tampered, "checksum"),
            ("trailing", valid + b"unexpected", "trailing bytes"),
            ("corrupt-signature", b"not png", "PNG signature"),
        )
        for name, image_bytes, expected_error in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(PdfRenderManifestError, expected_error):
                    inspect_png_bytes(image_bytes)

        with patch(
            "common_ir_pipeline.pdf_fusion.render_manifest.MAX_PNG_FILE_BYTES",
            len(valid) - 1,
        ):
            with self.assertRaisesRegex(PdfRenderManifestError, "compressed-file safety cap"):
                inspect_png_bytes(valid)

    def test_inspect_png_bytes_enforces_chunk_count_safety_cap(self) -> None:
        """Empty ancillary chunks cannot amplify PNG validation CPU work."""
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        image_data = zlib.compress(b"\x00\x00\x00\x00")
        image_bytes = (
            PNG_SIGNATURE
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"tEXt", b"")
            + _chunk(b"tEXt", b"")
            + _chunk(b"IDAT", image_data)
            + _chunk(b"IEND", b"")
        )
        with patch("common_ir_pipeline.pdf_fusion.render_manifest.MAX_PNG_CHUNK_COUNT", 5):
            self.assertEqual((digest(image_bytes), len(image_bytes), 1, 1), inspect_png_bytes(image_bytes))
        with patch("common_ir_pipeline.pdf_fusion.render_manifest.MAX_PNG_CHUNK_COUNT", 4):
            with self.assertRaisesRegex(PdfRenderManifestError, "chunk count exceeds safety cap"):
                inspect_png_bytes(image_bytes)

    def test_assembles_canonical_deterministic_document_manifest_and_validates_bytes(self) -> None:
        manifest = self.build_two_page_manifest()
        self.assertEqual([page.page for page in manifest.pages], [1, 2])
        self.assertEqual(manifest.source_pdf_relative_path, "source/notice.pdf")
        self.assertEqual([page.image_relative_path for page in manifest.pages], ["rendered/page-1.png", "rendered/page-2.png"])
        self.assertEqual(manifest, validate_render_manifest_files(manifest.to_dict(), artifact_root=self.root))

        scrambled = dict(reversed(list(manifest.to_dict().items())))
        rebuilt = PdfRenderManifest.from_dict(scrambled)
        self.assertEqual(manifest.canonical_json(), rebuilt.canonical_json())
        self.assertEqual(manifest.manifest_sha256(), rebuilt.manifest_sha256())
        self.assertEqual(
            manifest.canonical_json(),
            json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
        )

    def test_packaged_cross_language_json_schemas_accept_manifest(self) -> None:
        coordinate_schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_coordinate_manifest_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        render_schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath("schemas/pdf_render_manifest_v1.schema.json")
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(coordinate_schema)
        Draft202012Validator.check_schema(render_schema)
        registry = Registry().with_resource(
            coordinate_schema["$id"],
            Resource.from_contents(coordinate_schema),
        )
        Draft202012Validator(render_schema, registry=registry).validate(
            self.build_two_page_manifest().to_dict()
        )

    def test_rejects_unknown_missing_duplicate_gapped_and_cross_renderer_pages(self) -> None:
        manifest = self.build_two_page_manifest()
        payload = manifest.to_dict()
        payload["unknown"] = True
        with self.assertRaisesRegex(PdfRenderManifestError, "unexpected keys"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        del payload["source_pdf_size_bytes"]
        with self.assertRaisesRegex(PdfRenderManifestError, "missing keys"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["pages"][1]["page"] = 1
        payload["pages"][1]["coordinate_manifest"]["page"] = 1
        payload["pages"][1]["coordinate_manifest_sha256"] = PdfCoordinateManifest.from_dict(
            payload["pages"][1]["coordinate_manifest"]
        ).manifest_sha256()
        with self.assertRaisesRegex(PdfRenderManifestError, "contiguous"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["page_count"] = 3
        payload["pages"][0]["coordinate_manifest"]["page_count"] = 3
        payload["pages"][0]["coordinate_manifest_sha256"] = PdfCoordinateManifest.from_dict(
            payload["pages"][0]["coordinate_manifest"]
        ).manifest_sha256()
        payload["pages"][1]["page"] = 3
        payload["pages"][1]["coordinate_manifest"]["page"] = 3
        payload["pages"][1]["coordinate_manifest"]["page_count"] = 3
        payload["pages"][1]["coordinate_manifest_sha256"] = PdfCoordinateManifest.from_dict(
            payload["pages"][1]["coordinate_manifest"]
        ).manifest_sha256()
        with self.assertRaisesRegex(PdfRenderManifestError, "contiguous"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["pages"][1]["coordinate_manifest"]["renderer"] = "other-renderer"
        payload["pages"][1]["coordinate_manifest_sha256"] = digest(
            json.dumps(payload["pages"][1]["coordinate_manifest"], sort_keys=True, separators=(",", ":"))
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "renderer"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["pages"][1]["image_relative_path"] = payload["pages"][0]["image_relative_path"]
        with self.assertRaisesRegex(PdfRenderManifestError, "reuse an image_relative_path"):
            PdfRenderManifest.from_dict(payload)

    def test_rejects_duplicate_input_path_and_malformed_png_chunks(self) -> None:
        page_path, coordinate = self.make_page(1, page_count=1)
        with self.assertRaisesRegex(PdfRenderManifestError, "reuse a page image path"):
            assemble_render_manifest(
                artifact_root=self.root,
                source_pdf_path=self.source,
                pages=(RenderedPageInput(page_path, coordinate), RenderedPageInput(page_path, coordinate)),
            )

        cases = {
            "missing-idat": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 200, 400, 8, 2, 0, 0, 0)) + _chunk(b"IEND", b""),
            "unknown-critical": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 200, 400, 8, 2, 0, 0, 0)) + _chunk(b"ABCD", b"") + _chunk(b"IDAT", zlib.compress(b"\x00" + b"\0" * 600)) + _chunk(b"IEND", b""),
            "invalid-chunk-type": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 200, 400, 8, 2, 0, 0, 0)) + _chunk(b"a1cd", b"") + _chunk(b"IDAT", b"x") + _chunk(b"IEND", b""),
            "invalid-ihdr-fields": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 200, 400, 1, 2, 0, 0, 0)) + _chunk(b"IDAT", b"x") + _chunk(b"IEND", b""),
            "dimension-cap": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 20_001, 1, 8, 2, 0, 0, 0)) + _chunk(b"IDAT", b"x") + _chunk(b"IEND", b""),
            "split-idat": PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 200, 400, 8, 2, 0, 0, 0)) + _chunk(b"IDAT", b"x") + _chunk(b"tEXt", b"k\x00v") + _chunk(b"IDAT", b"y") + _chunk(b"IEND", b""),
            "trailing": png_bytes(b"x") + b"x",
            "bad-crc": png_bytes(b"x")[:-1] + b"x",
        }
        for name, image_bytes in cases.items():
            with self.subTest(name=name):
                page_path.write_bytes(image_bytes)
                bad_coordinate = coordinate_manifest(
                    source_sha256=digest(self.source.read_bytes()), page=1, page_count=1, image_sha256=digest(image_bytes)
                )
                with self.assertRaises(PdfRenderManifestError):
                    assemble_render_manifest(
                        artifact_root=self.root, source_pdf_path=self.source,
                        pages=(RenderedPageInput(page_path, bad_coordinate),),
                    )

    def test_rejects_png_formats_and_malformed_or_excessive_idat_output(self) -> None:
        """The renderer boundary accepts only bounded RGB/RGBA scanlines."""
        page_path, coordinate = self.make_page(1, page_count=1)
        valid_one_pixel_rgb = b"\x00\x01\x02\x03"
        valid_stream = zlib.compress(valid_one_pixel_rgb)
        cases: dict[str, tuple[bytes, str]] = {
            "palette": (
                png_with_idat(valid_stream, width=1, height=1, color_type=3),
                "non-interlaced 8-bit RGB or RGBA",
            ),
            "interlaced": (
                png_with_idat(valid_stream, width=1, height=1, interlace=1),
                "non-interlaced 8-bit RGB or RGBA",
            ),
            "malformed-deflate": (
                png_with_idat(b"this is not a zlib stream", width=1, height=1),
                "DEFLATE stream is invalid",
            ),
            "truncated-deflate": (
                png_with_idat(valid_stream[:-1], width=1, height=1),
                "DEFLATE stream is truncated",
            ),
            "trailing-zlib-bytes": (
                png_with_idat(valid_stream + b"unexpected", width=1, height=1),
                "bytes after the zlib stream",
            ),
            "excessive-output": (
                png_with_idat(zlib.compress(valid_one_pixel_rgb + b"extra"), width=1, height=1),
                "expands beyond expected scanline bytes",
            ),
            "invalid-filter": (
                png_with_idat(zlib.compress(b"\x05\x01\x02\x03"), width=1, height=1),
                "invalid scanline filter byte",
            ),
            "decompressed-cap": (
                png_with_idat(valid_stream, width=10_000, height=5_000),
                "expected decompressed scanlines exceed safety cap",
            ),
        }
        for name, (image_bytes, reason) in cases.items():
            with self.subTest(name=name):
                page_path.write_bytes(image_bytes)
                bad_coordinate = coordinate_manifest(
                    source_sha256=digest(self.source.read_bytes()),
                    page=1,
                    page_count=1,
                    image_sha256=digest(image_bytes),
                )
                with self.assertRaisesRegex(PdfRenderManifestError, reason):
                    assemble_render_manifest(
                        artifact_root=self.root,
                        source_pdf_path=self.source,
                        pages=(RenderedPageInput(page_path, bad_coordinate),),
                    )

    def test_rejects_coordinate_hash_source_or_image_binding_tampering(self) -> None:
        manifest = self.build_two_page_manifest()
        payload = manifest.to_dict()
        payload["pages"][0]["coordinate_manifest_sha256"] = digest("tampered")
        with self.assertRaisesRegex(PdfRenderManifestError, "coordinate_manifest_sha256"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["pages"][0]["image_sha256"] = digest("other image")
        with self.assertRaisesRegex(PdfRenderManifestError, "page_image_sha256"):
            PdfRenderManifest.from_dict(payload)

        payload = manifest.to_dict()
        payload["pages"][0]["coordinate_manifest"]["source_sha256"] = digest("other source")
        payload["pages"][0]["coordinate_manifest_sha256"] = digest(
            json.dumps(payload["pages"][0]["coordinate_manifest"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "source_sha256"):
            PdfRenderManifest.from_dict(payload)

    def test_rejects_tampered_files_and_non_png_replacement(self) -> None:
        manifest = self.build_two_page_manifest()
        (self.root / "source" / "notice.pdf").write_bytes(b"%PDF-1.7\nmodified")
        with self.assertRaisesRegex(PdfRenderManifestError, "source PDF SHA-256"):
            validate_render_manifest_files(manifest, artifact_root=self.root)

        # Restore the bound source, then prove the image bytes are separately
        # checked both as PNG and by exact hash/size.
        self.source.write_bytes(b"%PDF-1.7\nexample source bytes\n")
        (self.root / "rendered" / "page-1.png").write_bytes(b"not a PNG")
        with self.assertRaisesRegex(PdfRenderManifestError, "PNG signature"):
            validate_render_manifest_files(manifest, artifact_root=self.root)

    def test_rejects_png_dimensions_that_disagree_with_coordinate_manifest(self) -> None:
        page_path, coordinate = self.make_page(1)
        page_path.write_bytes(png_bytes(b"wrong dimensions", width=100, height=100))
        coordinate = coordinate_manifest(
            source_sha256=digest(self.source.read_bytes()),
            page=1,
            page_count=1,
            image_sha256=digest(page_path.read_bytes()),
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "PNG dimensions"):
            assemble_render_manifest(
                artifact_root=self.root,
                source_pdf_path=self.source,
                pages=(RenderedPageInput(page_path, coordinate),),
            )

    def test_rejects_unsafe_paths_and_symlink_artifacts(self) -> None:
        manifest = self.build_two_page_manifest()
        payload = copy.deepcopy(manifest.to_dict())
        payload["source_pdf_relative_path"] = "../outside.pdf"
        with self.assertRaisesRegex(PdfRenderManifestError, "safe relative path"):
            PdfRenderManifest.from_dict(payload)

        image = self.root / "rendered" / "page-1.png"
        target = self.root / "rendered" / "real-page-1.png"
        image.replace(target)
        try:
            image.symlink_to(target.name)
        except OSError as error:  # pragma: no cover - platforms with no symlink privilege
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaisesRegex(PdfRenderManifestError, "must not traverse a symlink"):
            validate_render_manifest_files(manifest, artifact_root=self.root)

    def test_assembler_rejects_page_gaps_coordinate_source_mismatch_and_bad_png(self) -> None:
        page_one_path, page_one_coordinate = self.make_page(1)
        page_three_path, _ = self.make_page(3)
        page_three_coordinate = coordinate_manifest(
            source_sha256=digest(self.source.read_bytes()),
            page=3,
            page_count=3,
            image_sha256=digest(page_three_path.read_bytes()),
        )
        page_one_coordinate = coordinate_manifest(
            source_sha256=digest(self.source.read_bytes()),
            page=1,
            page_count=3,
            image_sha256=digest(page_one_path.read_bytes()),
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "contiguous"):
            assemble_render_manifest(
                artifact_root=self.root,
                source_pdf_path=self.source,
                pages=(RenderedPageInput(page_one_path, page_one_coordinate), RenderedPageInput(page_three_path, page_three_coordinate)),
            )

        page_one_path, page_one_coordinate = self.make_page(1)
        page_two_path, page_two_coordinate = self.make_page(2)
        mismatched_source = coordinate_manifest(
            source_sha256=digest("different source"), page=2, image_sha256=page_two_coordinate.page_image_sha256,
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "source_sha256"):
            assemble_render_manifest(
                artifact_root=self.root,
                source_pdf_path=self.source,
                pages=(RenderedPageInput(page_one_path, page_one_coordinate), RenderedPageInput(page_two_path, mismatched_source)),
            )

        invalid_png = self.root / "rendered" / "page-2.png"
        invalid_png.write_bytes(b"not a png")
        invalid_coordinate = coordinate_manifest(
            source_sha256=digest(self.source.read_bytes()), page=2, image_sha256=digest(invalid_png.read_bytes()),
        )
        with self.assertRaisesRegex(PdfRenderManifestError, "PNG signature"):
            assemble_render_manifest(
                artifact_root=self.root,
                source_pdf_path=self.source,
                pages=(RenderedPageInput(page_one_path, page_one_coordinate), RenderedPageInput(invalid_png, invalid_coordinate)),
            )


if __name__ == "__main__":
    unittest.main()
