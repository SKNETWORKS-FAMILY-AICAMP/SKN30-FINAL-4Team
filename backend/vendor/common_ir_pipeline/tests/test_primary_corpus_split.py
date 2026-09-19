from __future__ import annotations

import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator

from common_ir_pipeline.pdf_fusion.primary_corpus_split import (
    PrimaryCorpusSplitError,
    PrimaryCorpusSplitFixture,
    canonical_primary_corpus_blind_reveal_json,
    canonical_primary_corpus_split_json,
    compute_blind_case_commitment,
    load_primary_corpus_split_file,
    parse_primary_corpus_blind_reveal_bytes,
    parse_primary_corpus_split_bytes,
    validate_split_source_baseline_bytes,
    validate_split_source_baseline_file,
    verify_blind_reveal,
)


def digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def public_case(
    case_id: str,
    role: str,
    suffix: int,
    state: str,
    checks: list[str],
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "role": role,
        "notice_id": f"PBLN_{suffix:015d}",
        "source_pdf_sha256": digest(f"pdf-{suffix}"),
        "planned_page_scope": [1, 2],
        "full_native_capture_required": True,
        "scope_claim": "reviewed_regions_only",
        "state_at_selection": state,
        "required_check_kinds": checks,
    }


BLIND_SALT = "ab" * 32
BACKEND_ROOT = Path(__file__).resolve().parents[3]
TRACKED_SPLIT = (
    BACKEND_ROOT
    / "baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json"
)
TRACKED_SOURCE_BASELINE = (
    BACKEND_ROOT / "baselines/pdf_fusion/bizinfo_existing_100.v1.json"
)
TRACKED_SPLIT_SHA256 = (
    "d6820fbe176df095cbd09e3faf56568167243b9db990982f295243d50f49fd51"
)


class PrimaryCorpusSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blind_case = public_case(
            "blind-x",
            "held_out",
            103645,
            "gold_pending",
            ["paragraph", "table_grid"],
        )
        self.public_cases = [
            public_case(
                "tuning-114788",
                "tuning",
                114788,
                "gold_ready",
                ["paragraph", "heading_relation", "table_grid", "table_continuation"],
            ),
            public_case(
                "known-121019",
                "known_regression",
                121019,
                "legacy_migration_required",
                ["legacy_structural_regression"],
            ),
            public_case(
                "held-104102",
                "held_out",
                104102,
                "gold_pending",
                ["paragraph", "heading_relation"],
            ),
            public_case(
                "held-124791",
                "held_out",
                124791,
                "gold_pending",
                ["table_grid", "table_continuation"],
            ),
            public_case(
                "negative-115310",
                "negative_control",
                115310,
                "gold_pending",
                ["table_grid_negative", "table_continuation_negative"],
            ),
        ]
        all_cases = self.public_cases + [self.blind_case]
        runs = []
        for case in sorted(all_cases, key=lambda item: item["notice_id"]):
            logical_id = case["notice_id"]
            artifacts = {
                "candidate_pack": {
                    "path": f"{logical_id}/pipeline/source_selection.json",
                    "sha256": digest(f"{logical_id}-candidate-pack"),
                    "counts": {"source_blocks": 3},
                },
                "common_ir": {
                    "path": f"{logical_id}/pipeline/common_ir_v1/source.pdf.json",
                    "sha256": digest(f"{logical_id}-common-ir"),
                    "counts": {"blocks": 5, "relations": 2},
                },
                "structured_profile": {
                    "path": f"{logical_id}/pipeline/structured_profile.v0.2.json",
                    "sha256": digest(f"{logical_id}-structured-profile"),
                    "counts": {"comparison_facts": 4, "support_components": 1},
                },
            }
            runs.append(
                {
                    "logical_id": logical_id,
                    "source": {
                        "path": f"{logical_id}/attachments/source.pdf",
                        "extension": "pdf",
                        "sha256": case["source_pdf_sha256"],
                    },
                    "artifact_count": 3,
                    "artifacts": artifacts,
                }
            )
        baseline = {
            "schema_version": "pdf_fusion_corpus_baseline/v1",
            "corpus_id": "pdf-primary-corpus-v1",
            "counts": {
                "all_runs": 6,
                "pdf_runs": 6,
                "extensions": {"pdf": 6},
                "artifacts": {
                    "candidate_pack": 6,
                    "common_ir": 6,
                    "structured_profile": 6,
                },
            },
            "all_runs": runs,
            "pdf_subset": {
                "logical_ids_sha256": sha256(
                    ("\n".join(run["logical_id"] for run in runs) + "\n").encode()
                ).hexdigest(),
                "runs": copy.deepcopy(runs),
            },
        }
        self.baseline_raw = (
            json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self.split_payload = {
            "schema_version": "pdf_primary_corpus_split/v1",
            "evaluation_only": True,
            "non_promotable": True,
            "standalone_validation_scope": "internal_consistency_only",
            "selection_boundary_commit": "1" * 40,
            "source_baseline": {
                "schema_version": "pdf_fusion_corpus_baseline/v1",
                "corpus_id": "pdf-primary-corpus-v1",
                "canonical_sha256": sha256(self.baseline_raw).hexdigest(),
            },
            "policy": {
                "role_cardinality": {
                    "tuning": 1,
                    "known_regression": 1,
                    "held_out": 2,
                    "sealed_blind": 1,
                    "negative_control": 1,
                },
                "current_supported_slices": [
                    "two_occurrence_paragraph",
                    "numbered_single_line_heading_to_next_paragraph",
                    "unit_span_table_grid",
                    "adjacent_page_between_rows_continuation",
                ],
            },
            "cases": self.public_cases,
            "sealed_case": {
                "case_id": "blind-x",
                "role": "sealed_blind",
                "state_at_selection": "sealed_unrevealed",
                "commitment": {
                    "algorithm": "sha256_salted_canonical_json_v1",
                    "digest": compute_blind_case_commitment(self.blind_case, BLIND_SALT),
                },
            },
        }

    def reveal_payload(self, split: PrimaryCorpusSplitFixture) -> dict[str, object]:
        return {
            "schema_version": "pdf_primary_corpus_blind_reveal/v1",
            "split_canonical_sha256": split.canonical_sha256,
            "sealed_case_id": "blind-x",
            "salt_hex": BLIND_SALT,
            "revealed_case": self.blind_case,
        }

    def bind_baseline(
        self, baseline: dict[str, object]
    ) -> tuple[PrimaryCorpusSplitFixture, bytes]:
        raw = (
            json.dumps(baseline, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        payload = copy.deepcopy(self.split_payload)
        payload["source_baseline"]["canonical_sha256"] = sha256(raw).hexdigest()  # type: ignore[index]
        return PrimaryCorpusSplitFixture(payload), raw

    @staticmethod
    def mutate_source_digest(
        baseline: dict[str, object], notice_id: str, replacement: str
    ) -> None:
        for collection in (baseline["all_runs"], baseline["pdf_subset"]["runs"]):  # type: ignore[index]
            for run in collection:  # type: ignore[union-attr]
                if run["logical_id"] == notice_id:
                    run["source"]["sha256"] = replacement
                    break

    def test_tracked_a45_split_is_canonical_and_source_bound(self) -> None:
        split = load_primary_corpus_split_file(TRACKED_SPLIT)
        self.assertEqual(split.canonical_sha256, TRACKED_SPLIT_SHA256)
        self.assertEqual(
            validate_split_source_baseline_file(split, TRACKED_SOURCE_BASELINE),
            "78c828fc0de4e32ccfe681cece53dfbf1a3b106c48e762484be5e8a1cd46358f",
        )
        self.assertEqual(
            [case["role"] for case in split.payload["cases"]],
            ["tuning", "known_regression", "held_out", "held_out", "negative_control"],
        )
        self.assertEqual(split.payload["sealed_case"]["role"], "sealed_blind")

    def test_schema_runtime_canonical_and_immutable_split(self) -> None:
        schema_root = Path(__file__).resolve().parents[1] / "src/common_ir_pipeline/pdf_fusion/schemas"
        split_schema = json.loads((schema_root / "pdf_primary_corpus_split_v1.schema.json").read_text())
        reveal_schema = json.loads((schema_root / "pdf_primary_corpus_blind_reveal_v1.schema.json").read_text())
        Draft202012Validator.check_schema(split_schema)
        Draft202012Validator.check_schema(reveal_schema)
        split_validator = Draft202012Validator(split_schema)
        split_validator.validate(self.split_payload)

        for mutation in ("negative_state", "held_forbidden_check"):
            bad = copy.deepcopy(self.split_payload)
            if mutation == "negative_state":
                bad["cases"][4]["state_at_selection"] = "gold_ready"
            else:
                bad["cases"][2]["required_check_kinds"] = [
                    "legacy_structural_regression"
                ]
            self.assertTrue(list(split_validator.iter_errors(bad)), mutation)
            with self.assertRaises(PrimaryCorpusSplitError):
                canonical_primary_corpus_split_json(bad)

        # Strict ordering is documented with $comment and enforced by the
        # semantic validator; JSON Schema only enforces set membership here.
        semantic_only = copy.deepcopy(self.split_payload)
        semantic_only["cases"][2]["planned_page_scope"] = [2, 1]
        self.assertFalse(list(split_validator.iter_errors(semantic_only)))
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "strictly increasing"):
            canonical_primary_corpus_split_json(semantic_only)
        semantic_only = copy.deepcopy(self.split_payload)
        semantic_only["cases"][2]["required_check_kinds"] = [
            "heading_relation",
            "paragraph",
        ]
        self.assertFalse(list(split_validator.iter_errors(semantic_only)))
        with self.assertRaisesRegex(
            PrimaryCorpusSplitError, "canonical check-kind order"
        ):
            canonical_primary_corpus_split_json(semantic_only)

        raw = canonical_primary_corpus_split_json(self.split_payload)
        split = parse_primary_corpus_split_bytes(raw)
        self.assertEqual(raw, split.canonical_json())
        with self.assertRaises(TypeError):
            split.payload["cases"][0]["role"] = "held_out"  # type: ignore[index]

        reveal = self.reveal_payload(split)
        Draft202012Validator(reveal_schema).validate(reveal)
        reveal_raw = canonical_primary_corpus_blind_reveal_json(reveal, split)
        verified = parse_primary_corpus_blind_reveal_bytes(reveal_raw, split)
        self.assertEqual(verified.revealed_case["notice_id"], self.blind_case["notice_id"])

    def test_role_cardinality_uniqueness_and_selection_state_are_exact(self) -> None:
        mutations: list[tuple[dict[str, object], str]] = []
        reordered = copy.deepcopy(self.split_payload)
        reordered["cases"][2], reordered["cases"][4] = reordered["cases"][4], reordered["cases"][2]  # type: ignore[index]
        mutations.append((reordered, "canonical public-role order"))
        duplicate = copy.deepcopy(self.split_payload)
        duplicate["cases"][3]["notice_id"] = duplicate["cases"][2]["notice_id"]  # type: ignore[index]
        mutations.append((duplicate, "notice_id values must be unique"))
        wrong_state = copy.deepcopy(self.split_payload)
        wrong_state["cases"][4]["state_at_selection"] = "gold_ready"  # type: ignore[index]
        mutations.append((wrong_state, "negative_control must remain gold_pending"))
        unknown = copy.deepcopy(self.split_payload)
        unknown["unknown"] = True
        mutations.append((unknown, "unexpected keys"))
        for payload, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(
                PrimaryCorpusSplitError, message
            ):
                canonical_primary_corpus_split_json(payload)

    def test_reader_rejects_duplicate_nonfinite_and_noncanonical_json(self) -> None:
        raw = canonical_primary_corpus_split_json(self.split_payload)
        duplicate = raw.replace(
            b'"evaluation_only":true',
            b'"evaluation_only":true,"evaluation_only":true',
            1,
        )
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "duplicate JSON key"):
            parse_primary_corpus_split_bytes(duplicate)
        nonfinite = raw.replace(b'"tuning":1', b'"tuning":NaN', 1)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "non-finite"):
            parse_primary_corpus_split_bytes(nonfinite)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "not canonical"):
            parse_primary_corpus_split_bytes(raw + b"\n")

        split = PrimaryCorpusSplitFixture(self.split_payload)
        reveal_payload = self.reveal_payload(split)
        reveal_raw = canonical_primary_corpus_blind_reveal_json(reveal_payload, split)
        duplicate_reveal = reveal_raw.replace(
            b'"salt_hex":"', b'"salt_hex":"' + BLIND_SALT.encode() + b'","salt_hex":"', 1
        )
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "duplicate JSON key"):
            parse_primary_corpus_blind_reveal_bytes(duplicate_reveal, split)
        nonfinite_reveal = reveal_raw.replace(b'"planned_page_scope":[1,2]', b'"planned_page_scope":[NaN,2]', 1)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "non-finite"):
            parse_primary_corpus_blind_reveal_bytes(nonfinite_reveal, split)
        unknown_reveal = copy.deepcopy(reveal_payload)
        unknown_reveal["unknown"] = True
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "unexpected keys"):
            canonical_primary_corpus_blind_reveal_json(unknown_reveal, split)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "not canonical"):
            parse_primary_corpus_blind_reveal_bytes(reveal_raw + b"\n", split)

    def test_salted_reveal_is_bound_to_split_case_and_canonical_payload(self) -> None:
        split = PrimaryCorpusSplitFixture(self.split_payload)
        reveal = self.reveal_payload(split)
        verified = verify_blind_reveal(reveal, split)
        self.assertEqual(verified.split_canonical_sha256, split.canonical_sha256)

        for field, replacement in (
            ("salt_hex", "cd" * 32),
            ("split_canonical_sha256", "0" * 64),
        ):
            bad = copy.deepcopy(reveal)
            bad[field] = replacement
            with self.subTest(field=field), self.assertRaises(PrimaryCorpusSplitError):
                verify_blind_reveal(bad, split)
        tampered = copy.deepcopy(reveal)
        tampered["revealed_case"]["planned_page_scope"] = [1, 3]  # type: ignore[index]
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "commitment mismatch"):
            verify_blind_reveal(tampered, split)

    def test_source_baseline_binds_every_public_and_revealed_identity(self) -> None:
        split = PrimaryCorpusSplitFixture(self.split_payload)
        reveal = verify_blind_reveal(self.reveal_payload(split), split)
        self.assertEqual(
            validate_split_source_baseline_bytes(split, self.baseline_raw, reveal=reveal),
            sha256(self.baseline_raw).hexdigest(),
        )
        tampered_receipt_payload = self.reveal_payload(split)
        tampered_receipt_payload["salt_hex"] = "cd" * 32
        object.__setattr__(reveal, "payload", tampered_receipt_payload)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "commitment mismatch"):
            validate_split_source_baseline_bytes(
                split, self.baseline_raw, reveal=reveal
            )
        reveal = verify_blind_reveal(self.reveal_payload(split), split)

        baseline = json.loads(self.baseline_raw)
        public_notice = self.public_cases[2]["notice_id"]
        self.mutate_source_digest(baseline, public_notice, digest("wrong"))
        bad_split, bad_raw = self.bind_baseline(baseline)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "does not bind public case"):
            validate_split_source_baseline_bytes(bad_split, bad_raw)

        baseline = json.loads(self.baseline_raw)
        self.mutate_source_digest(
            baseline, self.blind_case["notice_id"], digest("wrong blind")
        )
        rebound, bad_raw = self.bind_baseline(baseline)
        rebound_reveal_payload = self.reveal_payload(rebound)
        # Split digest changes, but the sealed commitment remains valid.
        rebound_reveal = verify_blind_reveal(rebound_reveal_payload, rebound)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "revealed blind case"):
            validate_split_source_baseline_bytes(rebound, bad_raw, reveal=rebound_reveal)

    def test_source_baseline_rejects_fabricated_derived_fields(self) -> None:
        mutations: list[tuple[str, object, str]] = [
            ("count", 5, "counts.all_runs does not match"),
            ("logical_hash", "0" * 64, "logical_ids_sha256 does not match"),
            ("artifact_count", 2, "artifact_count does not match"),
        ]
        for kind, replacement, message in mutations:
            baseline = json.loads(self.baseline_raw)
            if kind == "count":
                baseline["counts"]["all_runs"] = replacement
            elif kind == "logical_hash":
                baseline["pdf_subset"]["logical_ids_sha256"] = replacement
            else:
                baseline["all_runs"][0]["artifact_count"] = replacement
                baseline["pdf_subset"]["runs"][0]["artifact_count"] = replacement
            split, raw = self.bind_baseline(baseline)
            with self.subTest(kind=kind), self.assertRaisesRegex(
                PrimaryCorpusSplitError, message
            ):
                validate_split_source_baseline_bytes(split, raw)

        baseline = json.loads(self.baseline_raw)
        baseline["pdf_subset"]["runs"].pop()
        split, raw = self.bind_baseline(baseline)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "exactly equal filtered all_runs"):
            validate_split_source_baseline_bytes(split, raw)

    def test_source_baseline_rejects_global_source_digest_reuse(self) -> None:
        baseline = json.loads(self.baseline_raw)
        replacement = baseline["all_runs"][0]["source"]["sha256"]
        second_notice = baseline["all_runs"][1]["logical_id"]
        self.mutate_source_digest(baseline, second_notice, replacement)
        split, raw = self.bind_baseline(baseline)
        with self.assertRaisesRegex(PrimaryCorpusSplitError, "repeats a source digest"):
            validate_split_source_baseline_bytes(split, raw)

    def test_pdf_subset_is_independently_type_checked_before_equality(self) -> None:
        mutations = (
            ("structured_profile", "support_components", True),
            ("candidate_pack", "source_blocks", 3.0),
        )
        for artifact_name, count_name, replacement in mutations:
            baseline = json.loads(self.baseline_raw)
            baseline["pdf_subset"]["runs"][0]["artifacts"][artifact_name][
                "counts"
            ][count_name] = replacement
            split, raw = self.bind_baseline(baseline)
            with self.subTest(replacement=replacement), self.assertRaisesRegex(
                PrimaryCorpusSplitError, "must be a non-negative integer"
            ):
                validate_split_source_baseline_bytes(split, raw)

    def test_file_loader_rejects_symlink(self) -> None:
        from common_ir_pipeline.pdf_fusion.primary_corpus_split import load_primary_corpus_split_file

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "split.json"
            target.write_bytes(canonical_primary_corpus_split_json(self.split_payload))
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(PrimaryCorpusSplitError, "non-symlink"):
                load_primary_corpus_split_file(link)

    def test_cli_verifies_binding_without_claiming_gate_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            split_path = root / "split.json"
            baseline_path = root / "baseline.json"
            reveal_path = root / "reveal.json"
            split_path.write_bytes(canonical_primary_corpus_split_json(self.split_payload))
            split = parse_primary_corpus_split_bytes(split_path.read_bytes())
            baseline_path.write_bytes(self.baseline_raw)
            reveal_path.write_bytes(
                canonical_primary_corpus_blind_reveal_json(
                    self.reveal_payload(split), split
                )
            )
            package_root = Path(__file__).resolve().parents[1]
            script = Path(__file__).resolve().parents[3] / "scripts/verify_pdf_primary_corpus_split.py"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--split",
                    str(split_path),
                    "--source-baseline",
                    str(baseline_path),
                    "--expected-split-sha256",
                    split.canonical_sha256,
                    "--reveal",
                    str(reveal_path),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(package_root / "src")},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["status"], "verified")
            self.assertNotIn("quality_gate_status", report)
            self.assertTrue(report["blind_reveal_verified"])

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--split",
                    str(split_path),
                    "--source-baseline",
                    str(baseline_path),
                    "--expected-split-sha256",
                    "0" * 64,
                ],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(package_root / "src")},
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("expected-split-sha256", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
