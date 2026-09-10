"""Slice 1: 원본 문서 → Common IR v1 → Request Profile v0.1.2 스냅샷.

값 판정과 근거 접지는 전부 vendored 패키지(서버 측 exact-span 재료화)가 한다.
여기서는 (1) 파싱을 subprocess 로 돌리고, (2) LLM 포트를 패키지가 요구하는
selector 시그니처로 어댑트하고, (3) 실패를 reason code 로 보존한다.
프롬프트 문구를 새로 쓰지 않는다: 패키지의 것을 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any, Callable
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError

from .ports.llm import (
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
    PARTIAL_MATERIALIZATION,
    REPAIR_BUDGET_EXHAUSTED,
    REQUEST_TYPE_CONTAINER_AMBIGUOUS,
    REQUEST_TYPE_CONTAINER_MISSING,
    REQUEST_TYPE_SELECTION_INVALID,
    CommonIrArtifact,
    ProfileSnapshot,
    StageDiagnostic,
)

from common_ir_pipeline.schema import validation_errors  # noqa: E402
import semantic_structuring.request_profile_v012 as _request_profile_contract  # noqa: E402
from semantic_structuring.request_profile_v012 import (  # noqa: E402
    REQUEST_PIPELINE_VERSION,
    assemble_request_profile_v012,
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


_REQUEST_TYPE_COMPAT_GLYPH = "■"
DEFAULT_PARSE_TIMEOUT_SECONDS = 120.0
MAX_HWPX_MEMBERS = 10_000
MAX_HWPX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def _ensure_request_type_checked_glyph_compatibility() -> None:
    """Keep older vendored profile packages compatible with real form glyphs.

    The source text is never rewritten: the resolver still receives the
    original CandidatePack and its selection/evidence spans. The vendored
    package owns the canonical set; this idempotent boundary shim only fills
    the missing glyph when an older package is present. With the current
    package (which includes ``■``) this is a no-op.
    """

    checked = frozenset(getattr(_request_profile_contract, "CHECKED_GLYPHS", ()))
    if _REQUEST_TYPE_COMPAT_GLYPH not in checked:
        _request_profile_contract.CHECKED_GLYPHS = checked | {_REQUEST_TYPE_COMPAT_GLYPH}


_ensure_request_type_checked_glyph_compatibility()


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
    timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS,
) -> CommonIrArtifact:
    """vendored E2E 러너를 subprocess 로 돌려 Common IR v1 을 만든다.

    러너는 CLI 전용(``main() -> int``)이고 import 가능한 진입점이 없다.
    문서 전체 파싱 실패는 분석 자체의 치명 실패다 (초안 §9.4).
    """

    input_path = Path(input_path)
    run_dir = Path(run_dir)
    if (
        not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("parser timeout must be positive")
    if source_kind == "hwpx":
        _validate_hwpx_resource_bounds(input_path)
    command = [
        sys.executable,
        "-m",
        "common_ir_pipeline.run_rhwp_e2e",
        "--notice-id", notice_id,
        "--input", str(input_path),
        "--source-kind", source_kind,
        "--run-dir", str(run_dir),
    ]
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=_subprocess_env(),
    )
    try:
        stdout, stderr = process.communicate(timeout=float(timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(input_path),
                reason_code=PARSE_FAILED,
                message=f"parser exceeded {float(timeout_seconds):g} second deadline",
            )
        ) from None

    if process.returncode != 0:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(input_path),
                reason_code=PARSE_FAILED,
                message=(stderr or stdout or "").strip()[:2000],
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


def _validate_hwpx_resource_bounds(input_path: Path) -> None:
    """Reject HWPX containers whose declared expansion can exhaust a worker."""

    try:
        with ZipFile(input_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_HWPX_MEMBERS:
                raise ValueError("HWPX container has too many members")
            unpacked = sum(member.file_size for member in members)
            if unpacked > MAX_HWPX_UNCOMPRESSED_BYTES:
                raise ValueError("HWPX declared uncompressed size exceeds limit")
            if any(member.flag_bits & 0x1 for member in members):
                raise ValueError("encrypted HWPX members are not supported")
    except (OSError, BadZipFile) as error:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(input_path),
                reason_code=PARSE_FAILED,
                message=f"invalid HWPX container: {type(error).__name__}",
            )
        ) from None
    except ValueError as error:
        raise StageError(
            StageDiagnostic(
                stage="parse_to_common_ir",
                unit=str(input_path),
                reason_code=PARSE_FAILED,
                message=str(error),
            )
        ) from None


def _subprocess_env() -> dict[str, str]:
    """Build a secret-free environment for the untrusted parser process."""

    allowed = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "LD_LIBRARY_PATH",
        "FONTCONFIG_PATH",
        "FONTCONFIG_FILE",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    entries = [str(path) for path in vendor.VENDOR_PATHS]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)

    # Some Linux images load a FreeType build through Pillow that is
    # incompatible with ``rhwp`` (``FT_Palette_Data_Get`` at import time).
    # Deployment may opt into the host-compatible library explicitly.  Keep
    # this trusted-process setting out of request/job payloads, require an
    # absolute existing file, and preserve any operator supplied preload list.
    freetype = os.environ.get("PREREVIEW_FREETYPE_LIB", "").strip()
    if freetype:
        library = Path(freetype)
        if not library.is_absolute() or not library.is_file():
            raise RuntimeError(
                "PREREVIEW_FREETYPE_LIB must name an existing absolute file"
            )
        preloads = [
            item for item in os.environ.get("LD_PRELOAD", "").split() if item
        ]
        if str(library) not in preloads:
            preloads.insert(0, str(library))
        env["LD_PRELOAD"] = " ".join(preloads)
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


def _request_type_option_blocks(pack) -> list[Any]:
    """Mirror the vendored resolver's option-container predicate.

    This is intentionally structural rather than exception-message based. It
    lets the worker report whether the form has no resolvable option container
    or more than one, while leaving the actual exact-span selection to the
    vendored resolver.
    """

    labels = tuple(_request_profile_contract.REQUEST_TYPE_LABELS.values())
    checked = tuple(_request_profile_contract.CHECKED_GLYPHS)
    return [
        block
        for block in pack.blocks
        if any(label in block.text for label in labels)
        and any(glyph in block.text for glyph in checked)
    ]


def _request_type_preflight_diagnostic(pack) -> StageDiagnostic | None:
    """Validate the server-owned request type before any LLM call."""

    option_blocks = _request_type_option_blocks(pack)
    if not option_blocks:
        return StageDiagnostic(
            stage="structure_request_profile",
            unit=pack.pack_id,
            reason_code=REQUEST_TYPE_CONTAINER_MISSING,
            message="request_type option container is missing",
            terminated_because=REQUEST_TYPE_CONTAINER_MISSING,
        )
    if len(option_blocks) > 1:
        return StageDiagnostic(
            stage="structure_request_profile",
            unit=pack.pack_id,
            reason_code=REQUEST_TYPE_CONTAINER_AMBIGUOUS,
            message="request_type option containers are ambiguous",
            terminated_because=REQUEST_TYPE_CONTAINER_AMBIGUOUS,
        )
    try:
        resolve_request_type_from_candidate_pack(pack)
    except ValueError:
        # Do not expose vendor exception details or infer a reason from their
        # wording. The one-container shape was found, but its checked option
        # cannot be resolved safely.
        return StageDiagnostic(
            stage="structure_request_profile",
            unit=pack.pack_id,
            reason_code=REQUEST_TYPE_SELECTION_INVALID,
            message="request_type checked option is invalid or unresolved",
            terminated_because=REQUEST_TYPE_SELECTION_INVALID,
        )
    return None


# ------------------------------------------------------------ 3. selector

# 패키지 ``_remote_selection`` 이 쓰는 것과 같은 수리 지시문. 문구를 새로
# 만들면 계약이 갈라지므로 그대로 복사해 둔다.
_REPAIR_INSTRUCTION = (
    " The prior parsed selection failed server exact-span/provenance validation. "
    "Return a complete replacement RequestSourceSelectionV012. Modify only invalid anchors or selection structure, "
    "preserve valid entries, and copy literal CandidatePack substrings exactly, including Markdown syntax such as ** when present."
)


def _selection_validation_messages(error: ValidationError) -> list[str]:
    """스키마 위반을 모델이 고칠 수 있는 형태로 옮긴다.

    **메시지에서 문자열을 지우지 않는다.** 이 값이 가는 곳은 다음 호출의
    ``server_validation_errors`` 하나뿐이고, 받는 쪽은 그 입력을 만든 모델
    자신이다. 로그·진단·사용자 응답 어디에도 닿지 않으므로 가릴 대상이 없다.

    지우면 오히려 힌트가 부서진다. 이전 구현은 raw 의 모든 문자열을 메시지에서
    치환했는데, 짧고 흔한 값 하나가 정당한 스키마 상수를 갉아먹었다. facts 에
    ``target`` 이 있으면 허용 목록의 ``support_target`` 이
    ``support_[redacted]`` 가 되어, "이 중에서 고르라" 는 안내가 고를 수 없는
    목록과 함께 나갔다. 보완 시도가 같은 진단을 반복한 원인이다.

    ``include_input=False`` 는 유지한다. 모델은 자기가 보낸 값을
    ``prior_selection`` 으로 그대로 다시 받으므로 여기서 되풀이할 필요가 없고,
    프롬프트만 커진다.
    """
    messages: list[str] = []
    for issue in error.errors(include_input=False, include_context=False):
        location = ".".join(str(part) for part in issue.get("loc", ()))
        message = str(issue.get("msg", "validation failed"))
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
    """Provider-neutral ``LLMClient``를 패키지 selector 시그니처로 어댑트한다.

    함수명은 vendored 초기 vLLM 실험과의 호환을 위해 남아 있다. 현재
    `worker.main`은 OpenAI adapter를 주입하며 vLLM endpoint를 구성하거나 호출하지
    않는다.

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
                repair_payload["server_validation_errors"] = (
                    _selection_validation_messages(validation_error)
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


# 어긋난 묶음만 덜어내고 나머지를 살릴 때, 덜어내도 되는 것들.
#
# **facts / support_components / program_hierarchy 는 넣지 않는다.** 그것이
# 프로필의 본체다. 사실이 없는 프로필은 부분 결과가 아니라 빈 결과이고,
# 그것을 정상으로 저장하는 것은 §11 이 금지한 바로 그 행위다.
#
# 여기 있는 둘은 수행체계(delivery) 축의 재료다. 빠지면 FIT·SIM 의 해당 축이
# 근거 없음으로 INSUFFICIENT 를 내고, 그것이 화면에 그대로 나간다 — 없는 것을
# 있다고 하지 않는다. 대신 CPL 13 개와 나머지 축은 살아남는다.
#
# 순서가 곧 시도 순서다. 관찰된 실패가 delivery_relations 에 몰려 있으므로
# 그것부터 덜어낸다.
_ISOLATABLE_GROUPS = ("delivery_relations", "delivery_methods")


def _materialize_isolated(
    error: RequestMaterializationError,
    document: dict[str, Any],
    pack,
    model_id: str,
) -> tuple[dict[str, Any], list[str], dict[str, int]] | None:
    """어긋난 묶음을 덜어내고 다시 재료화한다. 못 살리면 ``None``.

    재료화는 **순수 로컬 함수**다. LLM 도 네트워크도 타지 않으므로 이 재시도는
    공짜다. 보완 호출을 늘리는 것과는 성격이 다르다.

    팀원 검증은 하나도 완화하지 않는다. 같은 ``assemble_request_profile_v012``
    를 그대로 다시 부르고, 통과하는 부분만 남긴다.
    """
    baseline = error.selection.model_dump(mode="json")
    dropped: list[str] = []
    counts: dict[str, int] = {}
    for group in _ISOLATABLE_GROUPS:
        if not baseline.get(group):
            continue
        dropped.append(group)
        counts[group] = len(baseline[group])
        payload = {**baseline, **{name: [] for name in dropped}}
        try:
            trimmed = RequestSourceSelectionV012.model_validate(payload)
            profile = assemble_request_profile_v012(
                document,
                pack,
                trimmed,
                model_id=model_id,
                prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
            )
        except (ValidationError, ValueError):
            continue
        return profile, dropped, counts
    return None


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

    request_type_diagnostic = _request_type_preflight_diagnostic(pack)
    if request_type_diagnostic is not None:
        # Request type is server-owned deterministic input. Do not spend an
        # LLM call or let a vendor ValueError escape when this gate fails.
        return failed([request_type_diagnostic], 0, [])

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
        attempted = [
            StageDiagnostic(
                stage="structure_request_profile",
                unit=row.get("error_type"),
                reason_code=MATERIALIZATION_FAILED,
                message=str(row.get("validation_error"))[:2000],
                attempt=row.get("repair_attempt"),
            )
            for row in error.diagnostics
        ]
        # 보완을 다 써도 실패했다면, 어긋난 묶음만 덜어내고 나머지를 살려 본다.
        # 지금까지는 delivery relation 하나가 CPL·FIT 재료까지 함께 죽였다.
        isolated = _materialize_isolated(error, document, pack, model_id)
        if isolated is not None:
            profile, dropped, counts = isolated
            return ProfileSnapshot(
                profile_id=profile_id,
                status="OK",
                profile=profile,
                candidate_pack_id=pack.pack_id,
                common_ir=common_ir,
                model_id=model_id,
                prompt_version=REQUEST_SELECTION_PROMPT_VERSION,
                profile_contract_version=REQUEST_PIPELINE_VERSION,
                selection_attempts=error.retry_count + 1,
                usage=list(error.usage),
                # 무엇을 덜어냈는지가 여기 남지 않으면 "조용히 버리기" 가 된다.
                diagnostics=attempted
                + [
                    StageDiagnostic(
                        stage="structure_request_profile",
                        unit=",".join(dropped),
                        reason_code=PARTIAL_MATERIALIZATION,
                        message=(
                            "근거 불일치로 제외한 묶음: "
                            + ", ".join(f"{name}={counts[name]}" for name in dropped)
                        ),
                        attempt=error.retry_count + 1,
                    )
                ],
            )
        return failed(
            attempted
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
