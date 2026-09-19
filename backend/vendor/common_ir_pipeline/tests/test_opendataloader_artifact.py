from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.opendataloader_artifact import (
    MAX_JSON_DEPTH,
    MAX_STRING_BYTES,
    OdlObjectEvidence,
    OdlPageToSourcePage,
    OpenDataLoaderArtifact,
    OpenDataLoaderArtifactError,
    OpenDataLoaderParser,
    canonical_json_bytes,
    canonical_json_sha256,
    decode_opendataloader_json_bytes,
    load_opendataloader_artifact_file,
    load_opendataloader_json_file,
    validate_opendataloader_artifact,
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def raw_document() -> dict[str, object]:
    # The reader deliberately treats this as opaque JSON.  Its real metadata
    # is passed as OdlObjectEvidence at the parser-facing boundary below.
    return {"document": {"pages": [{"number": 5, "objects": [{"id": "table-1", "bbox": [10, 20, 30, 40]}]}]}}


def artifact(raw: dict[str, object] | None = None) -> OpenDataLoaderArtifact:
    raw = raw or raw_document()
    raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return OpenDataLoaderArtifact(
        notice_id="PBLN_000000000104102", source_pdf_sha256=digest("source"),
        raw_json_sha256=digest(raw_bytes), canonical_json_sha256=canonical_json_sha256(raw),
        parser=OpenDataLoaderParser(name="opendataloader-pdf", version="2.5.7", config_sha256=digest("config"), ocr_enabled=False),
        source_page_count=14, odl_page_count=14, page_scope=(5,),
        odl_page_to_source_page=(OdlPageToSourcePage(odl_page=5, source_page=5),),
    )


class OpenDataLoaderArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = raw_document()
        self.raw_bytes = json.dumps(self.raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.artifact = artifact(self.raw)

    def test_accepts_104102_style_subset_and_extracted_source_page_five(self) -> None:
        accepted = validate_opendataloader_artifact(
            self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=14,
            objects=(OdlObjectEvidence(odl_page=5, object_id="table-1", bbox=(10, 20, 30, 40)),),
        )
        self.assertEqual(accepted.source_page_for_odl_page(5), 5)
        self.assertEqual(accepted.coordinate_space, "odl_pdf_points_unverified")
        self.assertEqual(accepted.to_dict()["page_scope"], [5])

    def test_raw_and_canonical_hashes_have_deliberately_different_meanings(self) -> None:
        reordered_bytes = b'{"document":{"pages":[]},"a":1}'
        reordered = json.loads(reordered_bytes)
        equivalent_bytes = b'{ "a" : 1, "document" : { "pages" : [] } }'
        self.assertEqual(canonical_json_sha256(reordered), canonical_json_sha256(json.loads(equivalent_bytes)))
        self.assertNotEqual(digest(reordered_bytes), digest(equivalent_bytes))
        self.assertEqual(canonical_json_bytes(reordered), b'{"a":1,"document":{"pages":[]}}')

    def test_exact_envelope_parser_and_page_map(self) -> None:
        payload = self.artifact.to_dict()
        self.assertEqual(OpenDataLoaderArtifact.from_dict(payload), self.artifact)
        payload["extra"] = True
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "unexpected keys"):
            OpenDataLoaderArtifact.from_dict(payload)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "ocr_enabled"):
            OpenDataLoaderParser("opendataloader-pdf", "2.5.7", digest("config"), True)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "bijectively cover"):
            OpenDataLoaderArtifact(
                notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest("raw"), canonical_json_sha256=digest("canonical"),
                parser=self.artifact.parser, source_page_count=14, odl_page_count=14, page_scope=(5,),
                odl_page_to_source_page=(OdlPageToSourcePage(5, 4),),
            )
        remapped = OpenDataLoaderArtifact(
            notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest("raw"), canonical_json_sha256=digest("canonical"),
            parser=self.artifact.parser, source_page_count=14, odl_page_count=2, page_scope=(3, 5),
            odl_page_to_source_page=(OdlPageToSourcePage(1, 5), OdlPageToSourcePage(2, 3)),
        )
        self.assertEqual(remapped.source_page_for_odl_page(1), 5)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "sorted and unique"):
            OpenDataLoaderArtifact(
                notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest("raw"), canonical_json_sha256=digest("canonical"),
                parser=self.artifact.parser, source_page_count=14, odl_page_count=14, page_scope=(4, 5),
                odl_page_to_source_page=(OdlPageToSourcePage(5, 5), OdlPageToSourcePage(5, 4)),
            )

    def test_rejects_duplicate_object_identity_bad_bbox_and_unmapped_page(self) -> None:
        objects = (
            OdlObjectEvidence(5, "same", (1, 2, 3, 4)),
            OdlObjectEvidence(5, "same", (5, 6, 7, 8)),
        )
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "duplicate"):
            validate_opendataloader_artifact(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=14, objects=objects)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "strictly increasing"):
            OdlObjectEvidence(5, "bad", (1, 2, 1, 4))
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "outside"):
            validate_opendataloader_artifact(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=14, objects=(OdlObjectEvidence(4, "x", (1, 2, 3, 4)),))

    def test_raw_reader_rejects_duplicate_keys_nonfinite_surrogates_and_caps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (b'{"x":1,"x":2}', "duplicate key"),
                (b'{"x":NaN}', "non-finite"),
                (b'{"x":"\\ud800"}', "surrogate"),
                (json.dumps({"x": "x" * (MAX_STRING_BYTES + 1)}).encode(), "string exceeds"),
                (("[" * (MAX_JSON_DEPTH + 2) + "0" + "]" * (MAX_JSON_DEPTH + 2)).encode(), "nesting depth"),
            )
            for index, (contents, message) in enumerate(cases):
                path = root / f"bad-{index}.json"
                path.write_bytes(contents)
                with self.subTest(message=message), self.assertRaisesRegex(OpenDataLoaderArtifactError, message):
                    load_opendataloader_json_file(path)

    def test_file_reader_rejects_symlink_and_schema_has_runtime_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_bytes(self.artifact.canonical_json())
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(OpenDataLoaderArtifactError):
                load_opendataloader_artifact_file(link)
            if hasattr(os, "mkfifo"):
                fifo = root / "artifact.fifo"
                os.mkfifo(fifo)
                with self.assertRaisesRegex(OpenDataLoaderArtifactError, "regular file"):
                    load_opendataloader_artifact_file(fifo)
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/opendataloader_artifact_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.artifact.to_dict())
        bad = self.artifact.to_dict()
        bad["parser"]["ocr_enabled"] = True
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(bad)))

    def test_raw_reader_returns_immutable_validated_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.json"
            path.write_bytes(self.raw_bytes)
            raw, document = load_opendataloader_json_file(path)
            self.assertEqual(raw, self.raw_bytes)
            self.assertEqual(
                validate_opendataloader_artifact(self.artifact, document, raw_bytes=raw, actual_odl_page_count=14),
                self.artifact,
            )
            with self.assertRaises(TypeError):
                document["new"] = 1  # type: ignore[index]
            with self.assertRaises(TypeError):
                document["document"]["pages"] = ()  # type: ignore[index]

        decoded = decode_opendataloader_json_bytes(self.raw_bytes)
        self.assertEqual(canonical_json_bytes(decoded), canonical_json_bytes(self.raw))
        with self.assertRaises(TypeError):
            decoded["new"] = 1  # type: ignore[index]

    def test_hash_binding_page_count_and_coordinate_allowlist_fail_closed(self) -> None:
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "raw_json_sha256"):
            validate_opendataloader_artifact(self.artifact, self.raw, raw_bytes=b"{}", actual_odl_page_count=14)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "canonical_json_sha256"):
            validate_opendataloader_artifact(self.artifact, {"different": True}, raw_bytes=self.raw_bytes, actual_odl_page_count=14)
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "page count"):
            validate_opendataloader_artifact(self.artifact, self.raw, raw_bytes=self.raw_bytes, actual_odl_page_count=5)
        raw_a = b'{"a":1}'
        mapping_b = {"b": 2}
        split_binding = OpenDataLoaderArtifact(
            notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest(raw_a),
            canonical_json_sha256=canonical_json_sha256(mapping_b), parser=self.artifact.parser,
            source_page_count=1, odl_page_count=1, page_scope=(1,),
            odl_page_to_source_page=(OdlPageToSourcePage(1, 1),),
        )
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "do not describe the same JSON"):
            validate_opendataloader_artifact(
                split_binding, mapping_b, raw_bytes=raw_a, actual_odl_page_count=1,
            )
        bad = self.artifact.to_dict()
        bad["coordinate_space"] = "pdf_user_space"
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "coordinate_space"):
            OpenDataLoaderArtifact.from_dict(bad)
