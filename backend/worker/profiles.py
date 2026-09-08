"""Slice 1: 원본 문서 → Common IR v1 → Request Profile v0.1.2 스냅샷.

값 판정과 근거 접지는 전부 vendored 패키지(서버 측 exact-span 재료화)가 한다.
여기서는 (1) 파싱을 subprocess 로 돌리고, (2) LLM 포트를 패키지가 요구하는
selector 시그니처로 어댑트하고, (3) 실패를 reason code 로 보존한다.
프롬프트 문구를 새로 쓰지 않는다: 패키지의 것을 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from pydantic import ValidationError

from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)

from . import vendor
from .contracts.profile_snapshot import (
    CANDIDATE_PACK_EMPTY,
    COMMON_IR_INVALID,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    MATERIALIZATION_FAILED,
    PARSE_FAILED,
    REPAIR_BUDGET_EXHAUSTED,
    CommonIrArtifact,
    ProfileSnapshot,
    StageDiagnostic,
)

from common_ir_pipeline.schema import validation_errors  # noqa: E402
from semantic_structuring.request_profile_v012 import (  # noqa: E402
    REQUEST_PIPELINE_VERSION,
    RequestSourceSelectionV012,
    build_request_candidate_pack,
    resolve_request_type_from_candidate_pack,
)
from semantic_structuring.run_request_profile_v012 import (  # noqa: E402
    REQUEST_SELECTION_PROMPT_VERSION,
    RequestMaterializationError,
    RequestSourceSelectionParseError,
    _normalize_remote_selection_payload,
    request_selection_instructions,
    select_and_materialize_with_repairs,
    selection_request_payload,
)


class StageError(RuntimeError):
    """reason code 를 달고 올라오는 단계 실패. 호출자가 처리 방식을 정한다."""

    def __init__(self, diagnostic: StageDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


# ---------------------------------------------------------------- 1. 파싱


def parse_to_common_ir(
    *,
    input_path: Path | str,
    notice_id: str,
    source_kind: str,
    run_dir: Path | str,
) -> CommonIrArtifact:
    """vendored E2E 러너를 subprocess 로 돌려 Common IR v1 을 만든다.

    러너는 CLI 전용(``main() -> int``)이고 import 가능한 진입점이 없다.
    문서 전체 파싱 실패는 분석 자체의 치명 실패다 (초안 §9.4).
    """

    input_path = Path(input_path)
    run_dir = Path(run_dir)
    command = [
        sys.executable,
        "-m",
        "common_ir_pipeline.run_rhwp_e2e",
        "--notice-id", notice_id,
        "--input", str(input_path),
        "--source-kind", source_kind,
        "--run-dir", str(run_dir),
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=_subprocess_env(),
    )
    if completed.returncode != 0:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(input_path),
                reason_code=PARSE_FAILED,
                message=(completed.stderr or completed.stdout or "").strip()[:2000],
            )
        )

    common_ir_path = run_dir / "common_ir_v1" / f"{notice_id}.{source_kind}.json"
    manifest_path = run_dir / "run_manifest.json"
    try:
        document = json.loads(common_ir_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(common_ir_path),
                reason_code=PARSE_FAILED,
                message=f"{type(error).__name__}: {error}",
            )
        ) from None

    errors = validation_errors(document)
    source = document.get("document", {})
    artifact = CommonIrArtifact(
        run_dir=str(run_dir),
        notice_id=notice_id,
        source_kind=source_kind,
        source_path=str(input_path.resolve()),
        source_sha256=manifest.get("input", {}).get("sha256", ""),
        common_ir_path=str(common_ir_path),
        common_ir_document_id=source.get("document_id", ""),
        manifest=manifest,
        block_count=len(document.get("blocks", [])),
        validation_errors=errors,
        document=document,
    )
    if errors:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=artifact.common_ir_document_id or str(common_ir_path),
                reason_code=COMMON_IR_INVALID,
                message="; ".join(errors)[:2000],
            )
        )
    return artifact


def _subprocess_env() -> dict[str, str]:
    """vendored 패키지가 자식 프로세스에서도 import 되게 PYTHONPATH 를 얹는다."""

    env = dict(os.environ)
    entries = [str(path) for path in vendor.VENDOR_PATHS]
    existing = env.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


# ------------------------------------------------------- 2. CandidatePack


def build_pack(document: dict[str, Any]):
    """CandidatePack 생성. 블록이 하나도 없으면 구조화를 시작하지 않는다.

    ``CandidatePack.blocks`` 는 ``min_length=1`` 이라 빈 팩은 빈 객체로
    돌아오지 않고 ValidationError 로 터진다. 두 경로 모두 같은 reason code 다.
    """

    try:
        pack = build_request_candidate_pack(document)
    except (ValidationError, ValueError) as error:
        raise StageError(
            StageDiagnostic(
                stage="build_pack",
                unit=document.get("document", {}).get("document_id"),
                reason_code=CANDIDATE_PACK_EMPTY,
                message=f"{type(error).__name__}: {error}"[:2000],
            )
        ) from None
    if not pack.blocks:
        raise StageError(
            StageDiagnostic(
                stage="build_pack",
                unit=document.get("document", {}).get("document_id"),
                reason_code=CANDIDATE_PACK_EMPTY,
                message="CandidatePack contains no blocks",
            )
        )
    return pack


def resolve_request_type(pack) -> dict[str, Any]:
    """요청 유형은 원본 체크박스 글리프로 서버가 정한다.

    LLM 판정 대상이 아니며 LLM 이 이 값을 덮어써서도 안 된다.
    """

    return resolve_request_type_from_candidate_pack(pack)


# ------------------------------------------------------------ 3. selector

# 패키지 ``_remote_selection`` 이 쓰는 것과 같은 수리 지시문. 문구를 새로
# 만들면 계약이 갈라지므로 그대로 복사해 둔다.
_REPAIR_INSTRUCTION = (
    " The prior parsed selection failed server exact-span/provenance validation. "
    "Return a complete replacement RequestSourceSelectionV012. Modify only invalid anchors or selection structure, "
    "preserve valid entries, and copy literal CandidatePack substrings exactly, including Markdown syntax such as ** when present."
)


def _selection_validation_messages(
    error: ValidationError, raw: dict[str, Any]
) -> list[str]:
    """Keep Pydantic repair hints useful without echoing model input."""

    raw_strings: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str):
            raw_strings.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(raw)
    messages: list[str] = []
    for issue in error.errors(include_input=False, include_context=False):
        location = ".".join(str(part) for part in issue.get("loc", ()))
        message = str(issue.get("msg", "validation failed"))
        for value in sorted(raw_strings, key=len, reverse=True):
            if value:
                message = message.replace(value, "[redacted]")
        messages.append(f"{location}: {message}" if location else message)
    return messages


def make_vllm_selector(
    llm_client,
    *,
    model_profile: str,
    pack,
    document: dict[str, Any],
    profile_id: str,
) -> Callable[
    [RequestSourceSelectionV012 | None, list[str] | None],
    tuple[RequestSourceSelectionV012, dict[str, Any]],
]:
    """``LLMClient.generate_structured`` 를 패키지 selector 시그니처로 어댑트한다.

    ponytail: 패키지 오케스트레이션이 동기라서 여기서 ``asyncio.run`` 으로
    끊는다. 워커를 async 루프 안에서 돌리게 되면 그때 스레드로 뺀다.
    """

    def selector(
        prior_selection: RequestSourceSelectionV012 | None,
        server_validation_errors: list[str] | None,
    ) -> tuple[RequestSourceSelectionV012, dict[str, Any]]:
        repair = prior_selection is not None
        instructions = request_selection_instructions()
        payload = selection_request_payload(pack, document, profile_id)
        if repair:
            instructions += _REPAIR_INSTRUCTION
            payload["prior_selection"] = prior_selection.model_dump(mode="json")
            payload["server_validation_errors"] = server_validation_errors or []

        def call(
            call_instructions: str, call_payload: dict[str, Any]
        ) -> RequestSourceSelectionV012:
            return asyncio.run(
                llm_client.generate_structured(
                    task_name="request_source_selection_v012",
                    messages=[
                        Message(role="system", content=call_instructions),
                        Message(
                            role="user",
                            content=json.dumps(call_payload, ensure_ascii=False),
                        ),
                    ],
                    response_schema=RequestSourceSelectionV012,
                    model_profile=model_profile,
                )
            )

        try:
            selection = call(instructions, payload)
        except LLMInvalidResponseError as error:
            # The adapter keeps parsed schema-invalid JSON in ``raw``.  Reuse
            # the vendored remote compatibility rule before spending a call:
            # value_span_candidate_id is authoritative and redundant
            # anchor_text is removed, but missing anchors are never invented.
            if not isinstance(error.raw, dict):
                raise
            normalized = _normalize_remote_selection_payload(error.raw)
            try:
                selection = RequestSourceSelectionV012.model_validate(normalized)
            except ValidationError as validation_error:
                if repair:
                    raise error from None
                repair_payload = selection_request_payload(pack, document, profile_id)
                repair_payload["prior_selection"] = normalized
                repair_payload["server_validation_errors"] = _selection_validation_messages(
                    validation_error, normalized
                )
                selection = call(
                    instructions + _REPAIR_INSTRUCTION,
                    repair_payload,
                )
        # 패키지 오케스트레이터는 selector 예외 중 RequestSourceSelectionParseError
        # 만 잡는다. ValueError 로 던지면 아무도 잡지 않아 FAILED 스냅샷이 아니라
        # raw 예외로 새어 나간다. 포트 예외로 던져 LLM_INVALID_RESPONSE 로 격리한다.
        if selection.profile_id != profile_id:
            raise LLMInvalidResponseError(
                "selection profile_id does not match requested profile_id"
            )
        if selection.candidate_pack_id != pack.pack_id:
            raise LLMInvalidResponseError(
                "selection candidate_pack_id does not match supplied CandidatePack"
            )
        # 사용량은 포트가 돌려주지 않는다. 값을 지어내지 않고 repair 여부만 남긴다.
        return selection, {"repair": repair}

    return selector


# ------------------------------------------------------------ 4. 구조화


def structure_request_profile(
    *,
    document: dict[str, Any],
    pack,
    profile_id: str,
    selector,
    model_id: str,
    max_repairs: int = 1,
    common_ir: CommonIrArtifact | None = None,
) -> ProfileSnapshot:
    """선택 → 서버 재료화. 실패는 부분 프로파일이 아니라 FAILED 스냅샷이 된다."""

    def failed(
        diagnostics: list[StageDiagnostic], attempts: int, usage: list[dict[str, Any]]
    ) -> ProfileSnapshot:
        return ProfileSnapshot(
            profile_id=profile_id,
            status="FAILED",
            profile=None,
            candidate_pack_id=pack.pack_id,
            common_ir=common_ir,
            model_id=model_id,
            prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
            profile_contract_version=REQUEST_PIPELINE_VERSION,
            selection_attempts=attempts,
            usage=usage,
            diagnostics=diagnostics,
        )

    try:
        profile, _selection, usage, diagnostics = select_and_materialize_with_repairs(
            selector,
            document,
            pack,
            profile_id,
            max_repairs=max_repairs,
            model_id=model_id,
        )
    except RequestMaterializationError as error:
        return failed(
            [
                StageDiagnostic(
                    stage="structure_request_profile",
                    unit=row.get("error_type"),
                    reason_code=MATERIALIZATION_FAILED,
                    message=str(row.get("validation_error"))[:2000],
                    attempt=row.get("repair_attempt"),
                )
                for row in error.diagnostics
            ]
            + [
                StageDiagnostic(
                    stage="structure_request_profile",
                    unit=profile_id,
                    reason_code=REPAIR_BUDGET_EXHAUSTED,
                    message=str(error)[:2000],
                    attempt=error.retry_count + 1,
                    terminated_because=REPAIR_BUDGET_EXHAUSTED,
                )
            ],
            error.retry_count + 1,
            list(error.usage),
        )
    except RequestSourceSelectionParseError as error:
        return failed(
            [
                StageDiagnostic(
                    stage="structure_request_profile",
                    unit=row.get("error_type"),
                    reason_code=LLM_INVALID_RESPONSE,
                    message=str(row.get("validation_error"))[:2000],
                    attempt=row.get("repair_attempt"),
                    terminated_because=LLM_INVALID_RESPONSE,
                )
                for row in error.diagnostics
            ],
            error.retry_count + 1,
            list(error.usage),
        )
    except (LLMTimeoutError, LLMUnavailableError, LLMInvalidResponseError) as error:
        reason_code = {
            LLMTimeoutError: LLM_TIMEOUT,
            LLMUnavailableError: LLM_UNAVAILABLE,
            LLMInvalidResponseError: LLM_INVALID_RESPONSE,
        }[type(error)]
        return failed(
            [
                StageDiagnostic(
                    stage="structure_request_profile",
                    unit=profile_id,
                    reason_code=reason_code,
                    message=str(error)[:2000],
                    attempt=1,
                    terminated_because=reason_code,
                )
            ],
            1,
            [],
        )

    return ProfileSnapshot(
        profile_id=profile_id,
        status="OK",
        profile=profile,
        candidate_pack_id=pack.pack_id,
        common_ir=common_ir,
        model_id=model_id,
        prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
        profile_contract_version=REQUEST_PIPELINE_VERSION,
        selection_attempts=len(usage),
        usage=usage,
        diagnostics=[
            StageDiagnostic(
                stage="structure_request_profile",
                unit=row.get("error_type"),
                reason_code=MATERIALIZATION_FAILED,
                message=str(row.get("validation_error"))[:2000],
                attempt=row.get("repair_attempt"),
            )
            for row in diagnostics
        ],
    )
