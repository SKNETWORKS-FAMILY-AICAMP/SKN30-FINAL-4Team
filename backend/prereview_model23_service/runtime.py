"""Verified one-time loader for the frozen Model 2 and Model 3 runtimes.

The process has no database, Supabase, Storage, queue, or OpenAI dependency.
It verifies the registered joblib/parquet bytes before importing them and keeps
the loaded bundle/reference pool resident until process shutdown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import importlib.util
from importlib import metadata
import json
import math
from pathlib import Path
import sys
from typing import Any

__all__ = [
    "EXPECTED_MODEL2_BUNDLE_SHA256",
    "EXPECTED_MODEL2_COHORT_SHA256",
    "EXPECTED_MODEL2_TAXONOMY_SHA256",
    "EXPECTED_MODEL3_POOL_SHA256",
    "Model23Runtime",
    "Model23RuntimeError",
    "canonical_input_digest",
    "verified_runtime_manifest_sha256",
]


EXPECTED_MODEL2_BUNDLE_SHA256 = (
    "0c5b93e2a04c778ace8e07d7551b1fc7c3cc2b91cde80d94b2b1b1cf38cbabff"
)
EXPECTED_MODEL2_COHORT_SHA256 = (
    "551c4b2018d567419cc39344e87646e70f2d0a2ce58b6748d36573254ba0439d"
)
EXPECTED_MODEL2_TAXONOMY_SHA256 = (
    "78ebba0d5264fc2b9d8092baba4f790d4fea147e0ad98e4ede3c87f248c22239"
)
EXPECTED_MODEL3_POOL_SHA256 = (
    "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf"
)

_ARTIFACTS = {
    "models/model2_canonical/model2_p3_bundle.joblib": EXPECTED_MODEL2_BUNDLE_SHA256,
    "serving/model2/cohort_reference.parquet": EXPECTED_MODEL2_COHORT_SHA256,
    "data/processed/business_taxonomy.parquet": EXPECTED_MODEL2_TAXONOMY_SHA256,
    "serving/model3/design_features_v3.parquet": EXPECTED_MODEL3_POOL_SHA256,
}
_REQUIRED_RUNTIME_DISTRIBUTIONS = {
    "fastapi": "0.115.14",
    "starlette": "0.46.2",
    "uvicorn": "0.52.4",
    "numpy": "2.4.4",
    "pandas": "3.0.3",
    "scikit-learn": "1.8.0",
    "scipy": "1.17.1",
    "joblib": "1.5.3",
    "pyarrow": "25.0.1",
    "xgboost-cpu": "3.4.1",
}
_SOURCE_DIRECTORIES = (
    "pipelines/model2",
    "pipelines/model3",
    "pipelines/shared",
)
_EXPLICIT_SOURCE_FILES = (
    "serving/model2/predict.py",
    "serving/model2/feature_builder.py",
    "serving/model2/preprocessing.py",
    "serving/model2/masking.py",
    "serving/model2/proximity.py",
    "serving/model2/router.py",
    "serving/model3/score.py",
    "serving/model3/inference.py",
    "serving/shared/preconsultation_adapter.py",
    "pipelines/model1/m01_support_type.py",
    "experiments/model2/core/m69_m2_source_features.py",
    "experiments/model2/core/m73_m2_routing_improvement.py",
    "experiments/model2/core/m82_m2_proximity_features.py",
)
_BASE_KEYS = (
    "row_id",
    "title",
    "support_type",
    "support_method",
    "support_unit",
    "amount_type",
    "cohort",
    "category_large",
    "industry",
    "industry_grp",
    "agency_type",
    "agency_grp",
    "program_stem",
    "year",
)
_QUANTITY_CONTEXT = {
    "support_scale": "지원규모",
    "cost_sharing": "자부담",
    "support_period": "지원기간",
    "total_budget": "총사업비",
}
_MODEL2_ROW_FIELDS = {
    "row_id",
    "input_completeness",
    "missing_features",
    "status",
    "pred_log10",
    "pred_won",
    "bucket_proba",
    "bucket",
    "bucket_edges_won",
    "proximity",
    "evidence_quality",
}
_MODEL2_ADAPTER_FIELDS = {
    "program_duration_years",
    "project_duration_years",
    "duration_evidence",
    "support_count_candidates",
    "support_count_basis",
    "amounts",
    "review",
}
_MODEL3_ROW_FIELDS = {
    "row_id",
    "score",
    "level",
    "cohort_key",
    "cohort_n",
    "top1_axis",
}
_MODEL3_AXES = {
    "log_per_recipient",
    "log_support_count",
    "support_ratio",
    "project_duration",
}


class Model23RuntimeError(RuntimeError):
    """Opaque runtime failure; never include source paths or document text."""

    def __init__(self) -> None:
        super().__init__("model23_runtime_unavailable")


def canonical_input_digest(value: Mapping[str, object]) -> str:
    """Hash the canonical worker payload, excluding its HTTP envelope."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise Model23RuntimeError() from None
    return sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    if not candidate.is_file() or candidate.is_symlink():
        raise Model23RuntimeError()
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise Model23RuntimeError() from None
    return resolved


