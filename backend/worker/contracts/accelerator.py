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
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic import field_validator, model_validator


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
    "PageImageBinding",
    "PageImageInput",
    "PageRange",
    "SignedStorageCapability",
    "StorageResourceCaps",
    "SuryaLayoutRequest",
    "SuryaLayoutResultArtifactManifest",
    "AcceleratorJobStatus",
    "validate_accelerator_result_acceptance",
    "validate_accelerator_dispatch",
    "build_logical_compute_key",
]


_SHA256_LENGTH = 64
_PUBLIC_REASON_CODE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_WORKER_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
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


def _canonical_https_origin(url: str) -> str:
    """Return the one canonical origin admitted by this v1 contract."""

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

    Signed URLs are intentionally represented by :class:`SecretStr`; their
    query signatures are capability material, not an API token field.  A
    separate ``token``/``jwt``/``DATABASE_URL`` property must never cross this
    boundary, even in an otherwise nested JSON object.
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
        str_strip_whitespace=True,
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
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Any:
        _reject_credential_fields(obj)
        return super().model_validate(obj, *args, **kwargs)

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
        return super().model_validate_json(json_data, *args, **kwargs)


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

    ``url`` is a :class:`SecretStr` so Pydantic repr/JSON logging masks its
    signature.  Future HTTP adapters must use :meth:`signed_url` only at the
    request boundary and must log :meth:`safe_log_record` instead.
    """

    method: Literal["GET", "PUT"]
    url: SecretStr = Field(repr=False)
    storage_host: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    path: str = Field(min_length=1, repr=False)
    object_key: str = Field(min_length=1, repr=False)
    expires_at: datetime
    resource_caps: StorageResourceCaps
    redirects_allowed: Literal[False] = False

    @field_validator("url")
    @classmethod
    def _require_https_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("signed storage capability URL must use HTTPS")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("signed storage capability URL has an invalid port") from error
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("signed storage capability URL must not contain userinfo or a fragment")
        # The current provider-neutral contract deliberately permits only the
        # canonical HTTPS origin (443).  Should a provider require another
        # port, the contract must grow an explicit origin field and policy
        # comparison rather than silently treating hostname alone as origin.
        if port not in {None, 443}:
            raise ValueError("signed storage capability URL must use HTTPS port 443")
        return value

    @field_validator("path")
    @classmethod
    def _require_absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("capability path must be absolute")
        return value

    @field_validator("bucket")
    @classmethod
    def _require_simple_bucket_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value:
            raise ValueError("bucket must be a single storage bucket name")
        return value

    @field_validator("object_key")
    @classmethod
    def _require_relative_object_key(cls, value: str) -> str:
        if value.startswith("/") or value in {".", ".."} or ".." in value.split("/"):
            raise ValueError("object_key must be a relative storage key")
        return value

    @field_validator("expires_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _match_url_binding(self) -> "SignedStorageCapability":
        parsed = urlsplit(self.url.get_secret_value())
        if parsed.hostname != self.storage_host:
            raise ValueError("storage_host must match the signed URL host")
        if parsed.path != self.path:
            raise ValueError("capability path must match the signed URL path")
        expected_object_suffix = f"/{self.bucket}/{self.object_key}"
        if not parsed.path.endswith(expected_object_suffix):
            raise ValueError("capability path must bind its bucket and object_key")
        return self

    def signed_url(self) -> str:
        """Return the raw signed URL only for the future network adapter."""

        return self.url.get_secret_value()

    def binding_for_digest(self) -> dict[str, Any]:
        """Return stable, non-secret capability semantics for request identity.

        URL query signatures and expiration intentionally do not participate:
        replacing an expired capability must not create another logical job.
        """

        return {
            "method": self.method,
            "storage_host": self.storage_host,
            "bucket": self.bucket,
            "path": self.path,
            "object_key": self.object_key,
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "redirects_allowed": self.redirects_allowed,
        }

    def safe_log_record(self) -> dict[str, Any]:
        """Metadata suitable for logs; document paths and query signatures stay hidden."""

        return {
            "method": self.method,
            "url": "<redacted>",
            "storage_host": self.storage_host,
            "bucket": self.bucket,
            "path": "<redacted>",
            "object_key": "<redacted>",
            "expires_at": self.expires_at.isoformat(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "redirects_allowed": self.redirects_allowed,
        }


class PageImageInput(_AcceleratorContractModel):
    """One canonical, immutable page image and its GET-only capability."""

    page_number: int = Field(ge=1)
    page_image_sha256: str
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=1)
    pixel_width: int = Field(gt=0)
    pixel_height: int = Field(gt=0)
    capability: SignedStorageCapability = Field(repr=False)

    @field_validator("page_image_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("page_image_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def _require_get_capability(self) -> "PageImageInput":
        if self.capability.method != "GET":
            raise ValueError("page image capability must use GET")
        if self.size_bytes > self.capability.resource_caps.max_bytes:
            raise ValueError("page image size exceeds its capability cap")
        if self.mime_type not in self.capability.resource_caps.allowed_mime_types:
            raise ValueError("page image MIME type is not allowed by its capability")
        return self

    @property
    def rendered_pixels(self) -> int:
        """Concrete pixel count used against the request-wide resource cap."""

        return self.pixel_width * self.pixel_height


class PageImageBinding(_AcceleratorContractModel):
    """Non-secret page/hash binding repeated by a remote result manifest."""

    page_number: int = Field(ge=1)
    page_image_sha256: str

    @field_validator("page_image_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("page_image_sha256 must be a lowercase SHA-256 hex digest")
        return value


class LayoutComputeIdentity(_AcceleratorContractModel):
    """The immutable inputs that define a Surya layout computation.

    Storage object locations, signed URL query tokens, expiry timestamps, DB
    attempts and fence values are intentionally absent.  The same pages run by
    a reclaimed attempt therefore yield the same ``logical_compute_key``.
    """

    source_sha256: str
    source_page_count: int = Field(ge=1)
    page_range: PageRange
    page_images: tuple[PageImageInput, ...] = Field(min_length=1)
    mode: Literal["layout"] = "layout"
    pipeline_revision: str = Field(min_length=1)
    model_weights_sha256: str
    config_digest: str
    coordinate_manifest_sha256: str
    worker_image_digest: str = Field(min_length=1)
    # These are explicit wire fields rather than computed_field properties so
    # ``model_validate(model.model_dump())`` is stable.  Empty values are
    # filled only at first construction; supplied values are recomputed and
    # rejected if they do not describe the immutable identity above.
    logical_compute_key: str = ""

    @field_validator(
        "source_sha256",
        "model_weights_sha256",
        "config_digest",
        "coordinate_manifest_sha256",
    )
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("identity hash fields must be lowercase SHA-256 hex digests")
        return value

    @field_validator("worker_image_digest")
    @classmethod
    def _validate_worker_digest(cls, value: str) -> str:
        return _validate_worker_image_digest(value)

    @model_validator(mode="after")
    def _validate_page_coverage(self) -> "LayoutComputeIdentity":
        if self.page_range.end_page > self.source_page_count:
            raise ValueError("page_range extends beyond source_page_count")
        numbers = tuple(image.page_number for image in self.page_images)
        if len(set(numbers)) != len(numbers):
            raise ValueError("page_images must not repeat a page number")
        if tuple(sorted(numbers)) != self.page_range.page_numbers:
            raise ValueError("page_images must cover the page_range exactly once")
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
            "page_images": [
                {
                    "page_number": image.page_number,
                    "page_image_sha256": image.page_image_sha256,
                }
                for image in sorted(self.page_images, key=lambda image: image.page_number)
            ],
            "page_range": self.page_range.model_dump(mode="json"),
            "mode": self.mode,
            "pipeline_revision": self.pipeline_revision,
            "model_weights_sha256": self.model_weights_sha256,
            "config_digest": self.config_digest,
            "coordinate_manifest_sha256": self.coordinate_manifest_sha256,
        }

def build_logical_compute_key(identity: LayoutComputeIdentity) -> str:
    """Build the attempt- and capability-independent computation identity."""

    return _sha256_digest(identity.compute_key_payload())


class ArtifactDescriptor(_AcceleratorContractModel):
    """Artifact metadata returned by a remote accelerator, before local acceptance."""

    object_key: str = Field(min_length=1, repr=False)
    sha256: str
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    @field_validator("sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("artifact SHA-256 must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("object_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if value.startswith("/") or value in {".", ".."} or ".." in value.split("/"):
            raise ValueError("artifact object_key must be a relative storage key")
        return value


class SuryaLayoutRequest(_AcceleratorContractModel):
    """Provider-neutral request envelope for a remote Surya layout job."""

    contract_version: Literal["surya_layout_request/v1"] = "surya_layout_request/v1"
    input_schema_version: Literal["surya_layout_input/v1"] = "surya_layout_input/v1"
    output_schema_version: Literal["surya_layout_result_manifest/v1"] = (
        "surya_layout_result_manifest/v1"
    )
    identity: LayoutComputeIdentity
    result_upload_capability: SignedStorageCapability = Field(repr=False)
    resource_caps: AcceleratorResourceCaps
    logical_compute_key: str = ""
    request_digest: str = ""

    @model_validator(mode="after")
    def _require_put_output(self) -> "SuryaLayoutRequest":
        if self.result_upload_capability.method != "PUT":
            raise ValueError("result upload capability must use PUT")
        if self.resource_caps.max_page_count < len(self.identity.page_images):
            raise ValueError("resource max_page_count is below the requested page count")
        if self.resource_caps.max_total_input_bytes < sum(
            page.size_bytes for page in self.identity.page_images
        ):
            raise ValueError("resource max_total_input_bytes is below page image input bytes")
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

        expected_request_digest = _sha256_digest(self.request_digest_payload())
        if self.request_digest and self.request_digest != expected_request_digest:
            raise ValueError("request_digest does not match request envelope")
        if not self.request_digest:
            object.__setattr__(self, "request_digest", expected_request_digest)
        return self

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
            "page_image_capabilities": [
                page.capability.binding_for_digest()
                for page in sorted(self.identity.page_images, key=lambda page: page.page_number)
            ],
            "result_upload_capability": self.result_upload_capability.binding_for_digest(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
            "worker_image_digest": self.identity.worker_image_digest,
        }

    def safe_log_record(self) -> dict[str, Any]:
        """Envelope metadata fit for a log line without URLs or object paths."""

        return {
            "contract_version": self.contract_version,
            "logical_compute_key": self.logical_compute_key,
            "request_digest": self.request_digest,
            "page_count": len(self.identity.page_images),
            "input_capabilities": [
                page.capability.safe_log_record() for page in self.identity.page_images
            ],
            "result_upload_capability": self.result_upload_capability.safe_log_record(),
            "resource_caps": self.resource_caps.model_dump(mode="json"),
        }

    def to_wire_payload(self) -> dict[str, Any]:
        """Return the explicit network payload, including signed URLs.

        Pydantic deliberately masks ``SecretStr`` in ``model_dump(mode="json")``
        and ``model_dump_json``.  A provider adapter must use this method only
        at its outbound network boundary; it must never use this payload for
        logs, persistence, or a retry identity.
        """

        identity = self.identity
        return {
            "contract_version": self.contract_version,
            "input_schema_version": self.input_schema_version,
            "output_schema_version": self.output_schema_version,
            "identity": {
                "source_sha256": identity.source_sha256,
                "source_page_count": identity.source_page_count,
                "page_range": identity.page_range.model_dump(mode="json"),
                "page_images": [
                    {
                        "page_number": page.page_number,
                        "page_image_sha256": page.page_image_sha256,
                        "size_bytes": page.size_bytes,
                        "mime_type": page.mime_type,
                        "pixel_width": page.pixel_width,
                        "pixel_height": page.pixel_height,
                        "capability": _capability_wire_payload(page.capability),
                    }
                    for page in identity.page_images
                ],
                "mode": identity.mode,
                "pipeline_revision": identity.pipeline_revision,
                "model_weights_sha256": identity.model_weights_sha256,
                "config_digest": identity.config_digest,
                "coordinate_manifest_sha256": identity.coordinate_manifest_sha256,
                "worker_image_digest": identity.worker_image_digest,
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


def _capability_wire_payload(capability: SignedStorageCapability) -> dict[str, Any]:
    """Serialize capability material only for an outbound accelerator request."""

    return {
        "method": capability.method,
        "url": capability.signed_url(),
        "storage_host": capability.storage_host,
        "bucket": capability.bucket,
        "path": capability.path,
        "object_key": capability.object_key,
        "expires_at": capability.expires_at.isoformat(),
        "resource_caps": capability.resource_caps.model_dump(mode="json"),
        "redirects_allowed": capability.redirects_allowed,
    }


class AcceleratorStorageScope(_AcceleratorContractModel):
    """One indivisible outbound storage grant owned by deployment policy.

    Keeping method, canonical origin, endpoint shape, bucket and object prefix
    in one rule prevents the Cartesian-product widening caused by independent
    allow-lists.  The endpoint template must bind both declared path values;
    for example ``/storage/v1/object/sign/{bucket}/{object_key}``.
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
            or "\\" in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError(
                "endpoint path template must be an absolute canonical path with one "
                "{bucket} and one {object_key} placeholder"
            )
        remainder = value.replace("{bucket}", "").replace("{object_key}", "")
        if "{" in remainder or "}" in remainder:
            raise ValueError("endpoint path template contains an unsupported placeholder")
        return value

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value:
            raise ValueError("dispatch scope bucket must be one storage bucket")
        return value

    @field_validator("object_key_prefix")
    @classmethod
    def _validate_object_prefix(cls, value: str) -> str:
        if (
            value.startswith("/")
            or not value.endswith("/")
            or not value[:-1]
            or any(part in {"", ".", ".."} for part in value[:-1].split("/"))
        ):
            raise ValueError(
                "dispatch object-key prefix must be non-empty relative path segments ending in '/'"
            )
        return value

    def permits(self, capability: SignedStorageCapability) -> bool:
        parsed = urlsplit(capability.signed_url())
        origin = f"https://{parsed.hostname}" if parsed.hostname else ""
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
    required_expiry = now + timedelta(seconds=request.resource_caps.ttl_seconds)
    capabilities = (
        *(page.capability for page in request.identity.page_images),
        request.result_upload_capability,
    )
    for capability in capabilities:
        if not any(scope.permits(capability) for scope in policy.allowed_scopes):
            raise AcceleratorContractError(
                "storage capability is not allowed by any bound dispatch scope"
            )
        if capability.expires_at < required_expiry:
            raise AcceleratorContractError("storage capability expires before dispatch TTL")


