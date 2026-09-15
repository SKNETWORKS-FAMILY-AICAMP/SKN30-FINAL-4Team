#!/usr/bin/env python3
"""Run the six-notice Existing Profile canary against pinned Common IR.

This command is deliberately inert without ``--execute-openai``.  Its model
phase accepts only the six Common IR documents that pass the shared
manual-adjudication preflight.  Frozen Gold is not opened, hashed, or otherwise
inspected until every provider call has finished.  The post-call local semantic
gate is therefore unable to influence an OpenAI request.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Mapping
from zipfile import ZIP_STORED, ZipFile, ZipInfo


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from worker import announcement_profiles, vendor  # noqa: E402,F401
from worker.adapters.openai_llm_client import (  # noqa: E402
    OpenAICompletionTelemetry,
    OpenAILLMClient,
)
from worker.ports.llm import LLMClient  # noqa: E402
from worker.profiles import StageError  # noqa: E402

from scripts import run_existing_a_routing_canary as routing_canary  # noqa: E402
from scripts.compare_existing_profile_semantics import _regular_file_sha256  # noqa: E402
from worker.evaluation.existing_profile_semantic_diff import (  # noqa: E402
    ExistingProfileSemanticError,
    compare_semantic_profile_corpora,
    write_semantic_report,
)
from worker.evaluation.existing_profile_diff import (  # noqa: E402
    ExistingProfileComparisonError,
    _close_descriptors,
    _directory_fd_is_within,
    _open_absolute_directory_chain_no_follow,
    write_comparison_report,
)


REPORT_SCHEMA_VERSION = "existing_profile_canary/v1"
REPORT_FILE_NAME = "existing-profile-canary.v1.json"
CANDIDATE_ZIP_FILE_NAME = "existing-profile-canary.candidates.v1.zip"
BASELINE_ARCHIVE_SHA256 = routing_canary.BASELINE_ARCHIVE_SHA256
GOLD_FREEZE_MANIFEST_SHA256 = routing_canary.GOLD_FREEZE_MANIFEST_SHA256
CORRECTED_NOTICE_IDS = routing_canary.CORRECTED_NOTICE_IDS
PINNED_OPENAI_MODEL_ID = "gpt-5.6-terra"
TASK_COMPLETION_TOKEN_CAPS = {
    announcement_profiles.SECTION_SCOPE_TASK: 4096,
    announcement_profiles.BLOCK_ROUTER_TASK: 16384,
    announcement_profiles.SOURCE_SELECTION_TASK: 32768,
    announcement_profiles.ANCHOR_CORRECTION_TASK: 4096,
}
DEFAULT_TIMEOUT_SECONDS = 90.0
MAX_TIMEOUT_SECONDS = 120.0
SOURCE_SELECTION_ATTEMPTS = 2  # initial selection plus at most one server-guided repair

# These are limits, reserved before the corresponding task reaches the
# provider.  The two attachment scope calls are a fixed property of the
# reviewed baseline; anchor correction is bounded separately because it is
# conditional on a selection response.
TASK_BUDGETS = {
    announcement_profiles.SECTION_SCOPE_TASK: 2,
    announcement_profiles.BLOCK_ROUTER_TASK: 6,
    announcement_profiles.SOURCE_SELECTION_TASK: 12,
    announcement_profiles.ANCHOR_CORRECTION_TASK: 12,
}
MAX_OPENAI_CALLS = sum(TASK_BUDGETS.values())
PROMPT_HASHES = {
    "section_scope": "55e40c4e9297556d3cfea7ce3c26652f386ce3fb5bb834ff34e7e396b4c39396",
    "block_router": "9ff379ff0a6f2a1f4b152bcbf034b4d9e53ef44fab8aa1331dcab86230a676e9",
    "source_selection": "dd9c1e030e83e1339d2f626e07e5b10300caa920a3f2bdde86fbaf54bbd7d0c3",
    "anchor_correction": "488a429af775d9d56507b98e4c0970bf31d7ab636bbe450c6a190dcb5d0c2e28",
    "repair": "8096b56f4614f9637452e871c8cbb2168127ba80322d9cc0406eb7eb0703adaa",
    "support_scale_repair": "62e018220d6b2c468e80f8fef3e7b16171ad23fa9a07134272926558ca6fffc2",
}
_SAFE_STAGE_CODES = frozenset({
    "common_ir_v1_preparation", "section_scope_discovery", "section_scope_apply",
    "block_candidate_router", "native_exact_candidate_transform", "source_selection",
    "anchor_correction", "final_profile_assembly",
})
_SAFE_REASON_CODES = frozenset({
    "COMMON_IR_INVALID", "LLM_INVALID_RESPONSE", "LLM_TIMEOUT", "LLM_UNAVAILABLE",
    "CANDIDATE_PACK_EMPTY", "MATERIALIZATION_FAILED", "REPAIR_BUDGET_EXHAUSTED",
})


class ExistingProfileCanaryError(ValueError):
    """The fixed full-profile canary cannot safely proceed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExistingProfileCanaryError(message)


