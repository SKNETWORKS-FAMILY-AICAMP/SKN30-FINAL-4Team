#!/usr/bin/env python3
"""Evaluate composite-candidate structural coverage on a frozen Gold corpus.

This is an offline, read-only canary.  It projects every immutable native
Common-IR occurrence into a synthetic A pack, runs the deterministic
composite generator, and prints aggregate-only JSON.  It does *not* reproduce
the LLM router, compare semantic Profile quality, call a network service, or
write into the Gold directory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker import vendor  # noqa: E402,F401 - installs the reviewed vendor path

from semantic_structuring.common_ir_v1 import (  # noqa: E402
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
    COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
    project_common_ir_v1,
)
from semantic_structuring.composite_candidates import (  # noqa: E402
    COMPOSITE_CANDIDATE_GENERATOR_VERSION,
    FATAL_COMPOSITE_DIAGNOSTIC_CODES,
    generate_composite_candidates,
)
from semantic_structuring.models import (  # noqa: E402
    CandidatePack,
    SourceBlock,
    SourceRelation,
)


REPORT_SCHEMA_VERSION = "existing_composite_shadow_report/v1"
MAX_JSON_ARTIFACT_BYTES = 128 * 1024 * 1024


class CompositeShadowEvaluationError(ValueError):
    """A frozen-corpus or evaluation invariant was not satisfied."""


def _required_sha256(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompositeShadowEvaluationError(f"{label} manifest SHA-256 is invalid")
    return value


def _load_object(
    path: Path, *, label: str, expected_sha256: object | None = None
) -> dict[str, Any]:
    try:
        # Reject known-oversized artifacts before allocating their full
        # contents.  The post-read check remains necessary because the file
        # can change between stat and read.
        if path.stat().st_size > MAX_JSON_ARTIFACT_BYTES:
            raise CompositeShadowEvaluationError(f"{label} exceeds the read cap")
        payload = path.read_bytes()
        if len(payload) > MAX_JSON_ARTIFACT_BYTES:
            raise CompositeShadowEvaluationError(f"{label} exceeds the read cap")
        if expected_sha256 is not None:
            expected_sha256 = _required_sha256(expected_sha256, label=label)
            if sha256(payload).hexdigest() != expected_sha256:
                raise CompositeShadowEvaluationError(f"{label} SHA-256 mismatch")
        value = json.loads(payload)
    except CompositeShadowEvaluationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompositeShadowEvaluationError(f"cannot read {label}: {path.name}") from error
    if not isinstance(value, dict):
        raise CompositeShadowEvaluationError(f"{label} must be a JSON object")
    return value


def _corpus_member(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise CompositeShadowEvaluationError("Common IR manifest path is missing")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise CompositeShadowEvaluationError(
            "Common IR manifest path escapes the Gold root"
        ) from error
    if not candidate.is_file():
        raise CompositeShadowEvaluationError(
            f"Common IR manifest member is missing: {relative}"
        )
    return candidate


def _native_projection_pack(document: dict[str, Any], notice_id: str) -> CandidatePack:
    """Build a deterministic all-native upper-bound pack for structural canary use."""

    try:
        projection = project_common_ir_v1(document)
    except (KeyError, TypeError, ValueError) as error:
        raise CompositeShadowEvaluationError(
            f"Common IR native projection failed: {notice_id}"
        ) from error
    if projection.notice_id != notice_id:
        raise CompositeShadowEvaluationError(
            f"manifest/Common IR notice mismatch: {notice_id}"
        )
    blocks: list[SourceBlock] = []
    for block in sorted(
        document.get("blocks") or [], key=lambda item: item.get("reading_order", 0)
    ):
        if not isinstance(block, dict) or block.get("kind") in {
            "table",
            "table_candidate",
        }:
            continue
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            continue
        occurrence_text = {
            item.get("occurrence_id"): item.get("text")
            for item in block.get("occurrences") or []
            if isinstance(item, dict)
            and isinstance(item.get("occurrence_id"), str)
            and isinstance(item.get("text"), str)
        }
        for ordinal, occurrence_id in enumerate(block.get("text_occurrence_ids") or []):
            text = occurrence_text.get(occurrence_id)
            if not isinstance(text, str) or not text.strip():
                continue
            blocks.append(
                SourceBlock(
                    block_id=f"native:{block_id}:{ordinal}",
                    text=text,
                    relation=SourceRelation.CANDIDATE,
                    block_kind=block.get("kind"),
                    source_order=block.get("reading_order"),
                    source_occurrence_ids=[occurrence_id],
                    common_ir_block_id=block_id,
                    common_ir_occurrence_ids=(occurrence_id,),
                )
            )
    # Explicit table cells already have one immutable occurrence per source
    # block.  Inferred/partial tables deliberately contribute no source atom;
    # the generator reports their structural diagnostic from Common IR.
    blocks.extend(projection.table_cell_blocks)
    return CandidatePack(
        pack_id=f"{notice_id}-native-composite-shadow-v1",
        notice_id=notice_id,
        extraction_scope="candidate_pack",
        question=(
            "Offline read-only structural canary over immutable native "
            "Common IR occurrences."
        ),
        blocks=blocks,
        generator=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR,
        generator_version=COMMON_IR_V1_CANDIDATE_PACK_GENERATOR_VERSION,
        common_ir_document_id=projection.common_ir_document_id,
    )


def evaluate_gold_corpus(
    gold_root: Path,
    *,
    notice_ids: set[str] | None = None,
    expected_notice_count: int | None = None,
    include_notices: bool = False,
) -> dict[str, Any]:
    """Return an aggregate-only report without mutating corpus or runtime state."""

    root = gold_root.resolve()
    if not root.is_dir():
        raise CompositeShadowEvaluationError("Gold root does not exist")
    freeze_manifest = _load_object(
        _corpus_member(root, "freeze_manifest.json"), label="freeze manifest"
    )
    dataset_version = freeze_manifest.get("dataset_version")
    if (
        not isinstance(dataset_version, str)
        or not dataset_version
        or len(dataset_version) > 200
        or any(ord(character) < 32 for character in dataset_version)
    ):
        raise CompositeShadowEvaluationError("freeze manifest dataset_version is invalid")
    manifest = _load_object(
        _corpus_member(root, "profile_manifest.json"),
        label="profile manifest",
        expected_sha256=_required_sha256(
            freeze_manifest.get("profile_manifest_sha256"),
            label="profile manifest",
        ),
    )
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise CompositeShadowEvaluationError("profile manifest has no profiles")
    frozen_notice_count = freeze_manifest.get("notice_count")
    if type(frozen_notice_count) is not int or frozen_notice_count != len(profiles):
        raise CompositeShadowEvaluationError(
            "freeze/profile manifest notice count mismatch"
        )

    selected: list[tuple[str, Path, object]] = []
    seen: set[str] = set()
    for item in profiles:
        if not isinstance(item, dict):
            raise CompositeShadowEvaluationError("profile manifest entry must be an object")
        notice_id = item.get("pblanc_id")
        if not isinstance(notice_id, str) or not notice_id:
            raise CompositeShadowEvaluationError("profile manifest notice id is missing")
        if notice_id in seen:
            raise CompositeShadowEvaluationError(f"duplicate profile manifest notice: {notice_id}")
        seen.add(notice_id)
        if notice_ids is not None and notice_id not in notice_ids:
            continue
        frozen = item.get("frozen")
        common_ir = frozen.get("common_ir") if isinstance(frozen, dict) else None
        relative_path = common_ir.get("path") if isinstance(common_ir, dict) else None
        artifact_sha256 = common_ir.get("sha256") if isinstance(common_ir, dict) else None
        selected.append(
            (
                notice_id,
                _corpus_member(root, relative_path),
                _required_sha256(artifact_sha256, label="Common IR"),
            )
        )

    if notice_ids is not None:
        missing = sorted(notice_ids - {notice_id for notice_id, _, _ in selected})
        if missing:
            raise CompositeShadowEvaluationError(
                f"requested notices are absent from the manifest: {', '.join(missing)}"
            )
    if expected_notice_count is not None and len(selected) != expected_notice_count:
        raise CompositeShadowEvaluationError(
            f"expected {expected_notice_count} notices, found {len(selected)}"
        )

    candidate_kinds: Counter[str] = Counter()
    diagnostic_codes: Counter[str] = Counter()
    per_notice: list[dict[str, Any]] = []
    cross_block_count = 0
    fatal_notice_count = 0
    for notice_id, common_ir_path, artifact_sha256 in sorted(selected):
        document = _load_object(
            common_ir_path,
            label="Common IR",
            expected_sha256=artifact_sha256,
        )
        pack = _native_projection_pack(document, notice_id)
        generation = generate_composite_candidates(document, pack)
        local_kinds = Counter(candidate.kind for candidate in generation.candidates)
        local_diagnostics = Counter(item.code for item in generation.diagnostics)
        local_fatal_codes = sorted(
            set(local_diagnostics).intersection(FATAL_COMPOSITE_DIAGNOSTIC_CODES)
        )
        fatal_notice_count += bool(local_fatal_codes)
        local_cross_block_count = sum(
            len({atom.common_ir_block_id for atom in candidate.atoms}) != 1
            for candidate in generation.candidates
        )
        cross_block_count += local_cross_block_count
        candidate_kinds.update(local_kinds)
        diagnostic_codes.update(local_diagnostics)
        per_notice.append(
            {
                "notice_id": notice_id,
                "native_pack_block_count": len(pack.blocks),
                "candidate_count": len(generation.candidates),
                "candidate_kind_counts": dict(sorted(local_kinds.items())),
                "diagnostic_code_counts": dict(sorted(local_diagnostics.items())),
                "fatal_diagnostic_codes": local_fatal_codes,
                "cross_common_ir_block_candidate_count": local_cross_block_count,
            }
        )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": (
            "valid"
            if cross_block_count == 0 and fatal_notice_count == 0
            else "invalid"
        ),
        "dataset_version": dataset_version,
        "generator_version": COMPOSITE_CANDIDATE_GENERATOR_VERSION,
        "evaluation_scope": "all_native_projection_upper_bound",
        "limitations": [
            "does_not_reproduce_llm_a_routing",
            "does_not_measure_profile_semantic_accuracy",
            "top_left_span_header_band_is_shadow_heuristic",
        ],
        "notice_count": len(per_notice),
        "notice_with_candidate_count": sum(
            item["candidate_count"] > 0 for item in per_notice
        ),
        "candidate_count": sum(candidate_kinds.values()),
        "candidate_kind_counts": dict(sorted(candidate_kinds.items())),
        "diagnostic_code_counts": dict(sorted(diagnostic_codes.items())),
        "invariants": {
            "cross_common_ir_block_candidate_count": cross_block_count,
            "fatal_diagnostic_notice_count": fatal_notice_count,
        },
    }
    if include_notices:
        report["notices"] = per_notice
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-root", required=True, type=Path)
    parser.add_argument(
        "--notice-id",
        action="append",
        default=None,
        help="PBLN id to evaluate; repeat to select multiple notices",
    )
    parser.add_argument("--expected-notice-count", type=int)
    parser.add_argument(
        "--include-notices",
        action="store_true",
        help="include aggregate counts for each notice (never source text or candidate ids)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.expected_notice_count is not None and args.expected_notice_count < 1:
        raise SystemExit("--expected-notice-count must be positive")
    try:
        report = evaluate_gold_corpus(
            args.gold_root,
            notice_ids=set(args.notice_id) if args.notice_id else None,
            expected_notice_count=args.expected_notice_count,
            include_notices=args.include_notices,
        )
    except CompositeShadowEvaluationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
