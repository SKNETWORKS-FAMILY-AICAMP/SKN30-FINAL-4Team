"""Verified one-time loader for the frozen Model 1 serving artifact.

This module has no database, Supabase, OpenAI, Storage, or queue dependency.
It intentionally holds document text only for the duration of one prediction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

__all__ = [
    "EXPECTED_WEIGHT_SHA256",
    "Model1Runtime",
    "Model1RuntimeError",
    "canonical_input_digest",
    "verified_runtime_manifest_sha256",
]


EXPECTED_WEIGHT_SHA256 = "8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779"
_RUNTIME_FILES = (
    "inference.py",
    "label_mapping.json",
    "model/config.json",
    "model/model.safetensors",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)
_SERVICE_FILES = (
    "__init__.py",
    "app.py",
    "contract.py",
    "entrypoint.py",
    "healthcheck.py",
    "runtime.py",
    "settings.py",
    "requirements-model1.txt",
)
_FIELDS = ("title", "purpose", "content", "target_text")


class Model1RuntimeError(RuntimeError):
    """Opaque runtime failure; callers must not see source paths or input."""

    def __init__(self, detail: str = "model1_runtime_unavailable") -> None:
        super().__init__(detail)


def canonical_input_digest(value: Mapping[str, str]) -> str:
    """Return the stable idempotency digest of the exact four input fields."""

    payload = {field: value[field] for field in _FIELDS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Model1RuntimeError()
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise Model1RuntimeError() from None
    return module


def verified_runtime_manifest_sha256(runtime_directory: Path, preprocessor_path: Path) -> str:
    """Verify the immutable Model 1 bytes and return their service identity.

    This intentionally does not import Torch or allocate GPU memory, so the
    deployment operator can pin the resulting value in the backend before the
    resident service is launched.
    """

    try:
        if runtime_directory.is_symlink() or preprocessor_path.is_symlink():
            raise ValueError
        runtime = runtime_directory.resolve(strict=True)
        helper = preprocessor_path.resolve(strict=True)
        if not runtime.is_dir() or not helper.is_file() or helper.is_symlink():
            raise ValueError
        entries: dict[str, str] = {}
        for relative in _RUNTIME_FILES:
            path = runtime / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError
            entries[relative] = _file_sha256(path)
        if entries["model/model.safetensors"] != EXPECTED_WEIGHT_SHA256:
            raise ValueError
        entries["pipeline/dl07_m1_apply.py"] = _file_sha256(helper)
        service_directory = Path(__file__).resolve().parent
        for relative in _SERVICE_FILES:
            path = service_directory / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError
            entries[f"service/{relative}"] = _file_sha256(path)
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()
    except Model1RuntimeError:
        raise
    except Exception:
        raise Model1RuntimeError() from None


@dataclass
class Model1Runtime:
    """The only object that imports Torch/Transformers and owns GPU state."""

    runtime_directory: Path
    preprocessor_path: Path
    require_cuda: bool = True
    device: str = "cuda"
    _predict: Callable[[list[str]], object] | None = None
    _manifest_sha256: str | None = None

    @property
    def runtime_manifest_sha256(self) -> str:
        if self._manifest_sha256 is None:
            raise Model1RuntimeError()
        return self._manifest_sha256

    def load_and_warm(self) -> None:
        """Verify immutable bytes, require CUDA, then load/warm exactly once."""

        if self._predict is not None:
            return
        try:
            if self.device not in {"cpu", "cuda"} or self.require_cuda != (self.device == "cuda"):
                raise ValueError
            # The team's frozen inference.py chooses CUDA from
            # torch.cuda.is_available().  Set this *before importing torch or
            # inference* so explicit CPU mode cannot accidentally claim a GPU
            # merely because one is visible on a shared RunPod host.
            if self.device == "cpu":
                os.environ["CUDA_VISIBLE_DEVICES"] = ""
            runtime = self.runtime_directory.resolve(strict=True)
            helper = self.preprocessor_path.resolve(strict=True)
            self._manifest_sha256 = verified_runtime_manifest_sha256(
                self.runtime_directory, self.preprocessor_path
            )

            import torch

            if self.device == "cuda" and not torch.cuda.is_available():
                raise ValueError
            helper_parent = str(helper.parent)
            if helper_parent not in sys.path:
                sys.path.insert(0, helper_parent)
            _load_module("dl07_m1_apply", helper)
            module = _load_module("prereview_resident_model1_inference", runtime / "inference.py")
            predict = getattr(module, "predict", None)
            if not callable(predict):
                raise ValueError
            # The wrapper's first invocation performs AutoModel loading and
            # allocates a small real CUDA inference.  No request is accepted
            # until this succeeds.
            warm = predict(["지원 사업 분류 준비"], already_cleaned=False)
            _validate_predictions(warm)
            self._predict = predict
        except Model1RuntimeError:
            raise
        except Exception:
            self._manifest_sha256 = None
            raise Model1RuntimeError() from None

    def predict(self, fields: Mapping[str, str]) -> dict[str, object]:
        if self._predict is None:
            raise Model1RuntimeError()
        text = "\n".join(fields[field].strip() for field in _FIELDS if fields[field].strip())
        try:
            result = self._predict([text], already_cleaned=False)
            return _validate_predictions(result)
        except Model1RuntimeError:
            raise
        except Exception:
            raise Model1RuntimeError() from None


def _validate_predictions(value: object) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        raise Model1RuntimeError()
    row = value[0]
    if set(row) != {"support_type_pred", "confidence", "status"}:
        raise Model1RuntimeError()
    label, confidence, status = row["support_type_pred"], row["confidence"], row["status"]
    if (
        not isinstance(label, str)
        or not label.strip()
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
        or status not in {"판단보류", "참고용", "신뢰"}
    ):
        raise Model1RuntimeError()
    return {
        "support_type_pred": label,
        "confidence": float(confidence),
        "status": status,
    }
