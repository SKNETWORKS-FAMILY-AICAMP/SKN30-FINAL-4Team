"""Request Profile v0.1.2 source-selection runner.

The remote path calls one OpenAI-compatible Structured Outputs model for the
strict ``RequestSourceSelectionV012`` contract. The model never writes final
values, offsets, evidence, projections, or a profile: those remain server-side
exact-span materialization work in ``request_profile_v012``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from .common_ir_v1 import common_ir_v1_identity
from .pipeline import _openai_schema, _usage
from .request_profile_v012 import (
    REQUEST_PIPELINE_VERSION,
    RequestSourceSelectionV012,
    assemble_request_profile_v012,
    build_request_candidate_pack,
    candidate_pack_artifact,
    resolve_request_type_from_candidate_pack,
)


REQUEST_SELECTION_PROMPT_VERSION = "request_source_selection_v0.1.2"
LifecycleObserver = Callable[[str, dict[str, Any]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error_message(error: BaseException) -> str:
    """Return a bounded diagnostic without allowing an API key to leak."""

    message = str(error)
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    # A defensive fallback for an SDK error that echoes a bearer-like token.
    return re.sub(r"(?:sk|rk|sess)-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)[:2_000]


def _safe_optional_identifier(value: Any) -> str | None:
    """Keep only a bounded scalar identifier; never serialize an SDK object."""

    if not isinstance(value, str):
        return None
    return re.sub(r"(?:sk|rk|sess)-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)[:512]


def _emit_lifecycle(
    observer: LifecycleObserver | None, stage: str, **details: Any,
) -> None:
    """Emit only secret-free lifecycle details to an injected observer."""

    if observer is not None:
        observer(stage, {key: value for key, value in details.items() if value is not None})


def _write_artifact_with_lifecycle(
    writer: Callable[[Path, dict[str, Any]], None], path: Path, payload: dict[str, Any],
    *, artifact_name: str, observer: LifecycleObserver | None = None,
) -> None:
    """Write an artifact and make ordinary write failures observable."""

    _emit_lifecycle(observer, "artifact_write_started", artifact=artifact_name, path=str(path))
    try:
        writer(path, payload)
    except Exception as error:
        _emit_lifecycle(
            observer, "artifact_write_failed", artifact=artifact_name, path=str(path),
            error_type=type(error).__name__, error_message=_safe_error_message(error),
        )
        raise
    _emit_lifecycle(observer, "artifact_write_completed", artifact=artifact_name, path=str(path))


def request_selection_instructions() -> str:
    """Request-specific semantic boundary, intentionally free of Gold labels."""

    return (
        "You select source anchors from one Korean pre-review request CandidatePack. "
        "Return only the RequestSourceSelectionV012 JSON schema. Do not write value_raw, summaries, "
        "offsets, evidence, normalized numbers, derived projections, or any server-derived keys outside the schema. "
        "field_states is the one optional exception: emit it only for source-visible mentioned_unresolved, extraction_failed, "
        "not_applicable, or partial states. For partial, fact_ids are optional hints only; the server derives final field IDs from "
        "the Raw Facts it actually materializes. If you nevertheless emit a field_state beside selected values, the server keeps the values "
        "and normalizes the final state to partial with diagnostics. "
        "Every selected legacy anchor_text must be copied exactly and be one contiguous source span. If literal anchor_text repeats within "
        "one source block, do not choose an occurrence or calculate an offset: select the supplied value_span_candidate_id instead. "
        "With value_span_candidate_id you may include only its matching source_block_id as a provenance hint; never include anchor_text. "
        "This repeated-anchor rule applies identically to every anchor-bearing selection: comparison/request-context Facts, support component "
        "name/applies-to, program hierarchy name, and delivery actor, role, action, and method. "
        "Extract requested/proposed values in this request, not a baseline value mentioned only for before-after comparison. "
        "Never put before-after comparison narration (such as →, 변경 전/후, 기존 대비, 시범사업 대비) into purpose_goal or support_content. "
        "For purpose_goal, select only the requested policy-purpose expression itself; do not select a sentence that narrates a retained prior purpose, "
        "a before/after comparison, or maintain-and-expand wording (for example '기존 목적을 유지하되 ... 확대'). "
        "When a purpose sentence mixes an intervention/change mechanism (for example '지급 구조를 단계별로 나누고' or "
        "'멘토링을 ... 구체화하여') with a separately contiguous policy outcome, select only the exact outcome span (for example "
        "'사업화 실행력을 높임'). Never summarize, trim, or join disconnected spans; if no standalone exact outcome exists, omit purpose_goal. "
        "For every Raw Fact, remove proposal/year/change label prefixes such as '2027년 요청안(변경 후):' and select only the content after the colon. "
        "Select only the requested programme's final applied value. A value noted as 변경 없음 may still be selected when the actual final "
        "value is independently explicit (for example a stated amount/period). If the source says only 동일(변경 없음) or another reference "
        "without an independently explicit final value, do not copy a Raw Fact: emit field_states with status mentioned_unresolved and, when apt, "
        "reason_codes containing unchanged_by_reference. "
        "Do not use any Gold, expected result, score, fixture label, or comparison outcome; none is source evidence. "
        "Use only the 16 comparison fields and six request_context fields permitted by the schema. "
        "Do not infer absent facts, business entities, relationships, or canonical vocabulary. "
        "applicant_eligibility, support_target, eligibility_conditions, and exclusions describe who may apply or be supported, "
        "which positive conditions narrow that set, or who is not allowed. A selection/review priority is not any of those fields: "
        "when the source says an applicant may still apply but is ranked higher/lower at screening or selection, do not create one of "
        "those four Facts; omit it rather than treating a priority as ineligibility or exclusion. "
        "request_type is server-resolved read-only context from the form checkbox. Do not output, modify, or infer request_type; "
        "it is outside RequestSourceSelectionV012. "
        "Keep program_period (official whole-program operation) distinct from support_period (selected recipient agreement/execution). "
        "For every program_period Fact, you must select a CandidatePack value_span_candidate_id whose candidate_kind is "
        "program_period_date_range; legacy anchor_text is not allowed for this field. Select only that candidate's literal date-range span "
        "(for example '2027년 1월~12월' or the explicit official open interval '공고일~2027.12.31'). Do not include '(12개월)', a label, "
        "or before/after change narration in that Raw Fact. Do not infer an unstated start/end date; only the listed candidate is eligible. "
        "do not treat application/receipt periods as either. support_scale contains only selected/support count, actual amount, "
        "rate, or limit: never duration or activity/session counts. In a 변경 후, 요청안, or 확산사업 context, if an amount, rate, "
        "or selected/supported count is independently written as an exact span, you must select each as support_scale even when '(변경 없음)' "
        "or '단가 자체는 유지' is adjacent. Only a reference with no value of its own, such as '동일(변경 없음)', becomes mentioned_unresolved. "
        "Education 4회 or another activity/session count remains outside support_scale. "
        "support_content is a narrow escape hatch only for material support content that cannot safely be assigned to support_activities, "
        "support_methods, or support_items. If an exact span is selected in any of those three fields, never select that same span in "
        "support_content; do not duplicate it as a fallback. "
        "support_components identify named packages, stages, or menus; they do not replace a Raw support_items Fact when the source separately "
        "states a concrete thing the recipient receives. For example, a grant/support fund is an item, whereas the act of paying it is not a "
        "recipient activity. When the component heading and an independently stated body occurrence are different exact spans, keep the heading "
        "as the component name and select the body occurrence as support_items. "
        "support_activities means what the recipient is enabled to do (for example training, commercialization, demonstration, or employment), "
        "not the provider's payment/disbursement action. "
        "A named recipient service such as mentoring is support_methods, not support_items; its exact format can be a separate method. "
        "When a named service is explicitly provided alongside another package and has its own format or cadence, create its own support_package "
        "component and link the service, format, and cadence to it; do not attach that format to the unrelated monetary package. "
        "Raw Facts must not reuse the same exact span with another Raw Fact. A structural component name may reuse one exact span only with a Raw Fact "
        "whose primary_component_id is that same component; do not use this exception across different components or comparison fields. "
        "support_methods contains only the method by which support is actually provided or paid (for example '사후 정산 지급'). "
        "A one-to-one mentoring format is also a support_method. Frequency, total sessions, payment-stage composition, and other material delivery "
        "details that are not safely an activity, method, or item belong in support_content as their shortest final-state exact spans. "
        "If one sentence contains a workflow such as selection → recruitment/placement → attendance confirmation → payment, never select the whole workflow. "
        "Select an independently exact providing/payment-method span only when it can be separated safely; do not create comparison Raw Facts for selection, "
        "recruitment, placement, or attendance confirmation steps. "
        "Create support components and primary_component_id only where the source explicitly names the package, type, or stage. "
        "Do not create a stage_support merely because one grant is paid in 1st/2nd stages or instalments. A stage is an independent component only "
        "when it has its own explicitly stated recipient, eligibility, exclusion, or participation boundary. Otherwise keep payment stages and their "
        "amounts under the parent support package as support_content/support_scale. "
        "When an explicit support-component heading/section has a local row '실제 수혜자: 사업주' or '실제 수혜자: 근로자', select the beneficiary "
        "value alone as an exact span and create a beneficiary Fact whose primary_component_id is that selected component. Do not create either value "
        "or component by inference when this explicit heading/row structure is absent. For every support_component you select, if its local section "
        "contains an explicit non-empty '실제 수혜자:' row, you must select exactly one such beneficiary Fact linked to that component; do not treat "
        "an independent or bare '동일' reference elsewhere as component-local. "
        "Create a delivery relation only if actor and role/action are explicitly connected in one paragraph or one explicit table row; "
        "never join separate regions. An actor must be an institution, organisation, or operational entity, never a diagram/layout label such as "
        "'(정책지정)→설명'. An explicit nested-table organisation chart can use table_column_pair only for actor plus explicit role OR actor plus explicit "
        "action cells in the same Common IR table and same column on consecutive non-empty semantic rows; do not coerce an action into role_raw or infer one. "
        "For a paragraph relation_container use source_block_id and anchor_text; "
        "common_ir_block_id is only required for table_row/table_column_pair. delivery_methods describe programme execution, not beneficiary support methods. "
        "If a value is not safely selectable, omit it rather than guessing."
    )


def selection_request_payload(pack, document: dict[str, Any], profile_id: str) -> dict[str, Any]:
    """The exact no-Gold payload given to the remote selector or dry-run file."""

    request_type = resolve_request_type_from_candidate_pack(pack)
    return {
        "profile_id": profile_id,
        "read_only_context": {
            "request_type": {
                "selected_code": request_type["selected_code"],
                "label": request_type["value_raw"],
            },
        },
        "candidate_pack": candidate_pack_artifact(pack, document),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_run_status(status_path: Path, status: dict[str, Any]) -> bool:
    """Best-effort status persistence without masking the primary run result.

    A status-write failure cannot itself be persisted reliably, so it is emitted
    as one compact, secret-free structured log line. All other artifact writes
    are represented inside the status document.
    """

    status["run_status_artifact"] = {"path": str(status_path), "write_succeeded": True}
    try:
        _write_json(status_path, status)
    except Exception as error:
        status["run_status_artifact"] = {"path": str(status_path), "write_succeeded": False}
        print(json.dumps({
            "event": "request_remote_run_status_write_failed",
            "path": str(status_path),
            "error_type": type(error).__name__,
            "error_message": _safe_error_message(error),
        }, ensure_ascii=False), flush=True)
        return False
    return True


def _new_remote_run_status(
    *, profile_id: str, pack, document: dict[str, Any], model_id: str,
    profile_path: Path, selection_path: Path, failure_path: Path,
    selection_failure_path: Path,
) -> dict[str, Any]:
    """Initialize the secret-free remote lifecycle document."""

    identity = common_ir_v1_identity(document)
    return {
        "artifact_kind": "request_remote_run_status",
        "pipeline_version": REQUEST_PIPELINE_VERSION,
        "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
        "mode": "remote",
        "outcome": "running",
        "started_at": _utc_now(),
        "completed_at": None,
        "profile_id": profile_id,
        "model_id": model_id,
        "candidate_pack_id": pack.pack_id,
        "candidate_pack_generator": pack.generator,
        "candidate_pack_generator_version": pack.generator_version,
        "common_ir_document_id": pack.common_ir_document_id,
        "common_ir_source_sha256": identity["source_sha256"],
        "events": [],
        "artifacts": {
            "profile": {"path": str(profile_path), "write_succeeded": False},
            "selection": {"path": str(selection_path), "write_succeeded": False},
            "failure_profile": {"path": str(failure_path), "write_succeeded": False},
            "failure_selection": {"path": str(selection_failure_path), "write_succeeded": False},
        },
        "exception": None,
        "usage": [],
        "response_text_stored": False,
        "api_key_stored": False,
    }


def _status_observer(status: dict[str, Any], *, status_path: Path | None = None) -> LifecycleObserver:
    """Append lifecycle events and persist them immediately when a path is given.

    Remote calls can be interrupted outside Python (for example by an executor
    timeout). Persisting each safe lifecycle event prevents a stale `running`
    status from hiding whether a request had reached the API.
    """

    def observe(stage: str, details: dict[str, Any]) -> None:
        status["events"].append({"at": _utc_now(), "stage": stage, **details})
        if status_path is not None:
            _write_run_status(status_path, status)
    return observe


class RequestMaterializationError(RuntimeError):
    """A parsed selection could not pass server exact-span materialization."""

    def __init__(
        self, error: ValueError, *, selection: RequestSourceSelectionV012,
        retry_count: int, diagnostics: list[dict[str, Any]], usage: list[dict[str, Any]],
    ):
        super().__init__(str(error))
        self.selection = selection
        self.retry_count = retry_count
        self.diagnostics = diagnostics
        self.usage = usage


class RequestSourceSelectionParseError(RuntimeError):
    """A remote response arrived but failed JSON/schema compatibility parsing.

    This deliberately contains diagnostics and token usage only.  It never
    keeps the raw model response, which could otherwise become an accidental
    second source artifact.
    """

    def __init__(
        self, error: BaseException, *, call_usage: dict[str, Any], repair: bool,
    ):
        super().__init__(_safe_error_message(error))
        self.call_usage = call_usage
        self.repair = repair
        self.retry_count = 0
        self.usage: list[dict[str, Any]] = [call_usage]
        self.diagnostics: list[dict[str, Any]] = [{
            "repair_attempt": 1,
            "error_type": type(error).__name__,
            "validation_error": _safe_error_message(error),
        }]


def _selection_artifact(
    selection: RequestSourceSelectionV012, pack, document: dict[str, Any], usage: list[dict[str, Any]],
    *, retry_count: int, repair_diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = common_ir_v1_identity(document)
    request_type = resolve_request_type_from_candidate_pack(pack)
    return {
        "artifact_kind": "request_source_selection_artifact",
        "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
        "pipeline_version": REQUEST_PIPELINE_VERSION,
        "profile_id": selection.profile_id,
        "candidate_pack_id": pack.pack_id,
        "server_resolved_context": {
            "request_type": {
                "selected_code": request_type["selected_code"],
                "label": request_type["value_raw"],
            },
        },
        "common_ir_identity": identity,
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": identity["source_sha256"],
        },
        "selection": selection.model_dump(mode="json"),
        "usage": usage,
        "retry_count": retry_count,
        "repair_diagnostics": repair_diagnostics,
        "response_text_stored": False,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _failure_selection_artifact(
    error: RequestMaterializationError, pack, document: dict[str, Any], *, profile_id: str,
) -> dict[str, Any]:
    """Secret-free parsed-selection diagnostic; distinct from the failure profile artifact."""

    identity = common_ir_v1_identity(document)
    request_type = resolve_request_type_from_candidate_pack(pack)
    return {
        "artifact_kind": "request_source_selection_failure_artifact",
        "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
        "pipeline_version": REQUEST_PIPELINE_VERSION,
        "profile_id": profile_id,
        "candidate_pack_id": pack.pack_id,
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": identity["source_sha256"],
        },
        "server_resolved_context": {"request_type": {
            "selected_code": request_type["selected_code"], "label": request_type["value_raw"],
        }},
        "selection": error.selection.model_dump(mode="json"),
        "validation_diagnostics": error.diagnostics,
        "usage": error.usage,
        "retry_count": error.retry_count,
        "response_text_stored": False,
    }


def _selection_parse_failure_artifact(
    error: RequestSourceSelectionParseError, pack, document: dict[str, Any], *, profile_id: str,
) -> dict[str, Any]:
    """Write parse diagnostics without serializing a raw remote response."""

    identity = common_ir_v1_identity(document)
    request_type = resolve_request_type_from_candidate_pack(pack)
    return {
        "artifact_kind": "request_source_selection_parse_failure_artifact",
        "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
        "pipeline_version": REQUEST_PIPELINE_VERSION,
        "profile_id": profile_id,
        "candidate_pack_id": pack.pack_id,
        "candidate_pack_lineage": {
            "candidate_pack_id": pack.pack_id,
            "candidate_pack_generator": pack.generator,
            "candidate_pack_generator_version": pack.generator_version,
            "common_ir_document_id": pack.common_ir_document_id,
            "common_ir_source_sha256": identity["source_sha256"],
        },
        "server_resolved_context": {"request_type": {
            "selected_code": request_type["selected_code"], "label": request_type["value_raw"],
        }},
        "validation_diagnostics": error.diagnostics,
        "usage": error.usage,
        "retry_count": error.retry_count,
        "response_text_stored": False,
    }


def _normalize_remote_selection_payload(value: Any) -> Any:
    """Normalize one known Structured Output compatibility artifact in memory.

    Some providers return a redundant ``anchor_text`` alongside an authoritative
    ``value_span_candidate_id`` despite the JSON-schema contract.  Only on the
    remote-response path do we discard that redundant locator before Pydantic
    parsing.  Local ``--selection`` files remain strict, while the server still
    verifies an optional ``source_block_id`` hint against the candidate ID.
    """

    if isinstance(value, list):
        return [_normalize_remote_selection_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _normalize_remote_selection_payload(item) for key, item in value.items()}
    if normalized.get("value_span_candidate_id") is not None and "anchor_text" in normalized:
        normalized.pop("anchor_text")
    return normalized


def _remote_selection(
    client: OpenAI, pack, document: dict[str, Any], profile_id: str,
    *, prior_selection: RequestSourceSelectionV012 | None = None,
    validation_errors: list[str] | None = None,
    lifecycle: LifecycleObserver | None = None,
) -> tuple[RequestSourceSelectionV012, dict[str, Any]]:
    repair = prior_selection is not None
    instructions = request_selection_instructions()
    if repair:
        instructions += (
            " The prior parsed selection failed server exact-span/provenance validation. "
            "Return a complete replacement RequestSourceSelectionV012. Modify only invalid anchors or selection structure, "
            "preserve valid entries, and copy literal CandidatePack substrings exactly, including Markdown syntax such as ** when present."
        )
    payload = selection_request_payload(pack, document, profile_id)
    if repair:
        payload["prior_selection"] = prior_selection.model_dump(mode="json")
        payload["server_validation_errors"] = validation_errors or []
    model_id = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
    _emit_lifecycle(lifecycle, "api_request_started", model_id=model_id, repair=repair)
    response = client.responses.create(
        model=model_id,
        reasoning={"effort": os.environ.get("OPENAI_REASONING_EFFORT", "medium")},
        store=False,
        input=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text={"format": {
            "type": "json_schema", "name": "request_source_selection_v012", "strict": True,
            "schema": _openai_schema(RequestSourceSelectionV012),
        }},
    )
    _emit_lifecycle(
        lifecycle, "api_response_received",
        api_response_status=str(getattr(response, "status", None)),
        api_response_id=_safe_optional_identifier(getattr(response, "id", None)), repair=repair,
    )
    if response.status != "completed" or not response.output_text:
        raise RuntimeError(f"request source selection returned incomplete status: {response.status}")
    call_usage = _usage(response, 0).__dict__
    try:
        # Remote-only compatibility normalization.  Do not reuse this path for
        # local artifacts: those are part of the strict selection contract.
        selection = RequestSourceSelectionV012.model_validate(
            _normalize_remote_selection_payload(json.loads(response.output_text)),
        )
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        _emit_lifecycle(
            lifecycle, "parsed_selection_failed", repair=repair,
            error_type=type(error).__name__, error_message=_safe_error_message(error),
        )
        raise RequestSourceSelectionParseError(
            error, call_usage=call_usage, repair=repair,
        ) from error
    if selection.profile_id != profile_id:
        raise RuntimeError("remote selection profile_id does not match requested profile_id")
    if selection.candidate_pack_id != pack.pack_id:
        raise RuntimeError("remote selection candidate_pack_id does not match supplied CandidatePack")
    _emit_lifecycle(lifecycle, "parsed_selection_validated", repair=repair)
    return selection, call_usage


def select_and_materialize_with_repairs(
    selector: Callable[[RequestSourceSelectionV012 | None, list[str] | None], tuple[RequestSourceSelectionV012, dict[str, Any]]],
    document: dict[str, Any], pack, profile_id: str, *, max_repairs: int, model_id: str,
    lifecycle: LifecycleObserver | None = None,
) -> tuple[dict[str, Any], RequestSourceSelectionV012, list[dict[str, Any]], list[dict[str, Any]]]:
    """Call selection once, then at most ``max_repairs`` server-guided repairs.

    This orchestration deliberately retries only after parsed selection reaches
    the server and fails exact-span/provenance validation. It is usable with a
    fake selector in offline tests and never stores a raw model response.
    """

    selection: RequestSourceSelectionV012 | None = None
    usage: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for attempt in range(max_repairs + 1):
        _emit_lifecycle(lifecycle, "selection_attempt_started", attempt=attempt + 1, repair=bool(attempt))
        try:
            selection, call_usage = selector(
                selection if attempt else None,
                [diagnostics[-1]["validation_error"]] if attempt else None,
            )
        except RequestSourceSelectionParseError as error:
            # Include usage from successful preceding attempts plus the parsed
            # response that failed schema normalization.
            error.retry_count = attempt
            error.usage = [*usage, error.call_usage]
            error.diagnostics = [{
                "repair_attempt": attempt + 1,
                "error_type": row["error_type"],
                "validation_error": row["validation_error"],
            } for row in error.diagnostics]
            raise
        usage.append(call_usage)
        _emit_lifecycle(lifecycle, "selection_attempt_completed", attempt=attempt + 1, repair=bool(attempt))
        try:
            _emit_lifecycle(lifecycle, "server_materialization_started", attempt=attempt + 1)
            profile = assemble_request_profile_v012(
                document, pack, selection, model_id=model_id, prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
            )
            _emit_lifecycle(lifecycle, "server_materialization_completed", attempt=attempt + 1)
            return profile, selection, usage, diagnostics
        except ValueError as error:
            diagnostics.append({
                "repair_attempt": attempt + 1,
                "error_type": type(error).__name__,
                "validation_error": str(error),
            })
            _emit_lifecycle(
                lifecycle, "server_materialization_failed", attempt=attempt + 1,
                error_type=type(error).__name__, error_message=_safe_error_message(error),
            )
            if attempt == max_repairs:
                raise RequestMaterializationError(
                    error, selection=selection, retry_count=attempt, diagnostics=diagnostics, usage=usage,
                ) from error
            _emit_lifecycle(lifecycle, "server_guided_repair_scheduled", next_attempt=attempt + 2)
    raise AssertionError("unreachable")


def _load_selection(path: Path, profile_id: str) -> RequestSourceSelectionV012:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Selection artifacts and runner templates are not the strict model
    # contract. Accept only their nested/known selection payload.
    raw = raw.get("selection", raw)
    for key in ("artifact_kind", "selection_contract", "remote_model_called"):
        raw.pop(key, None)
    if raw.get("profile_id") is None:
        raw["profile_id"] = profile_id
    elif raw["profile_id"] != profile_id:
        raise ValueError("--profile-id must exactly match selection profile_id")
    return RequestSourceSelectionV012.model_validate(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--common-ir", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--selection", type=Path, help="precomputed selection JSON/artifact; no remote model call")
    mode.add_argument("--remote", action="store_true", help="call configured OpenAI-compatible structured-output model")
    mode.add_argument("--remote-repair-from", type=Path, help="resume one server-guided remote repair from a failure selection artifact")
    mode.add_argument("--dry-run", action="store_true", help="write remote request payload only; no API call")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-pack-output", type=Path)
    parser.add_argument("--selection-artifact-output", type=Path)
    parser.add_argument("--max-repairs", type=int, default=1, help="server-guided remote anchor repairs after materialization failure (default: 1)")
    parser.add_argument("--repair-reason", help="explicit semantic-review reason for --remote-repair-from; permits one corrective repair even when the prior selection materializes")
    args = parser.parse_args()
    if args.max_repairs < 0:
        parser.error("--max-repairs must be zero or greater")

    document = json.loads(args.common_ir.read_text(encoding="utf-8"))
    pack = build_request_candidate_pack(document)
    if args.candidate_pack_output:
        _write_json(args.candidate_pack_output, candidate_pack_artifact(pack, document))

    if args.dry_run:
        _write_json(args.output, {
            "artifact_kind": "request_source_selection_dry_run",
            "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
            "remote_model_called": False,
            "instructions": request_selection_instructions(),
            "input": selection_request_payload(pack, document, args.profile_id),
        })
        return

    if args.remote or args.remote_repair_from:
        load_dotenv()
        if not os.environ.get("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY is not set; add it to .env without printing it")
        model_id = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
        artifact_path = args.selection_artifact_output or args.output.with_suffix(".selection.json")
        failure_path = args.output.with_suffix(".failure.json")
        selection_failure_path = args.selection_artifact_output or args.output.with_suffix(".selection.failure.json")
        status_path = args.output.with_suffix(".run-status.json")
        run_status = _new_remote_run_status(
            profile_id=args.profile_id, pack=pack, document=document, model_id=model_id,
            profile_path=args.output, selection_path=artifact_path, failure_path=failure_path,
            selection_failure_path=selection_failure_path,
        )
        lifecycle = _status_observer(run_status, status_path=status_path)
        _emit_lifecycle(lifecycle, "remote_run_started")
        _write_run_status(status_path, run_status)

        def write_remote_artifact(name: str, path: Path, payload: dict[str, Any]) -> None:
            try:
                _write_artifact_with_lifecycle(
                    _write_json, path, payload, artifact_name=name, observer=lifecycle,
                )
            except Exception:
                run_status["artifacts"][name]["write_succeeded"] = False
                raise
            run_status["artifacts"][name]["write_succeeded"] = True

        try:
            client = OpenAI()
            if args.remote_repair_from:
                prior_selection = _load_selection(args.remote_repair_from, args.profile_id)
                try:
                    assemble_request_profile_v012(
                        document, pack, prior_selection, model_id=model_id,
                        prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
                    )
                except ValueError as error:
                    validation_errors = [str(error), *([args.repair_reason] if args.repair_reason else [])]
                else:
                    if not args.repair_reason:
                        raise ValueError("--remote-repair-from requires a failed selection or --repair-reason")
                    validation_errors = [args.repair_reason]
                _emit_lifecycle(lifecycle, "resumed_server_guided_repair_started")
                selection, call_usage = _remote_selection(
                    client, pack, document, args.profile_id,
                    prior_selection=prior_selection, validation_errors=validation_errors, lifecycle=lifecycle,
                )
                _emit_lifecycle(lifecycle, "server_materialization_started", attempt=1, repair=True)
                profile = assemble_request_profile_v012(
                    document, pack, selection, model_id=model_id,
                    prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
                )
                _emit_lifecycle(lifecycle, "server_materialization_completed", attempt=1, repair=True)
                usage = [call_usage]
                repair_diagnostics = [{"repair_attempt": 1, "validation_error": validation_errors[0]}]
            else:
                profile, selection, usage, repair_diagnostics = select_and_materialize_with_repairs(
                    lambda prior, errors: _remote_selection(
                        client, pack, document, args.profile_id,
                        prior_selection=prior, validation_errors=errors, lifecycle=lifecycle,
                    ),
                    document, pack, args.profile_id, max_repairs=args.max_repairs,
                    model_id=model_id, lifecycle=lifecycle,
                )
            write_remote_artifact("selection", artifact_path, _selection_artifact(
                selection, pack, document, usage,
                retry_count=len(repair_diagnostics), repair_diagnostics=repair_diagnostics,
            ))
            write_remote_artifact("profile", args.output, profile)
        except Exception as error:
            materialization_error = error if isinstance(error, RequestMaterializationError) else None
            parse_error = error if isinstance(error, RequestSourceSelectionParseError) else None
            _emit_lifecycle(
                lifecycle, "remote_run_exception", error_type=type(error).__name__,
                error_message=_safe_error_message(error),
            )
            run_status["outcome"] = "failed"
            run_status["completed_at"] = _utc_now()
            run_status["exception"] = {
                "class": type(error).__name__, "message": _safe_error_message(error),
            }
            if materialization_error is not None:
                run_status["usage"] = materialization_error.usage
            elif parse_error is not None:
                run_status["usage"] = parse_error.usage
            try:
                write_remote_artifact("failure_profile", failure_path, {
                "status": "failed",
                "stage": (
                    "request_server_materialization" if materialization_error
                    else "request_source_selection_parse" if parse_error
                    else "request_source_selection"
                ),
                "error_type": type(error).__name__,
                "error_message": _safe_error_message(error), "response_text_stored": False,
                "retry_count": (
                    materialization_error.retry_count if materialization_error
                    else parse_error.retry_count if parse_error else 0
                ),
                "repair_diagnostics": (
                    materialization_error.diagnostics if materialization_error
                    else parse_error.diagnostics if parse_error else []
                ),
                "usage": (
                    materialization_error.usage if materialization_error
                    else parse_error.usage if parse_error else []
                ),
                })
            except Exception:
                # Preserve and re-raise the original remote/materialization error.
                pass
            if materialization_error is not None:
                try:
                    write_remote_artifact("failure_selection", selection_failure_path, _failure_selection_artifact(
                        materialization_error, pack, document, profile_id=args.profile_id,
                    ))
                except Exception:
                    # The status artifact records this write failure when possible.
                    pass
            elif parse_error is not None:
                try:
                    write_remote_artifact("failure_selection", selection_failure_path, _selection_parse_failure_artifact(
                        parse_error, pack, document, profile_id=args.profile_id,
                    ))
                except Exception:
                    # The status artifact records this write failure when possible.
                    pass
            _emit_lifecycle(lifecycle, "remote_run_failed")
            _write_run_status(status_path, run_status)
            raise
        run_status["outcome"] = "completed"
        run_status["completed_at"] = _utc_now()
        run_status["usage"] = usage
        _emit_lifecycle(lifecycle, "remote_run_completed")
        _write_run_status(status_path, run_status)
        return
    elif args.selection:
        selection = _load_selection(args.selection, args.profile_id)
    else:
        # Safe default: no model call. --dry-run writes full model input;
        # no mode writes a compact fillable selection shell.
        _write_json(args.output, {
            "artifact_kind": "request_source_selection_template",
            "selection_contract": REQUEST_SELECTION_PROMPT_VERSION,
            "profile_id": args.profile_id, "candidate_pack_id": pack.pack_id,
            "program_hierarchy": [], "facts": [],
            "support_components": [], "delivery_relations": [], "delivery_methods": [], "field_states": [],
            "remote_model_called": False,
        })
        return

    if not args.remote:
        try:
            profile = assemble_request_profile_v012(
                document, pack, selection, model_id="not_called", prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
            )
        except ValueError as error:
            _write_json(args.output.with_suffix(".failure.json"), {
                "status": "failed", "stage": "request_server_materialization", "error_type": type(error).__name__,
                "error_message": str(error), "response_text_stored": False,
                "retry_count": 0, "repair_diagnostics": [],
            })
            raise
    _write_json(args.output, profile)


if __name__ == "__main__":
    main()
