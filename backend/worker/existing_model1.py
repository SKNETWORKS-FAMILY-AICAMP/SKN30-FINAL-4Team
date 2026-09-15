"""Deterministic Model 1 inputs and trusted Existing-KB backfill orchestration.

The Existing KB is shared reference data, so Model 1 output is versioned data
rather than a transient analysis result.  This module deliberately knows
nothing about PostgreSQL or torch: callers provide a small repository port and
the existing :class:`MlModel` subprocess adapter.  Keeping that separation
means loading KLUE-BERT can never leak into the worker parent process.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any, Protocol

from .contracts.ml_result import MlModelId
from .ml_reference import MODEL_1_ALLOWED_STATUSES, MODEL_1_CLASSES, WITHHELD_STATUS, MlModel


MODEL1_EXISTING_INPUT_ASSEMBLY_VERSION = "existing-profile-model1-input-v1"

# These are intentionally the same four fields and order used by
# ``build_ml_inputs`` and ``ml_child._model1_text``.  The only difference is
# provenance: Existing inputs come from already-approved KB facts.
MODEL1_FIELDS: tuple[str, ...] = ("title", "purpose", "content", "target_text")
PURPOSE_FIELDS = ("purpose_goal",)
CONTENT_FIELDS = (
    "support_components",
    "support_activities",
    "support_methods",
    "support_items",
    "support_content",
)
TARGET_FIELDS = (
    "applicant_eligibility",
    "support_target",
    "eligibility_conditions",
    "beneficiary",
    "applicable_entity",
    "exclusions",
    "duplicate_support_conditions",
    "participation_requirements",
)
# ``kb.fact_occurrence.status`` is constrained to these exact two values.
APPROVED_FACT_STATUSES = frozenset({"identified", "partial"})
_WHITESPACE = re.compile(r"\s+")


def _clean(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()


@dataclass(frozen=True, slots=True)
class ExistingModel1Input:
    """One auditable Model 1 input assembled from a current KB Profile."""

    profile_version_id: str
    payload: dict[str, str]
    input_sha256: str
    missing_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExistingModel1Profile:
    """The minimal approved KB material required for Existing classification."""

    profile_version_id: str
    portal_metadata: Mapping[str, Any]
    facts: Sequence[Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ExistingModel1Prediction:
    support_type_pred: str
    confidence: float
    prediction_status: str

    @property
    def effective_support_type(self) -> str | None:
        """Service-only downstream label; it is deliberately not persisted."""

        return None if self.prediction_status == WITHHELD_STATUS else self.support_type_pred


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    current_profiles: int
    predicted: int
    skipped: int
    dry_run: bool
    promoted: bool


def current_model1_input_hashes(
    profiles: Iterable[ExistingModel1Profile],
) -> dict[str, str]:
    """Assemble a complete current-corpus identity map for promotion checks.

    The map deliberately contains the same four-field input hash that is sent
    to the subprocess.  Rebuilding it under the promotion advisory lock makes
    title/fact changes detectable even when the Profile UUID itself is stable.
    """

    values = [assemble_existing_model1_input(profile) for profile in profiles]
    result = {item.profile_version_id: item.input_sha256 for item in values}
    if len(result) != len(values):
        raise ValueError("current Existing corpus has duplicate profile versions")
    return result


class ExistingModel1BackfillRepository(Protocol):
    """DB port; the concrete script owns SQL and transaction boundaries."""

    def current_profiles(self) -> list[ExistingModel1Profile]: ...

    def has_result(
        self, *, profile_version_id: str, configuration_id: str, input_sha256: str
    ) -> bool: ...

    def start_run(self, *, configuration_id: str, profile_count: int) -> str: ...

    def record_invocation(
        self,
        *,
        processing_run_id: str,
        input_sha256: str,
        output_sha256: str | None,
        status: str,
    ) -> None: ...

    def write_result(
        self,
        *,
        profile_version_id: str,
        configuration_id: str,
        input_sha256: str,
        prediction: ExistingModel1Prediction,
        processing_run_id: str,
    ) -> None: ...

    def write_failure(
        self,
        *,
        profile_version_id: str,
        configuration_id: str,
        input_sha256: str,
        reason_code: str,
        processing_run_id: str,
    ) -> None: ...

    def verify_and_promote(
        self,
        *,
        configuration_id: str,
        expected_inputs: Mapping[str, str],
        processing_run_id: str,
    ) -> None: ...

    def finish_run(self, *, processing_run_id: str, succeeded: bool, error_code: str | None = None) -> None: ...


def _ordered_values(
    facts: Iterable[Mapping[str, Any]], fields: Sequence[str]
) -> list[str]:
    allowed = set(fields)
    rows: list[tuple[int, int, str]] = []
    for encounter, fact in enumerate(facts):
        field_name = _clean(fact.get("field_name"))
        if field_name not in allowed:
            continue
        if _clean(fact.get("status")).lower() not in APPROVED_FACT_STATUSES:
            continue
        value = _clean(fact.get("value_raw"))
        if not value:
            continue
        ordinal = fact.get("ordinal")
        ordinal = ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else encounter
        rows.append((ordinal, encounter, value))
    rows.sort()
    # Do not de-duplicate.  Model 1 was trained on source text, where a
    # repeated approved fact can be meaningful; retaining every occurrence is
    # also what makes this assembly faithfully follow persisted source order.
    return [value for _, _, value in rows]


def assemble_existing_model1_input(profile: ExistingModel1Profile) -> ExistingModel1Input:
    """Build Model 1's frozen input from portal metadata and approved facts.

    Component *facts* are already in ``kb.fact_occurrence`` and retain their
    global persisted ordinal there.  ``support_component.name_raw`` is not
    injected separately: it has no global fact ordinal and doing so would
    change Model 1's frozen evidence ordering.  No generated summary, raw PDF
    text, or numeric support amount is introduced here.
    """

    title = _clean(profile.portal_metadata.get("title"))
    payload = {
        "title": title,
        "purpose": "\n".join(_ordered_values(profile.facts, PURPOSE_FIELDS)),
        "content": "\n".join(_ordered_values(profile.facts, CONTENT_FIELDS)),
        "target_text": "\n".join(_ordered_values(profile.facts, TARGET_FIELDS)),
    }
    missing = tuple(field for field in MODEL1_FIELDS if not payload[field])
    # Hash a canonical field object rather than joined text.  This prevents an
    # accidental delimiter collision from making different four-field inputs
    # look identical in the audit trail.
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ExistingModel1Input(
        profile_version_id=profile.profile_version_id,
        payload=payload,
        input_sha256=digest,
        missing_fields=missing,
    )


def normalize_existing_model1_prediction(raw: Mapping[str, Any]) -> ExistingModel1Prediction:
    """Validate the already-normalized subprocess response for durable use."""

    label = raw.get("support_type_pred")
    status = raw.get("status")
    confidence = raw.get("confidence")
    if not isinstance(label, str) or label.strip() not in MODEL_1_CLASSES:
        raise ValueError("model1 output has an unsupported support type")
    if not isinstance(status, str) or status not in MODEL_1_ALLOWED_STATUSES:
        raise ValueError("model1 output has an unsupported status")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("model1 output confidence is not numeric")
    numeric_confidence = float(confidence)
    if not math.isfinite(numeric_confidence) or not 0 <= numeric_confidence <= 1:
        raise ValueError("model1 output confidence is outside [0, 1]")
    cleaned_label = label.strip()
    return ExistingModel1Prediction(
        support_type_pred=cleaned_label,
        confidence=numeric_confidence,
        prediction_status=status,
    )


def _output_sha256(prediction: ExistingModel1Prediction) -> str:
    return sha256(
        json.dumps(
            {
                "support_type_pred": prediction.support_type_pred,
                "confidence": prediction.confidence,
                "prediction_status": prediction.prediction_status,
                # This is derived when Existing classifications are consumed;
                # it is included in the audit hash to make the withheld gate
                # explicit without adding another database column.
                "effective_support_type": prediction.effective_support_type,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class ExistingModel1Backfill:
    """Run a configuration-scoped, idempotent Existing Model 1 backfill."""

    def __init__(
        self,
        repository: ExistingModel1BackfillRepository,
        model: MlModel,
        *,
        configuration_id: str,
    ) -> None:
        if model.model_id is not MlModelId.MODEL_1_SUPPORT_TYPE:
            raise ValueError("Existing classification requires Model 1")
        if not configuration_id.strip():
            raise ValueError("configuration_id must not be blank")
        self._repository = repository
        self._model = model
        self._configuration_id = configuration_id

    def run(self, *, dry_run: bool = False) -> BackfillSummary:
        profiles = self._repository.current_profiles()
        inputs = [assemble_existing_model1_input(profile) for profile in profiles]
        expected = current_model1_input_hashes(profiles)
        if not inputs:
            raise ValueError("current Existing corpus is empty; refusing configuration promotion")
        if any(len(item.missing_fields) == len(MODEL1_FIELDS) for item in inputs):
            raise ValueError("an Existing profile has no approved Model 1 input")
        if dry_run:
            return BackfillSummary(len(inputs), predicted=0, skipped=0, dry_run=True, promoted=False)

        run_id = self._repository.start_run(
            configuration_id=self._configuration_id, profile_count=len(inputs)
        )
        predicted = skipped = 0
        try:
            for item in inputs:
                if self._repository.has_result(
                    profile_version_id=item.profile_version_id,
                    configuration_id=self._configuration_id,
                    input_sha256=item.input_sha256,
                ):
                    skipped += 1
                    continue
                try:
                    prediction = normalize_existing_model1_prediction(self._model.predict(item.payload))
                except Exception:
                    self._repository.record_invocation(
                        processing_run_id=run_id,
                        input_sha256=item.input_sha256,
                        output_sha256=None,
                        status="failed",
                    )
                    self._repository.write_failure(
                        profile_version_id=item.profile_version_id,
                        configuration_id=self._configuration_id,
                        input_sha256=item.input_sha256,
                        reason_code="MODEL_EXECUTION_FAILED",
                        processing_run_id=run_id,
                    )
                    raise
                self._repository.record_invocation(
                    processing_run_id=run_id,
                    input_sha256=item.input_sha256,
                    output_sha256=_output_sha256(prediction),
                    status="succeeded",
                )
                self._repository.write_result(
                    profile_version_id=item.profile_version_id,
                    configuration_id=self._configuration_id,
                    input_sha256=item.input_sha256,
                    prediction=prediction,
                    processing_run_id=run_id,
                )
                predicted += 1
            # Repository implementation must lock the configuration and
            # re-read *current* profiles/results in one transaction before it
            # flips the active configuration.  A partial corpus is never live.
            self._repository.verify_and_promote(
                configuration_id=self._configuration_id,
                expected_inputs=expected,
                processing_run_id=run_id,
            )
        except Exception as error:
            try:
                self._repository.finish_run(
                    processing_run_id=run_id,
                    succeeded=False,
                    error_code=type(error).__name__,
                )
            except Exception as finish_error:
                # Finalisation is best-effort after the real work has failed.
                # Preserve the original traceback and expose only the finish
                # exception type in its diagnostic note—never DB details.
                error.add_note(
                    "processing run failure finalization also failed: "
                    f"{type(finish_error).__name__}"
                )
            raise
        self._repository.finish_run(processing_run_id=run_id, succeeded=True)
        return BackfillSummary(len(inputs), predicted, skipped, dry_run=False, promoted=True)
