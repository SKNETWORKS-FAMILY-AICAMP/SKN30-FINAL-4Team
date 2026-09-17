"""Provider-neutral contract for default-off remote document acceleration.

This module is deliberately a *wire-contract and validation* slice.  It does
not know about RunPod, PostgreSQL, Supabase, or a deployment environment.  The
EC2 worker remains the owner of the DB lease/fence and of final artifact
acceptance; an accelerator receives only narrowly scoped, short-lived storage
capabilities.

The contract follows ``PDF_DOCUMENT_FUSION_DESIGN.md`` sections 2, 3, 10 and
11.  In particular, a logical computation is identified from immutable input
artifacts and pipeline identity, never from a retry attempt or a signed URL.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic import field_serializer, field_validator, model_validator

from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    PdfRenderManifestError,
)


__all__ = [
    "AcceleratorContractError",
    "AcceleratorCredentialError",
    "AcceleratorFailureKind",
    "AcceleratorJobState",
    "AcceleratorDispatchPolicy",
    "AcceleratorStorageScope",
    "AcceleratorResourceCaps",
    "ArtifactDescriptor",
    "LayoutComputeIdentity",
    "PageCoordinateBinding",
    "PageImageInput",
    "PageRange",
    "RenderManifestInput",
    "SignedStorageCapability",
    "StorageResourceCaps",
    "SuryaProducerIdentity",
    "SuryaLayoutRequest",
    "SuryaLayoutReconciliationHandle",
    "SuryaLayoutResultArtifactManifest",
    "AcceleratorJobStatus",
    "validate_accelerator_result_acceptance",
    "validate_accelerator_dispatch",
    "build_logical_compute_key",
    "build_surya_layout_reconciliation_logical_compute_key",
    "build_surya_layout_result_object_key",
]


_SHA256_LENGTH = 64
_MAX_IDENTITY_STRING_CHARS = 256
_MAX_SIGNED_URL_CHARS = 8_192
_MAX_SIGNED_QUERY_CHARS = 4_096
_MAX_SIGNED_QUERY_PARAMETERS = 32
_MAX_OBJECT_KEY_CHARS = 1_024
_MAX_STORAGE_PATH_CHARS = 2_048
_MAX_SAFE_SEGMENT_CHARS = 128
_PUBLIC_REASON_CODE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_WORKER_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMMUTABLE_MODEL_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SAFE_STORAGE_SEGMENT = re.compile(
    rf"[A-Za-z0-9](?:[A-Za-z0-9._-]{{0,{_MAX_SAFE_SEGMENT_CHARS - 1}}})?"
)
_MIME_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SIGNED_QUERY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_EXTERNAL_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ARTIFACT_SCHEMA_VERSION = re.compile(
    r"[a-z0-9][a-z0-9._-]{0,126}/v[1-9][0-9]{0,9}"
)
_FORBIDDEN_CREDENTIAL_KEYS = frozenset(
    {
        "database_url",
        "database_dsn",
        "db_url",
        "postgres_url",
        "supabase_service_role_key",
        "service_role_key",
        "supabase_anon_key",
        "anon_key",
        "user_jwt",
        "jwt",
        "authorization",
        "authorization_header",
        "access_token",
        "refresh_token",
        "bearer_token",
        "api_key",
        "token",
        "cookie",
        "x_api_key",
        "database_password",
        "db_password",
        "postgres_password",
        "password",
        "passwd",
        "secret",
        "credential",
    }
)

_CREDENTIAL_KEY_PARTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "cookie",
        "jwt",
        "authorization",
        "auth",
        "bearer",
        "dsn",
    }
)
_CREDENTIAL_KEY_QUALIFIERS = frozenset(
    {
        "api",
        "access",
        "client",
        "private",
        "public",
        "service",
        "signing",
        "encryption",
        "webhook",
    }
)


class AcceleratorContractError(ValueError):
    """A local contract violation before a remote accelerator is contacted."""


class AcceleratorCredentialError(AcceleratorContractError):
    """A DB, Supabase, browser, or bearer credential was supplied by mistake."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    """Return the stable byte representation used by the two digest contracts."""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def build_surya_layout_result_object_key(logical_compute_key: str) -> str:
    """Return the canonical immutable output key for one Surya computation."""

    if not isinstance(logical_compute_key, str) or not _is_sha256(logical_compute_key):
        raise ValueError("logical compute key must be a lowercase SHA-256 digest")
    return f"accelerator/surya-layout/{logical_compute_key}/result.json"


def _has_control_or_whitespace(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    )


def _validate_safe_storage_segment(value: str, *, name: str) -> str:
    # Internal artifact keys are generated identifiers, not arbitrary user
    # filenames.  Requiring an ASCII alphanumeric first character
    # intentionally rejects hidden/private ``.foo`` and ``_foo`` segments.
    if (
        value in {"", ".", ".."}
        or "%" in value
        or "\\" in value
        or _has_control_or_whitespace(value)
        or _SAFE_STORAGE_SEGMENT.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be one canonical ASCII storage segment")
    return value


def _validate_relative_object_key(value: str, *, name: str) -> str:
    if (
        not value
        or len(value) > _MAX_OBJECT_KEY_CHARS
        or value.startswith("/")
        or value.endswith("/")
        or "%" in value
        or "\\" in value
        or _has_control_or_whitespace(value)
    ):
        raise ValueError(f"{name} must be a canonical relative storage key")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} contains a non-canonical path segment")
    for segment in segments:
        _validate_safe_storage_segment(segment, name=name)
    return value


def _validate_absolute_storage_path(value: str, *, name: str) -> str:
    if (
        not value.startswith("/")
        or value == "/"
        or len(value) > _MAX_STORAGE_PATH_CHARS
        or value.endswith("/")
        or "%" in value
        or "\\" in value
        or _has_control_or_whitespace(value)
    ):
        raise ValueError(f"{name} must be a canonical absolute storage path")
    segments = value[1:].split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{name} contains a non-canonical path segment")
    for segment in segments:
        _validate_safe_storage_segment(segment, name=name)
    return value


def _validate_mime_type(value: str) -> str:
    if _MIME_TYPE.fullmatch(value) is None:
        raise ValueError("MIME type must use canonical lowercase type/subtype syntax")
    return value


def _validate_external_job_id(value: str) -> str:
    if _EXTERNAL_JOB_ID.fullmatch(value) is None:
        raise ValueError(
            "external_job_id must be a bounded canonical provider identifier"
        )
    return value


def _validate_artifact_schema_version(value: str) -> str:
    if _ARTIFACT_SCHEMA_VERSION.fullmatch(value) is None:
        raise ValueError("artifact schema_version must be a bounded canonical identifier")
    return value


def _validate_producer_string(name: str, value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > _MAX_IDENTITY_STRING_CHARS
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(
            f"producer {name} must be a non-empty canonical string of at most "
            f"{_MAX_IDENTITY_STRING_CHARS} characters"
        )
    return value


def _validate_signed_url_syntax(value: str) -> Any:
    """Parse one canonical, bounded signed HTTPS URL without normalisation."""

    if (
        not value
        or len(value) > _MAX_SIGNED_URL_CHARS
        or not value.isascii()
        or _has_control_or_whitespace(value)
        or "\\" in value
    ):
        raise ValueError("signed storage capability URL is not canonical ASCII")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("signed storage capability URL must use HTTPS")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("signed storage capability URL has an invalid port") from error
    if (
        parsed.username
        or parsed.password
        or parsed.fragment
        or port is not None
        or parsed.netloc != parsed.hostname
    ):
        raise ValueError(
            "signed storage capability URL must use a canonical HTTPS origin"
        )
    _validate_absolute_storage_path(parsed.path, name="signed URL path")
    if (
        not parsed.query
        or len(parsed.query) > _MAX_SIGNED_QUERY_CHARS
        or _INVALID_PERCENT_ESCAPE.search(parsed.query) is not None
        # Literal '+' is form-decoded as a space and makes signatures
        # ambiguous.  Providers must percent-encode an actual plus as %2B.
        or "+" in parsed.query
    ):
        raise ValueError("signed storage capability query is missing or too large")
    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=_MAX_SIGNED_QUERY_PARAMETERS,
        )
    except ValueError as error:
        raise ValueError("signed storage capability query is malformed") from error
    if not query_pairs or any(
        _SIGNED_QUERY_NAME.fullmatch(name) is None
        or _has_control_or_whitespace(query_value)
        for name, query_value in query_pairs
    ):
        raise ValueError("signed storage capability query is not canonical")
    if urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")) != value:
        raise ValueError("signed storage capability URL must be exactly canonical")
    return parsed


def _canonical_https_origin(url: str) -> str:
    """Return the one canonical origin admitted by this v1 contract."""

    if (
        not url
        or len(url) > 253 + len("https://")
        or _has_control_or_whitespace(url)
        or "\\" in url
    ):
        raise ValueError("storage origin must be a canonical HTTPS origin")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("storage origin must use HTTPS")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("storage origin has an invalid port") from error
    if (
        parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ValueError("storage origin must be a canonical HTTPS origin")
    canonical = f"https://{parsed.hostname}"
    if url != canonical:
        raise ValueError("storage origin must use its canonical HTTPS spelling")
    return canonical


def _validate_public_reason_code(value: object) -> object:
    """Reject provider-controlled prose, URLs, and credential-like failures."""

    if value is None:
        return value
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _PUBLIC_REASON_CODE.fullmatch(value) is None
    ):
        raise ValueError("reason_code must be a lowercase public machine code")
    return value


def _validate_worker_image_digest(value: str) -> str:
    if _WORKER_IMAGE_DIGEST.fullmatch(value) is None:
        raise ValueError("worker_image_digest must be sha256:<64 lowercase hex characters>")
    return value


def _normalise_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _looks_like_credential_key(key: object) -> bool:
    """Return whether an arbitrary *unknown* field looks credential-bearing.

    This intentionally does not treat every ``*_key`` as a secret: the public
    contract legitimately contains ``object_key`` and ``logical_compute_key``.
    It does reject both exact known names and the credential vocabulary that a
    future adapter must never be tempted to smuggle through a nested payload.
    """

    normalised = _normalise_key(key)
    if normalised in _FORBIDDEN_CREDENTIAL_KEYS:
        return True
    parts = tuple(part for part in normalised.split("_") if part)
    if any(part in _CREDENTIAL_KEY_PARTS for part in parts):
        return True
    return normalised.endswith("_key") and any(
        part in _CREDENTIAL_KEY_QUALIFIERS for part in parts
    )


def _reject_credential_fields(value: object) -> None:
    """Reject credential-shaped fields without retaining their key or value.

    Signed URL query signatures are capability material, not an API token
    field.  Their raw value is moved into a private attribute and excluded
    from generic dumps after validation.  A separate
    ``token``/``jwt``/``DATABASE_URL`` property must never cross this boundary,
    even in an otherwise nested JSON object.
    """

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _looks_like_credential_key(key):
                raise AcceleratorCredentialError(
                    "credential fields are not permitted in accelerator contracts"
                )
            _reject_credential_fields(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_credential_fields(nested)


class _AcceleratorContractModel(BaseModel):
    """Strict, immutable models with a credential-free construction boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        # Contract identifiers must reject, rather than silently normalize,
        # leading/trailing whitespace.  Individual validators enforce the
        # narrower alphabets appropriate to each field.
        str_strip_whitespace=False,
        # Pydantic's default human-readable ValidationError includes the bad
        # input value.  These objects cross a signed-capability boundary, so a
        # validation failure must not print a URL signature or a malformed
        # credential-shaped value into worker logs.
        hide_input_in_errors=True,
    )

    def __init__(self, /, **data: Any) -> None:
        _reject_credential_fields(data)
        super().__init__(**data)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Any:
        """Fail closed: accelerator contracts may never bypass validation."""

        raise AcceleratorContractError(
            "model_construct is forbidden for accelerator contract models"
        )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Any:
        """Fail closed because Pydantic does not validate ``update`` values."""

        del update, deep
        raise AcceleratorContractError(
            "model_copy is forbidden for accelerator contract models"
        )

    def copy(self, *args: Any, **kwargs: Any) -> Any:
        """Also close Pydantic v1's deprecated unvalidated-copy escape hatch."""

        del args, kwargs
        raise AcceleratorContractError("copy is forbidden for accelerator contract models")

    def _assert_live_integrity(self) -> None:
        """Hook for models that protect post-validation capability state."""

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Any:
        _reject_credential_fields(obj)
        validated = super().model_validate(obj, *args, **kwargs)
        validated._assert_live_integrity()
        return validated

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            parsed = json.loads(json_data)
        except (TypeError, ValueError):
            # Let Pydantic report malformed JSON using its standard error.
            return super().model_validate_json(json_data, *args, **kwargs)
        _reject_credential_fields(parsed)
        validated = super().model_validate_json(json_data, *args, **kwargs)
        validated._assert_live_integrity()
        return validated


