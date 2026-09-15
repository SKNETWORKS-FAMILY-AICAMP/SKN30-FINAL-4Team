"""Explicit opt-in facade for exact-native CandidatePack augmentation.

The facade is intentionally standalone: callers choose whether the transform
is enabled and it neither routes blocks nor serializes a source-selection
artifact.  It only exposes lossless native lexical candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .models import CandidatePack, SourceBlock
from .native_line_atoms import augment_pack_with_native_line_atoms
from .native_span_composition import augment_pack_with_native_continuations


@dataclass(frozen=True, slots=True)
class NativeExactTransformOptions:
    """Versionless local switch for the bounded exact-native transforms."""

    enabled: bool = False
    include_line_atoms: bool = True
    include_continuations: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")
        if not isinstance(self.include_line_atoms, bool):
            raise TypeError("include_line_atoms must be bool")
        if not isinstance(self.include_continuations, bool):
            raise TypeError("include_continuations must be bool")
        if self.enabled and not (self.include_line_atoms or self.include_continuations):
            raise ValueError("enabled native exact transforms require at least one transform")


DISABLED_NATIVE_EXACT_TRANSFORMS = NativeExactTransformOptions()
ENABLED_NATIVE_EXACT_TRANSFORMS = NativeExactTransformOptions(enabled=True)
NATIVE_EXACT_TRANSFORM_GENERATOR = "semantic_structuring.native_exact_transform"
NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION = "1"
NATIVE_EXACT_TRANSFORM_PACK_SUFFIX = "-native-exact-v1"


@dataclass(frozen=True, slots=True)
class NativeExactTransformResult:
    """Transformed pack together with its durable parent identity.

    CandidatePack's generator/version identify the exact transform variant.
    Parent identity is also stored on the transformed pack so a serialized
    artifact remains auditable and a second application stays idempotent.
    """

    pack: CandidatePack
    parent_pack_id: str
    parent_generator: str | None
    parent_generator_version: str | None
    parent_common_ir_document_id: str | None


def _transform_variant(options: NativeExactTransformOptions) -> str:
    enabled = []
    if options.include_line_atoms:
        enabled.append("lines")
    if options.include_continuations:
        enabled.append("continuations")
    return "+".join(enabled)


def _update_identity_digest(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, byteorder="big"))
    digest.update(encoded)


def _source_block_order(block: SourceBlock) -> tuple[int, str]:
    return (
        block.source_order if block.source_order is not None else 10**12,
        block.block_id,
    )


def _source_identity_digest(
    pack: CandidatePack,
    *,
    base_blocks: list[SourceBlock],
    parent_pack_id: str,
    parent_generator: str,
    parent_generator_version: str,
) -> str:
    """Hash parent lineage plus ordered atomic content without exposing text."""

    digest = hashlib.sha256()
    for value in (
        parent_pack_id,
        parent_generator,
        parent_generator_version,
        pack.common_ir_document_id or "",
        pack.notice_id,
        str(pack.extraction_scope),
        pack.question,
    ):
        _update_identity_digest(digest, value)
    for block in base_blocks:
        canonical = json.dumps(
            block.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _update_identity_digest(digest, canonical)
    return digest.hexdigest()


def apply_native_exact_transforms(
    pack: CandidatePack,
    *,
    options: NativeExactTransformOptions,
) -> NativeExactTransformResult:
    """Return the transformed pack plus its immutable, out-of-band origin."""

    if not isinstance(options, NativeExactTransformOptions):
        raise TypeError("options must be NativeExactTransformOptions")
    parent_pack_id = pack.parent_pack_id or pack.pack_id
    result_identity = dict(
        parent_pack_id=parent_pack_id,
        parent_generator=pack.parent_generator or pack.generator,
        parent_generator_version=pack.parent_generator_version or pack.generator_version,
        parent_common_ir_document_id=pack.common_ir_document_id,
    )
    if not options.enabled:
        return NativeExactTransformResult(pack=pack, **result_identity)

    # Reject a graph forged through Pydantic ``model_copy`` before deriving
    # anything from it.  Keep ``pack`` itself, rather than the round-tripped
    # value, so SourceBlock private Common-IR geometry survives.
    CandidatePack.model_validate(pack.model_dump(mode="python"))
    if (
        result_identity["parent_generator"] is None
        or result_identity["parent_generator_version"] is None
        or pack.common_ir_document_id is None
    ):
        raise ValueError("enabled native exact transforms require complete Common IR CandidatePack lineage")
    # Rebuild derived blocks from atomic sources on every enabled invocation.
    # This makes the facade idempotent for a fixed option set and ensures a
    # pre-existing line atom cannot interrupt base-block continuation order.
    base_blocks = sorted(
        (
            block
            for block in pack.blocks
            if not block.source_spans and block.native_parent_block_id is None
        ),
        key=_source_block_order,
    )
    variant = _transform_variant(options)
    source_digest = _source_identity_digest(
        pack,
        base_blocks=base_blocks,
        parent_pack_id=parent_pack_id,
        parent_generator=result_identity["parent_generator"],
        parent_generator_version=result_identity["parent_generator_version"],
    )
    transformed = pack.model_copy(update={
        "pack_id": (
            f"{parent_pack_id}{NATIVE_EXACT_TRANSFORM_PACK_SUFFIX}-"
            f"{variant}-{source_digest[:16]}"
        ),
        "generator": NATIVE_EXACT_TRANSFORM_GENERATOR,
        "generator_version": f"{NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION}:{variant}",
        "parent_pack_id": parent_pack_id,
        "parent_generator": result_identity["parent_generator"],
        "parent_generator_version": result_identity["parent_generator_version"],
        "blocks": base_blocks,
    })
    # This order intentionally matches the reviewed RunPod runner and the
    # Gold100 source-universe comparator: lines first, then continuations.
    if options.include_line_atoms:
        transformed = augment_pack_with_native_line_atoms(transformed)
    if options.include_continuations:
        transformed = augment_pack_with_native_continuations(transformed)
    # Both helpers intentionally return the original instance when they have
    # nothing to add.  Validate unconditionally so the model_copy above can
    # never return a graph whose transformed lineage violates CandidatePack.
    CandidatePack.model_validate(transformed.model_dump(mode="python"))
    return NativeExactTransformResult(pack=transformed, **result_identity)


def augment_pack_with_native_exact_transforms(
    pack: CandidatePack,
    *,
    options: NativeExactTransformOptions,
) -> CandidatePack:
    """Apply exact-native transforms only when an explicit enabled option is supplied.

    The reviewed order is native lines first, then bounded continuations.
    The latter retains the runner/comparator's deterministic ordering rule.
    """

    return apply_native_exact_transforms(pack, options=options).pack