def _verify_prompt_pins() -> None:
    """Fail closed if a model-facing prompt drifted from this reviewed run."""

    # Evaluate every prompt used by this full chain now, before a client is
    # constructed.  Comparing just a maintained hash dictionary would miss a
    # stale local correction/repair literal.
    actual = {
        "section_scope": sha256(announcement_profiles._scope_instructions().encode("utf-8")).hexdigest(),
        "block_router": sha256(announcement_profiles._router_instructions().encode("utf-8")).hexdigest(),
        "source_selection": sha256(announcement_profiles._source_selection_instructions().encode("utf-8")).hexdigest(),
        "anchor_correction": sha256(announcement_profiles._ANCHOR_CORRECTION_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "repair": sha256(announcement_profiles._REPAIR_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "support_scale_repair": sha256(
            announcement_profiles._SUPPORT_SCALE_REPAIR_INSTRUCTIONS.encode("utf-8")
        ).hexdigest(),
    }
    _require(actual == PROMPT_HASHES, "announcement profile prompt hash pin mismatch")
    _require(
        announcement_profiles._PROMPT_BUNDLE_COMPONENT_HASHES == PROMPT_HASHES,
        "announcement profile prompt bundle hash map mismatch",
    )


class _CountingLlm:
    """Reserve a fixed, task-specific provider budget before delegation."""

    def __init__(self, delegate: LLMClient) -> None:
        self._delegate = delegate
        self.calls: list[dict[str, int | str]] = []
        self._by_task: Counter[str] = Counter()

    async def generate_structured(self, **kwargs: Any) -> Any:
        task_name = kwargs.get("task_name")
        _require(isinstance(task_name, str) and task_name in TASK_BUDGETS, "canary provider task is disallowed")
        _require(len(self.calls) < MAX_OPENAI_CALLS, "canary hard provider-call budget is exhausted")
        _require(self._by_task[task_name] < TASK_BUDGETS[task_name], "canary task provider-call budget is exhausted")
        # Append before delegation: transport failures still consume a call.
        call: dict[str, int | str] = {"task_name": task_name, "duration_ms": 0}
        self.calls.append(call)
        self._by_task[task_name] += 1
        started = time.perf_counter()
        try:
            return await self._delegate.generate_structured(**kwargs)
        finally:
            call["duration_ms"] = max(0, round((time.perf_counter() - started) * 1000))


class _TaskDispatchLlm:
    """Keep response caps task-specific without weakening the LLM port."""

    def __init__(self, delegates: Mapping[str, LLMClient]) -> None:
        _require(set(delegates) == set(TASK_BUDGETS), "canary task dispatcher is incomplete")
        self._delegates = dict(delegates)

    async def generate_structured(self, **kwargs: Any) -> Any:
        task_name = kwargs.get("task_name")
        _require(isinstance(task_name, str) and task_name in self._delegates, "canary provider task is disallowed")
        return await self._delegates[task_name].generate_structured(**kwargs)


def _safe_error(error: Exception) -> dict[str, str]:
    diagnostic = error.diagnostic if isinstance(error, StageError) else None
    if diagnostic is not None:
        stage = str(getattr(diagnostic, "stage", ""))
        reason = str(getattr(diagnostic, "reason_code", ""))
        return {
            "stage": stage if stage in _SAFE_STAGE_CODES else "canary",
            "reason_code": reason if reason in _SAFE_REASON_CODES else "UNKNOWN",
        }
    return {"stage": "canary", "reason_code": type(error).__name__}


def build_plan(*, baseline_zip: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read only the pinned baseline; this function must never touch Gold."""

    _verify_prompt_pins()
    try:
        documents, baseline_sha256, member_sha256 = routing_canary._read_baseline_common_ir(
            baseline_zip
        )
    except routing_canary.ExistingARoutingCanaryError as error:
        raise ExistingProfileCanaryError(str(error)) from error
    attachment_counts = {
        notice_id: routing_canary._attachment_count(document)
        for notice_id, document in documents.items()
    }
    _require(
        sum(count > 0 for count in attachment_counts.values()) == TASK_BUDGETS[announcement_profiles.SECTION_SCOPE_TASK],
        "pinned baseline scope-call plan no longer matches its budget",
    )
    return documents, {
        "schema_version": REPORT_SCHEMA_VERSION,
        "execution_status": "planned",
        "semantic_profile_status": "not_run",
        "baseline": {"archive_sha256": baseline_sha256},
        # This is a reviewed constant, not a read of the Gold filesystem.
        "gold_oracle": {"freeze_manifest_sha256": GOLD_FREEZE_MANIFEST_SHA256, "read_phase": "post_openai_only"},
        "notice_ids": list(CORRECTED_NOTICE_IDS),
        "model": None,
        "pins": {
            "openai_model_id": PINNED_OPENAI_MODEL_ID,
            "prompt_bundle_version": announcement_profiles.PROMPT_BUNDLE_VERSION,
            "prompt_sha256": dict(PROMPT_HASHES),
            "native_exact_candidate_mode": "lines+continuations",
            "composite_candidate_mode": "shadow",
            "source_selection_attempts": SOURCE_SELECTION_ATTEMPTS,
            "max_completion_tokens_by_task": dict(TASK_COMPLETION_TOKEN_CAPS),
        },
        "calls": {"hard_budget": MAX_OPENAI_CALLS, "task_budgets": dict(TASK_BUDGETS), "planned_minimum": 14},
        "notices": [
            {"notice_id": notice_id, "baseline_common_ir_sha256": member_sha256[notice_id], "attachment_section_count": attachment_counts[notice_id]}
            for notice_id in CORRECTED_NOTICE_IDS
        ],
        "limitations": [
            "baseline_common_ir_only_in_openai_requests",
            "manual_adjudication_metadata_rejected_before_provider_construction",
            "gold_is_not_read_until_all_openai_calls_finish",
            "zero_provider_retries_and_at_most_one_source_selection_repair",
            "report_excludes_raw_source_model_responses_and_secrets",
        ],
    }


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _prepare_output_directory_before_openai(
    output_dir: Path, gold_root: Path
) -> Path:
    """Check only the writable output boundary; do not inspect Gold yet."""

    lexical = Path(os.path.abspath(output_dir.expanduser()))
    lexical_gold = Path(os.path.abspath(gold_root.expanduser()))
    try:
        lexical.relative_to(lexical_gold)
    except ValueError:
        pass
    else:
        raise ExistingProfileCanaryError(
            "output directory must be outside the frozen Gold root"
        )
    descriptors: list[int] = []
    try:
        descriptors = _open_absolute_directory_chain_no_follow(
            lexical, label="output directory"
        )
        _require(not os.listdir(descriptors[-1]), "output directory must be empty")
        return lexical
    except ExistingProfileComparisonError as error:
        raise ExistingProfileCanaryError(str(error)) from error
    finally:
        _close_descriptors(descriptors)


def _open_publish_directory(output_dir: Path, gold_root: Path) -> tuple[int, list[int], list[int]]:
    """Open output and Gold through no-follow directory FDs after OpenAI.

    The caller owns returned descriptors and must close both chains.  The
    descriptor relationship, rather than a path comparison, prevents a rename
    race from redirecting candidate publication into frozen Gold.
    """

    output = Path(os.path.abspath(output_dir.expanduser()))
    gold = Path(os.path.abspath(gold_root.expanduser()))
    output_chain: list[int] = []
    gold_chain: list[int] = []
    try:
        output_chain = _open_absolute_directory_chain_no_follow(output, label="output directory")
        gold_chain = _open_absolute_directory_chain_no_follow(gold, label="Gold root")
        output_fd = output_chain[-1]
        gold_fd = gold_chain[-1]
        _require(not _directory_fd_is_within(output_fd, gold_fd), "opened output directory is inside the frozen Gold root")
        return output_fd, output_chain, gold_chain
    except ExistingProfileComparisonError as error:
        _close_descriptors(gold_chain)
        _close_descriptors(output_chain)
        raise ExistingProfileCanaryError(str(error)) from error
    except Exception:
        _close_descriptors(gold_chain)
        _close_descriptors(output_chain)
        raise


def _publish_fd_bytes(directory_fd: int, gold_fd: int, *, name: str, payload: bytes) -> None:
    """Atomically publish 0600 bytes without following names or directory races."""

    _require(
        Path(name).name == name
        and name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name,
        "unsafe output filename",
    )
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    linked = False
    publish_complete = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _require(not _directory_fd_is_within(directory_fd, gold_fd), "opened output directory moved inside frozen Gold root")
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        linked = True
        if _directory_fd_is_within(directory_fd, gold_fd):
            os.unlink(name, dir_fd=directory_fd)
            linked = False
            raise ExistingProfileCanaryError("opened output directory moved into frozen Gold during publish")
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        publish_complete = True
    except FileExistsError as error:
        raise ExistingProfileCanaryError(f"canary output already exists: {name}") from error
    except OSError as error:
        raise ExistingProfileCanaryError("cannot publish canary output") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if linked and not publish_complete:
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.external_attr = 0o100600 << 16
    info.create_system = 3
    return info


def write_candidate_zip(
    artifacts: Mapping[str, announcement_profiles.FinalizedAnnouncementProfileArtifacts],
    documents: Mapping[str, Mapping[str, Any]],
    *, output_dir: Path,
    gold_root: Path,
) -> tuple[Path, str]:
    """Write exactly profile, selection, and Common IR files for each notice."""

    _require(tuple(artifacts) == CORRECTED_NOTICE_IDS, "candidate artifacts do not cover exactly the fixed notices")
    output_fd: int | None = None
    output_chain: list[int] = []
    gold_chain: list[int] = []
    try:
        payload = BytesIO()
        with ZipFile(payload, "w", compression=ZIP_STORED, allowZip64=False) as archive:
            for notice_id in CORRECTED_NOTICE_IDS:
                document = documents[notice_id]
                source_kind = document["document"]["source_kind"]
                _require(isinstance(source_kind, str) and source_kind, "baseline Common IR source kind is invalid")
                bundle = artifacts[notice_id]
                members = (
                    (f"{notice_id}/pipeline/structured_profile.v0.2.json", bundle.profile),
                    (f"{notice_id}/pipeline/source_selection.json", bundle.source_selection),
                    (f"{notice_id}/pipeline/common_ir_v1/{notice_id}.{source_kind}.json", document),
                )
                for name, value in members:
                    archive.writestr(_zip_info(name), _canonical_json(value))
        candidate_payload = payload.getvalue()
        candidate_sha256 = sha256(candidate_payload).hexdigest()
        output_fd, output_chain, gold_chain = _open_publish_directory(output_dir, gold_root)
        _require(not os.listdir(output_fd), "output directory changed after preflight")
        _publish_fd_bytes(
            output_fd,
            gold_chain[-1],
            name=CANDIDATE_ZIP_FILE_NAME,
            payload=candidate_payload,
        )
    except (OSError, ValueError, TypeError) as error:
        raise ExistingProfileCanaryError("cannot write deterministic candidate ZIP") from error
    finally:
        _close_descriptors(gold_chain)
        _close_descriptors(output_chain)
    candidate_path = (
        Path(os.path.abspath(output_dir.expanduser())) / CANDIDATE_ZIP_FILE_NAME
    )
    published_sha256 = _regular_file_sha256(
        candidate_path,
        label="candidate archive",
        max_bytes=routing_canary.MAX_ARCHIVE_BYTES,
    )
    _require(
        published_sha256 == candidate_sha256,
        "published candidate ZIP does not match the generated bytes",
    )
    return candidate_path, candidate_sha256


def _semantic_summary(
    candidate_zip: Path, *, baseline_zip: Path, gold_root: Path,
    expected_baseline_sha256: str,
    expected_gold_freeze_manifest_sha256: str,
    expected_candidate_sha256: str,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Run the existing B/G/C semantic gate only after provider work is done."""

    try:
        # This reads and verifies Gold after every OpenAI call has finished.
        routing_canary._load_gold_pins(gold_root)
        candidate_sha256 = _regular_file_sha256(
            candidate_zip,
            label="candidate archive",
            max_bytes=routing_canary.MAX_ARCHIVE_BYTES,
        )
        result = compare_semantic_profile_corpora(
            baseline_zip,
            gold_root,
            candidate_zip,
            expected_reference_count=100,
            expected_candidate_count=len(CORRECTED_NOTICE_IDS),
            notice_ids=CORRECTED_NOTICE_IDS,
        )
    except (ExistingProfileSemanticError, ValueError):
        return ({"status": "failed", "reason_code": "semantic_gate_input_or_validation_failed"}, None)
    counts = result.get("counts")
    _require(isinstance(counts, dict), "semantic gate report has invalid counts")
    candidate_identity = result.get("candidate")
    baseline_identity = result.get("baseline")
    gold_identity = result.get("gold")
    _require(
        isinstance(baseline_identity, Mapping)
        and baseline_identity.get("archive_sha256") == expected_baseline_sha256,
        "semantic gate baseline identity does not match the preflight baseline",
    )
    _require(
        isinstance(gold_identity, Mapping)
        and gold_identity.get("freeze_manifest_sha256") == expected_gold_freeze_manifest_sha256,
        "semantic gate Gold identity does not match the pinned freeze manifest",
    )
    _require(
        isinstance(candidate_identity, Mapping)
        and candidate_sha256 == expected_candidate_sha256
        and candidate_identity.get("archive_sha256") == expected_candidate_sha256,
        "semantic gate candidate identity does not match the pinned candidate ZIP",
    )
    return {
        "status": "passed" if counts.get("failed") == 0 else "failed",
        "counts": {key: value for key, value in counts.items() if isinstance(value, int)},
        # Keep this report source-free: no semantic atom, artifact, or error text.
        "compared_notice_ids": list(CORRECTED_NOTICE_IDS),
    }, result


def execute_canary(
    *, documents: Mapping[str, dict[str, Any]], plan: Mapping[str, Any], llm_client: LLMClient,
    model_id: str, timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, announcement_profiles.FinalizedAnnouncementProfileArtifacts]]:
    _require(model_id == PINNED_OPENAI_MODEL_ID, "model id does not match the pinned canary model")
    _require(0 < timeout_seconds <= MAX_TIMEOUT_SECONDS, "timeout is outside the bounded canary range")
    counting_llm = _CountingLlm(llm_client)
    rows: list[dict[str, Any]] = []
    artifacts: dict[str, announcement_profiles.FinalizedAnnouncementProfileArtifacts] = {}
    status = "succeeded"
    for notice_id in CORRECTED_NOTICE_IDS:
        row = next(item for item in plan["notices"] if item["notice_id"] == notice_id).copy()
        try:
            artifacts[notice_id] = announcement_profiles.structure_announcement_profile_artifacts(
                documents[notice_id], counting_llm, model_profile="existing_profile_canary",
                composite_candidate_mode="shadow", native_exact_candidate_mode="lines+continuations",
                source_selection_attempts=SOURCE_SELECTION_ATTEMPTS,
            )
            row["profile"] = {"status": "valid"}
        except Exception as error:  # never persist model/provider/source text
            row["profile"] = {"status": "failed", "error": _safe_error(error)}
            rows.append(row)
            status = "failed"
            break  # fixed canary deliberately stops at the first notice failure
        rows.append(row)
    calls_by_task = Counter(str(call["task_name"]) for call in counting_llm.calls)
    report = dict(plan)
    report["execution_status"] = status
    report["model"] = {
        "provider": "openai", "model_id": model_id, "transport": "chat_completions_json_schema",
        "store": False, "timeout_seconds": timeout_seconds,
        "max_completion_tokens_by_task": dict(TASK_COMPLETION_TOKEN_CAPS),
        "max_retries": 0, "reasoning_effort": "medium",
    }
    report["calls"] = {
        **plan["calls"], "attempted": len(counting_llm.calls),
        "attempted_by_task": dict(sorted(calls_by_task.items())),
        "latency_ms": sum(int(call["duration_ms"]) for call in counting_llm.calls),
    }
    if status == "succeeded":
        scope_calls = calls_by_task[announcement_profiles.SECTION_SCOPE_TASK]
        router_calls = calls_by_task[announcement_profiles.BLOCK_ROUTER_TASK]
        selection_calls = calls_by_task[announcement_profiles.SOURCE_SELECTION_TASK]
        call_plan_ok = (
            scope_calls == TASK_BUDGETS[announcement_profiles.SECTION_SCOPE_TASK]
            and router_calls == TASK_BUDGETS[announcement_profiles.BLOCK_ROUTER_TASK]
            and len(CORRECTED_NOTICE_IDS) <= selection_calls <= TASK_BUDGETS[announcement_profiles.SOURCE_SELECTION_TASK]
            and len(counting_llm.calls) >= 14
        )
        report["calls"]["successful_plan_status"] = "valid" if call_plan_ok else "failed"
        if not call_plan_ok:
            report["execution_status"] = "failed"
            report["post_provider_error"] = {
                "stage": "call_plan_validation", "reason_code": "CALL_MINIMUM_OR_FIXED_COUNT_MISMATCH"
            }
    else:
        report["calls"]["successful_plan_status"] = "not_applicable"
    report["notices"] = rows
    return report, artifacts


def write_report(report: Mapping[str, Any], *, output_dir: Path, gold_root: Path) -> Path:
    try:
        return write_comparison_report(
            report,
            output_dir=output_dir,
            gold_root=gold_root,
            report_file_name=REPORT_FILE_NAME,
        )
    except ExistingProfileComparisonError as error:
        raise ExistingProfileCanaryError("cannot write canary report") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", required=True, type=Path)
    parser.add_argument("--gold-root", required=True, type=Path)
    parser.add_argument("--execute-openai", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-dir", type=Path)
    return parser


def _report_publication_failure_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return source-free accounting when no safe on-disk report is possible."""

    calls = report.get("calls")
    calls = calls if isinstance(calls, Mapping) else {}
    usage = calls.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}

    def numeric(mapping: Mapping[str, Any], key: str) -> int | None:
        value = mapping.get(key)
        return value if type(value) is int and value >= 0 else None

    return {
        "status": "failed",
        "reason_code": "REPORT_PUBLICATION_FAILED",
        "report_file": None,
        "attempted_calls": numeric(calls, "attempted"),
        "provider_completed_calls": numeric(usage, "provider_completed_calls"),
        "prompt_tokens": numeric(usage, "prompt_tokens"),
        "completion_tokens": numeric(usage, "completion_tokens"),
        "total_tokens": numeric(usage, "total_tokens"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        documents, plan = build_plan(baseline_zip=args.baseline_zip)
        if not args.execute_openai:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
            return 0
        _require(args.model == PINNED_OPENAI_MODEL_ID, "--model must equal the pinned canary model")
        _require(args.output_dir is not None, "--output-dir is required with --execute-openai")
        _require(0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS, "--timeout-seconds is outside the bounded canary range")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        _require(bool(api_key), "OPENAI_API_KEY is required with --execute-openai")
        _prepare_output_directory_before_openai(args.output_dir, args.gold_root)
        _require(os.environ.get("OPENAI_LOG", "").strip().lower() != "debug", "OPENAI_LOG=debug is forbidden for this canary")
        telemetry: list[OpenAICompletionTelemetry] = []
        clients = {
            task_name: OpenAILLMClient(
                api_key=api_key, model_profiles={"existing_profile_canary": args.model},
                timeout_seconds=args.timeout_seconds, max_completion_tokens=token_cap,
                reasoning_effort="medium",
                telemetry_callback=telemetry.append,
            )
            for task_name, token_cap in TASK_COMPLETION_TOKEN_CAPS.items()
        }
        client = _TaskDispatchLlm(clients)
        report, artifacts = execute_canary(
            documents=documents, plan=plan, llm_client=client, model_id=args.model,
            timeout_seconds=args.timeout_seconds,
        )
        report["calls"]["usage"] = {
            "prompt_tokens": sum(item.prompt_tokens or 0 for item in telemetry),
            "completion_tokens": sum(item.completion_tokens or 0 for item in telemetry),
            "total_tokens": sum(item.total_tokens or 0 for item in telemetry),
            "provider_completed_calls": len(telemetry),
        }
        if report["execution_status"] == "succeeded":
            try:
                candidate_zip, candidate_sha256 = write_candidate_zip(
                    artifacts,
                    documents,
                    output_dir=args.output_dir,
                    gold_root=args.gold_root,
                )
                report["candidate"] = {
                    "archive_sha256": candidate_sha256,
                    "file": candidate_zip.name,
                }
                semantic_summary, semantic_report = _semantic_summary(
                    candidate_zip,
                    baseline_zip=args.baseline_zip,
                    gold_root=args.gold_root,
                    expected_baseline_sha256=str(report["baseline"]["archive_sha256"]),
                    expected_gold_freeze_manifest_sha256=str(report["gold_oracle"]["freeze_manifest_sha256"]),
                    expected_candidate_sha256=candidate_sha256,
                )
                report["semantic_gate"] = semantic_summary
                report["semantic_profile_status"] = semantic_summary["status"]
                if semantic_report is not None:
                    semantic_path = write_semantic_report(
                        semantic_report, output_dir=args.output_dir, gold_root=args.gold_root
                    )
                    report["semantic_gate"]["opaque_report_file"] = semantic_path.name
            except Exception as error:  # provider work is done: preserve a safe report
                report["semantic_gate"] = {
                    "status": "failed",
                    "reason_code": "POST_PROVIDER_ARTIFACT_OR_SEMANTIC_GATE_FAILURE",
                }
                report["semantic_profile_status"] = "failed"
                report["post_provider_error"] = {
                    "stage": "post_provider_artifact_or_semantic_gate",
                    "reason_code": type(error).__name__,
                }
        else:
            report["semantic_gate"] = {"status": "not_run"}
            report["semantic_profile_status"] = "not_run"
        try:
            report_path = write_report(
                report, output_dir=args.output_dir, gold_root=args.gold_root
            )
        except ExistingProfileCanaryError:
            print(
                json.dumps(
                    _report_publication_failure_summary(report),
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 1
    except ExistingProfileCanaryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"ERROR: canary failed: {type(error).__name__}", file=sys.stderr)
        return 1
    succeeded = report["execution_status"] == "succeeded" and report["semantic_gate"]["status"] == "passed"
    print(json.dumps({"status": "passed" if succeeded else "failed", "report_file": report_path.name, "attempted_calls": report["calls"]["attempted"]}, sort_keys=True))
    return 0 if succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
