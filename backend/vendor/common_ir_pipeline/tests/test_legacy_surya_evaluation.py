from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.legacy_surya_evaluation import (
    LegacySuryaEvaluationError,
    LegacySuryaGeometryProposal,
    LegacySuryaLayoutEvaluation,
    MAX_JSON_STRING_BYTES,
    _assert_limits,
    parse_legacy_surya_layout_evaluation_bytes,
    parse_legacy_surya_layout_evaluation_file,
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def legacy_payload() -> dict[str, object]:
    return {
        "status": "ok", "notice_id": "PBLN_000000000121019", "mode": "all_page_layout_only_table_diagram_candidates",
        "pages": [{"page": 1, "image_size": {"width": 1653, "height": 2337}, "block_count": 3, "blocks": [
            {"block_index": 0, "label": "SectionHeader", "source_bbox": [100, 100, 700, 180],
             "result": {"raw": "OCR must never escape", "html": "<p>nor HTML</p>"}},
            {"block_index": 1, "label": "Text", "source_bbox": [100, 200, 1200, 300], "result": {"raw": "secret"}},
            {"block_index": 2, "label": "ListGroup", "source_bbox": [100, 320, 1000, 440], "table": {"cells": [["not trusted"]]}},
        ]}],
    }


def raw_payload(payload: dict[str, object] | None = None) -> bytes:
    return json.dumps(payload or legacy_payload(), separators=(",", ":")).encode("utf-8")


class LegacySuryaEvaluationTests(unittest.TestCase):
    def parse(self, raw: bytes, **overrides: object):
        bindings: dict[str, object] = {
            "notice_id": "PBLN_000000000121019", "source_pdf_sha256": digest("pdf"),
            "raw_json_sha256": digest(raw), "stage_record_sha256": digest("stage"),
            "legacy_render_manifest_sha256": digest("legacy-render"), "page_scope": (1,),
            "page_count": 1, "format_variant": "all_pages",
        }
        bindings.update(overrides)
        return parse_legacy_surya_layout_evaluation_bytes(raw, **bindings)

    def test_geometry_only_projection_supports_text_headers_and_lists(self) -> None:
        payload = legacy_payload()
        # Legacy block_index is only an identity.  Deliberately sparse values
        # must not become a false reading-order claim.
        for index, block_index in enumerate((10, 35, 99)):
            payload["pages"][0]["blocks"][index]["block_index"] = block_index  # type: ignore[index]
        envelope = self.parse(raw_payload(payload))
        proposals = envelope.to_dict()["pages"][0]["geometry_proposals"]
        self.assertEqual([item["canonical_label"] for item in proposals], ["section_header", "text", "list_group"])
        self.assertEqual([item["legacy_block_index"] for item in proposals], [10, 35, 99])
        self.assertEqual([item["legacy_traversal_order"] for item in proposals], [0, 1, 2])
        self.assertNotIn("reading_order", proposals[0])
        self.assertEqual(envelope.to_dict()["ordering_status"], "legacy_surya_traversal_unverified")
        serialized = envelope.canonical_json().decode("utf-8").lower()
        self.assertNotIn("ocr", serialized)
        self.assertNotIn("html", serialized)
        self.assertNotIn("cells", serialized)
        self.assertTrue(envelope.evaluation_only)
        self.assertTrue(envelope.non_promotable)
        self.assertEqual(envelope.coordinate_status, "legacy_surya_unverified")

    def test_canonical_envelope_and_schema(self) -> None:
        raw = raw_payload()
        first, second = self.parse(raw), self.parse(raw)
        self.assertEqual(first.canonical_json(), second.canonical_json())
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath(
            "schemas/legacy_surya_layout_evaluation_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(first.to_dict())
        self.assertNotIn("surya_layout_artifact/v1", first.canonical_json().decode("utf-8"))

    def test_rejects_wrong_bindings_scope_bboxes_and_repeated_identities(self) -> None:
        raw = raw_payload()
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "raw_json_sha256"):
            self.parse(raw, raw_json_sha256=digest("wrong"))
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "notice_id"):
            self.parse(raw, notice_id="PBLN_wrong")
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "page_count"):
            self.parse(raw, page_scope=(1, 2), page_count=2)

        bad = legacy_payload()
        bad["pages"][0]["blocks"][0]["source_bbox"] = [-1, 0, 1, 1]  # type: ignore[index]
        invalid_raw = raw_payload(bad)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "outside"):
            self.parse(invalid_raw)

        bad = legacy_payload()
        bad["pages"][0]["blocks"][1]["block_index"] = 0  # type: ignore[index]
        invalid_raw = raw_payload(bad)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "unique non-negative"):
            self.parse(invalid_raw)

        bad = legacy_payload()
        bad["pages"].append(bad["pages"][0])  # type: ignore[union-attr,index]
        invalid_raw = raw_payload(bad)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "page_count"):
            self.parse(invalid_raw)

    def test_rejects_duplicate_keys_nan_depth_string_and_targeted(self) -> None:
        duplicate = b'{"notice_id":"PBLN_000000000121019","notice_id":"PBLN_000000000121019","pages":[]}'
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "duplicate JSON key"):
            self.parse(duplicate)
        nan = raw_payload().replace(b"100", b"NaN", 1)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "non-finite"):
            self.parse(nan)
        # Non-finite exponent overflow must fail even in a field that is not
        # otherwise consumed by the geometry projection.
        overflow = raw_payload()[:-1] + b',"ignored_untrusted_field":1e999}'
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "non-finite"):
            self.parse(overflow)
        deep = b'{"notice_id":"PBLN_000000000121019","pages":' + b"[" * 34 + b"]" * 34 + b"}"
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "nesting"):
            self.parse(deep)
        bad = legacy_payload()
        bad["status"] = "x" * (MAX_JSON_STRING_BYTES + 1)
        too_long = raw_payload(bad)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "string exceeds"):
            self.parse(too_long)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "only legacy all_pages"):
            self.parse(raw_payload(), format_variant="targeted")
        bad = legacy_payload()
        bad["mode"] = "unknown"
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "successful all-pages"):
            self.parse(raw_payload(bad))
        bad = legacy_payload()
        bad["pages"][0]["block_count"] = 2  # type: ignore[index]
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "block_count"):
            self.parse(raw_payload(bad))

    def test_direct_proposals_and_envelopes_fail_closed(self) -> None:
        valid = LegacySuryaGeometryProposal(
            page=1, legacy_block_index=7, legacy_label="Table", canonical_label="table",
            bbox_px=(1, 2, 20, 30), legacy_traversal_order=0,
        )
        for kwargs, message in (
            ({"page": 0}, "proposal page"),
            ({"page": 257}, "no greater than 256"),
            ({"legacy_block_index": -1}, "legacy_block_index"),
            ({"legacy_traversal_order": -1}, "legacy_traversal_order"),
            ({"legacy_label": "Unknown"}, "legacy_label"),
            ({"canonical_label": "text"}, "canonical_label"),
            ({"bbox_px": [1, 2, 20, 30]}, "four-coordinate tuple"),
            ({"bbox_px": (1, 2, float("inf"), 30)}, "finite"),
            ({"bbox_px": (-1, 2, 20, 30)}, "non-negative"),
        ):
            values = {
                "page": valid.page, "legacy_block_index": valid.legacy_block_index,
                "legacy_label": valid.legacy_label, "canonical_label": valid.canonical_label,
                "bbox_px": valid.bbox_px, "legacy_traversal_order": valid.legacy_traversal_order,
            }
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(LegacySuryaEvaluationError, message):
                LegacySuryaGeometryProposal(**values)

        envelope_args = {
            "notice_id": "PBLN_000000000121019", "source_pdf_sha256": digest("pdf"),
            "raw_json_sha256": digest("raw"), "stage_record_sha256": digest("stage"),
            "legacy_render_manifest_sha256": digest("render"), "page_scope": (1,),
            "page_count": 1, "format_variant": "all_pages",
        }
        non_dense = LegacySuryaGeometryProposal(
            page=1, legacy_block_index=9, legacy_label="Text", canonical_label="text",
            bbox_px=(1, 2, 20, 30), legacy_traversal_order=1,
        )
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "dense deterministic"):
            LegacySuryaLayoutEvaluation(pages=((1, 100, 100, (non_dense,)),), **envelope_args)
        duplicate_identity = LegacySuryaGeometryProposal(
            page=1, legacy_block_index=7, legacy_label="Text", canonical_label="text",
            bbox_px=(30, 2, 60, 30), legacy_traversal_order=1,
        )
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "repeat legacy block"):
            LegacySuryaLayoutEvaluation(pages=((1, 100, 100, (valid, duplicate_identity)),), **envelope_args)
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "outside"):
            LegacySuryaLayoutEvaluation(pages=((1, 10, 10, (valid,)),), **envelope_args)

    def test_schema_and_runtime_mutations_remain_in_lockstep(self) -> None:
        envelope = self.parse(raw_payload())
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath(
            "schemas/legacy_surya_layout_evaluation_v1.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        mutations = (
            (("ordering_status",), "reading_order"),
            (("pages", 0, "geometry_proposals", 0, "legacy_traversal_order"), -1),
            (("pages", 0, "geometry_proposals", 0, "canonical_label"), "table"),
        )
        for path, replacement in mutations:
            payload = envelope.to_dict()
            target: object = payload
            for part in path[:-1]:
                target = target[part]  # type: ignore[index]
            target[path[-1]] = replacement  # type: ignore[index]
            with self.subTest(path=path):
                self.assertTrue(list(validator.iter_errors(payload)))
        with self.assertRaisesRegex(LegacySuryaEvaluationError, "non-JSON primitive"):
            _assert_limits(object())

    def test_safe_file_reader_rejects_symlink_and_reads_regular_file(self) -> None:
        raw = raw_payload()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "legacy.json"
            regular.write_bytes(raw)
            parsed = parse_legacy_surya_layout_evaluation_file(regular, notice_id="PBLN_000000000121019",
                source_pdf_sha256=digest("pdf"), raw_json_sha256=digest(raw), stage_record_sha256=digest("stage"),
                legacy_render_manifest_sha256=digest("legacy-render"), page_scope=(1,), page_count=1, format_variant="all_pages")
            self.assertEqual(parsed.raw_json_sha256, digest(raw))
            link = root / "link.json"
            link.symlink_to(regular)
            with self.assertRaisesRegex(LegacySuryaEvaluationError, "symlink"):
                parse_legacy_surya_layout_evaluation_file(link, notice_id="PBLN_000000000121019",
                    source_pdf_sha256=digest("pdf"), raw_json_sha256=digest(raw), stage_record_sha256=digest("stage"),
                    legacy_render_manifest_sha256=digest("legacy-render"), page_scope=(1,), page_count=1, format_variant="all_pages")


if __name__ == "__main__":
    unittest.main()
