from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, sentinel

from jsonschema import Draft202012Validator

import common_ir_pipeline.pdf_fusion.primary_table_continuation_gold as contract
from common_ir_pipeline.pdf_fusion.primary_table_continuation_gold import (
    PrimaryTableContinuationGoldError,
    PrimaryTableContinuationGoldFixture,
    canonical_primary_table_continuation_gold_json,
    load_primary_table_continuation_gold_file,
    parse_primary_table_continuation_gold_bytes,
    validate_primary_table_continuation_gold,
    validate_primary_table_continuation_gold_against_inputs,
)
import common_ir_pipeline.pdf_fusion.primary_table_grid_gold as base_contract
from common_ir_pipeline.pdf_fusion.primary_table_grid_gold import (
    PrimaryTableGridGoldFixture,
    load_primary_table_grid_gold_file,
)


NOTICE_ID = "PBLN_000000000114788"
SOURCE_SHA = "1" * 64
NATIVE_SHA = "2" * 64
ACTUAL_BASE_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_table_grid_gold_114788_p3_p4.v1.json"
)
ACTUAL_CONTINUATION_GOLD = (
    Path(__file__).resolve().parents[3]
    / "baselines/pdf_reconstruction/primary_table_continuation_gold_114788_p3_p4.v1.json"
)
ACTUAL_CONTINUATION_GOLD_SHA256 = (
    "36d58cf6c7fb3e85e429937a927fb4a1c5542e487736359aa760b2191c4b98eb"
)


def _encoded(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _occ(page: int, index: int) -> str:
    return f"occ:inspector:p{page}:t{index}"


def _base_scope(page: int, *, status: str) -> dict:
    prefix = f"p{page}.support"
    pending = status == "pending_human_confirmation"
    return {
        "scope_id": f"{prefix}.table-grid",
        "physical_page": page,
        "segment_id": f"{prefix}.segment",
        "review_status": status,
        "reviewer_ref": None if pending else "reviewer:base",
        "confirmed_at": None if pending else "2026-09-20T00:00:00Z",
        "occurrence_ids": [
            _occ(page, 10),
            _occ(page, 11),
            _occ(page, 12),
            _occ(page, 13),
        ],
        "row_ids": [f"{prefix}.r1"],
        "column_ids": [f"{prefix}.c1", f"{prefix}.c2"],
        "cells": [
            {
                "cell_id": f"{prefix}.r1.c1",
                "row_ids": [f"{prefix}.r1"],
                "column_ids": [f"{prefix}.c1"],
                "content_status": "populated",
                "occurrence_ids": [_occ(page, 11)],
            },
            {
                "cell_id": f"{prefix}.r1.c2",
                "row_ids": [f"{prefix}.r1"],
                "column_ids": [f"{prefix}.c2"],
                "content_status": "populated",
                "occurrence_ids": [_occ(page, 12)],
            },
        ],
        "hard_negatives": [
            {
                "kind": "forbidden_segment_membership",
                "occurrence_id": _occ(page, 10),
                "anchor_position": "before_segment",
            },
            {
                "kind": "forbidden_segment_membership",
                "occurrence_id": _occ(page, 13),
                "anchor_position": "after_segment",
            },
        ],
    }


def _base_payload(*, status: str = "pending_human_confirmation") -> dict:
    return {
        "schema_version": "pdf_primary_table_grid_gold/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "canonical_page_renders": [
                {"physical_page": 3, "canonical_render_sha256": "3" * 64},
                {"physical_page": 4, "canonical_render_sha256": "4" * 64},
            ],
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
        },
        "page_scope": [3, 4],
        "reviewed_scopes": [
            _base_scope(3, status=status),
            _base_scope(4, status=status),
        ],
    }


def _payload(
    base: PrimaryTableGridGoldFixture,
    *,
    status: str = "pending_human_confirmation",
) -> dict:
    pending = status == "pending_human_confirmation"
    return {
        "schema_version": "pdf_primary_table_continuation_gold/v1",
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": "internal_consistency_only",
        "notice_id": NOTICE_ID,
        "source": {
            "source_pdf_sha256": SOURCE_SHA,
            "canonical_page_renders": [
                {"physical_page": 3, "canonical_render_sha256": "3" * 64},
                {"physical_page": 4, "canonical_render_sha256": "4" * 64},
            ],
            "native_capture": {
                "schema_version": "pdf_inspector_native_capture/v1",
                "canonical_sha256": NATIVE_SHA,
                "extractor_version": "1.17.0",
            },
            "base_table_grid_gold": {
                "schema_version": "pdf_primary_table_grid_gold/v1",
                "canonical_sha256": base.canonical_sha256,
            },
        },
        "page_scope": [3, 4],
        "reviewed_continuations": [
            {
                "continuation_id": "p3-p4.support.between-rows",
                "predecessor_scope_id": "p3.support.table-grid",
                "successor_scope_id": "p4.support.table-grid",
                "relation_kind": "between_rows",
                "column_mapping": [
                    {
                        "predecessor_column_id": "p3.support.c1",
                        "successor_column_id": "p4.support.c1",
                    },
                    {
                        "predecessor_column_id": "p3.support.c2",
                        "successor_column_id": "p4.support.c2",
                    },
                ],
                "review_status": status,
                "reviewer_ref": None if pending else "reviewer:continuation",
                "confirmed_at": None if pending else "2026-09-20T00:00:00Z",
            }
        ],
    }


class PrimaryTableContinuationGoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            files("common_ir_pipeline.pdf_fusion")
            .joinpath(
                "schemas/pdf_primary_table_continuation_gold_v1.schema.json"
            )
            .read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def base(
        self, *, status: str = "pending_human_confirmation"
    ) -> PrimaryTableGridGoldFixture:
        return PrimaryTableGridGoldFixture.from_dict(_base_payload(status=status))

    def test_schema_runtime_and_canonical_textless_contract(self) -> None:
        base = self.base()
        payload = _payload(base)
        self.schema_validator.validate(payload)
        fixture = PrimaryTableContinuationGoldFixture.from_dict(
            payload, base_table_grid_gold=base
        )
        self.assertEqual(
            fixture.canonical_json(),
            canonical_primary_table_continuation_gold_json(
                payload, base_table_grid_gold=base
            ),
        )
        self.assertEqual(fixture.quality_gate_status, "not_evaluable_gold_pending")
        serialized = fixture.canonical_json().decode("utf-8")
        for forbidden in (
            '"text"',
            '"bbox"',
            "surya",
            "opendataloader",
            "candidate_id",
            "table_grid_id",
            "occ:inspector",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_actual_114788_confirmed_fixture_loads_with_exact_digest(self) -> None:
        base = load_primary_table_grid_gold_file(ACTUAL_BASE_GOLD)
        raw = ACTUAL_CONTINUATION_GOLD.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        self.schema_validator.validate(payload)
        fixture = load_primary_table_continuation_gold_file(
            ACTUAL_CONTINUATION_GOLD, base_table_grid_gold=base
        )
        self.assertEqual(fixture.canonical_json(), raw)
        self.assertEqual(fixture.canonical_sha256, ACTUAL_CONTINUATION_GOLD_SHA256)
        self.assertTrue(fixture.has_trusted_confirmation)
        self.assertEqual(
            fixture.quality_gate_status,
            "not_evaluable_input_replay_required",
        )
        reviewed = fixture.to_dict()["reviewed_continuations"][0]
        self.assertEqual(reviewed["review_status"], "human_confirmed")
        self.assertEqual(reviewed["reviewer_ref"], "reviewer:project-owner")
        self.assertEqual(reviewed["confirmed_at"], "2026-09-19T15:19:38Z")
        self.assertEqual(
            fixture.to_dict()["reviewed_continuations"][0]["column_mapping"],
            [
                {
                    "predecessor_column_id": "p3.support.c1",
                    "successor_column_id": "p4.support.c1",
                },
                {
                    "predecessor_column_id": "p3.support.c2",
                    "successor_column_id": "p4.support.c2",
                },
            ],
        )

    def test_endpoints_are_adjacent_and_page_scope_is_exact(self) -> None:
        base = self.base()
        same_page = _payload(base)
        same_page["reviewed_continuations"][0][
            "successor_scope_id"
        ] = "p3.support.table-grid"
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "adjacent increasing"
        ):
            validate_primary_table_continuation_gold(
                same_page, base_table_grid_gold=base
            )

        unused_page = _payload(base)
        unused_page["page_scope"] = [3]
        unused_page["source"]["canonical_page_renders"] = unused_page["source"][
            "canonical_page_renders"
        ][:1]
        with self.assertRaises(PrimaryTableContinuationGoldError):
            validate_primary_table_continuation_gold(
                unused_page, base_table_grid_gold=base
            )

    def test_column_mapping_must_be_total_bijective_and_canonical(self) -> None:
        base = self.base()
        missing = _payload(base)
        missing["reviewed_continuations"][0]["column_mapping"].pop()
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "total canonical bijection"
        ):
            validate_primary_table_continuation_gold(
                missing, base_table_grid_gold=base
            )

        duplicate = _payload(base)
        mappings = duplicate["reviewed_continuations"][0]["column_mapping"]
        mappings[1]["successor_column_id"] = mappings[0]["successor_column_id"]
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "bijective"
        ):
            validate_primary_table_continuation_gold(
                duplicate, base_table_grid_gold=base
            )

        reordered = _payload(base)
        reordered["reviewed_continuations"][0]["column_mapping"].reverse()
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "total canonical bijection"
        ):
            validate_primary_table_continuation_gold(
                reordered, base_table_grid_gold=base
            )

    def test_fan_out_and_relation_kind_fail_closed(self) -> None:
        base = self.base()
        fan_out = _payload(base)
        second = deepcopy(fan_out["reviewed_continuations"][0])
        second["continuation_id"] = "p3-p4.support.second-between-rows"
        fan_out["reviewed_continuations"].append(second)
        fan_out["reviewed_continuations"].sort(
            key=lambda item: item["continuation_id"]
        )
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "only one continuation"
        ):
            validate_primary_table_continuation_gold(
                fan_out, base_table_grid_gold=base
            )

        wrong_kind = _payload(base)
        wrong_kind["reviewed_continuations"][0]["relation_kind"] = "same_row"
        self.assertFalse(self.schema_validator.is_valid(wrong_kind))
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "between_rows"
        ):
            validate_primary_table_continuation_gold(
                wrong_kind, base_table_grid_gold=base
            )

    def test_cycle_detector_is_iterative_and_fails_cycles(self) -> None:
        self.assertFalse(contract._has_cycle({"a": "b", "b": "c"}))
        self.assertTrue(contract._has_cycle({"a": "b", "b": "c", "c": "a"}))

    def test_confirmed_relation_requires_confirmed_base_and_both_trust_gates(
        self,
    ) -> None:
        pending_base = self.base()
        confirmed_payload = _payload(pending_base, status="human_confirmed")
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "confirmed endpoint"
        ):
            validate_primary_table_continuation_gold(
                confirmed_payload, base_table_grid_gold=pending_base
            )

        confirmed_base = self.base(status="human_confirmed")
        confirmed = PrimaryTableContinuationGoldFixture.from_dict(
            _payload(confirmed_base, status="human_confirmed"),
            base_table_grid_gold=confirmed_base,
        )
        self.assertFalse(confirmed.has_trusted_confirmation)
        with (
            patch.object(
                base_contract,
                "TRUSTED_CONFIRMED_TABLE_GRID_GOLD_SHA256S",
                frozenset({confirmed_base.canonical_sha256}),
            ),
            patch.object(
                contract,
                "TRUSTED_CONFIRMED_TABLE_CONTINUATION_GOLD_SHA256S",
                frozenset({confirmed.canonical_sha256}),
            ),
        ):
            self.assertTrue(confirmed.has_trusted_confirmation)
            self.assertEqual(
                confirmed.quality_gate_status,
                "not_evaluable_input_replay_required",
            )

    def test_base_digest_and_forbidden_fields_fail_closed(self) -> None:
        base = self.base()
        mismatch = _payload(base)
        mismatch["source"]["base_table_grid_gold"]["canonical_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "canonical hash"
        ):
            validate_primary_table_continuation_gold(
                mismatch, base_table_grid_gold=base
            )

        leaked = _payload(base)
        leaked["reviewed_continuations"][0]["table_grid_id"] = "forbidden"
        self.assertFalse(self.schema_validator.is_valid(leaked))
        with self.assertRaisesRegex(PrimaryTableContinuationGoldError, "keys"):
            validate_primary_table_continuation_gold(
                leaked, base_table_grid_gold=base
            )

    def test_parser_loader_and_canonical_bytes(self) -> None:
        base = self.base()
        raw = _encoded(_payload(base))
        fixture = parse_primary_table_continuation_gold_bytes(
            raw, base_table_grid_gold=base
        )
        self.assertEqual(fixture.canonical_json(), raw)
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "canonical"
        ):
            parse_primary_table_continuation_gold_bytes(
                raw + b"\n", base_table_grid_gold=base
            )
        duplicate = raw.replace(
            b'{"evaluation_only":true,',
            b'{"evaluation_only":true,"evaluation_only":true,',
            1,
        )
        with self.assertRaisesRegex(
            PrimaryTableContinuationGoldError, "duplicate"
        ):
            parse_primary_table_continuation_gold_bytes(
                duplicate, base_table_grid_gold=base
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continuation-gold.json"
            path.write_bytes(raw)
            self.assertEqual(
                load_primary_table_continuation_gold_file(
                    path, base_table_grid_gold=base
                ).canonical_json(),
                raw,
            )
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(
                PrimaryTableContinuationGoldError, "non-symlink"
            ):
                load_primary_table_continuation_gold_file(
                    link, base_table_grid_gold=base
                )

    def test_public_replay_delegates_to_base_gold_replay_and_returns_receipt(
        self,
    ) -> None:
        base = self.base()
        fixture = PrimaryTableContinuationGoldFixture.from_dict(
            _payload(base), base_table_grid_gold=base
        )
        calls: list[dict] = []

        def replay_base(value: object, **kwargs: object) -> SimpleNamespace:
            self.assertIs(value, base)
            calls.append(kwargs)
            return SimpleNamespace(fixture=base)

        with patch.object(
            contract,
            "validate_primary_table_grid_gold_against_inputs",
            side_effect=replay_base,
        ):
            replayed = validate_primary_table_continuation_gold_against_inputs(
                fixture,
                source_pdf=sentinel.source_pdf,
                native_capture=sentinel.native_capture,
                render_manifest=sentinel.render_manifest,
                render_artifact_root=sentinel.render_root,
                base_table_grid_gold=base,
            )
        self.assertTrue(replayed.is_replay_receipt)
        self.assertEqual(replayed.canonical_sha256, fixture.canonical_sha256)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["native_capture"], sentinel.native_capture)
        self.assertIs(calls[0]["render_manifest"], sentinel.render_manifest)


if __name__ == "__main__":
    unittest.main()
