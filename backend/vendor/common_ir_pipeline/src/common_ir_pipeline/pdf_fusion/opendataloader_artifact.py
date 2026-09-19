"""Fail-closed binding contract for cached OpenDataLoader JSON.

OpenDataLoader's JSON is parser output, not PDF evidence.  This module keeps
that output outside Common IR and binds it to one notice/PDF with two hashes:
the bytes which were received and a stable JSON representation.  It purposely
does not align ODL geometry to PDF user space; ``*_unverified`` is a real
state, not a shorthand for an implicit conversion.
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
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "opendataloader_artifact/v1"
PARSER_NAME = "opendataloader-pdf"
ENUMERATION_MODE = "recursive_structure_candidates/v1"
# v1 is deliberately evaluation-only.  A later, separately reviewed contract
# must carry the calibration proof before ODL coordinates can be promoted to
# PDF user space.
COORDINATE_SPACES_V1 = frozenset({"odl_pdf_points_unverified"})

# These limits apply to both the envelope and raw ODL JSON.  The raw document
# can contain a great deal more nesting than the small binding envelope, but
# it must still be bounded before callers enumerate recursive structures.
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000
MAX_STRING_BYTES = 16 * 1024
MAX_NOTICE_ID_CHARS = 256
MAX_PAGES = 256
MAX_OBJECTS = 25_000
MAX_ABSOLUTE_COORDINATE = 1_000_000.0
_SHA256_LENGTH = 64

_ARTIFACT_KEYS = frozenset({
    "schema_version", "notice_id", "source_pdf_sha256", "raw_json_sha256",
    "canonical_json_sha256", "parser", "source_page_count", "odl_page_count",
    "page_scope", "odl_page_to_source_page", "enumeration_mode", "coordinate_space",
})
_PARSER_KEYS = frozenset({"name", "version", "config_sha256", "ocr_enabled"})
_PAGE_MAPPING_KEYS = frozenset({"odl_page", "source_page"})


class OpenDataLoaderArtifactError(ValueError):
    """Raised when an ODL artifact cannot be safely accepted."""


def _exact_keys(value: object, expected: frozenset[str], *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenDataLoaderArtifactError(f"{name} must be an object")
    keys = frozenset(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise OpenDataLoaderArtifactError(f"{name} keys must be strings")
    missing, extra = sorted(expected - keys), sorted(keys - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing keys: " + ", ".join(missing))
        if extra:
            details.append("unexpected keys: " + ", ".join(extra))
        raise OpenDataLoaderArtifactError(f"{name} keys are invalid ({'; '.join(details)})")
    return value


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise OpenDataLoaderArtifactError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _string(name: str, value: object, *, maximum: int = MAX_NOTICE_ID_CHARS) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise OpenDataLoaderArtifactError(f"{name} must be a bounded non-empty string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise OpenDataLoaderArtifactError(f"{name} must not contain surrogate code points")
    return value


def _positive_int(name: str, value: object, *, maximum: int = MAX_PAGES) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise OpenDataLoaderArtifactError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenDataLoaderArtifactError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABSOLUTE_COORDINATE:
        raise OpenDataLoaderArtifactError(f"{name} must be a bounded finite number")
    return 0.0 if result == 0.0 else result


def _bbox(name: str, value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise OpenDataLoaderArtifactError(f"{name} must contain exactly four coordinates")
    result = tuple(_finite(f"{name}[{index}]", item) for index, item in enumerate(value))
    if not result[0] < result[2] or not result[1] < result[3]:
        raise OpenDataLoaderArtifactError(f"{name} must have strictly increasing x/y bounds")
    return result


def _assert_json_limits(value: object, *, depth: int = 1, counter: list[int] | None = None) -> None:
    """Check decoded JSON before semantic validation or recursive walking."""
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise OpenDataLoaderArtifactError("JSON exceeds the node cap")
    if depth > MAX_JSON_DEPTH:
        raise OpenDataLoaderArtifactError("JSON exceeds the nesting depth cap")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OpenDataLoaderArtifactError("JSON contains non-finite number")
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise OpenDataLoaderArtifactError("JSON string contains a surrogate code point")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise OpenDataLoaderArtifactError("JSON string is not valid UTF-8 text") from error
        if length > MAX_STRING_BYTES:
            raise OpenDataLoaderArtifactError("JSON string exceeds the byte cap")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise OpenDataLoaderArtifactError("JSON object keys must be strings")
            _assert_json_limits(key, depth=depth + 1, counter=counter)
            _assert_json_limits(nested, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_json_limits(nested, depth=depth + 1, counter=counter)
        return
    raise OpenDataLoaderArtifactError(f"JSON contains unsupported type {type(value).__name__}")


def _plain_json(value: Any) -> Any:
    """Turn immutable reader values back into standard JSON containers."""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON bytes after the same safety checks as reads."""
    _assert_json_limits(value)
    try:
        return json.dumps(_plain_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise OpenDataLoaderArtifactError("value is not canonically serializable JSON") from error


def assert_opendataloader_json_limits(value: object) -> None:
    """Apply the shared decoded-JSON resource limits without serializing it."""
    _assert_json_limits(value)


def canonical_json_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OpenDataLoaderArtifactError(f"JSON has duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise OpenDataLoaderArtifactError(f"JSON contains non-finite number {value!r}")


def _lexical_json_preflight(decoded: str) -> None:
    """Bound untrusted JSON tokens before ``json.loads`` allocates a tree."""
    tokens = 0
    depth = 0
    index = 0
    while index < len(decoded):
        character = decoded[index]
        if character in " \t\r\n,:":
            index += 1
            continue
        tokens += 1
        if tokens > MAX_JSON_NODES:
            raise OpenDataLoaderArtifactError("JSON exceeds the node cap")
        if character == '"':
            index += 1
            while index < len(decoded):
                if decoded[index] == "\\":
                    index += 2
                    continue
                if decoded[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise OpenDataLoaderArtifactError("JSON exceeds the nesting depth cap")
            index += 1
            continue
        if character in "]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        index += 1
        while index < len(decoded) and decoded[index] not in " \t\r\n,:[]{}":
            index += 1


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _decode_json_bytes(encoded: bytes, *, role: str) -> dict[str, Any]:
    if not isinstance(encoded, bytes) or len(encoded) > MAX_ARTIFACT_BYTES:
        raise OpenDataLoaderArtifactError(f"{role} exceeds the byte cap")
    try:
        decoded = encoded.decode("utf-8")
        if decoded.startswith("\ufeff"):
            raise OpenDataLoaderArtifactError(f"{role} must not include a UTF-8 BOM")
        _lexical_json_preflight(decoded)
        payload = json.loads(decoded, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except OpenDataLoaderArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise OpenDataLoaderArtifactError(f"failed to decode {role} JSON") from error
    _assert_json_limits(payload)
    if not isinstance(payload, dict):
        raise OpenDataLoaderArtifactError(f"{role} JSON root must be an object")
    return payload


def _read_stable_json_file(path: str | Path, *, role: str) -> tuple[bytes, dict[str, Any]]:
    """Read one regular file without following links or accepting file swaps."""
    target = Path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OpenDataLoaderArtifactError(f"{role} must be a regular file")
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise OpenDataLoaderArtifactError(f"{role} exceeds the byte cap")
            encoded = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise OpenDataLoaderArtifactError(f"{role} exceeds the byte cap")
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after or len(encoded) != before.st_size:
            raise OpenDataLoaderArtifactError(f"{role} changed while it was being read")
    except OpenDataLoaderArtifactError:
        raise
    except OSError as error:
        raise OpenDataLoaderArtifactError(f"failed to read {role} JSON") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return encoded, _decode_json_bytes(encoded, role=role)


def load_opendataloader_json_file(path: str | Path) -> tuple[bytes, Mapping[str, Any]]:
    """Load raw ODL JSON as immutable validated data, retaining its exact bytes."""
    encoded, payload = _read_stable_json_file(path, role="OpenDataLoader JSON")
    return encoded, _freeze_json(payload)


def decode_opendataloader_json_bytes(
    raw: bytes,
    *,
    role: str = "OpenDataLoader JSON",
) -> Mapping[str, Any]:
    """Decode untrusted ODL bytes with the same limits as the stable reader.

    This public boundary exists for lineage validators that already hold the
    immutable bytes.  Returning frozen containers prevents a validated value
    from being mutated before its canonical hash is checked.
    """
    return _freeze_json(_decode_json_bytes(raw, role=role))


@dataclass(frozen=True, slots=True)
class OpenDataLoaderParser:
    name: str
    version: str
    config_sha256: str
    ocr_enabled: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _string("parser.name", self.name))
        if self.name != PARSER_NAME:
            raise OpenDataLoaderArtifactError(f"parser.name must be {PARSER_NAME!r}")
        object.__setattr__(self, "version", _string("parser.version", self.version))
        object.__setattr__(self, "config_sha256", _sha("parser.config_sha256", self.config_sha256))
        if self.ocr_enabled is not False:
            raise OpenDataLoaderArtifactError("parser.ocr_enabled must be false")

    @classmethod
    def from_dict(cls, value: object) -> "OpenDataLoaderParser":
        mapping = _exact_keys(value, _PARSER_KEYS, name="parser")
        return cls(**dict(mapping))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "config_sha256": self.config_sha256, "ocr_enabled": False}


@dataclass(frozen=True, slots=True)
class OdlPageToSourcePage:
    odl_page: int
    source_page: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "odl_page", _positive_int("odl_page_to_source_page.odl_page", self.odl_page))
        object.__setattr__(self, "source_page", _positive_int("odl_page_to_source_page.source_page", self.source_page))

    @classmethod
    def from_dict(cls, value: object) -> "OdlPageToSourcePage":
        mapping = _exact_keys(value, _PAGE_MAPPING_KEYS, name="odl_page_to_source_page entry")
        return cls(**dict(mapping))

    def to_dict(self) -> dict[str, int]:
        return {"odl_page": self.odl_page, "source_page": self.source_page}


@dataclass(frozen=True, slots=True)
class OpenDataLoaderArtifact:
    notice_id: str
    source_pdf_sha256: str
    raw_json_sha256: str
    canonical_json_sha256: str
    parser: OpenDataLoaderParser
    source_page_count: int
    odl_page_count: int
    page_scope: tuple[int, ...]
    odl_page_to_source_page: tuple[OdlPageToSourcePage, ...]
    enumeration_mode: str = ENUMERATION_MODE
    coordinate_space: str = "odl_pdf_points_unverified"
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise OpenDataLoaderArtifactError(f"schema_version must be {SCHEMA_VERSION!r}")
        object.__setattr__(self, "notice_id", _string("notice_id", self.notice_id))
        object.__setattr__(self, "source_pdf_sha256", _sha("source_pdf_sha256", self.source_pdf_sha256))
        object.__setattr__(self, "raw_json_sha256", _sha("raw_json_sha256", self.raw_json_sha256))
        object.__setattr__(self, "canonical_json_sha256", _sha("canonical_json_sha256", self.canonical_json_sha256))
        if not isinstance(self.parser, OpenDataLoaderParser):
            raise OpenDataLoaderArtifactError("parser must be an OpenDataLoaderParser")
        source_count = _positive_int("source_page_count", self.source_page_count)
        odl_count = _positive_int("odl_page_count", self.odl_page_count)
        object.__setattr__(self, "source_page_count", source_count)
        object.__setattr__(self, "odl_page_count", odl_count)
        if not isinstance(self.page_scope, tuple) or not self.page_scope:
            raise OpenDataLoaderArtifactError("page_scope must be a non-empty tuple")
        scope = tuple(_positive_int(f"page_scope[{index}]", page, maximum=source_count) for index, page in enumerate(self.page_scope))
        if scope != tuple(sorted(set(scope))):
            raise OpenDataLoaderArtifactError("page_scope must be sorted and unique")
        object.__setattr__(self, "page_scope", scope)
        if not isinstance(self.odl_page_to_source_page, tuple) or not self.odl_page_to_source_page:
            raise OpenDataLoaderArtifactError("odl_page_to_source_page must be a non-empty tuple")
        if any(not isinstance(item, OdlPageToSourcePage) for item in self.odl_page_to_source_page):
            raise OpenDataLoaderArtifactError("odl_page_to_source_page must contain OdlPageToSourcePage values")
        mappings = self.odl_page_to_source_page
        odl_pages, source_pages = tuple(item.odl_page for item in mappings), tuple(item.source_page for item in mappings)
        if any(page > odl_count for page in odl_pages):
            raise OpenDataLoaderArtifactError("mapped odl_page must not exceed odl_page_count")
        if odl_pages != tuple(sorted(odl_pages)) or len(set(odl_pages)) != len(odl_pages):
            raise OpenDataLoaderArtifactError("odl_page_to_source_page odl_page values must be sorted and unique")
        if set(source_pages) != set(scope) or len(set(source_pages)) != len(source_pages):
            raise OpenDataLoaderArtifactError("odl_page_to_source_page must bijectively cover page_scope")
        if self.enumeration_mode != ENUMERATION_MODE:
            raise OpenDataLoaderArtifactError(f"enumeration_mode must be {ENUMERATION_MODE!r}")
        if self.coordinate_space not in COORDINATE_SPACES_V1:
            raise OpenDataLoaderArtifactError("coordinate_space is not an approved v1 value")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpenDataLoaderArtifact":
        _assert_json_limits(value)
        mapping = _exact_keys(value, _ARTIFACT_KEYS, name="artifact")
        if not isinstance(mapping["page_scope"], list) or not isinstance(mapping["odl_page_to_source_page"], list):
            raise OpenDataLoaderArtifactError("page_scope and odl_page_to_source_page must be arrays")
        return cls(
            schema_version=mapping["schema_version"], notice_id=mapping["notice_id"], source_pdf_sha256=mapping["source_pdf_sha256"],
            raw_json_sha256=mapping["raw_json_sha256"], canonical_json_sha256=mapping["canonical_json_sha256"],
            parser=OpenDataLoaderParser.from_dict(mapping["parser"]), source_page_count=mapping["source_page_count"],
            odl_page_count=mapping["odl_page_count"], page_scope=tuple(mapping["page_scope"]),
            odl_page_to_source_page=tuple(OdlPageToSourcePage.from_dict(item) for item in mapping["odl_page_to_source_page"]),
            enumeration_mode=mapping["enumeration_mode"], coordinate_space=mapping["coordinate_space"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "notice_id": self.notice_id, "source_pdf_sha256": self.source_pdf_sha256,
            "raw_json_sha256": self.raw_json_sha256, "canonical_json_sha256": self.canonical_json_sha256,
            "parser": self.parser.to_dict(), "source_page_count": self.source_page_count, "odl_page_count": self.odl_page_count,
            "page_scope": list(self.page_scope), "odl_page_to_source_page": [item.to_dict() for item in self.odl_page_to_source_page],
            "enumeration_mode": self.enumeration_mode, "coordinate_space": self.coordinate_space,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def artifact_sha256(self) -> str:
        return sha256(self.canonical_json()).hexdigest()

    def source_page_for_odl_page(self, odl_page: int) -> int:
        for item in self.odl_page_to_source_page:
            if item.odl_page == odl_page:
                return item.source_page
        raise OpenDataLoaderArtifactError("ODL page is outside the artifact page scope")


@dataclass(frozen=True, slots=True)
class OdlObjectEvidence:
    """Minimal validated ODL metadata; coordinates remain explicitly unverified."""
    odl_page: int
    object_id: str
    bbox: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "odl_page", _positive_int("ODL object page", self.odl_page))
        object.__setattr__(self, "object_id", _string("ODL object id", self.object_id, maximum=MAX_STRING_BYTES))
        object.__setattr__(self, "bbox", _bbox("ODL object bbox", self.bbox))


def validate_opendataloader_artifact(
    artifact: OpenDataLoaderArtifact | Mapping[str, Any],
    raw_json: Mapping[str, Any],
    *,
    raw_bytes: bytes,
    actual_odl_page_count: int,
    objects: Sequence[OdlObjectEvidence] = (),
) -> OpenDataLoaderArtifact:
    """Bind an envelope to raw ODL JSON and its extracted metadata.

    ``objects`` is intentionally a small parser-facing projection, rather
    than an alignment input.  It proves that page/id/bbox values have been
    checked while preventing this A0 boundary from claiming a coordinate
    transform or assigning native ownership.
    """
    if isinstance(artifact, Mapping):
        artifact = OpenDataLoaderArtifact.from_dict(artifact)
    if not isinstance(artifact, OpenDataLoaderArtifact):
        raise OpenDataLoaderArtifactError("artifact must be an OpenDataLoaderArtifact or object")
    if not isinstance(raw_json, Mapping):
        raise OpenDataLoaderArtifactError("raw OpenDataLoader JSON must be an object")
    _assert_json_limits(raw_json)
    canonical = canonical_json_bytes(raw_json)
    if sha256(canonical).hexdigest() != artifact.canonical_json_sha256:
        raise OpenDataLoaderArtifactError("canonical_json_sha256 does not bind the supplied OpenDataLoader JSON")
    if not isinstance(raw_bytes, bytes):
        raise OpenDataLoaderArtifactError("raw_bytes must be bytes")
    if sha256(raw_bytes).hexdigest() != artifact.raw_json_sha256:
        raise OpenDataLoaderArtifactError("raw_json_sha256 does not bind the supplied raw bytes")
    decoded_raw_bytes = _decode_json_bytes(raw_bytes, role="raw OpenDataLoader")
    if canonical_json_bytes(decoded_raw_bytes) != canonical:
        raise OpenDataLoaderArtifactError("raw bytes and decoded OpenDataLoader mapping do not describe the same JSON")
    if _positive_int("actual ODL page count", actual_odl_page_count) != artifact.odl_page_count:
        raise OpenDataLoaderArtifactError("actual ODL page count disagrees with artifact")
    if len(objects) > MAX_OBJECTS:
        raise OpenDataLoaderArtifactError("ODL object collection exceeds the safety cap")
    seen: set[tuple[int, str]] = set()
    mapped_pages = {item.odl_page for item in artifact.odl_page_to_source_page}
    for index, evidence in enumerate(objects):
        if not isinstance(evidence, OdlObjectEvidence):
            raise OpenDataLoaderArtifactError(f"objects[{index}] must be OdlObjectEvidence")
        if evidence.odl_page not in mapped_pages:
            raise OpenDataLoaderArtifactError("ODL object page is outside the artifact page scope")
        source_page = artifact.source_page_for_odl_page(evidence.odl_page)
        identity = (source_page, evidence.object_id)
        if identity in seen:
            raise OpenDataLoaderArtifactError("duplicate (mapped source page, object id) is forbidden")
        seen.add(identity)
    return artifact


def load_opendataloader_artifact_file(path: str | Path) -> OpenDataLoaderArtifact:
    """Load only the strict envelope; use ``validate_*`` to bind raw JSON."""
    _, payload = _read_stable_json_file(path, role="OpenDataLoader artifact")
    return OpenDataLoaderArtifact.from_dict(payload)
