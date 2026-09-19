"""Immutable, evaluation-only split contract for the A4.5 PDF corpus gate.

The split says *which* documents may be used for tuning and evaluation.  It
does not contain scores and can never produce a passing quality verdict.  A
separate gate report must consume a fully ready, source-baseline-bound split.

One held-out identity is committed before it is revealed.  Its commitment is
the SHA-256 of a domain separator, a random salt, and the canonical JSON bytes
of the revealed public-case object.  This prevents a later choice of a more
convenient held-out document while keeping its identity absent from the split.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "pdf_primary_corpus_split/v1"
REVEAL_SCHEMA_VERSION = "pdf_primary_corpus_blind_reveal/v1"
SOURCE_BASELINE_SCHEMA_VERSION = "pdf_fusion_corpus_baseline/v1"
STANDALONE_VALIDATION_SCOPE = "internal_consistency_only"
COMMITMENT_ALGORITHM = "sha256_salted_canonical_json_v1"
COMMITMENT_DOMAIN = b"pre-review/pdf-primary-corpus-blind-case/v1"

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_BASELINE_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 50_000
MAX_BASELINE_JSON_NODES = 2_000_000
MAX_STRING_BYTES = 4 * 1024
MAX_PAGES = 256

ROLE_CARDINALITY = MappingProxyType(
    {
        "tuning": 1,
        "known_regression": 1,
        "held_out": 2,
        "sealed_blind": 1,
        "negative_control": 1,
    }
)
SUPPORTED_SLICES = (
    "two_occurrence_paragraph",
    "numbered_single_line_heading_to_next_paragraph",
    "unit_span_table_grid",
    "adjacent_page_between_rows_continuation",
)
CHECK_KIND_ORDER = (
    "paragraph",
    "heading_relation",
    "table_grid",
    "table_continuation",
    "legacy_structural_regression",
    "table_grid_negative",
    "table_continuation_negative",
)
CHECK_KINDS = frozenset(CHECK_KIND_ORDER)
PUBLIC_ROLES = frozenset(
    {"tuning", "known_regression", "held_out", "negative_control"}
)
READINESS_VALUES = frozenset(
    {
        "gold_ready",
        "gold_pending",
        "legacy_migration_required",
        "sealed_unrevealed",
    }
)
_REVEAL_CONSTRUCTION_TOKEN = object()

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_NOTICE_ID = re.compile(r"^PBLN_[0-9]{15}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_CORPUS_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SALT = re.compile(r"^[0-9a-f]{64,128}$")

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "evaluation_only",
        "non_promotable",
        "standalone_validation_scope",
        "selection_boundary_commit",
        "source_baseline",
        "policy",
        "cases",
        "sealed_case",
    }
)
_BASELINE_BINDING_KEYS = frozenset(
    {"schema_version", "corpus_id", "canonical_sha256"}
)
_POLICY_KEYS = frozenset({"role_cardinality", "current_supported_slices"})
_CASE_KEYS = frozenset(
    {
        "case_id",
        "role",
        "notice_id",
        "source_pdf_sha256",
        "planned_page_scope",
        "full_native_capture_required",
        "scope_claim",
        "state_at_selection",
        "required_check_kinds",
    }
)
_SEALED_KEYS = frozenset(
    {"case_id", "role", "state_at_selection", "commitment"}
)
_COMMITMENT_KEYS = frozenset({"algorithm", "digest"})
_REVEAL_KEYS = frozenset(
    {
        "schema_version",
        "split_canonical_sha256",
        "sealed_case_id",
        "salt_hex",
        "revealed_case",
    }
)
_BASELINE_ROOT_KEYS = frozenset(
    {"schema_version", "corpus_id", "counts", "all_runs", "pdf_subset"}
)
_BASELINE_COUNTS_KEYS = frozenset(
    {"all_runs", "pdf_runs", "extensions", "artifacts"}
)
_BASELINE_PDF_SUBSET_KEYS = frozenset({"logical_ids_sha256", "runs"})
_BASELINE_RUN_KEYS = frozenset(
    {"logical_id", "source", "artifact_count", "artifacts"}
)
_BASELINE_SOURCE_KEYS = frozenset({"path", "extension", "sha256"})
_BASELINE_ARTIFACT_KEYS = frozenset({"path", "sha256", "counts"})
_BASELINE_ARTIFACT_NAMES = (
    "candidate_pack",
    "common_ir",
    "structured_profile",
)
_BASELINE_ARTIFACT_COUNT_KEYS = MappingProxyType(
    {
        "candidate_pack": frozenset({"source_blocks"}),
        "common_ir": frozenset({"blocks", "relations"}),
        "structured_profile": frozenset(
            {"comparison_facts", "support_components"}
        ),
    }
)
_BASELINE_EXTENSIONS = frozenset({"pdf", "hwp", "hwpx"})


class PrimaryCorpusSplitError(ValueError):
    """Raised when a corpus split or blind reveal is unsafe or ambiguous."""


def _exact_keys(
    value: object, expected: frozenset[str], *, name: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryCorpusSplitError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise PrimaryCorpusSplitError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise PrimaryCorpusSplitError(
            f"{name} keys are invalid ({'; '.join(detail)})"
        )
    return value


def _string(
    name: str,
    value: object,
    *,
    maximum: int = MAX_STRING_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PrimaryCorpusSplitError(f"{name} must be a string")
    if (not allow_empty and not value) or value != value.strip():
        raise PrimaryCorpusSplitError(f"{name} must be a trimmed string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PrimaryCorpusSplitError(
            f"{name} must not contain surrogate code points"
        ) from error
    if len(encoded) > maximum or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise PrimaryCorpusSplitError(f"{name} exceeds its string safety cap")
    return value


def _const(name: str, value: object, expected: object) -> None:
    if type(value) is not type(expected) or value != expected:
        raise PrimaryCorpusSplitError(f"{name} must equal {expected!r}")


def _sha(name: str, value: object) -> str:
    checked = _string(name, value, maximum=64)
    if _SHA256.fullmatch(checked) is None:
        raise PrimaryCorpusSplitError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return checked


def _identifier(name: str, value: object) -> str:
    checked = _string(name, value, maximum=128)
    if _IDENTIFIER.fullmatch(checked) is None:
        raise PrimaryCorpusSplitError(f"{name} is not a bounded identifier")
    return checked


def _assert_json_limits(
    value: object,
    *,
    maximum_nodes: int = MAX_JSON_NODES,
    depth: int = 1,
    counter: list[int] | None = None,
) -> None:
    counter = [0] if counter is None else counter
    counter[0] += 1
    if counter[0] > maximum_nodes:
        raise PrimaryCorpusSplitError("JSON exceeds the node safety cap")
    if depth > MAX_JSON_DEPTH:
        raise PrimaryCorpusSplitError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PrimaryCorpusSplitError("JSON contains a non-finite number")
        return
    if isinstance(value, str):
        _string("JSON string", value, allow_empty=True)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _string("JSON object key", key)
            _assert_json_limits(
                nested,
                maximum_nodes=maximum_nodes,
                depth=depth + 1,
                counter=counter,
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_json_limits(
                nested,
                maximum_nodes=maximum_nodes,
                depth=depth + 1,
                counter=counter,
            )
        return
    raise PrimaryCorpusSplitError(
        f"JSON contains unsupported type {type(value).__name__}"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain(nested) for nested in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(nested) for nested in value)
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    _assert_json_limits(value)
    try:
        return (
            json.dumps(
                _plain(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PrimaryCorpusSplitError(
            "artifact is not canonically serializable UTF-8 JSON"
        ) from error


def _canonical_check_kinds(value: object, *, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise PrimaryCorpusSplitError(f"{name} must be a non-empty array")
    if len(value) > len(CHECK_KIND_ORDER):
        raise PrimaryCorpusSplitError(f"{name} exceeds the check-kind cap")
    checked = [_string(f"{name}[]", item, maximum=64) for item in value]
    if any(item not in CHECK_KINDS for item in checked):
        raise PrimaryCorpusSplitError(f"{name} contains an unsupported check kind")
    expected = [item for item in CHECK_KIND_ORDER if item in checked]
    if checked != expected or len(set(checked)) != len(checked):
        raise PrimaryCorpusSplitError(
            f"{name} must be unique and in canonical check-kind order"
        )
    return checked


def _validate_public_case(value: object, *, name: str) -> dict[str, Any]:
    case = _exact_keys(value, _CASE_KEYS, name=name)
    case_id = _identifier(f"{name}.case_id", case["case_id"])
    role = _string(f"{name}.role", case["role"], maximum=32)
    if role not in PUBLIC_ROLES:
        raise PrimaryCorpusSplitError(f"{name}.role is unsupported")
    notice_id = _string(f"{name}.notice_id", case["notice_id"], maximum=20)
    if _NOTICE_ID.fullmatch(notice_id) is None:
        raise PrimaryCorpusSplitError(f"{name}.notice_id is invalid")
    source_pdf_sha256 = _sha(
        f"{name}.source_pdf_sha256", case["source_pdf_sha256"]
    )
    pages = case["planned_page_scope"]
    if not isinstance(pages, (list, tuple)) or not pages or len(pages) > MAX_PAGES:
        raise PrimaryCorpusSplitError(
            f"{name}.planned_page_scope must be a non-empty bounded array"
        )
    planned_page_scope: list[int] = []
    for page in pages:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGES:
            raise PrimaryCorpusSplitError(
                f"{name}.planned_page_scope contains an invalid physical page"
            )
        planned_page_scope.append(page)
    if planned_page_scope != sorted(set(planned_page_scope)):
        raise PrimaryCorpusSplitError(
            f"{name}.planned_page_scope must be unique and strictly increasing"
        )
    _const(
        f"{name}.full_native_capture_required",
        case["full_native_capture_required"],
        True,
    )
    _const(f"{name}.scope_claim", case["scope_claim"], "reviewed_regions_only")
    state_at_selection = _string(
        f"{name}.state_at_selection", case["state_at_selection"], maximum=32
    )
    if state_at_selection not in READINESS_VALUES or state_at_selection == "sealed_unrevealed":
        raise PrimaryCorpusSplitError(f"{name}.state_at_selection is unsupported")
    checks = _canonical_check_kinds(
        case["required_check_kinds"], name=f"{name}.required_check_kinds"
    )

    if role == "tuning" and state_at_selection != "gold_ready":
        raise PrimaryCorpusSplitError("the tuning case must be gold_ready")
    if role == "known_regression":
        if state_at_selection not in {"gold_ready", "legacy_migration_required"}:
            raise PrimaryCorpusSplitError(
                "the known_regression case has an invalid selection state"
            )
        if "legacy_structural_regression" not in checks:
            raise PrimaryCorpusSplitError(
                "the known_regression case requires legacy_structural_regression"
            )
    elif "legacy_structural_regression" in checks:
        raise PrimaryCorpusSplitError(
            "legacy_structural_regression is limited to known_regression"
        )
    negative_kinds = {"table_grid_negative", "table_continuation_negative"}
    if role == "negative_control":
        if state_at_selection != "gold_pending" or set(checks) != negative_kinds:
            raise PrimaryCorpusSplitError(
                "negative_control must remain gold_pending with both negative checks"
            )
    elif negative_kinds.intersection(checks):
        raise PrimaryCorpusSplitError(
            "negative check kinds are limited to negative_control"
        )
    if role == "held_out" and state_at_selection not in {"gold_ready", "gold_pending"}:
        raise PrimaryCorpusSplitError("held_out selection state is invalid")

    return {
        "case_id": case_id,
        "role": role,
        "notice_id": notice_id,
        "source_pdf_sha256": source_pdf_sha256,
        "planned_page_scope": planned_page_scope,
        "full_native_capture_required": True,
        "scope_claim": "reviewed_regions_only",
        "state_at_selection": state_at_selection,
        "required_check_kinds": checks,
    }


def validate_primary_corpus_split(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact split shape, cardinality, ordering, and uniqueness."""

    _assert_json_limits(value)
    root = _exact_keys(value, _ROOT_KEYS, name="corpus split")
    _const("schema_version", root["schema_version"], SCHEMA_VERSION)
    _const("evaluation_only", root["evaluation_only"], True)
    _const("non_promotable", root["non_promotable"], True)
    _const(
        "standalone_validation_scope",
        root["standalone_validation_scope"],
        STANDALONE_VALIDATION_SCOPE,
    )
    boundary = _string(
        "selection_boundary_commit", root["selection_boundary_commit"], maximum=40
    )
    if _COMMIT.fullmatch(boundary) is None:
        raise PrimaryCorpusSplitError(
            "selection_boundary_commit must be a lowercase Git commit SHA-1"
        )

    source = _exact_keys(
        root["source_baseline"], _BASELINE_BINDING_KEYS, name="source_baseline"
    )
    _const(
        "source_baseline.schema_version",
        source["schema_version"],
        SOURCE_BASELINE_SCHEMA_VERSION,
    )
    corpus_id = _string("source_baseline.corpus_id", source["corpus_id"], maximum=192)
    if _CORPUS_ID.fullmatch(corpus_id) is None:
        raise PrimaryCorpusSplitError("source_baseline.corpus_id is invalid")
    baseline_sha256 = _sha(
        "source_baseline.canonical_sha256", source["canonical_sha256"]
    )

    policy = _exact_keys(root["policy"], _POLICY_KEYS, name="policy")
    cardinality = _exact_keys(
        policy["role_cardinality"],
        frozenset(ROLE_CARDINALITY),
        name="policy.role_cardinality",
    )
    for role, expected in ROLE_CARDINALITY.items():
        _const(f"policy.role_cardinality.{role}", cardinality[role], expected)
    slices = policy["current_supported_slices"]
    if not isinstance(slices, (list, tuple)) or tuple(slices) != SUPPORTED_SLICES:
        raise PrimaryCorpusSplitError(
            "policy.current_supported_slices must equal the v1 supported-slice order"
        )

    values = root["cases"]
    if not isinstance(values, (list, tuple)) or len(values) != 5:
        raise PrimaryCorpusSplitError("cases must contain exactly five public cases")
    cases = [
        _validate_public_case(item, name=f"cases[{index}]")
        for index, item in enumerate(values)
    ]
    public_role_order = ["tuning", "known_regression", "held_out", "held_out", "negative_control"]
    if [case["role"] for case in cases] != public_role_order:
        raise PrimaryCorpusSplitError("cases must be in canonical public-role order")

    sealed = _exact_keys(root["sealed_case"], _SEALED_KEYS, name="sealed_case")
    sealed_case_id = _identifier("sealed_case.case_id", sealed["case_id"])
    _const("sealed_case.role", sealed["role"], "sealed_blind")
    _const(
        "sealed_case.state_at_selection",
        sealed["state_at_selection"],
        "sealed_unrevealed",
    )
    commitment = _exact_keys(
        sealed["commitment"], _COMMITMENT_KEYS, name="sealed_case.commitment"
    )
    _const(
        "sealed_case.commitment.algorithm",
        commitment["algorithm"],
        COMMITMENT_ALGORITHM,
    )
    commitment_digest = _sha(
        "sealed_case.commitment.digest", commitment["digest"]
    )

    case_ids = [case["case_id"] for case in cases] + [sealed_case_id]
    if len(case_ids) != len(set(case_ids)):
        raise PrimaryCorpusSplitError("case_id values must be globally unique")
    notice_ids = [case["notice_id"] for case in cases]
    if len(notice_ids) != len(set(notice_ids)):
        raise PrimaryCorpusSplitError("public notice_id values must be unique")
    source_digests = [case["source_pdf_sha256"] for case in cases]
    if len(source_digests) != len(set(source_digests)):
        raise PrimaryCorpusSplitError("public source PDF digests must be unique")

    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_only": True,
        "non_promotable": True,
        "standalone_validation_scope": STANDALONE_VALIDATION_SCOPE,
        "selection_boundary_commit": boundary,
        "source_baseline": {
            "schema_version": SOURCE_BASELINE_SCHEMA_VERSION,
            "corpus_id": corpus_id,
            "canonical_sha256": baseline_sha256,
        },
        "policy": {
            "role_cardinality": dict(ROLE_CARDINALITY),
            "current_supported_slices": list(SUPPORTED_SLICES),
        },
        "cases": cases,
        "sealed_case": {
            "case_id": sealed_case_id,
            "role": "sealed_blind",
            "state_at_selection": "sealed_unrevealed",
            "commitment": {
                "algorithm": COMMITMENT_ALGORITHM,
                "digest": commitment_digest,
            },
        },
    }