def _runtime_source_paths(root: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for relative_directory in _SOURCE_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir() or directory.is_symlink():
            raise Model23RuntimeError()
        for candidate in sorted(directory.glob("*.py")):
            if candidate.name.startswith("test_"):
                continue
            relative = candidate.relative_to(root).as_posix()
            paths.append((relative, _verified_file(root, relative)))
    for relative in _EXPLICIT_SOURCE_FILES:
        paths.append((relative, _verified_file(root, relative)))
    if not paths:
        raise Model23RuntimeError()
    return paths


def verified_runtime_manifest_sha256(ml_root: Path) -> str:
    """Verify registered artifacts and hash the complete serving closure."""

    try:
        if ml_root.is_symlink():
            raise ValueError
        root = ml_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        entries: dict[str, str] = {}
        for relative, expected in _ARTIFACTS.items():
            path = _verified_file(root, relative)
            actual = _file_sha256(path)
            if actual != expected:
                raise ValueError
            entries[f"artifact/{relative}"] = actual
        for relative, path in _runtime_source_paths(root):
            entries[f"ml/{relative}"] = _file_sha256(path)
        service_directory = Path(__file__).resolve().parent
        for path in sorted(service_directory.glob("*.py")):
            if not path.is_file() or path.is_symlink():
                raise ValueError
            entries[f"service/{path.name}"] = _file_sha256(path)
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()
    except Model23RuntimeError:
        raise
    except Exception:
        raise Model23RuntimeError() from None


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Model23RuntimeError()
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise Model23RuntimeError() from None
    return module


def _verify_runtime_dependencies() -> None:
    """Bind the source manifest's version expectations to the live image."""

    try:
        for distribution, expected in _REQUIRED_RUNTIME_DISTRIBUTIONS.items():
            if metadata.version(distribution) != expected:
                raise ValueError
    except Exception:
        raise Model23RuntimeError() from None


@dataclass
class Model23Runtime:
    """Owner of one loaded Model 2 bundle and one prepared Model 3 pool."""

    ml_root: Path
    _model2: Any | None = None
    _model3: Any | None = None
    _manifest_sha256: str | None = None

    @property
    def runtime_manifest_sha256(self) -> str:
        if self._manifest_sha256 is None:
            raise Model23RuntimeError()
        return self._manifest_sha256

    def load_and_warm(self) -> None:
        """Verify bytes, import both frozen entry points, and warm once."""

        if self._model2 is not None or self._model3 is not None:
            if self._model2 is None or self._model3 is None:
                raise Model23RuntimeError()
            return
        try:
            root = self.ml_root.resolve(strict=True)
            manifest = verified_runtime_manifest_sha256(root)
            # The exact expectations above are part of runtime.py and thus of
            # ``manifest``.  Refuse an image whose installed numerical or HTTP
            # runtime does not match those source-pinned expectations.
            _verify_runtime_dependencies()
            # The source tree is a read-only image closure.  Prevent Python
            # from attempting to create __pycache__ beside it.
            sys.dont_write_bytecode = True
            model2 = _load_module(
                "prereview_resident_model2_predict",
                root / "serving" / "model2" / "predict.py",
            )
            load_model2 = getattr(model2, "load", None)
            predict_model2 = getattr(model2, "predict_document", None)
            if not callable(load_model2) or not callable(predict_model2):
                raise ValueError
            load_model2()

            model3 = _load_module(
                "prereview_resident_model3_score",
                root / "serving" / "model3" / "score.py",
            )
            predict_model3 = getattr(model3, "score_document", None)
            implementation = getattr(model3, "_impl", None)
            load_pool = getattr(implementation, "_get_pool", None)
            if not callable(predict_model3) or not callable(load_pool):
                raise ValueError
            pool = load_pool()
            if not hasattr(pool, "__len__") or len(pool) == 0:
                raise ValueError

            # Exercise each frozen prediction graph before readiness.  These
            # fixed synthetic rows are startup probes, never stored results.
            warm2 = predict_model2(
                "지원기간 12개월, 기업당 최대 1억원, 사업비의 70% 지원",
                base={
                    "title": "상주 런타임 준비",
                    "support_type": "사업화",
                    "support_method": "grant",
                    "support_unit": "company",
                    "amount_type": "per_company",
                },
            )
            _validate_model2_output(_jsonable(warm2))
            warm3 = predict_model3(
                {
                    "features": {
                        "row_id": "WARMUP",
                        "support_type": "사업화",
                        "support_method": "grant",
                        "support_unit": "company",
                        "amount_type": "per_company",
                        "per_recipient": 100_000_000,
                        "support_count": 3,
                        "project_duration": 1.0,
                        "support_ratio": 70.0,
                    },
                    "duration_evidence": {
                        "project": {"basis": "지원기간", "value": 1.0}
                    },
                }
            )
            _validate_model3_scored(warm3)
            self._model2 = model2
            self._model3 = model3
            self._manifest_sha256 = manifest
        except Model23RuntimeError:
            self._model2 = None
            self._model3 = None
            self._manifest_sha256 = None
            raise
        except Exception:
            self._model2 = None
            self._model3 = None
            self._manifest_sha256 = None
            raise Model23RuntimeError() from None

    def predict_model2(self, payload: Mapping[str, object]) -> dict[str, object]:
        if self._model2 is None:
            raise Model23RuntimeError()
        try:
            raw = self._model2.predict_document(
                _text(payload),
                base=_base(payload),
            )
            return _validate_model2_output(_jsonable(raw))
        except Model23RuntimeError:
            raise
        except Exception:
            raise Model23RuntimeError() from None

    def predict_model3(self, payload: Mapping[str, object]) -> dict[str, object]:
        if self._model2 is None or self._model3 is None:
            raise Model23RuntimeError()
        try:
            text = _text(payload) or _quantity_evidence_text(payload)
            if not text:
                raise ValueError
            meta = self._model2.PA.adapt(text, base=_base(payload))
            return _validate_model3_scored(self._model3.score_document(meta))
        except Model23RuntimeError:
            raise
        except Exception:
            raise Model23RuntimeError() from None


def _text(payload: Mapping[str, object]) -> str:
    value = payload.get("evidence_text", "")
    return "" if value is None else str(value)


def _base(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: payload[key] for key in _BASE_KEYS if key in payload}


def _quantity_evidence_text(payload: Mapping[str, object]) -> str:
    quantities = payload.get("quantities")
    if not isinstance(quantities, Mapping):
        return ""
    lines: list[str] = []
    for key, values in quantities.items():
        label = _QUANTITY_CONTEXT.get(str(key))
        if label is None or not isinstance(values, list):
            continue
        lines.extend(f"{label}: {value.strip()}" for value in values if isinstance(value, str) and value.strip())
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value.__class__.__name__ == "NAType":
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _jsonable(tolist())
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _jsonable(converted)
    raise Model23RuntimeError()


def _validate_model2_output(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "model",
        "n",
        "context_digit_residue",
        "predictions",
        "adapter",
    }:
        raise Model23RuntimeError()
    if value["model"] != "model2_p3" or value["n"] != 1:
        raise Model23RuntimeError()
    if isinstance(value["context_digit_residue"], bool) or not isinstance(
        value["context_digit_residue"], int
    ):
        raise Model23RuntimeError()
    predictions = value["predictions"]
    if not isinstance(predictions, list) or len(predictions) != 1:
        raise Model23RuntimeError()
    row = predictions[0]
    if not isinstance(row, dict) or set(row) != _MODEL2_ROW_FIELDS:
        raise Model23RuntimeError()
    if row["status"] not in {"정상", "참고"} or row["input_completeness"] not in {
        "complete",
        "partial",
        "sparse",
    }:
        raise Model23RuntimeError()
    if row["bucket"] not in {"Low", "Mid", "High"}:
        raise Model23RuntimeError()
    if isinstance(row["pred_won"], bool) or not isinstance(row["pred_won"], int) or row["pred_won"] <= 0:
        raise Model23RuntimeError()
    if not _finite_number(row["pred_log10"]):
        raise Model23RuntimeError()
    if not isinstance(row["missing_features"], list) or not all(
        isinstance(item, str) for item in row["missing_features"]
    ):
        raise Model23RuntimeError()
    adapter = value["adapter"]
    if not isinstance(adapter, dict) or set(adapter) != _MODEL2_ADAPTER_FIELDS:
        raise Model23RuntimeError()
    # Canonical round-trip proves there are no NaN/Infinity values hidden in
    # nested adapter diagnostics or probability maps.
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise Model23RuntimeError() from None
    return value


def _validate_model3_scored(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("scored") is not True:
        raise Model23RuntimeError()
    row = _jsonable(value.get("result"))
    if not isinstance(row, dict) or set(row) != _MODEL3_ROW_FIELDS:
        raise Model23RuntimeError()
    if not _finite_number(row["score"]) or not 0.0 <= float(row["score"]) <= 1.0:
        raise Model23RuntimeError()
    if not isinstance(row["level"], str) or not row["level"].strip():
        raise Model23RuntimeError()
    if isinstance(row["cohort_n"], bool) or not isinstance(row["cohort_n"], int) or row["cohort_n"] <= 0:
        raise Model23RuntimeError()
    if row["top1_axis"] not in _MODEL3_AXES:
        raise Model23RuntimeError()
    return row


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
