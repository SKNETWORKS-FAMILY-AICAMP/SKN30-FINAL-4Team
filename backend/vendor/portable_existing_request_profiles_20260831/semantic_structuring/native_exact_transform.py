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

from .models import CandidatePack, NATIVE_EXACT_TRANSFORM_GENERATOR, SourceBlock
from .native_line_atoms import augment_pack_with_native_line_atoms
from .native_span_composition import augment_pack_with_native_continuations


@dataclass(frozen=True, slots=True)
class NativeExactTransformOptions:
    """Explicit, identity-bearing switch for bounded native transforms."""

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
NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION = "2"
NATIVE_EXACT_TRANSFORM_PACK_SUFFIX = "-native-exact-v2"
LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION = "1"


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


def _pack_suffix(producer_version: str) -> str:
    if producer_version == NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION:
        return NATIVE_EXACT_TRANSFORM_PACK_SUFFIX
    return f"-native-exact-v{producer_version}"


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
    """Return a current-v2 transformed pack plus immutable parent lineage."""

    return _apply_native_exact_transforms(
        pack,
        options=options,
        producer_version=NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    )


def _apply_native_exact_transforms(
    pack: CandidatePack,
    *,
    options: NativeExactTransformOptions,
    producer_version: str,
) -> NativeExactTransformResult:
    """Apply a selected producer version at the durable replay boundary."""

    if not isinstance(options, NativeExactTransformOptions):
        raise TypeError("options must be NativeExactTransformOptions")
    if producer_version not in {
        LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
        NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    }:
        raise ValueError("native exact transform producer version is unsupported")
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
            f"{parent_pack_id}{_pack_suffix(producer_version)}-"
            f"{variant}-{source_digest[:16]}"
        ),
        "generator": NATIVE_EXACT_TRANSFORM_GENERATOR,
        "generator_version": f"{producer_version}:{variant}",
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
        transformed = augment_pack_with_native_continuations(
            transformed,
            # v1 did not recognize the following-item structural boundary.
            # Its only supported use is exact persisted-pack replay.
            block_structural_following_item=(
                producer_version != LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION
            ),
        )
    # Both helpers intentionally return the original instance when they have
    # nothing to add.  Validate unconditionally so the model_copy above can
    # never return a graph whose transformed lineage violates CandidatePack.
    CandidatePack.model_validate(transformed.model_dump(mode="python"))
    return NativeExactTransformResult(pack=transformed, **result_identity)


def replay_persisted_native_exact_transforms(
    pack: CandidatePack,
    *,
    producer_version: str,
    include_line_atoms: bool,
    include_continuations: bool,
) -> CandidatePack:
    """Recreate a recorded v1/v2 pack; never use this for new production work."""

    options = NativeExactTransformOptions(
        enabled=True,
        include_line_atoms=include_line_atoms,
        include_continuations=include_continuations,
    )
    return _apply_native_exact_transforms(
        pack,
        options=options,
        producer_version=producer_version,
    ).pack


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
