"""Deterministic, fail-closed primitives for PDF fusion work.

This package is deliberately independent from Common IR v1 generation.  Its
coordinate manifest establishes how an archived PDF page was rendered before
any layout/OCR artifact may be aligned to native PDF evidence.
"""

from .coordinate_manifest import (
    AffineTransform,
    CoordinateManifestError,
    PdfCoordinateManifest,
    build_sidecar_binding,
    pixel_to_user_bbox,
    user_to_pixel_bbox,
    validate_sidecar_binding,
)
from .render_manifest import (
    PNG_MIME_TYPE,
    PdfRenderManifest,
    PdfRenderManifestError,
    RenderedPage,
    RenderedPageInput,
    assemble_render_manifest,
    validate_render_manifest_files,
)
from .surya_layout_artifact import (
    REGION_LABELS_V1,
    SCHEMA_VERSION as SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION,
    SuryaLayoutArtifact,
    SuryaLayoutArtifactError,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity,
    parse_surya_layout_artifact_bytes,
    validate_surya_layout_artifact,
)

__all__ = [
    "AffineTransform",
    "CoordinateManifestError",
    "PdfCoordinateManifest",
    "build_sidecar_binding",
    "pixel_to_user_bbox",
    "user_to_pixel_bbox",
    "validate_sidecar_binding",
    "PNG_MIME_TYPE",
    "PdfRenderManifest",
    "PdfRenderManifestError",
    "RenderedPage",
    "RenderedPageInput",
    "assemble_render_manifest",
    "validate_render_manifest_files",
    "REGION_LABELS_V1",
    "SURYA_LAYOUT_ARTIFACT_SCHEMA_VERSION",
    "SuryaLayoutArtifact",
    "SuryaLayoutArtifactError",
    "SuryaLayoutPage",
    "SuryaLayoutRegion",
    "SuryaProducerIdentity",
    "parse_surya_layout_artifact_bytes",
    "validate_surya_layout_artifact",
]