class AcceleratorFailureKind(StrEnum):
    """Terminal handling category owned by the EC2 worker, not a provider HTTP code."""

    CONTENT_FAILED = "content_failed"
    INFRA_RETRYABLE = "infra_retryable"
    FENCE_LOST = "fence_lost"


class AcceleratorJobState(StrEnum):
    """Provider-neutral job states.

    ``infra_retryable`` is terminal for an individual external job; a later
    fenced EC2 attempt may decide whether it can submit a replacement.
    ``fence_lost`` is likewise terminal locally and its artifact is discarded.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CONTENT_FAILED = AcceleratorFailureKind.CONTENT_FAILED
    INFRA_RETRYABLE = AcceleratorFailureKind.INFRA_RETRYABLE
    FENCE_LOST = AcceleratorFailureKind.FENCE_LOST
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in {self.QUEUED, self.RUNNING}


class PageRange(_AcceleratorContractModel):
    """Inclusive, 1-based page range as defined by the coordinate manifest."""

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "PageRange":
        if self.end_page < self.start_page:
            raise ValueError("end_page must be greater than or equal to start_page")
        return self

    @property
    def page_numbers(self) -> tuple[int, ...]:
        return tuple(range(self.start_page, self.end_page + 1))


class StorageResourceCaps(_AcceleratorContractModel):
    """Bounds attached to one signed GET or immutable signed PUT capability."""

    max_bytes: int = Field(gt=0)
    allowed_mime_types: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_mime_types(self) -> "StorageResourceCaps":
        canonical = tuple(
            sorted({_validate_mime_type(value) for value in self.allowed_mime_types})
        )
        object.__setattr__(self, "allowed_mime_types", canonical)
        return self


class AcceleratorResourceCaps(_AcceleratorContractModel):
    """Document-wide hard limits checked before a GPU job is submitted."""

    max_page_count: int = Field(gt=0)
    max_total_input_bytes: int = Field(gt=0)
    max_total_rendered_pixels: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    execution_timeout_seconds: int = Field(gt=0)
    ttl_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _ttl_covers_execution(self) -> "AcceleratorResourceCaps":
        if self.ttl_seconds < self.execution_timeout_seconds:
            raise ValueError("ttl_seconds must cover execution_timeout_seconds")
        return self


class SignedStorageCapability(_AcceleratorContractModel):
    """A constrained storage capability, never a general-purpose credential.

    The raw URL is copied into a private immutable scalar while the public
    field is replaced with a redaction marker.  It is also excluded from repr
    and every generic Pydantic dump; future HTTP adapters must use
    :meth:`signed_url` only at the request boundary.  An
    integrity fingerprint plus full binding revalidation makes even
    ``object.__setattr__`` tampering fail closed before transmission.

    Equality remains capability-instance sensitive because Pydantic includes
    private attributes.  Deduplication and reattachment must therefore use
    ``logical_compute_key``/``request_digest``, never model equality.
    """

    method: Literal["GET", "PUT"]
    access_mode: Literal["read_only", "create_only"]
    url: str = Field(repr=False, exclude=True)
    storage_host: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    path: str = Field(min_length=1, repr=False)
    object_key: str = Field(min_length=1, repr=False)
    expected_sha256: str | None = None
    expires_at: datetime
    resource_caps: StorageResourceCaps
    redirects_allowed: Literal[False] = False
    _signed_url_value: str = PrivateAttr(default="")
    _integrity_fingerprint: str = PrivateAttr(default="")

    @field_validator("url")
    @classmethod
    def _require_https_url(cls, value: str) -> str:
        _validate_signed_url_syntax(value)
        return value

    @field_validator("storage_host")
    @classmethod
    def _require_canonical_storage_host(cls, value: str) -> str:
        canonical_origin = _canonical_https_origin(f"https://{value}")
        return canonical_origin.removeprefix("https://")

    @field_validator("path")
    @classmethod
    def _require_absolute_path(cls, value: str) -> str:
        return _validate_absolute_storage_path(value, name="capability path")

    @field_validator("bucket")
    @classmethod
    def _require_simple_bucket_name(cls, value: str) -> str:
        return _validate_safe_storage_segment(value, name="bucket")

    @field_validator("object_key")
    @classmethod
    def _require_relative_object_key(cls, value: str) -> str:
        return _validate_relative_object_key(value, name="object_key")

    @field_validator("expected_sha256")
    @classmethod
    def _require_expected_hash(cls, value: str | None) -> str | None:
        if value is not None and not _is_sha256(value):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _match_url_binding(self) -> "SignedStorageCapability":
        if (
            self.url == "<redacted>"
            and self._signed_url_value
            and self._integrity_fingerprint
        ):
            # Pydantic may re-run an inner model validator when an already
            # validated capability is nested in another contract.  Verify the
            # original fingerprint; never bless post-validation mutation by
            # replacing it with a new fingerprint.
            self.signed_url()
            return self
        raw_url = self.url
        parsed = _validate_signed_url_syntax(raw_url)
        if parsed.hostname != self.storage_host:
            raise ValueError("storage_host must match the signed URL host")
        if parsed.path != self.path:
            raise ValueError("capability path must match the signed URL path")
        expected_object_suffix = f"/{self.bucket}/{self.object_key}"
        if not parsed.path.endswith(expected_object_suffix):
            raise ValueError("capability path must bind its bucket and object_key")
        if self.method == "GET" and self.access_mode != "read_only":
            raise ValueError("GET capability must declare read_only access")
        if self.method == "PUT" and self.access_mode != "create_only":
            raise ValueError("PUT capability must declare create_only access")
        if self.method == "GET" and self.expected_sha256 is None:
            raise ValueError("GET capability requires expected_sha256")
        if self.method == "PUT" and self.expected_sha256 is not None:
            raise ValueError("PUT capability must not declare expected_sha256")
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_signed_url_value"] = raw_url
        object.__setattr__(self, "url", "<redacted>")
        self.__pydantic_private__["_integrity_fingerprint"] = _sha256_digest(
            self._integrity_payload()
        )
        return self

    def signed_url(self) -> str:
        """Return raw capability material only after full live revalidation."""

        try:
            parsed = _validate_signed_url_syntax(self._signed_url_value)
            if (
                parsed.hostname != self.storage_host
                or parsed.path != self.path
                or not parsed.path.endswith(f"/{self.bucket}/{self.object_key}")
                or (self.method == "GET" and self.access_mode != "read_only")
                or (self.method == "PUT" and self.access_mode != "create_only")
                or (self.method == "GET" and self.expected_sha256 is None)
                or (self.method == "PUT" and self.expected_sha256 is not None)
            ):
                raise ValueError("live capability binding changed")
            current_fingerprint = _sha256_digest(self._integrity_payload())
        except (TypeError, ValueError) as error:
            raise AcceleratorContractError(
                "signed storage capability failed live integrity validation"
            ) from error
        if current_fingerprint != self._integrity_fingerprint:
            raise AcceleratorContractError(
                "signed storage capability changed after validation"
            )
        return self._signed_url_value

    def _assert_live_integrity(self) -> None:
        """Revalidate this capability when it crosses another model boundary."""

        self.signed_url()

    def _integrity_payload(self) -> dict[str, Any]:
        """All security-relevant fields protected against post-init mutation."""

        return {
            "method": self.method,
            "access_mode": self.access_mode,
            "url": self._signed_url_value,
            "storage_host": self.storage_host,
            "bucket": self.bucket,
            "path": self.path,
            "object_key": self.object_key,
            "expected_sha256": self.expected_sha256,
            "expires_at": self.expires_at.isoformat(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "redirects_allowed": self.redirects_allowed,
        }

    def binding_for_digest(self) -> dict[str, Any]:
        """Return stable, non-secret capability semantics for request identity.

        URL query signatures and expiration intentionally do not participate:
        replacing an expired capability must not create another logical job.
        """

        return {
            "method": self.method,
            "access_mode": self.access_mode,
            "storage_host": self.storage_host,
            "bucket": self.bucket,
            "path": self.path,
            "object_key": self.object_key,
            "expected_sha256": self.expected_sha256,
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "redirects_allowed": self.redirects_allowed,
        }

    def safe_log_record(self) -> dict[str, Any]:
        """Metadata suitable for logs; document paths and query signatures stay hidden."""

        return {
            "method": self.method,
            "access_mode": self.access_mode,
            "url": "<redacted>",
            "storage_host": self.storage_host,
            "bucket": self.bucket,
            "path": "<redacted>",
            "object_key": "<redacted>",
            "expected_sha256": self.expected_sha256,
            "expires_at": self.expires_at.isoformat(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "redirects_allowed": self.redirects_allowed,
        }


class PageCoordinateBinding(_AcceleratorContractModel):
    """Exact five-field binding emitted by one trusted render sidecar."""

    coordinate_manifest_schema_version: Literal["pdf_coordinate_manifest/v1"]
    coordinate_manifest_sha256: str
    source_sha256: str
    page: int = Field(ge=1)
    page_image_sha256: str

    @field_validator(
        "coordinate_manifest_sha256",
        "source_sha256",
        "page_image_sha256",
    )
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("page coordinate binding hashes must be lowercase SHA-256")
        return value


class RenderManifestInput(_AcceleratorContractModel):
    """Trusted render-manifest bytes plus a narrowly scoped signed GET."""

    schema_version: Literal["pdf_render_manifest/v1"]
    render_manifest_sha256: str
    size_bytes: int = Field(gt=0)
    mime_type: Literal["application/json"]
    capability: SignedStorageCapability = Field(repr=False)

    @field_validator("render_manifest_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError(
                "render_manifest_sha256 must be a lowercase SHA-256 hex digest"
            )
        return value

    @model_validator(mode="after")
    def _require_read_capability(self) -> "RenderManifestInput":
        if self.capability.method != "GET" or self.capability.access_mode != "read_only":
            raise ValueError("render manifest capability must be read-only GET")
        if self.size_bytes > self.capability.resource_caps.max_bytes:
            raise ValueError("render manifest size exceeds its capability cap")
        if self.capability.expected_sha256 != self.render_manifest_sha256:
            raise ValueError("render manifest hash must match capability expected_sha256")
        if self.capability.resource_caps.allowed_mime_types != ("application/json",):
            raise ValueError("render manifest capability MIME must be exactly application/json")
        return self


class PageImageInput(_AcceleratorContractModel):
    """One canonical, immutable page image and its GET-only capability."""

    page_number: int = Field(ge=1)
    page_image_sha256: str
    size_bytes: int = Field(gt=0)
    mime_type: Literal["image/png"]
    pixel_width: int = Field(gt=0)
    pixel_height: int = Field(gt=0)
    sidecar_binding: PageCoordinateBinding
    capability: SignedStorageCapability = Field(repr=False)

    @field_validator("page_image_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("page_image_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _require_get_capability(self) -> "PageImageInput":
        if self.capability.method != "GET" or self.capability.access_mode != "read_only":
            raise ValueError("page image capability must be read-only GET")
        if self.size_bytes > self.capability.resource_caps.max_bytes:
            raise ValueError("page image size exceeds its capability cap")
        if self.capability.expected_sha256 != self.page_image_sha256:
            raise ValueError("page image hash must match capability expected_sha256")
        if self.capability.resource_caps.allowed_mime_types != ("image/png",):
            raise ValueError("page image capability MIME must be exactly image/png")
        if self.sidecar_binding.page != self.page_number:
            raise ValueError("page image number does not match its sidecar binding")
        if self.sidecar_binding.page_image_sha256 != self.page_image_sha256:
            raise ValueError("page image hash does not match its sidecar binding")
        return self

    @property
    def rendered_pixels(self) -> int:
        """Concrete pixel count used against the request-wide resource cap."""

        return self.pixel_width * self.pixel_height


class SuryaProducerIdentity(_AcceleratorContractModel):
    """Pinned Surya producer identity shared with the portable artifact."""

    engine_id: Literal["surya"]
    engine_version: str = Field(min_length=1, max_length=_MAX_IDENTITY_STRING_CHARS)
    model_id: str = Field(min_length=1, max_length=_MAX_IDENTITY_STRING_CHARS)
    model_revision: str = Field(min_length=1, max_length=_MAX_IDENTITY_STRING_CHARS)
    model_weights_sha256: str
    pipeline_revision: str = Field(min_length=1, max_length=_MAX_IDENTITY_STRING_CHARS)
    config_sha256: str
    worker_image_digest: str

    @field_validator("model_weights_sha256", "config_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("producer hash fields must be lowercase SHA-256 digests")
        return value

    @field_validator(
        "engine_version",
        "model_id",
        "model_revision",
        "pipeline_revision",
    )
    @classmethod
    def _validate_identity_strings(cls, value: str, info: Any) -> str:
        return _validate_producer_string(info.field_name, value)

    @field_validator("model_revision")
    @classmethod
    def _validate_immutable_model_revision(cls, value: str) -> str:
        if _IMMUTABLE_MODEL_REVISION.fullmatch(value) is None:
            raise ValueError(
                "producer model_revision must be an immutable 40- or 64-character "
                "lowercase hex revision"
            )
        return value

    @field_validator("worker_image_digest")
    @classmethod
    def _validate_worker_digest(cls, value: str) -> str:
        return _validate_worker_image_digest(value)


class LayoutComputeIdentity(_AcceleratorContractModel):
    """The immutable inputs that define a Surya layout computation.

    Storage object locations, signed URL query tokens, expiry timestamps, DB
    attempts and fence values are intentionally absent.  The same pages run by
    a reclaimed attempt therefore yield the same ``logical_compute_key``.
    """

    source_sha256: str
    source_page_count: int = Field(ge=1)
    page_range: PageRange
    render_manifest: RenderManifestInput
    page_images: tuple[PageImageInput, ...] = Field(min_length=1)
    workload_scope: Literal["existing_pdf_shadow"]
    mode: Literal["layout"] = "layout"
    producer: SuryaProducerIdentity
    # These are explicit wire fields rather than computed_field properties so
    # ``model_validate(model.model_dump())`` is stable.  Empty values are
    # filled only at first construction; supplied values are recomputed and
    # rejected if they do not describe the immutable identity above.
    logical_compute_key: str = ""

    @field_validator("source_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("identity hash fields must be lowercase SHA-256 hex digests")
        return value

    @model_validator(mode="after")
    def _validate_page_coverage(self) -> "LayoutComputeIdentity":
        if self.page_range.end_page > self.source_page_count:
            raise ValueError("page_range extends beyond source_page_count")
        numbers = tuple(image.page_number for image in self.page_images)
        if numbers != self.page_range.page_numbers:
            raise ValueError(
                "page_images must cover the page_range exactly once in ascending order"
            )
        if any(
            image.sidecar_binding.source_sha256 != self.source_sha256
            for image in self.page_images
        ):
            raise ValueError("page sidecar source hash does not match source_sha256")
        expected_logical_compute_key = build_logical_compute_key(self)
        if self.logical_compute_key and self.logical_compute_key != expected_logical_compute_key:
            raise ValueError("logical_compute_key does not match immutable compute identity")
        if not self.logical_compute_key:
            object.__setattr__(self, "logical_compute_key", expected_logical_compute_key)
        return self

    def compute_key_payload(self) -> dict[str, Any]:
        """The complete stable payload behind ``logical_compute_key``."""

        return {
            "source_sha256": self.source_sha256,
            "source_page_count": self.source_page_count,
            "render_manifest": {
                "schema_version": self.render_manifest.schema_version,
                "render_manifest_sha256": self.render_manifest.render_manifest_sha256,
                "size_bytes": self.render_manifest.size_bytes,
                "mime_type": self.render_manifest.mime_type,
            },
            "page_images": [
                {
                    "page_number": image.page_number,
                    "page_image_sha256": image.page_image_sha256,
                    "size_bytes": image.size_bytes,
                    "mime_type": image.mime_type,
                    "pixel_width": image.pixel_width,
                    "pixel_height": image.pixel_height,
                    "sidecar_binding": image.sidecar_binding.model_dump(mode="json"),
                }
                for image in self.page_images
            ],
            "page_range": self.page_range.model_dump(mode="json"),
            "workload_scope": self.workload_scope,
            "mode": self.mode,
            "producer": self.producer.model_dump(mode="json"),
        }

def build_logical_compute_key(identity: LayoutComputeIdentity) -> str:
    """Build the attempt- and capability-independent computation identity."""

    return _sha256_digest(identity.compute_key_payload())


def build_surya_layout_reconciliation_logical_compute_key(
    trusted_render_manifest: PdfRenderManifest,
    producer: SuryaProducerIdentity,
) -> str:
    """Rebuild the Surya key without any ephemeral storage capability.

    The payload intentionally mirrors :meth:`LayoutComputeIdentity.compute_key_payload`
    for the complete trusted render manifest produced by the Existing-PDF
    factory.  Keeping this computation capability-free is what permits a
    restarted worker to authenticate a persisted reconciliation handle after
    the original create-only upload capability has expired or become
    impossible to reissue.
    """

    if not isinstance(trusted_render_manifest, PdfRenderManifest):
        raise TypeError("trusted_render_manifest must be a PdfRenderManifest")
    if not isinstance(producer, SuryaProducerIdentity):
        raise TypeError("producer must be a SuryaProducerIdentity")
    manifest = trusted_render_manifest
    return _sha256_digest(
        {
            "source_sha256": manifest.source_pdf_sha256,
            "source_page_count": manifest.page_count,
            "render_manifest": {
                "schema_version": manifest.schema_version,
                "render_manifest_sha256": manifest.manifest_sha256(),
                "size_bytes": len(manifest.canonical_json()),
                "mime_type": "application/json",
            },
            "page_images": [
                {
                    "page_number": page.page,
                    "page_image_sha256": page.image_sha256,
                    "size_bytes": page.image_size_bytes,
                    "mime_type": page.image_mime_type,
                    "pixel_width": page.coordinate_manifest.rendered_width_px,
                    "pixel_height": page.coordinate_manifest.rendered_height_px,
                    "sidecar_binding": build_sidecar_binding(
                        page.coordinate_manifest
                    ),
                }
                for page in manifest.pages
            ],
            "page_range": {"start_page": 1, "end_page": manifest.page_count},
            "workload_scope": "existing_pdf_shadow",
            "mode": "layout",
            "producer": producer.model_dump(mode="json"),
        }
    )


class ArtifactDescriptor(_AcceleratorContractModel):
    """Artifact metadata returned by a remote accelerator, before local acceptance."""

    object_key: str = Field(min_length=1, repr=False)
    sha256: str
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=255)
    schema_version: str = Field(min_length=1, max_length=256)

    @field_validator("sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("artifact SHA-256 must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("object_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _validate_relative_object_key(value, name="artifact object_key")

    @field_validator("mime_type")
    @classmethod
    def _validate_artifact_mime(cls, value: str) -> str:
        return _validate_mime_type(value)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        return _validate_artifact_schema_version(value)


class SuryaLayoutRequest(_AcceleratorContractModel):
    """Provider-neutral request envelope for a remote Surya layout job."""

    contract_version: Literal["surya_layout_request/v1"] = "surya_layout_request/v1"
    input_schema_version: Literal["surya_layout_input/v1"] = "surya_layout_input/v1"
    output_schema_version: Literal["surya_layout_artifact/v1"] = (
        "surya_layout_artifact/v1"
    )
    identity: LayoutComputeIdentity
    result_upload_capability: SignedStorageCapability = Field(repr=False)
    resource_caps: AcceleratorResourceCaps
    logical_compute_key: str = ""
    request_digest: str = ""
    _request_integrity_fingerprint: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _require_put_output(self) -> "SuryaLayoutRequest":
        if self._request_integrity_fingerprint:
            # Revalidation of an existing instance must verify the original
            # fingerprint rather than blessing object.__setattr__ mutations.
            self._assert_live_integrity()
            return self
        for capability in self._storage_capabilities():
            capability.signed_url()
        if (
            self.result_upload_capability.method != "PUT"
            or self.result_upload_capability.access_mode != "create_only"
        ):
            raise ValueError("result upload capability must be create-only PUT")
        if self.result_upload_capability.expected_sha256 is not None:
            raise ValueError("result upload capability cannot predeclare an output hash")
        if self.result_upload_capability.resource_caps.allowed_mime_types != (
            "application/json",
        ):
            raise ValueError("result upload capability MIME must be exactly application/json")
        if self.resource_caps.max_page_count < len(self.identity.page_images):
            raise ValueError("resource max_page_count is below the requested page count")
        total_input_bytes = self.identity.render_manifest.size_bytes + sum(
            page.size_bytes for page in self.identity.page_images
        )
        if self.resource_caps.max_total_input_bytes < total_input_bytes:
            raise ValueError(
                "resource max_total_input_bytes is below manifest and page input bytes"
            )
        if self.resource_caps.max_total_rendered_pixels < sum(
            page.rendered_pixels for page in self.identity.page_images
        ):
            raise ValueError("resource max_total_rendered_pixels is below rendered page pixels")
        if (
            self.resource_caps.max_output_bytes
            > self.result_upload_capability.resource_caps.max_bytes
        ):
            raise ValueError("result upload capability cap is below resource max_output_bytes")
        expected_logical_compute_key = self.identity.logical_compute_key
        if self.logical_compute_key and self.logical_compute_key != expected_logical_compute_key:
            raise ValueError("logical_compute_key does not match immutable compute identity")
        if not self.logical_compute_key:
            object.__setattr__(self, "logical_compute_key", expected_logical_compute_key)
        if self.result_upload_capability.object_key != build_surya_layout_result_object_key(
            expected_logical_compute_key
        ):
            raise ValueError("result upload key does not match logical_compute_key")

        expected_request_digest = _sha256_digest(self.request_digest_payload())
        if self.request_digest and self.request_digest != expected_request_digest:
            raise ValueError("request_digest does not match request envelope")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", expected_request_digest)
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_request_integrity_fingerprint"] = _sha256_digest(
            self._live_integrity_payload()
        )
        return self

    def _storage_capabilities(self) -> tuple[SignedStorageCapability, ...]:
        return (
            self.identity.render_manifest.capability,
            *(page.capability for page in self.identity.page_images),
            self.result_upload_capability,
        )

    def _live_integrity_payload(self) -> dict[str, Any]:
        """Canonical validated request state, including ephemeral capabilities."""

        return {
            "contract_version": self.contract_version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "identity": self.identity.compute_key_payload(),
            "identity_logical_compute_key": self.identity.logical_compute_key,
            "capabilities": [
                capability._integrity_payload()
                for capability in self._storage_capabilities()
            ],
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "logical_compute_key": self.logical_compute_key,
            "request_digest": self.request_digest,
        }

    def _assert_live_integrity(self) -> None:
        """Reject any stale digest or post-validation request mutation."""

        try:
            for capability in self._storage_capabilities():
                capability.signed_url()
            expected_logical_compute_key = build_logical_compute_key(self.identity)
            if self.identity.logical_compute_key != expected_logical_compute_key:
                raise AcceleratorContractError(
                    "request identity logical_compute_key changed after validation"
                )
            if self.logical_compute_key != expected_logical_compute_key:
                raise AcceleratorContractError(
                    "request logical_compute_key changed after validation"
                )
            expected_request_digest = _sha256_digest(self.request_digest_payload())
            if self.request_digest != expected_request_digest:
                raise AcceleratorContractError(
                    "request_digest no longer matches the live request envelope"
                )
            current_fingerprint = _sha256_digest(self._live_integrity_payload())
        except AcceleratorContractError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise AcceleratorContractError(
                "accelerator request failed live integrity validation"
            ) from error
        if current_fingerprint != self._request_integrity_fingerprint:
            raise AcceleratorContractError(
                "accelerator request changed after validation"
            )

    def request_digest_payload(self) -> dict[str, Any]:
        """Stable envelope semantics used to detect an incompatible reattach.

        It intentionally excludes raw signed URLs and expiry.  Renewing a
        short-lived capability cannot make an otherwise identical fenced job
        look like a conflicting computation.
        """

        return {
            "contract_version": self.contract_version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "logical_compute_key": self.logical_compute_key,
            "render_manifest_capability": (
                self.identity.render_manifest.capability.binding_for_digest()
            ),
            "page_image_capabilities": [
                page.capability.binding_for_digest()
                for page in self.identity.page_images
            ],
            "result_upload_capability": self.result_upload_capability.binding_for_digest(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
        }

    def safe_log_record(self) -> dict[str, Any]:
        """Envelope metadata fit for a log line without URLs or object paths."""

        return {
            "contract_version": self.contract_version,
            "logical_compute_key": self.logical_compute_key,
            "request_digest": self.request_digest,
            "page_count": len(self.identity.page_images),
            "render_manifest_capability": (
                self.identity.render_manifest.capability.safe_log_record()
            ),
            "input_capabilities": [
                page.capability.safe_log_record() for page in self.identity.page_images
            ],
            "result_upload_capability": self.result_upload_capability.safe_log_record(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
        }

    def to_wire_payload(self) -> dict[str, Any]:
        """Return the explicit network payload, including signed URLs.

        Generic model dumps deliberately omit the private signed URL value.  A
        provider adapter must use this method only at its outbound network
        boundary; it must never use this payload for logs, persistence, or a
        retry identity.
        """

        self._assert_live_integrity()
        identity = self.identity
        return {
            "contract_version": self.contract_version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "identity": {
                "source_sha256": identity.source_sha256,
                "source_page_count": identity.source_page_count,
                "page_range": identity.page_range.model_dump(mode="json"),
                "render_manifest": {
                    "schema_version": identity.render_manifest.schema_version,
                    "render_manifest_sha256": (
                        identity.render_manifest.render_manifest_sha256
                    ),
                    "size_bytes": identity.render_manifest.size_bytes,
                    "mime_type": identity.render_manifest.mime_type,
                    "capability": _capability_wire_payload(
                        identity.render_manifest.capability
                    ),
                },
                "page_images": [
                    {
                        "page_number": page.page_number,
                        "page_image_sha256": page.page_image_sha256,
                        "size_bytes": page.size_bytes,
                        "mime_type": page.mime_type,
                        "pixel_width": page.pixel_width,
                        "pixel_height": page.pixel_height,
                        "sidecar_binding": page.sidecar_binding.model_dump(mode="json"),
                        "capability": _capability_wire_payload(page.capability),
                    }
                    for page in identity.page_images
                ],
                "workload_scope": identity.workload_scope,
                "mode": identity.mode,
                "producer": identity.producer.model_dump(mode="json"),
                "logical_compute_key": identity.logical_compute_key,
            },
            "result_upload_capability": _capability_wire_payload(
                self.result_upload_capability
            ),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "logical_compute_key": self.logical_compute_key,
            "request_digest": self.request_digest,
        }

    @classmethod
    def from_wire_payload(cls, payload: Mapping[str, Any]) -> "SuryaLayoutRequest":
        """Validate a payload previously produced by :meth:`to_wire_payload`."""

        return cls.model_validate(payload)


class SuryaLayoutReconciliationHandle(_AcceleratorContractModel):
    """Credential-free state sufficient to accept a deterministic result.

    This is the only accelerator object intended for durable persistence.  It
    contains the complete trusted render lineage and pinned producer, but no
    signed URL, provider status, request digest, or input/output capability.
    Validation recomputes the logical key from that lineage and binds the
    storage key to the recomputed value.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, strict=True)

    contract_version: Literal["surya_layout_reconciliation/v1"] = (
        "surya_layout_reconciliation/v1"
    )
    artifact_schema_version: Literal["surya_layout_artifact/v1"] = (
        "surya_layout_artifact/v1"
    )
    artifact_content_type: Literal["application/json"] = "application/json"
    trusted_render_manifest: PdfRenderManifest
    producer: SuryaProducerIdentity
    logical_compute_key: str
    result_bucket: str
    result_object_key: str
    max_output_bytes: int = Field(gt=0)
    _integrity_fingerprint: str = PrivateAttr(default="")

    @field_validator("trusted_render_manifest", mode="before")
    @classmethod
    def _validate_render_manifest(cls, value: Any) -> PdfRenderManifest:
        if isinstance(value, PdfRenderManifest):
            return value
        if isinstance(value, Mapping):
            try:
                return PdfRenderManifest.from_dict(value)
            except PdfRenderManifestError as error:
                raise ValueError("trusted render manifest is invalid") from error
        raise ValueError("trusted_render_manifest must be a render manifest object")

    @field_serializer("trusted_render_manifest")
    def _serialize_render_manifest(
        self, value: PdfRenderManifest
    ) -> dict[str, Any]:
        return value.to_dict()

    @field_validator("logical_compute_key")
    @classmethod
    def _validate_logical_compute_key(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError(
                "logical_compute_key must be a lowercase SHA-256 digest"
            )
        return value

    @field_validator("result_bucket")
    @classmethod
    def _validate_result_bucket(cls, value: str) -> str:
        return _validate_safe_storage_segment(value, name="result bucket")

    @field_validator("result_object_key")
    @classmethod
    def _validate_result_object_key(cls, value: str) -> str:
        return _validate_relative_object_key(value, name="result object_key")

    @model_validator(mode="after")
    def _bind_reconciliation_lineage(self) -> "SuryaLayoutReconciliationHandle":
        if self._integrity_fingerprint:
            self._assert_live_integrity()
            return self
        self._assert_semantic_bindings()
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_integrity_fingerprint"] = _sha256_digest(
            self._integrity_payload()
        )
        return self

    def _assert_semantic_bindings(self) -> None:
        expected_logical_compute_key = (
            build_surya_layout_reconciliation_logical_compute_key(
                self.trusted_render_manifest,
                self.producer,
            )
        )
        if self.logical_compute_key != expected_logical_compute_key:
            raise ValueError(
                "logical_compute_key does not match trusted render lineage and producer"
            )
        if self.result_object_key != build_surya_layout_result_object_key(
            expected_logical_compute_key
        ):
            raise ValueError(
                "result object_key does not match the recomputed logical_compute_key"
            )

    def _integrity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "artifact_schema_version": self.artifact_schema_version,
            "artifact_content_type": self.artifact_content_type,
            "trusted_render_manifest": self.trusted_render_manifest.to_dict(),
            "producer": self.producer.model_dump(mode="json"),
            "logical_compute_key": self.logical_compute_key,
            "result_bucket": self.result_bucket,
            "result_object_key": self.result_object_key,
            "max_output_bytes": self.max_output_bytes,
        }

    def _assert_live_integrity(self) -> None:
        try:
            self._assert_semantic_bindings()
            current_fingerprint = _sha256_digest(self._integrity_payload())
        except AcceleratorContractError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise AcceleratorContractError(
                "Surya reconciliation handle failed live integrity validation"
            ) from error
        if (
            not self._integrity_fingerprint
            or current_fingerprint != self._integrity_fingerprint
        ):
            raise AcceleratorContractError(
                "Surya reconciliation handle changed after validation"
            )

    def validate_external_job_id(self, value: object) -> str:
        """Bind persistent-provider reconciliation to its deterministic job ID."""

        self._assert_live_integrity()
        if not isinstance(value, str) or value != self.logical_compute_key:
            raise AcceleratorContractError(
                "reconciliation external_job_id does not match logical_compute_key"
            )
        return value

    @classmethod
    def from_request(
        cls,
        request: SuryaLayoutRequest,
        trusted_render_manifest: PdfRenderManifest,
    ) -> "SuryaLayoutReconciliationHandle":
        """Derive the durable handle before dispatching ``request``."""

        if not isinstance(request, SuryaLayoutRequest):
            raise TypeError("request must be a SuryaLayoutRequest")
        request._assert_live_integrity()
        return cls(
            trusted_render_manifest=trusted_render_manifest,
            producer=request.identity.producer,
            logical_compute_key=request.logical_compute_key,
            result_bucket=request.result_upload_capability.bucket,
            result_object_key=request.result_upload_capability.object_key,
            max_output_bytes=request.resource_caps.max_output_bytes,
            artifact_schema_version=request.output_schema_version,
        )

    def to_persistence_payload(self) -> dict[str, Any]:
        """Return strict JSON-compatible state with no capability material."""

        self._assert_live_integrity()
        payload = self.model_dump(mode="json")
        _reject_credential_fields(payload)
        return payload

    @classmethod
    def from_persistence_payload(
        cls, payload: Mapping[str, Any]
    ) -> "SuryaLayoutReconciliationHandle":
        """Reconstruct and fully revalidate a persisted handle."""

        return cls.model_validate(payload)


