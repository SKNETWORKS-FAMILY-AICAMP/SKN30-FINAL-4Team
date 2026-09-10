"""One-shot child entry point for the team's Model 1, 2, and 3 serving code.

The serving modules use bare imports and mutate ``sys.path``.  Keeping them in
this process boundary lets the backend remain free of the model runtime while
still calling the frozen team entry points.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
ML_ROOT = REPO_ROOT / "ml"

# These are the categorical/text columns the serving adapter is allowed to
# carry through from L1.  Numeric values parsed from the document are owned by
# the team's adapter, not by this wire shim.
BASE_KEYS = (
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

# L1 keeps only verified raw quantity quotations for Model 3.  These labels are
# source-field context, not a numeric parser: the team's preconsultation
# adapter still owns all amount/ratio/duration interpretation.
QUANTITY_CONTEXT = {
    "support_scale": "지원규모",
    "cost_sharing": "자부담",
    "support_period": "지원기간",
    "total_budget": "총사업비",
}

MODEL1_SERVING_DIR_ENV = "ML_MODEL1_SERVING_DIR"
MODEL1_FIELDS = ("title", "purpose", "content", "target_text")


def _ensure_layout() -> None:
    # m2_features/m3_lab locate the project root by requiring ml/data to exist
    # before importing common.py.  The directory is tracked as an empty layout
    # marker; a child must never create or populate repository state while
    # serving a request.
    if not ML_ROOT.is_dir() or not (ML_ROOT / "data").is_dir():
        raise RuntimeError("team ML data layout is missing")


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("model entry point could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _text(payload: Mapping[str, Any]) -> str:
    value = payload.get("evidence_text", payload.get("text", ""))
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("evidence_text must be a string")
    return value


def _quantity_evidence_text(payload: Mapping[str, Any]) -> str:
    """Reassemble L1's verified raw quantity quotations for the team adapter."""

    quantities = payload.get("quantities")
    if quantities is None:
        return ""
    if not isinstance(quantities, Mapping):
        raise ValueError("quantities must be an object")

    lines: list[str] = []
    for key, values in quantities.items():
        label = QUANTITY_CONTEXT.get(str(key))
        if label is None:
            # L1's source table is closed; an unexpected quantity key cannot
            # silently become model evidence.
            continue
        if isinstance(values, str):
            raw_values = (values,)
        elif isinstance(values, (list, tuple)):
            raw_values = values
        else:
            raise ValueError(f"quantities[{key!r}] must be a string list")
        for raw in raw_values:
            if not isinstance(raw, str):
                raise ValueError(f"quantities[{key!r}] contains a non-string")
            raw = raw.strip()
            if raw:
                lines.append(f"{label}: {raw}")
    return "\n".join(lines)