class SuryaLayoutResultArtifactManifest(_AcceleratorContractModel):
    """Terminal remote result metadata; its artifact remains untrusted until EC2 verifies it."""

    contract_version: Literal["surya_layout_result_manifest/v1"] = (
        "surya_layout_result_manifest/v1"
    )
    external_job_id: str = Field(min_length=1)
    status: Literal["succeeded", "content_failed", "infra_retryable", "fence_lost", "cancelled"]
    logical_compute_key: str
    request_digest: str
    result_artifact: ArtifactDescriptor | None = None
    source_sha256: str
    source_page_count: int = Field(ge=1)
    page_range: PageRange
    page_images: tuple[PageImageBinding, ...] = Field(min_length=1)
    coordinate_manifest_sha256: str
    pipeline_revision: str = Field(min_length=1)
    model_weights_sha256: str
    config_digest: str
    worker_image_digest: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    reason_code: str | None = None

    @field_validator(
        "logical_compute_key",
        "request_digest",
        "source_sha256",
        "coordinate_manifest_sha256",
        "model_weights_sha256",
        "config_digest",
    )
    @classmethod
    def _validate_manifest_hashes(cls, value: str) -> str:
        if not _is_sha256(value):
            raise ValueError("manifest digest fields must be lowercase SHA-256 hex digests")
        return value

    @field_validator("worker_image_digest")
    @classmethod
    def _validate_worker_digest(cls, value: str) -> str:
        return _validate_worker_image_digest(value)

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
        page_numbers = tuple(image.page_number for image in self.page_images)
        if len(set(page_numbers)) != len(page_numbers):
            raise ValueError("manifest page_images must not repeat a page number")
        if tuple(sorted(page_numbers)) != self.page_range.page_numbers:
            raise ValueError("manifest page_images must cover the page_range exactly once")
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
        PageImageBinding(
            page_number=page.page_number,
            page_image_sha256=page.page_image_sha256,
        )
        for page in identity.page_images
    )
    if (
        manifest.source_sha256 != identity.source_sha256
        or manifest.source_page_count != identity.source_page_count
        or manifest.page_range != identity.page_range
        or manifest.page_images != expected_page_bindings
        or manifest.coordinate_manifest_sha256 != identity.coordinate_manifest_sha256
        or manifest.pipeline_revision != identity.pipeline_revision
        or manifest.model_weights_sha256 != identity.model_weights_sha256
        or manifest.config_digest != identity.config_digest
        or manifest.worker_image_digest != identity.worker_image_digest
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
    if artifact.mime_type not in output_capability.resource_caps.allowed_mime_types:
        raise AcceleratorContractError("result artifact MIME type is not permitted")
    if artifact.schema_version != "surya_layout_result/v1":
        raise AcceleratorContractError("result artifact schema_version is unsupported")
    return manifest
