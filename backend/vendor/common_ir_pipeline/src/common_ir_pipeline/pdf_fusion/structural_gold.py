"""Offline, image-adjudicated structural-Gold fixtures for PDF reconstruction.

This is deliberately a small read-only contract: it records reviewed page
structure, not parser output and not production Common IR evidence.  The
loader is strict so a fixture cannot quietly acquire fields, unbounded input,
or a different interpretation after it has been frozen.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = "pdf_structural_gold/v1"
MAX_ARTIFACT_BYTES = 512 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 1_000
MAX_STRING_BYTES = 4 * 1024
MAX_GROUPS = 16
MAX_CHILDREN_PER_GROUP = 32
MAX_TOLERANCE_PX = 64
EXPECTED_NOTICE_ID = "PBLN_000000000121019"
EXPECTED_SOURCE_SHA256 = "7f5cb27e0e0894ecf919e73a4290e2b6e186df0802ed80b2928d70eea365865a"
EXPECTED_PHYSICAL_PAGE = 5
EXPECTED_IMAGE_SIZE = (1653, 2337)
EXPECTED_RECORDED_RENDERED_IMAGE_SHA256 = "ffd9bd2111f0de09229259ffb95166cbefeb1959aa2c3b3475291e76df5b51c4"
EXPECTED_RENDERED_IMAGE_DIGEST_STATUS = "stage_recorded_not_locally_rehashed"
EXPECTED_RENDER_MANIFEST_SHA256 = "e1281e5184c8aed6e553e82939bb906c198dd413d438c0c67a004b1ec8f99e3c"
EXPECTED_RENDER_STAGE_SHA256 = "4f791ec9c7c0e15031313e27117f5b4d89ca8bbef0693b916a63e961ba864c53"
EXPECTED_RENDER_STAGE_FINGERPRINT = "22c8f48c2bf573f6307860eeba3457bf997eef61b124cc11534cc28f79df4639"
EXPECTED_CONFIDENCE = 0.8
EXPECTED_HEADERS = ("구 분", "인센티브")
EXPECTED_TITLE = "[붙임] 사업재편 인센티브"
EXPECTED_TABLE_BBOX = (159.0, 245.0, 1496.0, 1985.0)
EXPECTED_TABLE_BBOX_TOLERANCE = (8, 8, 8, 8)
EXPECTED_GROUP_COUNTS = (
    ("상법 특례", 6), ("공정거래법 특례", 6), ("산업집적법 특례", 1),
    ("세제 지원", 12), ("자금 지원", 9), ("연구개발 지원", 2),
    ("고용안정 지원", 2), ("중소중견 특별지원", 1), ("대·중견 특별 지원", 1),
    ("자산매각 지원", 2),
)
_SHA256_LENGTH = 64

_ROOT_KEYS = frozenset({"schema_version", "source", "adjudication", "title", "table", "below_table_notes", "footer", "native_term_assertion", "hard_negatives"})
_SOURCE_KEYS = frozenset({"notice_id", "source_pdf_sha256", "physical_page", "rendered_image"})
_IMAGE_KEYS = frozenset({
    "pixel_width", "pixel_height", "recorded_rendered_image_sha256",
    "rendered_image_digest_status", "render_manifest_sha256",
    "render_stage_sha256", "render_stage_fingerprint",
})
_ADJUDICATION_KEYS = frozenset({"method", "review_status", "human_confirmation_required", "confidence", "parser_outputs_are_truth"})
_TITLE_KEYS = frozenset({"text", "relation"})
_TABLE_KEYS = frozenset({"bbox_px", "bbox_tolerance_px", "headers", "physical_row_count", "groups"})
_TOLERANCE_KEYS = frozenset({"x0", "y0", "x1", "y1"})
_GROUP_KEYS = frozenset({"category", "child_count"})
_NOTES_KEYS = frozenset({"relation", "semantic_role", "note_count"})
_FOOTER_KEYS = frozenset({"text", "semantic_role"})
_TERM_ASSERTION_KEYS = frozenset({"accepted_native_term", "rejected_ocr_term"})
_NEGATIVE_KEYS = frozenset({
    "no_42_categories", "no_flattened_52_labels", "no_same_line_single_child_merge",
    "no_notes_or_footer_attachment", "no_checked_state_inference", "reject_surya_rowspans",
    "reject_ocr_typo", "do_not_detach_asset_sale_children", "table_list_conflict_production_evidence",
})


class StructuralGoldError(ValueError):
    """Raised when a structural-Gold fixture is not exactly the v1 contract."""


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralGoldError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise StructuralGoldError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise StructuralGoldError(f"{name} keys are invalid ({'; '.join(details)})")
    return value


def _string(name: str, value: object, *, maximum: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > maximum:
        raise StructuralGoldError(f"{name} must be a bounded non-empty trimmed string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise StructuralGoldError(f"{name} must not contain surrogate code points")
    return value


def _positive_int(name: str, value: object, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise StructuralGoldError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructuralGoldError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1_000_000:
        raise StructuralGoldError(f"{name} must be a bounded finite number")
    return 0.0 if number == 0.0 else number


def _sha256(name: str, value: object) -> str:
    digest = _string(name, value, maximum=_SHA256_LENGTH)
    if len(digest) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in digest):
        raise StructuralGoldError(f"{name} must be a lowercase SHA-256 hex digest")
    return digest


def _bbox(value: object, *, width: int, height: int) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise StructuralGoldError("table.bbox_px must contain exactly four coordinates")
    result = tuple(_finite(f"table.bbox_px[{index}]", item) for index, item in enumerate(value))
    if not result[0] < result[2] or not result[1] < result[3]:
        raise StructuralGoldError("table.bbox_px must have strictly increasing bounds")
    if result[0] < 0 or result[1] < 0 or result[2] > width or result[3] > height:
        raise StructuralGoldError("table.bbox_px must remain inside the rendered image")
    return result


def _assert_limits(value: object, *, depth: int = 1, counter: list[int] | None = None) -> None:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise StructuralGoldError("JSON exceeds the node cap")
    if depth > MAX_JSON_DEPTH:
        raise StructuralGoldError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuralGoldError("JSON contains non-finite number")
        return
    if isinstance(value, str):
        _string("JSON string", value)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise StructuralGoldError("JSON object keys must be strings")
            _assert_limits(key, depth=depth + 1, counter=counter)
            _assert_limits(nested, depth=depth + 1, counter=counter)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_limits(nested, depth=depth + 1, counter=counter)
        return
    raise StructuralGoldError(f"JSON contains unsupported type {type(value).__name__}")


def _duplicate_key_rejector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuralGoldError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise StructuralGoldError(f"JSON contains non-finite number {value!r}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise StructuralGoldError("fixture is not canonically serializable JSON") from error


def _validate(payload: Mapping[str, Any]) -> None:
    _assert_limits(payload)
    root = _exact_keys(payload, _ROOT_KEYS, name="fixture")
    if root["schema_version"] != SCHEMA_VERSION:
        raise StructuralGoldError(f"schema_version must be {SCHEMA_VERSION!r}")

    source = _exact_keys(root["source"], _SOURCE_KEYS, name="source")
    if _string("source.notice_id", source["notice_id"], maximum=64) != EXPECTED_NOTICE_ID:
        raise StructuralGoldError("source.notice_id is not this frozen fixture")
    source_hash = _sha256("source.source_pdf_sha256", source["source_pdf_sha256"])
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise StructuralGoldError("source.source_pdf_sha256 is not this frozen PDF")
    if _positive_int("source.physical_page", source["physical_page"], maximum=256) != EXPECTED_PHYSICAL_PAGE:
        raise StructuralGoldError("source.physical_page must be physical page 5")
    image = _exact_keys(source["rendered_image"], _IMAGE_KEYS, name="source.rendered_image")
    size = (_positive_int("source.rendered_image.pixel_width", image["pixel_width"]), _positive_int("source.rendered_image.pixel_height", image["pixel_height"]))
    if size != EXPECTED_IMAGE_SIZE:
        raise StructuralGoldError("source.rendered_image must be 1653x2337")
    if _sha256("source.rendered_image.recorded_rendered_image_sha256", image["recorded_rendered_image_sha256"]) != EXPECTED_RECORDED_RENDERED_IMAGE_SHA256:
        raise StructuralGoldError("source.rendered_image recorded digest is not the page-5 digest in the frozen render stage")
    if image["rendered_image_digest_status"] != EXPECTED_RENDERED_IMAGE_DIGEST_STATUS:
        raise StructuralGoldError("source.rendered_image digest status must disclose that the raster was not locally rehashed")
    if _sha256("source.rendered_image.render_manifest_sha256", image["render_manifest_sha256"]) != EXPECTED_RENDER_MANIFEST_SHA256:
        raise StructuralGoldError("source.rendered_image render manifest digest is not the locally rehashed frozen manifest")
    if _sha256("source.rendered_image.render_stage_sha256", image["render_stage_sha256"]) != EXPECTED_RENDER_STAGE_SHA256:
        raise StructuralGoldError("source.rendered_image render stage digest is not the locally rehashed frozen stage record")
    if _sha256("source.rendered_image.render_stage_fingerprint", image["render_stage_fingerprint"]) != EXPECTED_RENDER_STAGE_FINGERPRINT:
        raise StructuralGoldError("source.rendered_image render stage fingerprint is not the frozen stage fingerprint")

    adjudication = _exact_keys(root["adjudication"], _ADJUDICATION_KEYS, name="adjudication")
    if adjudication["method"] != "image_first_assisted_review":
        raise StructuralGoldError("adjudication.method must be image_first_assisted_review")
    if adjudication["review_status"] != "human_confirmed" or adjudication["human_confirmation_required"] is not False:
        raise StructuralGoldError("adjudication must retain the completed human confirmation")
    confidence = _finite("adjudication.confidence", adjudication["confidence"])
    if confidence != EXPECTED_CONFIDENCE:
        raise StructuralGoldError("adjudication.confidence must remain exactly 0.8 for this frozen fixture")
    if adjudication["parser_outputs_are_truth"] is not False:
        raise StructuralGoldError("parser outputs must not be made truth")

    title = _exact_keys(root["title"], _TITLE_KEYS, name="title")
    if _string("title.text", title["text"]) != EXPECTED_TITLE or title["relation"] != "above_table":
        raise StructuralGoldError("title must be the reviewed title above the table")

    table = _exact_keys(root["table"], _TABLE_KEYS, name="table")
    if _bbox(table["bbox_px"], width=size[0], height=size[1]) != EXPECTED_TABLE_BBOX:
        raise StructuralGoldError("table.bbox_px must be the reviewed approximate [159, 245, 1496, 1985]")
    tolerance = _exact_keys(table["bbox_tolerance_px"], _TOLERANCE_KEYS, name="table.bbox_tolerance_px")
    for key in sorted(_TOLERANCE_KEYS):
        if _positive_int(f"table.bbox_tolerance_px.{key}", tolerance[key], maximum=MAX_TOLERANCE_PX) > MAX_TOLERANCE_PX:
            raise StructuralGoldError("table bbox tolerance is too large")
    if tuple(tolerance[key] for key in ("x0", "y0", "x1", "y1")) != EXPECTED_TABLE_BBOX_TOLERANCE:
        raise StructuralGoldError("table.bbox_tolerance_px must retain the reviewed explicit tolerance")
    headers = table["headers"]
    if not isinstance(headers, list) or tuple(_string(f"table.headers[{index}]", item, maximum=64) for index, item in enumerate(headers)) != EXPECTED_HEADERS:
        raise StructuralGoldError("table must have exactly two headers: 구 분 and 인센티브")
    if _positive_int("table.physical_row_count", table["physical_row_count"], maximum=128) != 43:
        raise StructuralGoldError("table.physical_row_count must be 43")
    groups = table["groups"]
    if not isinstance(groups, list) or len(groups) != len(EXPECTED_GROUP_COUNTS):
        raise StructuralGoldError("table must preserve exactly 10 category groups, not 42 categories")
    actual_counts: list[tuple[str, int]] = []
    for index, group_value in enumerate(groups):
        group = _exact_keys(group_value, _GROUP_KEYS, name=f"table.groups[{index}]")
        category = _string(f"table.groups[{index}].category", group["category"], maximum=128)
        child_count = _positive_int(f"table.groups[{index}].child_count", group["child_count"], maximum=MAX_CHILDREN_PER_GROUP)
        actual_counts.append((category, child_count))
    if tuple(actual_counts) != EXPECTED_GROUP_COUNTS:
        raise StructuralGoldError("table category order and child-row counts must match the frozen 42 child rows")
    if sum(count for _, count in actual_counts) != 42 or 1 + sum(count for _, count in actual_counts) != table["physical_row_count"]:
        raise StructuralGoldError("child rows must sum to 42 and physical rows to 43")

    notes = _exact_keys(root["below_table_notes"], _NOTES_KEYS, name="below_table_notes")
    if notes["relation"] != "below_table" or notes["semantic_role"] != "explanatory_content" or notes["note_count"] != 3:
        raise StructuralGoldError("below-table notes must remain three separate semantic explanatory notes")
    footer = _exact_keys(root["footer"], _FOOTER_KEYS, name="footer")
    if _string("footer.text", footer["text"], maximum=16) != "- 5 -" or footer["semantic_role"] != "nonsemantic":
        raise StructuralGoldError("footer must remain separate nonsemantic '- 5 -'")
    term_assertion = _exact_keys(root["native_term_assertion"], _TERM_ASSERTION_KEYS, name="native_term_assertion")
    if term_assertion["accepted_native_term"] != "익금불산입" or term_assertion["rejected_ocr_term"] != "의금불산입":
        raise StructuralGoldError("native term assertion must preserve 익금불산입 and reject 의금불산입")

    negatives = _exact_keys(root["hard_negatives"], _NEGATIVE_KEYS, name="hard_negatives")
    for key in ("no_42_categories", "no_flattened_52_labels", "no_same_line_single_child_merge", "no_notes_or_footer_attachment", "no_checked_state_inference", "do_not_detach_asset_sale_children"):
        if negatives[key] is not True:
            raise StructuralGoldError(f"hard_negatives.{key} must be true")
    if negatives["reject_surya_rowspans"] != [10, 8]:
        raise StructuralGoldError("hard_negatives.reject_surya_rowspans must reject [10, 8]")
    if negatives["reject_ocr_typo"] != "의금불산입":
        raise StructuralGoldError("hard_negatives.reject_ocr_typo must reject 의금불산입")
    if negatives["table_list_conflict_production_evidence"] != "reject_parser_only_mixed_type_promotion":
        raise StructuralGoldError("table/list conflict must reject parser-only mixed-type production promotion")


@dataclass(frozen=True, slots=True)
class StructuralGoldFixture:
    """An immutable, canonical structural-Gold record; never parser evidence."""
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise StructuralGoldError("fixture must be an object")
        _validate(self.payload)
        object.__setattr__(self, "payload", _freeze(_plain(self.payload)))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StructuralGoldFixture":
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self.payload)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.payload)

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()


def parse_structural_gold_bytes(raw: bytes) -> StructuralGoldFixture:
    """Decode a bounded JSON fixture with duplicate/non-finite keys rejected."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise StructuralGoldError("fixture exceeds the byte cap")
    try:
        decoded = raw.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise StructuralGoldError("fixture must not include a UTF-8 BOM")
        value = json.loads(decoded, object_pairs_hook=_duplicate_key_rejector, parse_constant=_reject_constant)
    except StructuralGoldError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise StructuralGoldError("fixture is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise StructuralGoldError("fixture root must be an object")
    return StructuralGoldFixture.from_dict(value)


def load_structural_gold_file(path: str | Path) -> StructuralGoldFixture:
    """Read one regular local fixture without following a symlink."""
    candidate = Path(path)
    try:
        before = os.lstat(candidate)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise StructuralGoldError("fixture input must be a regular non-symlink file")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise StructuralGoldError("fixture exceeds the byte cap")
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise StructuralGoldError("fixture changed while opening")
            chunks: list[bytes] = []
            remaining = MAX_ARTIFACT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(raw) > MAX_ARTIFACT_BYTES or len(raw) != opened.st_size or (after.st_dev, after.st_ino, after.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
                raise StructuralGoldError("fixture changed while reading")
        finally:
            os.close(descriptor)
    except StructuralGoldError:
        raise
    except OSError as error:
        raise StructuralGoldError("fixture input cannot be safely opened") from error
    return parse_structural_gold_bytes(raw)
