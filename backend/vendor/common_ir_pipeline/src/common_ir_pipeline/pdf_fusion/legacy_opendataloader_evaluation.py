"""Evaluation-only binding for cached historical OpenDataLoader JSON.

Historical OpenDataLoader exports predate the strict
``opendataloader_artifact/v1`` contract.  They have no machine-bound parser
configuration or source lineage, so wrapping one in that schema would create a
false provenance claim.  This module deliberately produces only a separate,
non-promotable envelope.  It exposes no ODL text, geometry, table cells, or
candidate structures.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .opendataloader_artifact import (
    OpenDataLoaderArtifactError,
    assert_opendataloader_json_limits,
    canonical_json_bytes,
    decode_opendataloader_json_bytes,
    load_opendataloader_json_file,
)


SCHEMA_VERSION = "legacy_opendataloader_evaluation/v1"
COORDINATE_STATUS = "odl_pdf_points_unverified"
PARSER_NAME_CLAIM = "opendataloader-pdf"
PARSER_VERSION_CLAIM = "2.5.7"
PARSER_CLAIM_STATUS = "documented_not_machine_bound"
CONFIG_STATUS = "unavailable"
OCR_CLAIM_STATUS = "disabled_documented_not_machine_bound"
BINDING_BASIS = "curated_manifest_source_hash/v1"
MAX_PAGES = 256
_SHA256_LENGTH = 64
_ENVELOPE_KEYS = frozenset({
    "schema_version", "evaluation_only", "non_promotable", "coordinate_status",
    "notice_id", "source_pdf_sha256", "raw_json_sha256", "canonical_json_sha256",
    "source_page_count", "odl_page_count", "page_scope", "odl_page_to_source_page",
    "parser_name_claim", "parser_version_claim", "parser_claim_status",
    "config_status", "ocr_claim_status", "binding_basis",
})
_PAGE_MAPPING_KEYS = frozenset({"odl_page", "source_page"})


class LegacyOpenDataLoaderEvaluationError(ValueError):
    """Raised when a cached OpenDataLoader result cannot be safely evaluated."""


def _raise_from_strict(error: Exception) -> LegacyOpenDataLoaderEvaluationError:
    return LegacyOpenDataLoaderEvaluationError(str(error))


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise LegacyOpenDataLoaderEvaluationError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _string(name: str, value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise LegacyOpenDataLoaderEvaluationError(f"{name} must be a bounded non-empty trimmed string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise LegacyOpenDataLoaderEvaluationError(f"{name} must not contain a surrogate code point")
    return value


def _positive_page(name: str, value: object, *, maximum: int = MAX_PAGES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise LegacyOpenDataLoaderEvaluationError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyOpenDataLoaderEvaluationError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise LegacyOpenDataLoaderEvaluationError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("missing keys: " + ", ".join(missing))
        if extra:
            detail.append("unexpected keys: " + ", ".join(extra))
        raise LegacyOpenDataLoaderEvaluationError(f"{name} keys are invalid ({'; '.join(detail)})")
    return value


@dataclass(frozen=True, slots=True)
class LegacyOdlPageToSourcePage:
    """An explicit mapping; no assumption of matching source/ODL page numbers."""

    odl_page: int
    source_page: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "odl_page", _positive_page("odl_page_to_source_page.odl_page", self.odl_page))
        object.__setattr__(self, "source_page", _positive_page("odl_page_to_source_page.source_page", self.source_page))

    @classmethod
    def from_dict(cls, value: object) -> "LegacyOdlPageToSourcePage":
        return cls(**dict(_exact_keys(value, _PAGE_MAPPING_KEYS, name="odl_page_to_source_page entry")))

    def to_dict(self) -> dict[str, int]:
        return {"odl_page": self.odl_page, "source_page": self.source_page}


@dataclass(frozen=True, slots=True)
class LegacyOpenDataLoaderEvaluation:
    """Immutable, metadata-only binding for an historical ODL cache entry."""

    notice_id: str
    source_pdf_sha256: str
    raw_json_sha256: str
    canonical_json_sha256: str
    source_page_count: int
    odl_page_count: int
    page_scope: tuple[int, ...]
    odl_page_to_source_page: tuple[LegacyOdlPageToSourcePage, ...]
    schema_version: str = SCHEMA_VERSION
    evaluation_only: bool = True
    non_promotable: bool = True
    coordinate_status: str = COORDINATE_STATUS
    parser_name_claim: str = PARSER_NAME_CLAIM
    parser_version_claim: str = PARSER_VERSION_CLAIM
    parser_claim_status: str = PARSER_CLAIM_STATUS
    config_status: str = CONFIG_STATUS
    ocr_claim_status: str = OCR_CLAIM_STATUS
    binding_basis: str = BINDING_BASIS

    def __post_init__(self) -> None:
        constants = {
            "schema_version": SCHEMA_VERSION,
            "coordinate_status": COORDINATE_STATUS,
            "parser_name_claim": PARSER_NAME_CLAIM,
            "parser_version_claim": PARSER_VERSION_CLAIM,
            "parser_claim_status": PARSER_CLAIM_STATUS,
            "config_status": CONFIG_STATUS,
            "ocr_claim_status": OCR_CLAIM_STATUS,
            "binding_basis": BINDING_BASIS,
        }
        for name, expected in constants.items():
            if getattr(self, name) != expected:
                raise LegacyOpenDataLoaderEvaluationError(f"{name} must be {expected!r}")
        if self.evaluation_only is not True or self.non_promotable is not True:
            raise LegacyOpenDataLoaderEvaluationError("legacy ODL envelope must remain evaluation-only and non-promotable")
        object.__setattr__(self, "notice_id", _string("notice_id", self.notice_id))
        for name in ("source_pdf_sha256", "raw_json_sha256", "canonical_json_sha256"):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        source_count = _positive_page("source_page_count", self.source_page_count)
        odl_count = _positive_page("odl_page_count", self.odl_page_count)
        object.__setattr__(self, "source_page_count", source_count)
        object.__setattr__(self, "odl_page_count", odl_count)
        if not isinstance(self.page_scope, tuple) or not self.page_scope:
            raise LegacyOpenDataLoaderEvaluationError("page_scope must be a non-empty tuple")
        scope = tuple(_positive_page(f"page_scope[{index}]", value, maximum=source_count) for index, value in enumerate(self.page_scope))
        if scope != tuple(sorted(set(scope))):
            raise LegacyOpenDataLoaderEvaluationError("page_scope must be sorted and unique")
        object.__setattr__(self, "page_scope", scope)
        if not isinstance(self.odl_page_to_source_page, tuple) or not self.odl_page_to_source_page:
            raise LegacyOpenDataLoaderEvaluationError("odl_page_to_source_page must be a non-empty tuple")
        if any(not isinstance(item, LegacyOdlPageToSourcePage) for item in self.odl_page_to_source_page):
            raise LegacyOpenDataLoaderEvaluationError("odl_page_to_source_page has an invalid entry")
        odl_pages = tuple(item.odl_page for item in self.odl_page_to_source_page)
        source_pages = tuple(item.source_page for item in self.odl_page_to_source_page)
        if any(page > odl_count for page in odl_pages):
            raise LegacyOpenDataLoaderEvaluationError("mapped odl_page must not exceed odl_page_count")
        if odl_pages != tuple(sorted(odl_pages)) or len(set(odl_pages)) != len(odl_pages):
            raise LegacyOpenDataLoaderEvaluationError("odl_page_to_source_page odl_page values must be sorted and unique")
        if set(source_pages) != set(scope) or len(set(source_pages)) != len(source_pages):
            raise LegacyOpenDataLoaderEvaluationError("odl_page_to_source_page must bijectively cover page_scope")

    @classmethod
    def from_dict(cls, value: object) -> "LegacyOpenDataLoaderEvaluation":
        try:
            assert_opendataloader_json_limits(value)
        except OpenDataLoaderArtifactError as error:
            raise _raise_from_strict(error) from error
        mapping = _exact_keys(value, _ENVELOPE_KEYS, name="legacy ODL evaluation")
        if not isinstance(mapping["page_scope"], list) or not isinstance(mapping["odl_page_to_source_page"], list):
            raise LegacyOpenDataLoaderEvaluationError("page_scope and odl_page_to_source_page must be arrays")
        return cls(
            schema_version=mapping["schema_version"], evaluation_only=mapping["evaluation_only"],
            non_promotable=mapping["non_promotable"], coordinate_status=mapping["coordinate_status"],
            notice_id=mapping["notice_id"], source_pdf_sha256=mapping["source_pdf_sha256"],
            raw_json_sha256=mapping["raw_json_sha256"], canonical_json_sha256=mapping["canonical_json_sha256"],
            source_page_count=mapping["source_page_count"], odl_page_count=mapping["odl_page_count"],
            page_scope=tuple(mapping["page_scope"]),
            odl_page_to_source_page=tuple(LegacyOdlPageToSourcePage.from_dict(item) for item in mapping["odl_page_to_source_page"]),
            parser_name_claim=mapping["parser_name_claim"], parser_version_claim=mapping["parser_version_claim"],
            parser_claim_status=mapping["parser_claim_status"], config_status=mapping["config_status"],
            ocr_claim_status=mapping["ocr_claim_status"], binding_basis=mapping["binding_basis"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "evaluation_only": True, "non_promotable": True,
            "coordinate_status": self.coordinate_status, "notice_id": self.notice_id,
            "source_pdf_sha256": self.source_pdf_sha256, "raw_json_sha256": self.raw_json_sha256,
            "canonical_json_sha256": self.canonical_json_sha256,
            "source_page_count": self.source_page_count, "odl_page_count": self.odl_page_count,
            "page_scope": list(self.page_scope),
            "odl_page_to_source_page": [item.to_dict() for item in self.odl_page_to_source_page],
            "parser_name_claim": self.parser_name_claim, "parser_version_claim": self.parser_version_claim,
            "parser_claim_status": self.parser_claim_status, "config_status": self.config_status,
            "ocr_claim_status": self.ocr_claim_status, "binding_basis": self.binding_basis,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def source_page_for_odl_page(self, odl_page: int) -> int:
        for mapping in self.odl_page_to_source_page:
            if mapping.odl_page == odl_page:
                return mapping.source_page
        raise LegacyOpenDataLoaderEvaluationError("ODL page is outside this evaluation page scope")


def _direct_page(value: object, *, name: str) -> int:
    return _positive_page(name, value)


def _validate_typed_node_pages(value: object, *, mapped_odl_pages: set[int], inherited_page: int | None = None) -> int:
    """Validate each explicit typed ODL node page without returning ODL content.

    A table row or another nested typed node may inherit a typed parent page,
    but it can never inherit an unmapped page.  Direct ``page number`` values
    are always validated, including on untyped wrapper objects.
    """
    if isinstance(value, Mapping):
        direct_page: int | None = None
        if "page number" in value:
            direct_page = _direct_page(value["page number"], name="ODL node page number")
            if direct_page not in mapped_odl_pages:
                raise LegacyOpenDataLoaderEvaluationError("ODL node direct page is outside the mapped evaluation scope")
        current_page = direct_page if direct_page is not None else inherited_page
        typed_count = 0
        if "type" in value:
            if not isinstance(value["type"], str) or not value["type"].strip():
                raise LegacyOpenDataLoaderEvaluationError("ODL typed node must have a non-empty type")
            if current_page is None:
                raise LegacyOpenDataLoaderEvaluationError("ODL typed node has no direct or inherited page")
            typed_count = 1
        for nested in value.values():
            typed_count += _validate_typed_node_pages(nested, mapped_odl_pages=mapped_odl_pages, inherited_page=current_page)
        return typed_count
    if isinstance(value, (list, tuple)):
        return sum(_validate_typed_node_pages(item, mapped_odl_pages=mapped_odl_pages, inherited_page=inherited_page) for item in value)
    return 0


def validate_legacy_opendataloader_evaluation(
    evaluation: LegacyOpenDataLoaderEvaluation | Mapping[str, Any], raw_json: Mapping[str, Any], *, raw_bytes: bytes,
) -> LegacyOpenDataLoaderEvaluation:
    """Bind a metadata-only legacy envelope to the exact cached ODL JSON."""
    if isinstance(evaluation, Mapping):
        evaluation = LegacyOpenDataLoaderEvaluation.from_dict(evaluation)
    if not isinstance(evaluation, LegacyOpenDataLoaderEvaluation):
        raise LegacyOpenDataLoaderEvaluationError("evaluation must be a LegacyOpenDataLoaderEvaluation or object")
    if not isinstance(raw_json, Mapping):
        raise LegacyOpenDataLoaderEvaluationError("raw OpenDataLoader JSON must be an object")
    try:
        decoded = decode_opendataloader_json_bytes(raw_bytes, role="legacy OpenDataLoader JSON")
        raw_canonical = canonical_json_bytes(decoded)
        supplied_canonical = canonical_json_bytes(raw_json)
    except OpenDataLoaderArtifactError as error:
        raise _raise_from_strict(error) from error
    if sha256(raw_bytes).hexdigest() != evaluation.raw_json_sha256:
        raise LegacyOpenDataLoaderEvaluationError("raw_json_sha256 does not bind the supplied ODL bytes")
    if sha256(supplied_canonical).hexdigest() != evaluation.canonical_json_sha256:
        raise LegacyOpenDataLoaderEvaluationError("canonical_json_sha256 does not bind supplied ODL JSON")
    if raw_canonical != supplied_canonical:
        raise LegacyOpenDataLoaderEvaluationError("raw bytes and decoded ODL mapping do not describe the same JSON")
    root_count = _positive_page("ODL root number of pages", raw_json.get("number of pages"))
    if root_count != evaluation.odl_page_count:
        raise LegacyOpenDataLoaderEvaluationError("ODL root number of pages disagrees with evaluation envelope")
    mapped_pages = {item.odl_page for item in evaluation.odl_page_to_source_page}
    _validate_typed_node_pages(raw_json, mapped_odl_pages=mapped_pages)
    return evaluation


def build_legacy_opendataloader_evaluation_bytes(
    raw_bytes: bytes, *, notice_id: str, source_pdf_sha256: str, source_page_count: int,
    page_scope: Sequence[int], odl_page_to_source_page: Sequence[LegacyOdlPageToSourcePage],
) -> LegacyOpenDataLoaderEvaluation:
    """Create the separate historical envelope from a curated source-hash binding.

    The caller supplies page mappings from its review manifest.  This function
    does not infer parser configuration, OCR state, source identity, or a
    coordinate transform from the cache.
    """
    try:
        raw_json = decode_opendataloader_json_bytes(raw_bytes, role="legacy OpenDataLoader JSON")
    except OpenDataLoaderArtifactError as error:
        raise _raise_from_strict(error) from error
    odl_page_count = _positive_page("ODL root number of pages", raw_json.get("number of pages"))
    evaluation = LegacyOpenDataLoaderEvaluation(
        notice_id=notice_id, source_pdf_sha256=source_pdf_sha256,
        raw_json_sha256=sha256(raw_bytes).hexdigest(), canonical_json_sha256=sha256(canonical_json_bytes(raw_json)).hexdigest(),
        source_page_count=source_page_count, odl_page_count=odl_page_count,
        page_scope=tuple(page_scope), odl_page_to_source_page=tuple(odl_page_to_source_page),
    )
    return validate_legacy_opendataloader_evaluation(evaluation, raw_json, raw_bytes=raw_bytes)


def build_legacy_opendataloader_evaluation_file(
    path: str | Path, **bindings: Any,
) -> LegacyOpenDataLoaderEvaluation:
    """Use the strict bounded file reader, but keep the result legacy-only."""
    try:
        raw_bytes, _ = load_opendataloader_json_file(path)
    except OpenDataLoaderArtifactError as error:
        raise _raise_from_strict(error) from error
    return build_legacy_opendataloader_evaluation_bytes(raw_bytes, **bindings)