def canonical_primary_corpus_split_json(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(validate_primary_corpus_split(value))


def canonical_blind_case_json(value: Mapping[str, Any]) -> bytes:
    """Canonicalize the public held-out case committed by a blind reveal."""

    checked = _validate_public_case(value, name="blind case")
    if checked["role"] != "held_out":
        raise PrimaryCorpusSplitError("a blind case must reveal as held_out")
    return _canonical_bytes(checked)


def compute_blind_case_commitment(value: Mapping[str, Any], salt_hex: str) -> str:
    """Return the domain-separated salted commitment for one held-out case."""

    salt = _string("salt_hex", salt_hex, maximum=128)
    if _SALT.fullmatch(salt) is None or len(salt) % 2:
        raise PrimaryCorpusSplitError(
            "salt_hex must contain 32 to 64 bytes of lowercase hexadecimal salt"
        )
    material = (
        COMMITMENT_DOMAIN
        + b"\0"
        + bytes.fromhex(salt)
        + b"\0"
        + canonical_blind_case_json(value)
    )
    return sha256(material).hexdigest()


def validate_blind_reveal(
    value: Mapping[str, Any],
    split: "PrimaryCorpusSplitFixture | Mapping[str, Any]",
) -> dict[str, Any]:
    """Verify a reveal against the exact canonical split and commitment."""

    fixture = split if isinstance(split, PrimaryCorpusSplitFixture) else PrimaryCorpusSplitFixture(split)
    _assert_json_limits(value)
    reveal = _exact_keys(value, _REVEAL_KEYS, name="blind reveal")
    _const("blind reveal.schema_version", reveal["schema_version"], REVEAL_SCHEMA_VERSION)
    split_digest = _sha(
        "blind reveal.split_canonical_sha256", reveal["split_canonical_sha256"]
    )
    if split_digest != fixture.canonical_sha256:
        raise PrimaryCorpusSplitError("blind reveal does not bind the supplied split")
    sealed_case_id = _identifier(
        "blind reveal.sealed_case_id", reveal["sealed_case_id"]
    )
    if sealed_case_id != fixture.payload["sealed_case"]["case_id"]:
        raise PrimaryCorpusSplitError("blind reveal sealed_case_id does not match")
    salt_hex = _string("blind reveal.salt_hex", reveal["salt_hex"], maximum=128)
    revealed_case = _validate_public_case(
        reveal["revealed_case"], name="blind reveal.revealed_case"
    )
    if revealed_case["role"] != "held_out":
        raise PrimaryCorpusSplitError("blind reveal must contain a held_out case")
    expected = compute_blind_case_commitment(revealed_case, salt_hex)
    if expected != fixture.payload["sealed_case"]["commitment"]["digest"]:
        raise PrimaryCorpusSplitError("blind reveal commitment mismatch")
    public_cases = fixture.payload["cases"]
    if revealed_case["case_id"] != sealed_case_id:
        raise PrimaryCorpusSplitError("revealed case_id must equal sealed_case_id")
    if any(revealed_case["notice_id"] == case["notice_id"] for case in public_cases):
        raise PrimaryCorpusSplitError("revealed notice_id duplicates a public case")
    if any(
        revealed_case["source_pdf_sha256"] == case["source_pdf_sha256"]
        for case in public_cases
    ):
        raise PrimaryCorpusSplitError("revealed source PDF duplicates a public case")
    return {
        "schema_version": REVEAL_SCHEMA_VERSION,
        "split_canonical_sha256": split_digest,
        "sealed_case_id": sealed_case_id,
        "salt_hex": salt_hex,
        "revealed_case": revealed_case,
    }


def canonical_primary_corpus_blind_reveal_json(
    value: Mapping[str, Any],
    split: "PrimaryCorpusSplitFixture | Mapping[str, Any]",
) -> bytes:
    return _canonical_bytes(validate_blind_reveal(value, split))


@dataclass(frozen=True, slots=True)
class PrimaryCorpusSplitFixture:
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(validate_primary_corpus_split(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()

@dataclass(frozen=True, slots=True, init=False)
class VerifiedBlindReveal:
    payload: Mapping[str, Any]
    split_canonical_sha256: str

    def __init__(
        self,
        payload: Mapping[str, Any],
        split_canonical_sha256: str,
        *,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _REVEAL_CONSTRUCTION_TOKEN:
            raise PrimaryCorpusSplitError(
                "blind reveal receipt requires verified internal construction"
            )
        object.__setattr__(self, "payload", _freeze(_plain(payload)))
        object.__setattr__(self, "split_canonical_sha256", split_canonical_sha256)

    @property
    def revealed_case(self) -> Mapping[str, Any]:
        return self.payload["revealed_case"]


def verify_blind_reveal(
    reveal: Mapping[str, Any], split: PrimaryCorpusSplitFixture
) -> VerifiedBlindReveal:
    checked = validate_blind_reveal(reveal, split)
    return VerifiedBlindReveal(
        checked,
        split.canonical_sha256,
        _construction_token=_REVEAL_CONSTRUCTION_TOKEN,
    )


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise PrimaryCorpusSplitError(f"artifact contains duplicate JSON key {key!r}")
        value[key] = nested
    return value


def _reject_constant(value: str) -> None:
    raise PrimaryCorpusSplitError(f"artifact contains non-finite JSON number {value!r}")


def _decode_json(raw: bytes, *, maximum: int, name: str) -> Mapping[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise PrimaryCorpusSplitError(f"{name} exceeds the artifact byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise PrimaryCorpusSplitError(f"{name} must not include a UTF-8 BOM")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_key_rejector,
            parse_constant=_reject_constant,
        )
    except PrimaryCorpusSplitError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PrimaryCorpusSplitError(f"{name} is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise PrimaryCorpusSplitError(f"{name} root must be an object")
    return value


def parse_primary_corpus_split_bytes(raw: bytes) -> PrimaryCorpusSplitFixture:
    value = _decode_json(raw, maximum=MAX_ARTIFACT_BYTES, name="corpus split")
    fixture = PrimaryCorpusSplitFixture(value)
    if raw != fixture.canonical_json():
        raise PrimaryCorpusSplitError("corpus split bytes are not canonical UTF-8 JSON")
    return fixture


def parse_primary_corpus_blind_reveal_bytes(
    raw: bytes, split: PrimaryCorpusSplitFixture
) -> VerifiedBlindReveal:
    value = _decode_json(raw, maximum=MAX_ARTIFACT_BYTES, name="blind reveal")
    checked = validate_blind_reveal(value, split)
    canonical = _canonical_bytes(checked)
    if raw != canonical:
        raise PrimaryCorpusSplitError("blind reveal bytes are not canonical UTF-8 JSON")
    return VerifiedBlindReveal(
        checked,
        split.canonical_sha256,
        _construction_token=_REVEAL_CONSTRUCTION_TOKEN,
    )


def _read_regular_file(path: str | Path, *, maximum: int, name: str) -> bytes:
    candidate = Path(path)
    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise PrimaryCorpusSplitError(f"{name} must be a regular non-symlink file")
        if before.st_size > maximum:
            raise PrimaryCorpusSplitError(f"{name} exceeds the artifact byte cap")
        descriptor = os.open(
            candidate, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity(opened) != identity(before)
            or identity(after) != identity(opened)
            or len(raw) != opened.st_size
            or len(raw) > maximum
        ):
            raise PrimaryCorpusSplitError(f"{name} changed while reading")
        return raw
    except PrimaryCorpusSplitError:
        raise
    except OSError as error:
        raise PrimaryCorpusSplitError(f"{name} cannot be safely opened") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_primary_corpus_split_file(path: str | Path) -> PrimaryCorpusSplitFixture:
    return parse_primary_corpus_split_bytes(
        _read_regular_file(path, maximum=MAX_ARTIFACT_BYTES, name="corpus split")
    )


def load_primary_corpus_blind_reveal_file(
    path: str | Path, split: PrimaryCorpusSplitFixture
) -> VerifiedBlindReveal:
    return parse_primary_corpus_blind_reveal_bytes(
        _read_regular_file(path, maximum=MAX_ARTIFACT_BYTES, name="blind reveal"),
        split,
    )


def _baseline_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PrimaryCorpusSplitError(
            f"{name} must be a non-negative integer"
        )
    return value


def _baseline_relative_path(name: str, value: object) -> str:
    checked = _string(name, value, maximum=1_024)
    if "\\" in checked:
        raise PrimaryCorpusSplitError(f"{name} must use POSIX separators")
    pure = PurePosixPath(checked)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != checked
    ):
        raise PrimaryCorpusSplitError(f"{name} must be a canonical relative path")
    return checked


def _validate_baseline_artifact(
    value: object,
    *,
    logical_id: str,
    artifact_name: str,
    label: str,
) -> dict[str, Any]:
    artifact = _exact_keys(value, _BASELINE_ARTIFACT_KEYS, name=label)
    path = _baseline_relative_path(f"{label}.path", artifact["path"])
    if PurePosixPath(path).parts[0] != logical_id:
        raise PrimaryCorpusSplitError(
            f"{label}.path must remain below its logical_id"
        )
    digest = _sha(f"{label}.sha256", artifact["sha256"])
    expected_count_keys = _BASELINE_ARTIFACT_COUNT_KEYS[artifact_name]
    raw_counts = _exact_keys(
        artifact["counts"], expected_count_keys, name=f"{label}.counts"
    )
    counts = {
        key: _baseline_count(f"{label}.counts.{key}", raw_counts[key])
        for key in sorted(expected_count_keys)
    }
    return {"path": path, "sha256": digest, "counts": counts}


def _validate_baseline_run(
    value: object, *, index: int, collection: str = "all_runs"
) -> dict[str, Any]:
    label = f"source baseline {collection}[{index}]"
    run = _exact_keys(value, _BASELINE_RUN_KEYS, name=label)
    logical_id = _string(f"{label}.logical_id", run["logical_id"], maximum=192)
    if _CORPUS_ID.fullmatch(logical_id) is None:
        raise PrimaryCorpusSplitError(f"{label}.logical_id is invalid")
    source = _exact_keys(run["source"], _BASELINE_SOURCE_KEYS, name=f"{label}.source")
    source_path = _baseline_relative_path(f"{label}.source.path", source["path"])
    extension = _string(f"{label}.source.extension", source["extension"], maximum=4)
    if extension not in _BASELINE_EXTENSIONS:
        raise PrimaryCorpusSplitError(f"{label}.source.extension is unsupported")
    if PurePosixPath(source_path).suffix.lower() != f".{extension}":
        raise PrimaryCorpusSplitError(
            f"{label}.source extension disagrees with its path"
        )
    source_sha256 = _sha(f"{label}.source.sha256", source["sha256"])
    artifacts = _exact_keys(
        run["artifacts"], frozenset(_BASELINE_ARTIFACT_NAMES), name=f"{label}.artifacts"
    )
    canonical_artifacts = {
        name: _validate_baseline_artifact(
            artifacts[name],
            logical_id=logical_id,
            artifact_name=name,
            label=f"{label}.artifacts.{name}",
        )
        for name in _BASELINE_ARTIFACT_NAMES
    }
    artifact_count = _baseline_count(f"{label}.artifact_count", run["artifact_count"])
    if artifact_count != len(canonical_artifacts):
        raise PrimaryCorpusSplitError(f"{label}.artifact_count does not match artifacts")
    return {
        "logical_id": logical_id,
        "source": {
            "path": source_path,
            "extension": extension,
            "sha256": source_sha256,
        },
        "artifact_count": artifact_count,
        "artifacts": canonical_artifacts,
    }


def _validate_source_baseline(value: Mapping[str, Any]) -> dict[str, str]:
    root = _exact_keys(value, _BASELINE_ROOT_KEYS, name="source baseline")
    if root["schema_version"] != SOURCE_BASELINE_SCHEMA_VERSION:
        raise PrimaryCorpusSplitError("source baseline schema_version does not match")
    corpus_id = _string("source baseline.corpus_id", root["corpus_id"], maximum=192)
    if _CORPUS_ID.fullmatch(corpus_id) is None:
        raise PrimaryCorpusSplitError("source baseline.corpus_id is invalid")

    raw_runs = root["all_runs"]
    if not isinstance(raw_runs, list) or not raw_runs or len(raw_runs) > 100_000:
        raise PrimaryCorpusSplitError(
            "source baseline all_runs must be a non-empty bounded array"
        )
    runs = [_validate_baseline_run(run, index=index) for index, run in enumerate(raw_runs)]
    if [run["logical_id"] for run in runs] != sorted(run["logical_id"] for run in runs):
        raise PrimaryCorpusSplitError("source baseline all_runs must be ordered by logical_id")

    logical_ids: set[str] = set()
    source_paths: set[str] = set()
    source_digests: set[str] = set()
    artifact_paths: set[str] = set()
    for run in runs:
        if run["logical_id"] in logical_ids:
            raise PrimaryCorpusSplitError("source baseline repeats a logical_id")
        if run["source"]["path"] in source_paths:
            raise PrimaryCorpusSplitError("source baseline repeats a source path")
        if run["source"]["sha256"] in source_digests:
            raise PrimaryCorpusSplitError("source baseline repeats a source digest")
        logical_ids.add(run["logical_id"])
        source_paths.add(run["source"]["path"])
        source_digests.add(run["source"]["sha256"])
        for artifact in run["artifacts"].values():
            if artifact["path"] in artifact_paths:
                raise PrimaryCorpusSplitError("source baseline repeats an artifact path")
            artifact_paths.add(artifact["path"])

    pdf_runs = [run for run in runs if run["source"]["extension"] == "pdf"]
    subset = _exact_keys(
        root["pdf_subset"], _BASELINE_PDF_SUBSET_KEYS, name="source baseline.pdf_subset"
    )
    raw_subset_runs = subset["runs"]
    if not isinstance(raw_subset_runs, list) or len(raw_subset_runs) > 100_000:
        raise PrimaryCorpusSplitError(
            "source baseline pdf_subset.runs must be a bounded array"
        )
    subset_runs = [
        _validate_baseline_run(
            run, index=index, collection="pdf_subset.runs"
        )
        for index, run in enumerate(raw_subset_runs)
    ]
    if subset_runs != pdf_runs:
        raise PrimaryCorpusSplitError(
            "source baseline pdf_subset.runs must exactly equal filtered all_runs"
        )
    logical_ids_sha256 = _sha(
        "source baseline.pdf_subset.logical_ids_sha256",
        subset["logical_ids_sha256"],
    )
    expected_logical_ids_sha256 = sha256(
        ("\n".join(run["logical_id"] for run in pdf_runs) + "\n").encode("utf-8")
    ).hexdigest()
    if logical_ids_sha256 != expected_logical_ids_sha256:
        raise PrimaryCorpusSplitError(
            "source baseline pdf_subset.logical_ids_sha256 does not match runs"
        )

    counts = _exact_keys(root["counts"], _BASELINE_COUNTS_KEYS, name="source baseline.counts")
    if _baseline_count("source baseline.counts.all_runs", counts["all_runs"]) != len(runs):
        raise PrimaryCorpusSplitError("source baseline counts.all_runs does not match")
    if _baseline_count("source baseline.counts.pdf_runs", counts["pdf_runs"]) != len(pdf_runs):
        raise PrimaryCorpusSplitError("source baseline counts.pdf_runs does not match")
    extensions = Counter(run["source"]["extension"] for run in runs)
    raw_extensions = _exact_keys(
        counts["extensions"], frozenset(extensions), name="source baseline.counts.extensions"
    )
    if {
        key: _baseline_count(f"source baseline.counts.extensions.{key}", raw_extensions[key])
        for key in extensions
    } != dict(extensions):
        raise PrimaryCorpusSplitError("source baseline extension counts do not match")
    artifact_counts = Counter(
        name for run in runs for name in run["artifacts"]
    )
    raw_artifacts = _exact_keys(
        counts["artifacts"],
        frozenset(artifact_counts),
        name="source baseline.counts.artifacts",
    )
    if {
        key: _baseline_count(f"source baseline.counts.artifacts.{key}", raw_artifacts[key])
        for key in artifact_counts
    } != dict(artifact_counts):
        raise PrimaryCorpusSplitError("source baseline artifact counts do not match")

    return {run["logical_id"]: run["source"]["sha256"] for run in pdf_runs}


def validate_split_source_baseline_bytes(
    split: PrimaryCorpusSplitFixture,
    raw: bytes,
    *,
    reveal: VerifiedBlindReveal | None = None,
) -> str:
    """Bind a split to the exact canonical corpus baseline bytes."""

    value = _decode_json(raw, maximum=MAX_BASELINE_BYTES, name="source baseline")
    _assert_json_limits(value, maximum_nodes=MAX_BASELINE_JSON_NODES)
    identities = _validate_source_baseline(value)
    if value["corpus_id"] != split.payload["source_baseline"]["corpus_id"]:
        raise PrimaryCorpusSplitError("source baseline corpus_id does not match")
    try:
        canonical = (
            json.dumps(
                _plain(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PrimaryCorpusSplitError("source baseline is not canonical JSON") from error
    if raw != canonical:
        raise PrimaryCorpusSplitError("source baseline bytes are not canonical UTF-8 JSON")
    digest = sha256(raw).hexdigest()
    if digest != split.payload["source_baseline"]["canonical_sha256"]:
        raise PrimaryCorpusSplitError("source baseline canonical digest does not match")
    cases: Sequence[Mapping[str, Any]] = split.payload["cases"]
    for case in cases:
        if identities.get(case["notice_id"]) != case["source_pdf_sha256"]:
            raise PrimaryCorpusSplitError(
                f"source baseline does not bind public case {case['case_id']}"
            )
    if reveal is not None:
        if type(reveal) is not VerifiedBlindReveal:
            raise PrimaryCorpusSplitError(
                "source baseline reveal does not bind the supplied split"
            )
        # A receipt is a convenience wrapper, not an authority boundary.  The
        # complete payload and salted commitment are replayed against this
        # exact split every time the source baseline is checked.
        replayed_reveal = validate_blind_reveal(reveal.payload, split)
        case = replayed_reveal["revealed_case"]
        if identities.get(case["notice_id"]) != case["source_pdf_sha256"]:
            raise PrimaryCorpusSplitError(
                "source baseline does not bind the revealed blind case"
            )
    return digest


def validate_split_source_baseline_file(
    split: PrimaryCorpusSplitFixture,
    path: str | Path,
    *,
    reveal: VerifiedBlindReveal | None = None,
) -> str:
    return validate_split_source_baseline_bytes(
        split,
        _read_regular_file(path, maximum=MAX_BASELINE_BYTES, name="source baseline"),
        reveal=reveal,
    )