def _capability_wire_payload(capability: SignedStorageCapability) -> dict[str, Any]:
    """Serialize capability material only for an outbound accelerator request."""

    return {
        "method": capability.method,
        "access_mode": capability.access_mode,
        "url": capability.signed_url(),
        "storage_host": capability.storage_host,
        "bucket": capability.bucket,
        "path": capability.path,
        "object_key": capability.object_key,
        "expected_sha256": capability.expected_sha256,
        "expires_at": capability.expires_at.isoformat(),
        "resource_caps": capability.resource_caps.model_dump(mode="json"),
        "redirects_allowed": capability.redirects_allowed,
    }


class AcceleratorStorageScope(_AcceleratorContractModel):
    """One indivisible outbound storage grant owned by deployment policy.

    Keeping method, canonical origin, endpoint shape, bucket and object prefix
    in one rule prevents the Cartesian-product widening caused by independent
    allow-lists.  The endpoint template must bind both declared path values;
    For example, GET uses ``/storage/v1/object/sign/{bucket}/{object_key}``
    while Supabase signed-upload PUT uses
    ``/storage/v1/object/upload/sign/{bucket}/{object_key}``.
    """

    method: Literal["GET", "PUT"]
    origin: str = Field(min_length=1)
    endpoint_path_template: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    object_key_prefix: str = Field(min_length=1)

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        return _canonical_https_origin(value)

    @field_validator("endpoint_path_template")
    @classmethod
    def _validate_endpoint_template(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or value.count("{bucket}") != 1
            or value.count("{object_key}") != 1
            or "?" in value
            or "#" in value
            or "%" in value
            or "\\" in value
            or _has_control_or_whitespace(value)
        ):
            raise ValueError(
                "endpoint path template must be an absolute canonical path with one "
                "{bucket} and one {object_key} placeholder"
            )
        remainder = value.replace("{bucket}", "").replace("{object_key}", "")
        if "{" in remainder or "}" in remainder:
            raise ValueError("endpoint path template contains an unsupported placeholder")
        segments = value[1:].split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("endpoint path template contains a non-canonical segment")
        if "{bucket}" not in segments or "{object_key}" not in segments:
            raise ValueError("endpoint placeholders must each occupy a complete path segment")
        for segment in segments:
            if segment not in {"{bucket}", "{object_key}"}:
                _validate_safe_storage_segment(
                    segment,
                    name="endpoint static path segment",
                )
        rendered = value.replace("{bucket}", "safe-bucket").replace(
            "{object_key}", "safe-prefix/result.json"
        )
        _validate_absolute_storage_path(rendered, name="endpoint path template")
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        return _validate_safe_storage_segment(value, name="dispatch scope bucket")

    @field_validator("object_key_prefix")
    @classmethod
    def _validate_object_prefix(cls, value: str) -> str:
        if not value.endswith("/") or not value[:-1]:
            raise ValueError(
                "dispatch object-key prefix must be non-empty relative path segments ending in '/'"
            )
        _validate_relative_object_key(
            value[:-1],
            name="dispatch object-key prefix",
        )
        return value

    def permits(self, capability: SignedStorageCapability) -> bool:
        """Compare only public bindings after dispatch has live-validated URL."""

        origin = f"https://{capability.storage_host}"
        expected_path = self.endpoint_path_template.replace(
            "{bucket}", capability.bucket
        ).replace("{object_key}", capability.object_key)
        return (
            capability.method == self.method
            and origin == self.origin
            and capability.bucket == self.bucket
            and capability.object_key.startswith(self.object_key_prefix)
            and capability.path == expected_path
        )


