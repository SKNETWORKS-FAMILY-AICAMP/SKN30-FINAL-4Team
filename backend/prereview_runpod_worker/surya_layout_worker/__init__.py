"""Textless, PDF-free RunPod handler core for Surya layout inference.

The import surface intentionally exposes only the portable core.  Deployment
modules live beside it but are not eagerly imported:

* :mod:`.http_storage` binds signed HTTPS capabilities;
* :mod:`.surya_inference` lazily loads Pillow/Surya on the GPU image;
* :mod:`.settings` composes one strict cold-start configuration; and
* :mod:`.entrypoint` lazily imports the pinned RunPod SDK in ``main()``.

This keeps normal API/worker tests free of RunPod, Pillow and Surya imports.
"""

from .handler import (
    ContentPortFailure,
    InferencePort,
    InferenceContentFailure,
    InferenceInfrastructureFailure,
    InfrastructurePortFailure,
    RunPodSuryaLayoutHandler,
    RunPodSuryaExecutionPolicy,
    StorageContentFailure,
    StorageGetContentFailure,
    StorageInfrastructureFailure,
    StoragePort,
    StoragePutConflictFailure,
    SuryaLayoutPageResult,
    SuryaModelConfidenceMarker,
    handle_event,
)

__all__ = [
    "ContentPortFailure",
    "InferencePort",
    "InferenceContentFailure",
    "InferenceInfrastructureFailure",
    "InfrastructurePortFailure",
    "RunPodSuryaLayoutHandler",
    "RunPodSuryaExecutionPolicy",
    "StorageContentFailure",
    "StorageGetContentFailure",
    "StorageInfrastructureFailure",
    "StoragePort",
    "StoragePutConflictFailure",
    "SuryaLayoutPageResult",
    "SuryaModelConfidenceMarker",
    "handle_event",
]
