from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.legacy_opendataloader_evaluation import (
    LegacyOdlPageToSourcePage,
    LegacyOpenDataLoaderEvaluation,
    LegacyOpenDataLoaderEvaluationError,
    build_legacy_opendataloader_evaluation_bytes,
    build_legacy_opendataloader_evaluation_file,
    validate_legacy_opendataloader_evaluation,
)
from common_ir_pipeline.pdf_fusion.opendataloader_artifact import (
    OpenDataLoaderArtifact,
    OpenDataLoaderArtifactError,
    canonical_json_bytes,
)


CURATED_121019 = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/legacy_odl_121019_p5.evaluation.v1.json"
)


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def historical_odl() -> dict[str, object]:
    # Page 3/4 are intentionally mapped to different source pages: a legacy
    # cache may be a sparse PDF subset and mapping must be explicit.
    return {
        "number of pages": 4,
        "kids": [
            {"type": "paragraph", "page number": 3, "content": "opaque text"},
            {
                "type": "table", "page number": 4,
                "rows": [
                    {"type": "table row", "cells": [{"type": "cell", "content": "opaque"}]},
                ],
            },
        ],
    }


def build(raw: dict[str, object] | None = None) -> tuple[bytes, LegacyOpenDataLoaderEvaluation]:
    raw = raw or historical_odl()
    raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return raw_bytes, build_legacy_opendataloader_evaluation_bytes(
        raw_bytes,
        notice_id="PBLN_000000000121019",
        source_pdf_sha256=digest("curated source PDF"),
        source_page_count=5,
        page_scope=(2, 5),
        odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 5)),
    )