class AcceleratorDispatchPolicy(_AcceleratorContractModel):
    """Injected, deployment-owned outbound accelerator storage policy."""

    allowed_scopes: tuple[AcceleratorStorageScope, ...] = Field(min_length=1)
    max_ttl_seconds: int = Field(gt=0)
    max_execution_timeout_seconds: int = Field(gt=0)
    max_page_count: int = Field(gt=0)
    max_total_input_bytes: int = Field(gt=0)
    max_total_rendered_pixels: int = Field(gt=0)
    max_capability_bytes: int = Field(gt=0)
    allowed_mime_types: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_policy(self) -> "AcceleratorDispatchPolicy":
        canonical_mime_types = tuple(
            sorted({_validate_mime_type(value) for value in self.allowed_mime_types})
        )
        object.__setattr__(self, "allowed_mime_types", canonical_mime_types)
        return self


def validate_accelerator_dispatch(
    request: SuryaLayoutRequest,
    policy: AcceleratorDispatchPolicy,
    *,
    now: datetime,
) -> None:
    """Validate an outbound dispatch at a fixed, injected clock time.

    All source GET and result PUT capabilities must survive the request's full
    TTL, which in turn is already constrained to cover execution time.  This
    check belongs at dispatch, not at delayed result acceptance: a completed
    job remains auditable even when its one-time input GET URL has expired.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise AcceleratorContractError("dispatch time must include a timezone")
    request._assert_live_integrity()
    if request.resource_caps.ttl_seconds > policy.max_ttl_seconds:
        raise AcceleratorContractError("request TTL exceeds deployment policy ceiling")
    if (
        request.resource_caps.execution_timeout_seconds
        > policy.max_execution_timeout_seconds
    ):
        raise AcceleratorContractError(
            "request execution timeout exceeds deployment policy ceiling"
        )
    if request.resource_caps.max_output_bytes > policy.max_capability_bytes:
        raise AcceleratorContractError(
            "request output cap exceeds deployment capability-byte ceiling"
        )
    actual_page_count = len(request.identity.page_images)
    if (
        actual_page_count > policy.max_page_count
        or request.resource_caps.max_page_count > policy.max_page_count
    ):
        raise AcceleratorContractError(
            "request page count exceeds deployment policy ceiling"
        )
    actual_total_input_bytes = request.identity.render_manifest.size_bytes + sum(
        page.size_bytes for page in request.identity.page_images
    )
    if (
        actual_total_input_bytes > policy.max_total_input_bytes
        or request.resource_caps.max_total_input_bytes > policy.max_total_input_bytes
    ):
        raise AcceleratorContractError(
            "request aggregate input bytes exceed deployment policy ceiling"
        )
    aggregate_get_capability_bytes = (
        request.identity.render_manifest.capability.resource_caps.max_bytes
        + sum(
            page.capability.resource_caps.max_bytes
            for page in request.identity.page_images
        )
    )
    if aggregate_get_capability_bytes > policy.max_total_input_bytes:
        raise AcceleratorContractError(
            "aggregate GET capability bytes exceed deployment policy ceiling"
        )
    actual_total_rendered_pixels = sum(
        page.rendered_pixels for page in request.identity.page_images
    )
    if (
        actual_total_rendered_pixels > policy.max_total_rendered_pixels
        or request.resource_caps.max_total_rendered_pixels
        > policy.max_total_rendered_pixels
    ):
        raise AcceleratorContractError(
            "request rendered pixels exceed deployment policy ceiling"
        )
    required_expiry = now + timedelta(seconds=request.resource_caps.ttl_seconds)
    maximum_expiry = now + timedelta(seconds=policy.max_ttl_seconds)
    capabilities = (
        request.identity.render_manifest.capability,
        *(page.capability for page in request.identity.page_images),
        request.result_upload_capability,
    )
    for capability in capabilities:
        # request._assert_live_integrity() above already revalidates every raw
        # URL and capability fingerprint once at this network boundary.
        if not any(scope.permits(capability) for scope in policy.allowed_scopes):
            raise AcceleratorContractError(
                "storage capability is not allowed by any bound dispatch scope"
            )
        if capability.expires_at < required_expiry:
            raise AcceleratorContractError("storage capability expires before dispatch TTL")
        if capability.expires_at > maximum_expiry:
            raise AcceleratorContractError(
                "storage capability expiry exceeds deployment maximum TTL"
            )
        if capability.resource_caps.max_bytes > policy.max_capability_bytes:
            raise AcceleratorContractError(
                "storage capability byte cap exceeds deployment policy ceiling"
            )
        if not set(capability.resource_caps.allowed_mime_types).issubset(
            policy.allowed_mime_types
        ):
            raise AcceleratorContractError(
                "storage capability MIME type exceeds deployment policy allow-list"
            )


class SuryaLayoutResultArtifactManifest(_AcceleratorContractModel):
    """Terminal remote result metadata; its artifact remains untrusted until EC2 verifies it."""

    contract_version: Literal["surya_layout_result_manifest/v1"] = (
        "surya_layout_result_manifest/v1"
    )
    external_job_id: str = Field(min_length=1)
    status: Literal["succeeded", "content_failed", "infra_retryable", "cancelled"]
    logical_compute_key: str
    request_digest: str
    result_artifact: ArtifactDescriptor | None = None
    source_sha256: str
    source_page_count: int = Field(ge=1)
    page_range: PageRange
    render_manifest_schema_version: Literal["pdf_render_manifest/v1"]
    render_manifest_sha256: str
    page_images: tuple[PageCoordinateBinding, ...] = Field(min_length=1)
    producer: SuryaProducerIdentity
    started_at: datetime
    completed_at: datetime
    reason_code: str | None = None

    @field_validator("external_job_id")
    @classmethod
    def _validate_provider_job_id(cls, value: str) -> str:
        return _validate_external_job_id(value)

    @field_validator(
        "logical_compute_key",
        "request_digest",
        "source_sha256",
        "render_manifest_sha256",
    )
    @classmethod
    def _validate_manifest_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("manifest digest fields must be lowercase SHA-256 hex digests")
        return value

    @field_validator("reason_code", mode="before")
    @classmethod
    def _validate_reason_code(cls, value: object) -> object:
        return _validate_public_reason_code(value)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _validate_timing_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manifest timing must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_terminal_payload(self) -> "SuryaLayoutResultArtifactManifest":
        if self.page_range.end_page > self.source_page_count:
            raise ValueError("page_range extends beyond source_page_count")
        page_numbers = tuple(image.page for image in self.page_images)
        if page_numbers != self.page_range.page_numbers:
            raise ValueError(
                "manifest page_images must cover the page_range in ascending order"
            )
        if any(image.source_sha256 != self.source_sha256 for image in self.page_images):
            raise ValueError("manifest page binding source hash does not match source_sha256")
        if self.completed_at < self.started_at:
            raise ValueError("manifest completed_at must not precede started_at")
        if self.status == AcceleratorJobState.SUCCEEDED:
            if self.result_artifact is None:
                raise ValueError("succeeded manifest requires result_artifact")
            if self.reason_code is not None:
                raise ValueError("succeeded manifest must not carry a failure reason_code")
        else:
            if self.result_artifact is not None:
                raise ValueError("failed/cancelled manifest must not carry a result_artifact")
            if self.status == AcceleratorJobState.CANCELLED and self.reason_code is not None:
                raise ValueError("cancelled manifest cannot carry a reason_code")
            if self.status != AcceleratorJobState.CANCELLED and not self.reason_code:
                raise ValueError("failed manifest requires a public reason_code")
        return self


class AcceleratorJobStatus(_AcceleratorContractModel):
    """Polled state returned by an accelerator port.

    A success has a terminal artifact manifest.  Failure classes are explicit
    so the caller never treats a bad PDF, retryable provider failure, and a
    stale fence as equivalent.
    """

    external_job_id: str = Field(min_length=1)
    state: AcceleratorJobState
    logical_compute_key: str
    request_digest: str
    result_manifest: SuryaLayoutResultArtifactManifest | None = None
    reason_code: str | None = None

    @field_validator("external_job_id")
    @classmethod
    def _validate_provider_job_id(cls, value: str) -> str:
        return _validate_external_job_id(value)

    @field_validator("logical_compute_key", "request_digest")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("job status digest fields must be lowercase SHA-256 hex digests")
        return value

    @field_validator("reason_code", mode="before")
    @classmethod
    def _validate_reason_code(cls, value: object) -> object:
        return _validate_public_reason_code(value)

    @model_validator(mode="after")
    def _validate_state_payload(self) -> "AcceleratorJobStatus":
        if self.state == AcceleratorJobState.FENCE_LOST:
            raise ValueError(
                "fence_lost is a local coordinator outcome, not a remote job state"
            )
        if not self.state.is_terminal:
            if self.result_manifest is not None or self.reason_code is not None:
                raise ValueError("non-terminal accelerator status cannot carry a result or reason")
            return self

        if self.state == AcceleratorJobState.CANCELLED:
            if self.reason_code is not None:
                raise ValueError("cancelled status cannot carry a failure reason")
            if self.result_manifest is not None:
                self._validate_manifest_binding()
            return self

        if self.result_manifest is None:
            raise ValueError("terminal non-cancelled status requires a result manifest")
        self._validate_manifest_binding()

        if self.state == AcceleratorJobState.SUCCEEDED:
            if self.reason_code is not None:
                raise ValueError("succeeded status cannot carry a failure reason")
        else:
            if not self.reason_code:
                raise ValueError("failed status requires a public reason_code")
            if self.reason_code != self.result_manifest.reason_code:
                raise ValueError("failed status reason_code must match its result manifest")
        return self

    def _validate_manifest_binding(self) -> None:
        """Require a terminal manifest to be inseparable from its outer poll."""

        assert self.result_manifest is not None
        manifest = self.result_manifest
        if manifest.external_job_id != self.external_job_id:
            raise ValueError("result manifest external_job_id must match job status")
        if manifest.logical_compute_key != self.logical_compute_key:
            raise ValueError("result manifest logical_compute_key must match job status")
        if manifest.request_digest != self.request_digest:
            raise ValueError("result manifest request_digest must match job status")
        if manifest.status != self.state.value:
            raise ValueError("result manifest status must match job status state")


def validate_accelerator_result_acceptance(
    request: SuryaLayoutRequest,
    status: AcceleratorJobStatus,
) -> SuryaLayoutResultArtifactManifest:
    """Accept one completed accelerator result only when it matches its request.

    This is deliberately a pure EC2-worker-side gate: it does not download an
    object, contact a provider, mutate a database, or decide a retry.  A
    future adapter must call it *before* parsing a remote result artifact and
    then separately verify the downloaded object's digest and byte count.
    """

    request._assert_live_integrity()
    if status.state != AcceleratorJobState.SUCCEEDED or status.result_manifest is None:
        raise AcceleratorContractError("only a succeeded status with a manifest is acceptable")
    if status.logical_compute_key != request.logical_compute_key:
        raise AcceleratorContractError("status logical_compute_key does not match request")
    if status.request_digest != request.request_digest:
        raise AcceleratorContractError("status request_digest does not match request")

    manifest = status.result_manifest
    # AcceleratorJobStatus performs these same bindings at construction.  Keep
    # them here as defence in depth for an object produced by a future adapter
    # or deserializer that bypasses that model boundary.
    if (
        manifest.external_job_id != status.external_job_id
        or manifest.logical_compute_key != status.logical_compute_key
        or manifest.request_digest != status.request_digest
        or manifest.status != AcceleratorJobState.SUCCEEDED.value
    ):
        raise AcceleratorContractError("result manifest is not bound to the succeeded status")

    identity = request.identity
    expected_page_bindings = tuple(
        page.sidecar_binding
        for page in identity.page_images
    )
    if (
        manifest.source_sha256 != identity.source_sha256
        or manifest.source_page_count != identity.source_page_count
        or manifest.page_range != identity.page_range
        or manifest.render_manifest_schema_version
        != identity.render_manifest.schema_version
        or manifest.render_manifest_sha256
        != identity.render_manifest.render_manifest_sha256
        or manifest.page_images != expected_page_bindings
        or manifest.producer != identity.producer
    ):
        raise AcceleratorContractError("result manifest lineage does not match request identity")

    artifact = manifest.result_artifact
    if artifact is None:  # Defensive: succeeded manifests already reject this.
        raise AcceleratorContractError("succeeded result manifest has no result artifact")
    output_capability = request.result_upload_capability
    if artifact.object_key != output_capability.object_key:
        raise AcceleratorContractError("result artifact object_key does not match requested output")
    if artifact.size_bytes > request.resource_caps.max_output_bytes:
        raise AcceleratorContractError("result artifact exceeds request output cap")
    if artifact.size_bytes > output_capability.resource_caps.max_bytes:
        raise AcceleratorContractError("result artifact exceeds signed upload cap")
    if artifact.mime_type != "application/json":
        raise AcceleratorContractError("result artifact MIME type must be application/json")
    if artifact.schema_version != request.output_schema_version:
        raise AcceleratorContractError("result artifact schema_version is unsupported")
    return manifest