def _base(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    supplied = payload.get("base")
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ValueError("base must be an object")
        out.update({key: supplied[key] for key in BASE_KEYS if key in supplied})
    out.update({key: payload[key] for key in BASE_KEYS if key in payload})
    return out


def _adapt_for_model3(payload: Mapping[str, Any]) -> dict[str, Any]:
    supplied = payload.get("meta")
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ValueError("meta must be an object")
        return dict(supplied)

    adapter_path = ML_ROOT / "serving" / "shared" / "preconsultation_adapter.py"
    adapter = _load_module("team_preconsultation_adapter", adapter_path)
    text = _text(payload)
    if not text:
        text = _quantity_evidence_text(payload)
    if not text:
        raise ValueError("model3 requires evidence_text or verified quantities")
    return adapter.adapt(text, base=_base(payload))


def _model1_entry() -> Path:
    """Resolve the external Model 1 serving wrapper from an injected path."""

    raw_root = os.environ.get(MODEL1_SERVING_DIR_ENV)
    if not raw_root or not raw_root.strip():
        raise RuntimeError(f"{MODEL1_SERVING_DIR_ENV} is not set")
    root = Path(raw_root).expanduser()
    candidates = [root / "model1" / "inference.py"]
    if root.name.lower() == "model1":
        candidates.append(root / "inference.py")
    for entry in candidates:
        if entry.is_file():
            return entry
    raise FileNotFoundError(
        f"Model 1 inference.py was not found under {root}"
    )


def _prepare_model1_import_path() -> Path:
    """Expose the repository's unchanged ``dl07_m1_apply`` module to serving."""

    pipeline_dir = ML_ROOT / "pipelines" / "model1"
    module_path = pipeline_dir / "dl07_m1_apply.py"
    if not module_path.is_file():
        raise FileNotFoundError(
            f"Model 1 preprocessing module was not found: {module_path}"
        )
    path = str(pipeline_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    # The external wrapper also mutates sys.path.  Preload the exact file so a
    # same-named module elsewhere cannot win the bare import in inference.py.
    if "dl07_m1_apply" not in sys.modules:
        _load_module("dl07_m1_apply", module_path)
    return module_path


def _model1_text(payload: Mapping[str, Any]) -> str:
    """Join the four training fields in the frozen training order."""

    parts: list[str] = []
    for field in MODEL1_FIELDS:
        value = payload.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"model1 {field} must be a string")
        value = value.strip()
        if value:
            parts.append(value)
    if not parts:
        raise ValueError("model1 input fields are all empty")
    return "\n".join(parts)


def _run_model1(payload: Mapping[str, Any]) -> dict[str, Any]:
    entry = _model1_entry()
    _prepare_model1_import_path()
    model = _load_module("team_model1_inference", entry)
    result = model.predict([_model1_text(payload)], already_cleaned=True)
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError("model1 predict result must contain one row")
    row = result[0]
    if not isinstance(row, Mapping):
        raise ValueError("model1 predict row must be an object")
    return dict(row)


def _run_model2(payload: Mapping[str, Any]) -> Any:
    entry = ML_ROOT / "serving" / "model2" / "predict.py"
    model = _load_module("team_model2_predict", entry)
    return model.predict_document(_text(payload), base=_base(payload))


def _run_model3(payload: Mapping[str, Any]) -> Any:
    entry = ML_ROOT / "serving" / "model3" / "score.py"
    model = _load_module("team_model3_score", entry)
    scored = model.score_document(_adapt_for_model3(payload))
    if not isinstance(scored, Mapping) or not scored.get("scored"):
        raise ValueError("model3 input has fewer than the required valid axes")
    result = scored.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("model3 did not return a score row")
    # score_document adds confidence diagnostics around the serving row.  The
    # existing backend adapter consumes the row itself, just like the team's
    # DataFrame-record endpoint, and derives the display contract there.
    return dict(result)


def _jsonable(value: Any) -> Any:
    """Convert numpy/pandas scalar containers without inventing values."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # pandas uses NaN for a missing row id/feature.  JSON has no NaN value;
        # null is the wire representation of that same missing value.
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
    raise TypeError("model result contains a non-JSON value")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="team ML one-shot child")
    parser.add_argument(
        "--model", choices=("model1", "model2", "model3"), required=True
    )
    args = parser.parse_args(argv)

    try:
        request = json.load(sys.stdin)
        if not isinstance(request, Mapping):
            raise ValueError("request must be a JSON object")
        # Team modules are intentionally allowed to use their historical bare
        # imports and path setup, but their diagnostic prints must never share
        # the one-line JSON protocol.  Keep the capture in the child process;
        # the backend parent therefore remains unaware of both stdout and
        # ``sys.path`` changes made by the serving package.
        with contextlib.redirect_stdout(io.StringIO()):
            _ensure_layout()
            if args.model == "model1":
                result = _run_model1(request)
            elif args.model == "model2":
                result = _run_model2(request)
            else:
                result = _run_model3(request)
        response = _jsonable(result)
        sys.stdout.write(json.dumps(
            response, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ))
        sys.stdout.write("\n")
        return 0
    except Exception as error:  # noqa: BLE001 - safe child failure boundary
        # Never echo document text or raw model output into the diagnostic
        # stream; the parent only needs a nonzero exit and a safe category.
        print(f"model child failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