class LegacyOpenDataLoaderEvaluationTests(unittest.TestCase):
    def test_curated_121019_page_five_binding_is_stable_and_explicitly_legacy(self) -> None:
        evaluation = LegacyOpenDataLoaderEvaluation.from_dict(
            json.loads(CURATED_121019.read_text(encoding="utf-8"))
        )
        self.assertEqual(evaluation.notice_id, "PBLN_000000000121019")
        self.assertEqual(evaluation.page_scope, (5,))
        self.assertEqual(evaluation.source_page_for_odl_page(5), 5)
        self.assertEqual(
            digest(evaluation.canonical_json()),
            "36fd14646f0cdb3f36b82ab5fe53842fc74ab11b6f2dc6a9b3ac01feda01fd8f",
        )
        self.assertEqual(evaluation.config_status, "unavailable")

    def test_builds_separate_metadata_only_envelope_with_explicit_mapping(self) -> None:
        raw_bytes, evaluation = build()
        self.assertEqual(evaluation.schema_version, "legacy_opendataloader_evaluation/v1")
        self.assertTrue(evaluation.evaluation_only)
        self.assertTrue(evaluation.non_promotable)
        self.assertEqual(evaluation.coordinate_status, "odl_pdf_points_unverified")
        self.assertEqual(evaluation.source_page_for_odl_page(3), 2)
        self.assertEqual(evaluation.to_dict()["page_scope"], [2, 5])
        self.assertEqual(evaluation.raw_json_sha256, digest(raw_bytes))
        self.assertEqual(evaluation.canonical_json_sha256, digest(canonical_json_bytes(historical_odl())))
        self.assertEqual(evaluation.parser_claim_status, "documented_not_machine_bound")
        self.assertEqual(evaluation.config_status, "unavailable")
        self.assertEqual(evaluation.ocr_claim_status, "disabled_documented_not_machine_bound")
        self.assertFalse({"content", "nodes", "geometry", "table_cells"} & set(evaluation.to_dict()))

    def test_runtime_schema_parity_and_cannot_masquerade_as_strict_artifact(self) -> None:
        _, evaluation = build()
        payload = evaluation.to_dict()
        schema = json.loads(files("common_ir_pipeline.pdf_fusion").joinpath("schemas/legacy_opendataloader_evaluation_v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
        bad = dict(payload)
        bad["non_promotable"] = False
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(bad)))
        with self.assertRaisesRegex(OpenDataLoaderArtifactError, "schema_version|unexpected keys|missing keys"):
            OpenDataLoaderArtifact.from_dict(payload)
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "unexpected keys"):
            LegacyOpenDataLoaderEvaluation.from_dict({**payload, "config_sha256": digest("fabricated")})

    def test_rejects_raw_mapping_split_and_root_page_count_mutations(self) -> None:
        raw_bytes, evaluation = build()
        raw = historical_odl()
        split_raw = b'{"number of pages":4,"kids":[],"a":1}'
        split_mapping = {"number of pages": 4, "kids": [], "b": 2}
        split_evaluation = LegacyOpenDataLoaderEvaluation(
            notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest(split_raw),
            canonical_json_sha256=digest(canonical_json_bytes(split_mapping)), source_page_count=4,
            odl_page_count=4, page_scope=(1,),
            odl_page_to_source_page=(LegacyOdlPageToSourcePage(1, 1),),
        )
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "do not describe the same JSON"):
            validate_legacy_opendataloader_evaluation(split_evaluation, split_mapping, raw_bytes=split_raw)
        altered = dict(raw)
        altered["number of pages"] = 3
        altered_bytes = json.dumps(altered, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "raw_json_sha256"):
            validate_legacy_opendataloader_evaluation(evaluation, altered, raw_bytes=altered_bytes)
        altered_evaluation = LegacyOpenDataLoaderEvaluation(
            notice_id=evaluation.notice_id, source_pdf_sha256=evaluation.source_pdf_sha256,
            raw_json_sha256=digest(altered_bytes), canonical_json_sha256=digest(canonical_json_bytes(altered)),
            source_page_count=5, odl_page_count=4, page_scope=(2, 5),
            odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 5)),
        )
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "root number of pages disagrees"):
            validate_legacy_opendataloader_evaluation(altered_evaluation, altered, raw_bytes=altered_bytes)

    def test_rejects_page_contract_and_json_reader_mutations(self) -> None:
        unmapped = {"number of pages": 4, "kids": [{"type": "paragraph", "page number": 2}]}
        bytes_unmapped = json.dumps(unmapped, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "outside the mapped"):
            build_legacy_opendataloader_evaluation_bytes(
                bytes_unmapped, notice_id="N", source_pdf_sha256=digest("source"), source_page_count=5,
                page_scope=(2, 5), odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 5)),
            )
        orphan = {"number of pages": 4, "kids": [{"type": "table row", "cells": []}]}
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "no direct or inherited page"):
            build_legacy_opendataloader_evaluation_bytes(
                json.dumps(orphan, separators=(",", ":")).encode("utf-8"), notice_id="N", source_pdf_sha256=digest("source"), source_page_count=5,
                page_scope=(2, 5), odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 5)),
            )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_bytes(b'{"number of pages":4,"number of pages":4}')
            with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "duplicate key"):
                build_legacy_opendataloader_evaluation_file(
                    path, notice_id="N", source_pdf_sha256=digest("source"), source_page_count=4,
                    page_scope=(1,), odl_page_to_source_page=(LegacyOdlPageToSourcePage(1, 1),),
                )

    def test_mapping_must_be_bijective_and_page_scope_exact(self) -> None:
        raw_bytes = json.dumps({"number of pages": 4, "kids": []}, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "bijectively cover"):
            build_legacy_opendataloader_evaluation_bytes(
                raw_bytes, notice_id="N", source_pdf_sha256=digest("source"), source_page_count=5,
                page_scope=(2, 5), odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 2)),
            )
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "sorted and unique"):
            LegacyOpenDataLoaderEvaluation(
                notice_id="N", source_pdf_sha256=digest("source"), raw_json_sha256=digest(raw_bytes),
                canonical_json_sha256=digest(canonical_json_bytes({"number of pages": 4, "kids": []})),
                source_page_count=5, odl_page_count=4, page_scope=(5, 2),
                odl_page_to_source_page=(LegacyOdlPageToSourcePage(3, 2), LegacyOdlPageToSourcePage(4, 5)),
            )

    def test_from_dict_applies_json_resource_caps_before_materializing_arrays(self) -> None:
        _, evaluation = build()
        oversized = evaluation.to_dict()
        oversized["page_scope"] = [1] * 50_001
        with self.assertRaisesRegex(LegacyOpenDataLoaderEvaluationError, "node cap"):
            LegacyOpenDataLoaderEvaluation.from_dict(oversized)
