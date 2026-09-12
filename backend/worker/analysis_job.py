"""Real DB-polling analysis job orchestration.

The handler is worker-local: it accepts no browser token and imports no
FastAPI application code.  Network, database, parsing, and comparison edges
are explicit ports, allowing the same orchestration to run in an offline fake
E2E test and in the trusted worker process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Protocol

from .contracts.cpl_result import CplResult
from .contracts.fit_result import FitResult
from .contracts.ml_result import MlModelId
from .contracts.profile_snapshot import CommonIrArtifact
from .contracts.sim_result import SimComparisonResult, SimCommonProfile
from .cpl import analyze_cpl
from .fit import analyze_fit
from .ml_reference import MlModel, run_ml_reference
from .ports.embedding import EmbeddingClient
from .ports.llm import LLMClient
from .profiles import (
    DEFAULT_PARSE_TIMEOUT_SECONDS,
    build_pack,
    make_vllm_selector,
    parse_to_common_ir,
    structure_request_profile,
)
from .result_payload import build_result_payload
from .retrieval_inputs import assemble_inputs, pool
from .runtime import ClaimedJob
from .sim import compare_candidates
from .sim_inputs import build_common_profile


SOURCE_MAX_BYTES = 50 * 1024 * 1024
DERIVED_MAX_BYTES = 200 * 1024 * 1024
REQUEST_PROFILE_SCHEMA = "pre_review_request_profile/v0.1"
EMBEDDING_ASSEMBLY_VERSION = "approved-facts-role-aware-v1"

StageCallback = Callable[[str, str, str | None], None]


def _notify_stage(
    callback: StageCallback | None,
    stage: str,
    status: str,
    detail: str | None = None,
) -> None:
    if callback is None:
        return
    try:
        callback(stage, status, detail)
    except Exception:
        # Progress reporting must never change the worker result.
        return


@contextmanager
def _stage(callback: StageCallback | None, name: str) -> Iterator[None]:
    _notify_stage(callback, name, "started")
    try:
        yield
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"[:240]
        _notify_stage(callback, name, "failed", detail)
        raise
    else:
        _notify_stage(callback, name, "succeeded")


class AnalysisJobContractError(RuntimeError):
    """A claimed job or persisted artifact violates the trusted contract."""


class AnalysisJobUnavailable(RuntimeError):
    """A worker dependency is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    bucket: str
    object_key: str
    content_sha256: str
    artifact_type: str
    mime_type: str
    size_bytes: int
    schema_version: str | None = None
    logical_id: str | None = None


@dataclass(frozen=True, slots=True)
class CachedRequestProfile:
    common_ir: ArtifactRef
    structured_profile: ArtifactRef


@dataclass(frozen=True, slots=True)
class ProducedRequestProfile:
    common_ir: Mapping[str, Any]
    profile: Mapping[str, Any]
    common_ir_content: bytes
    profile_content: bytes
    common_ir_logical_id: str
    profile_logical_id: str
    common_ir_schema: str
    profile_schema: str
    # 그 실행이 실제로 쓴 CandidatePack. 정량 근거의 source_block_id 와 좌표가
    # 가리키는 것이 이 팩이라, 아래 단계가 원문을 볼 때 다시 만들지 않는다.
    # 팩을 나르지 않는 생산자도 있으므로 없으면 정량 맥락 파생만 건너뛴다.
    candidate_pack: Any = None


@dataclass(frozen=True, slots=True)
class EmbeddingConfiguration:
    configuration_id: str
    provider: str
    model_id: str
    dimensions: int
    max_input_tokens: int
    assembly_version: str


@dataclass(frozen=True, slots=True)
class ExistingCandidate:
    profile_version_id: str
    source_profile_id: str
    notice_id: str
    average_similarity: float
    purpose_similarity: float
    target_similarity: float
    support_similarity: float
    profile_artifact: ArtifactRef
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ExistingProfileDocument:
    candidate: ExistingCandidate
    profile: Mapping[str, Any]


class WorkerObjectStorage(Protocol):
    def get(self, *, bucket: str, object_key: str, max_bytes: int) -> bytes: ...

    def put_if_absent(
        self,
        *,
        bucket: str,
        object_key: str,
        content: bytes,
        content_type: str,
    ) -> bool: ...

    def delete(self, *, bucket: str, object_key: str) -> None: ...


class AnalysisStore(Protocol):
    def cached_request_profile(self, *, analysis_run_id: str) -> CachedRequestProfile | None: ...

    def register_request_profile(
        self,
        *,
        analysis_run_id: str,
        processing_run_id: str,
        source_bucket: str,
        source_object_key: str,
        common_ir: ArtifactRef,
        structured_profile: ArtifactRef,
        profile: Mapping[str, Any],
    ) -> None: ...

    def active_embedding_configuration(self) -> EmbeddingConfiguration: ...

    def match_existing_profiles(
        self,
        *,
        configuration_id: str,
        purpose: Sequence[float],
        target: Sequence[float],
        support: Sequence[float],
        limit: int,
    ) -> list[ExistingCandidate]: ...


class RequestProfileProducer(Protocol):
    def produce(
        self,
        *,
        source_path: Path,
        source_kind: str,
        analysis_run_id: str,
        run_dir: Path,
    ) -> ProducedRequestProfile: ...


class AnalysisEngine(Protocol):
    def build_payload(
        self,
        *,
        profile: Mapping[str, Any],
        common_ir: Mapping[str, Any],
        candidates: Sequence[ExistingProfileDocument],
        candidate_pack: Any = None,
        quantity_hold_reason: str | None = None,
    ) -> Mapping[str, Any]: ...


class VendoredRequestProfileProducer:
    """Run the current HWP/HWPX → Common IR → Request Profile pipeline."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model_profile: str,
        model_id: str,
        max_repairs: int = 1,
        parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS,
        stage_callback: StageCallback | None = None,
        diagnostics_sink: DiagnosticsSink | None = None,
    ) -> None:
        self._llm = llm_client
        self._model_profile = model_profile
        self._model_id = model_id
        self._max_repairs = max_repairs
        self._stage_callback = stage_callback
        self._diagnostics_sink = diagnostics_sink
        if parse_timeout_seconds <= 0:
            raise ValueError("parse timeout must be positive")
        self._parse_timeout_seconds = float(parse_timeout_seconds)

    def set_stage_callback(self, callback: StageCallback | None) -> None:
        self._stage_callback = callback

    def set_diagnostics_sink(self, sink: DiagnosticsSink | None) -> None:
        self._diagnostics_sink = sink

    def produce(
        self,
        *,
        source_path: Path,
        source_kind: str,
        analysis_run_id: str,
        run_dir: Path,
    ) -> ProducedRequestProfile:
        with _stage(self._stage_callback, "parse"):
            common = parse_to_common_ir(
                input_path=source_path,
                notice_id=analysis_run_id,
                source_kind=source_kind,
                run_dir=run_dir,
                timeout_seconds=self._parse_timeout_seconds,
            )
        with _stage(self._stage_callback, "common_ir"):
            if not isinstance(common.document, Mapping):
                raise AnalysisJobContractError("Common IR document is not an object")
        with _stage(self._stage_callback, "structured_profile"):
            pack = build_pack(common.document)
            profile_id = f"request:{analysis_run_id}"
            selector = make_vllm_selector(
                self._llm,
                model_profile=self._model_profile,
                pack=pack,
                document=common.document,
                profile_id=profile_id,
            )
            snapshot = structure_request_profile(
                document=common.document,
                pack=pack,
                profile_id=profile_id,
                selector=selector,
                model_id=self._model_id,
                max_repairs=self._max_repairs,
                common_ir=common,
            )
            if snapshot.status != "OK" or not isinstance(snapshot.profile, dict):
                # 예외 메시지에는 reason code 만 실린다. 어떤 검증이 왜 깨졌는지는
                # ``snapshot.diagnostics`` 에만 있어서, 여기서 흘리면 실패한
                # 실행에서 그 이유가 어디에도 남지 않는다.
                if self._diagnostics_sink is not None and snapshot.diagnostics:
                    self._diagnostics_sink(
                        "structured_profile", list(snapshot.diagnostics)
                    )
                reasons = sorted(
                    {
                        row.reason_code
                        for row in snapshot.diagnostics
                        if row.reason_code
                    }
                )
                suffix = f" ({','.join(reasons)})" if reasons else ""
                raise AnalysisJobContractError(
                    f"request profile materialisation failed{suffix}"
                )
        return ProducedRequestProfile(
            common_ir=common.document,
            profile=snapshot.profile,
            common_ir_content=_json_bytes(common.document),
            profile_content=_json_bytes(snapshot.profile),
            common_ir_logical_id=common.common_ir_document_id,
            profile_logical_id=profile_id,
            common_ir_schema=str(common.document.get("schema_version") or "common_ir_v1"),
            profile_schema=str(snapshot.profile.get("schema_version") or ""),
            candidate_pack=pack,
        )


def _common_ir_artifact(common_ir: Mapping[str, Any]) -> CommonIrArtifact:
    """Adapt the persisted Common IR mapping to the ML boundary contract."""

    document = dict(common_ir)
    identity = document.get("document")
    identity = identity if isinstance(identity, Mapping) else {}
    provenance = identity.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    blocks = document.get("blocks")
    blocks = blocks if isinstance(blocks, list) else []
    return CommonIrArtifact(
        run_dir="",
        notice_id="",
        source_kind="",
        source_path="",
        source_sha256=str(provenance.get("source_sha256") or ""),
        common_ir_path="",
        common_ir_document_id=str(identity.get("document_id") or ""),
        manifest={},
        block_count=len(blocks),
        document=document,
    )


def _profile_title(profile: Mapping[str, Any]) -> str | None:
    identity = profile.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    for key in ("program_name", "title_raw"):
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            return value
    hierarchy = profile.get("program_hierarchy")
    hierarchy = hierarchy if isinstance(hierarchy, Mapping) else {}
    nodes = hierarchy.get("nodes")
    if isinstance(nodes, list):
        ordered = [
            *(
                node
                for node in nodes
                if isinstance(node, Mapping) and node.get("level") == "detail_program"
            ),
            *(node for node in nodes if isinstance(node, Mapping)),
        ]
        for node in ordered:
            value = node.get("name_raw")
            if isinstance(value, str) and value.strip():
                return value
    return None


# CPL 이 만든 진단을 밖으로 흘려보내는 자리. 공개 응답에는 싣지 않는다 —
# 프론트 계약을 늘리지 않으면서 로컬 기록기가 받아 적을 수 있게만 한다.
DiagnosticsSink = Callable[[str, Sequence[Any]], None]


class CoreAnalysisEngine:
    """Compose the current worker-owned CPL/FIT/SIM implementations."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        cpl_model_profile: str,
        fit_model_profile: str,
        sim_model_profile: str,
        max_repairs: int = 1,
        ml_models: Mapping[MlModelId, MlModel | None] | None = None,
        stage_callback: StageCallback | None = None,
        diagnostics_sink: DiagnosticsSink | None = None,
    ) -> None:
        self._llm = llm_client
        # CPL 의 의미 축 분류가 쓸 단계 프로필 이름이다. 지금 배포는 네 이름을
        # 모두 같은 모델에 매핑하므로 별도 모델이 아니라 논리 라우팅이다.
        # 단계별 모델이 실제로 필요해지면 그때 환경변수를 더한다.
        self._cpl_model_profile = cpl_model_profile
        self._fit_model_profile = fit_model_profile
        self._sim_model_profile = sim_model_profile
        self._max_repairs = max_repairs
        self._ml_models = dict(ml_models or {})
        self._stage_callback = stage_callback
        self._diagnostics_sink = diagnostics_sink

    def set_stage_callback(self, callback: StageCallback | None) -> None:
        self._stage_callback = callback

    def set_diagnostics_sink(self, sink: DiagnosticsSink | None) -> None:
        self._diagnostics_sink = sink

    def build_payload(
        self,
        *,
        profile: Mapping[str, Any],
        common_ir: Mapping[str, Any],
        candidates: Sequence[ExistingProfileDocument],
        candidate_pack: Any = None,
        quantity_hold_reason: str | None = None,
    ) -> Mapping[str, Any]:
        request = dict(profile)
        with _stage(self._stage_callback, "cpl"):
            cpl: CplResult = analyze_cpl(
                request,
                self._llm,
                model_profile=self._cpl_model_profile,
                common_ir=common_ir,
                candidate_pack=candidate_pack,
                quantity_hold_reason=quantity_hold_reason,
            )
        if self._diagnostics_sink is not None and cpl.diagnostics:
            # 재검 탈락 사유처럼 결과 payload 에 실리지 않는 기록이다. 어디에
            # 적을지는 받는 쪽이 정한다.
            self._diagnostics_sink("cpl", list(cpl.diagnostics))
        with _stage(self._stage_callback, "fit"):
            fit: FitResult = analyze_fit(
                cpl,
                self._llm,
                model_profile=self._fit_model_profile,
                max_repairs=self._max_repairs,
            )
        with _stage(self._stage_callback, "sim"):
            request_common = build_common_profile(
                request, self._llm, model_profile=self._sim_model_profile
            )
            candidate_commons: list[SimCommonProfile] = []
            sim_profiles: dict[str, SimCommonProfile] = {
                request_common.source_profile_id or "": request_common
            }
            titles: dict[str, str | None] = {}
            similarities: dict[str, float] = {}
            profile_version_ids: dict[str, str] = {}
            for document in candidates:
                candidate_common = build_common_profile(
                    dict(document.profile),
                    self._llm,
                    model_profile=self._sim_model_profile,
                )
                if not candidate_common.source_profile_id:
                    raise AnalysisJobContractError(
                        "existing profile has no source_profile_id"
                    )
                if candidate_common.source_profile_id != document.candidate.source_profile_id:
                    raise AnalysisJobContractError(
                        "existing profile identity does not match its DB lineage"
                    )
                candidate_commons.append(candidate_common)
                sim_profiles[candidate_common.source_profile_id] = candidate_common
                titles[candidate_common.source_profile_id] = document.candidate.title
                similarities[candidate_common.source_profile_id] = (
                    document.candidate.average_similarity
                )
                profile_version_ids[candidate_common.source_profile_id] = (
                    document.candidate.profile_version_id
                )

            sim: SimComparisonResult = compare_candidates(
                request_common,
                candidate_commons,
                self._llm,
                model_profile=self._sim_model_profile,
                max_repairs=self._max_repairs,
            )
            sim = replace(
                sim,
                candidates=[
                    replace(row, title=titles.get(row.candidate_profile_id or ""))
                    for row in sim.candidates
                ],
            )
        with _stage(self._stage_callback, "ml"):
            ml_result = run_ml_reference(
                request,
                self._ml_models,
                cpl_result=cpl,
                common_ir=_common_ir_artifact(common_ir),
                title=_profile_title(request),
            )
        return build_result_payload(
            profile=request,
            cpl=cpl,
            fit=fit,
            sim=sim,
            sim_profiles=sim_profiles,
            retrieval_similarities=similarities,
            profile_version_ids=profile_version_ids,
            ml_result=ml_result,
        )


class AnalysisJobHandler:
    """Handle one leased analysis run and return fenced-result JSON."""

    def __init__(
        self,
        *,
        storage: WorkerObjectStorage,
        store: AnalysisStore,
        producer: RequestProfileProducer,
        embedding_client: EmbeddingClient,
        analysis_engine: AnalysisEngine,
        top_k: int = 5,
        request_bucket: str = "request-temp",
        stage_callback: StageCallback | None = None,
    ) -> None:
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        self._storage = storage
        self._store = store
        self._producer = producer
        self._embedding = embedding_client
        self._engine = analysis_engine
        self._top_k = top_k
        self._request_bucket = request_bucket
        self._stage_callback = None
        self.set_stage_callback(stage_callback)

    def set_stage_callback(self, callback: StageCallback | None) -> None:
        """Attach optional operator progress reporting to this worker graph."""

        self._stage_callback = callback
        for component in (self._producer, self._engine):
            setter = getattr(component, "set_stage_callback", None)
            if callable(setter):
                setter(callback)

    def set_diagnostics_sink(self, sink: DiagnosticsSink | None) -> None:
        """Collect stage diagnostics from every component that offers them.

        구조화 실패 진단은 생산자에, CPL 진단은 엔진에 있다. 호출부가 둘을
        따로 찾아 꽂게 두면 한쪽을 빠뜨렸다는 사실이 드러나지 않는다.
        """

        for component in (self._producer, self._engine):
            setter = getattr(component, "set_diagnostics_sink", None)
            if callable(setter):
                setter(sink)

    def handle(self, job: ClaimedJob) -> Mapping[str, Any]:
        run_id = str(job.job_pk)
        processing_id = str(job.processing_run_pk)
        bucket, object_key, expected_sha, source_kind = self._source_claim(job)
        cached = self._store.cached_request_profile(analysis_run_id=run_id)
        if cached is None:
            source = self._storage.get(
                bucket=bucket, object_key=object_key, max_bytes=SOURCE_MAX_BYTES
            )
            _verify_content(source, expected_sha, "source")
            with tempfile.TemporaryDirectory(prefix="pre-review-worker-") as directory:
                root = Path(directory)
                source_path = root / f"source.{source_kind}"
                source_path.write_bytes(source)
                produced = self._producer.produce(
                    source_path=source_path,
                    source_kind=source_kind,
                    analysis_run_id=run_id,
                    run_dir=root / "pipeline",
                )
            quantity_hold_reason = None
            profile, common_ir, candidate_pack = self._publish_profile(
                run_id=run_id,
                processing_id=processing_id,
                source_bucket=bucket,
                source_object_key=object_key,
                produced=produced,
            )
        else:
            common_ir = self._load_json_artifact(cached.common_ir)
            profile = self._load_json_artifact(cached.structured_profile)
            candidate_pack, quantity_hold_reason = _resumed_candidate_pack(
                profile, common_ir
            )
        _validate_profile_lineage(
            profile=profile,
            common_ir=common_ir,
            run_id=run_id,
            source_sha256=expected_sha,
        )

        configuration = self._store.active_embedding_configuration()
        vectors = self._embed(profile, configuration)
        matches = self._store.match_existing_profiles(
            configuration_id=configuration.configuration_id,
            purpose=vectors["purpose"],
            target=vectors["target"],
            support=vectors["support"],
            limit=self._top_k,
        )
        candidates = [
            ExistingProfileDocument(
                candidate=match,
                profile=self._load_json_artifact(match.profile_artifact),
            )
            for match in matches
        ]
        result = self._engine.build_payload(
            profile=profile,
            common_ir=common_ir,
            candidates=candidates,
            candidate_pack=candidate_pack,
            quantity_hold_reason=quantity_hold_reason,
        )
        if not isinstance(result, Mapping):
            raise AnalysisJobContractError("analysis engine returned a non-object result")
        return dict(result)

    def _source_claim(self, job: ClaimedJob) -> tuple[str, str, str, str]:
        bucket = job.payload.get("source_bucket")
        key = job.payload.get("source_object_key")
        digest = job.payload.get("source_content_sha256")
        if not all(isinstance(value, str) and value.strip() for value in (bucket, key, digest)):
            raise AnalysisJobContractError("claim has incomplete source coordinates")
        assert isinstance(bucket, str) and isinstance(key, str) and isinstance(digest, str)
        if bucket != self._request_bucket:
            raise AnalysisJobContractError("claim source bucket is not allowed")
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise AnalysisJobContractError("claim source SHA-256 is invalid")
        suffix = Path(key).suffix.lower().removeprefix(".")
        if suffix not in {"hwp", "hwpx"}:
            raise AnalysisJobContractError("claim source format is not supported")
        return bucket, key, digest.lower(), suffix

    def _publish_profile(
        self,
        *,
        run_id: str,
        processing_id: str,
        source_bucket: str,
        source_object_key: str,
        produced: ProducedRequestProfile,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Any]:
        if produced.profile_schema != REQUEST_PROFILE_SCHEMA:
            raise AnalysisJobContractError("request profile schema is not supported by the DB")
        common_ref = _derived_ref(
            run_id, "common_ir", produced.common_ir_content,
            "application/json", produced.common_ir_schema, produced.common_ir_logical_id,
        )
        profile_ref = _derived_ref(
            run_id, "structured_profile", produced.profile_content,
            "application/json", produced.profile_schema, produced.profile_logical_id,
        )
        for artifact, content in (
            (common_ref, produced.common_ir_content),
            (profile_ref, produced.profile_content),
        ):
            self._storage.put_if_absent(
                bucket=artifact.bucket,
                object_key=artifact.object_key,
                content=content,
                content_type=artifact.mime_type,
            )

        # These keys are content-addressed and can be observed by a newer
        # processing attempt after this attempt loses its lease.  Deleting
        # them as compensation would race that newer attempt and could leave
        # its committed source_artifact rows dangling.  Registration is the
        # commit point; unreferenced objects are reclaimed by retention/GC.
        self._store.register_request_profile(
            analysis_run_id=run_id,
            processing_run_id=processing_id,
            source_bucket=source_bucket,
            source_object_key=source_object_key,
            common_ir=common_ref,
            structured_profile=profile_ref,
            profile=produced.profile,
        )
        return produced.profile, produced.common_ir, produced.candidate_pack

    def _load_json_artifact(self, artifact: ArtifactRef) -> Mapping[str, Any]:
        content = self._storage.get(
            bucket=artifact.bucket,
            object_key=artifact.object_key,
            max_bytes=DERIVED_MAX_BYTES,
        )
        _verify_content(content, artifact.content_sha256, artifact.artifact_type)
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AnalysisJobContractError("persisted JSON artifact is invalid") from None
        if not isinstance(value, dict):
            raise AnalysisJobContractError("persisted JSON artifact is not an object")
        return value

    def _embed(
        self, profile: Mapping[str, Any], configuration: EmbeddingConfiguration
    ) -> dict[str, list[float]]:
        if configuration.provider != "openai":
            raise AnalysisJobContractError("active embedding provider is unsupported")
        if configuration.assembly_version != EMBEDDING_ASSEMBLY_VERSION:
            raise AnalysisJobContractError(
                "active embedding assembly version is unsupported"
            )
        inputs = assemble_inputs(
            profile,
            model=configuration.model_id,
            max_input_tokens=configuration.max_input_tokens,
        )
        output: dict[str, list[float]] = {}
        for scope in ("purpose", "target", "support"):
            item = inputs[scope]
            batch = asyncio.run(self._embedding.embed([chunk.text for chunk in item.chunks]))
            if batch.model_name != configuration.model_id:
                raise AnalysisJobContractError("embedding response model does not match DB configuration")
            vector = pool(batch.vectors, [chunk.token_count for chunk in item.chunks])
            if len(vector) != configuration.dimensions or any(
                not math.isfinite(value) for value in vector
            ):
                raise AnalysisJobContractError("embedding dimension does not match DB configuration")
            output[scope] = vector
        return output


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _verify_content(content: bytes, expected: str, label: str) -> None:
    if sha256(content).hexdigest() != expected.lower():
        raise AnalysisJobContractError(f"{label} content hash does not match its lineage")


def _derived_ref(
    run_id: str,
    artifact_type: str,
    content: bytes,
    mime_type: str,
    schema_version: str,
    logical_id: str,
) -> ArtifactRef:
    digest = sha256(content).hexdigest()
    return ArtifactRef(
        bucket="request-temp",
        object_key=f"{run_id}/{artifact_type}/{digest}.json",
        content_sha256=digest,
        artifact_type=artifact_type,
        mime_type=mime_type,
        size_bytes=len(content),
        schema_version=schema_version,
        logical_id=logical_id,
    )


def _resumed_candidate_pack(
    profile: Mapping[str, Any], common_ir: Mapping[str, Any]
) -> tuple[Any | None, str | None]:
    """캐시로 이어받은 실행에서 쓸 CandidatePack 과, 못 쓸 때의 사유.

    그 실행이 쓴 팩은 저장되지 않는다. 저장된 Common IR 로 다시 만들되, **그때와
    같은 조건으로 만들어졌는지** 프로파일이 남긴 기록과 대조한다. IR 바이트
    동일성은 아티팩트 해시가 이미 보장하므로 남은 변수는 생성기뿐이다.

    common_ir_source_sha256 은 원본 HWP/HWPX 해시라 파싱 결과 동일성을
    말해 주지 않는다. 여기서 쓰지 않는다.

    값 span 후보 생성기(value_span_candidate_generator)는 요구하지 않는다.
    그것은 기간 후보 목록을 바꾸지만 블록 텍스트·좌표는 건드리지 않는다.

    같다고 해서 안전을 주장하지 않는다. 다르거나 기록이 없으면 멈춘다.
    """

    recorded = (profile.get("processing_metadata") or {}).get("candidate_pack")
    recorded = recorded if isinstance(recorded, Mapping) else {}
    generator = recorded.get("candidate_pack_generator")
    version = recorded.get("candidate_pack_generator_version")
    if not generator or not version:
        return None, (
            "저장된 프로파일에 CandidatePack 생성기 기록이 없어 정량 맥락을 "
            "파생하지 않았다"
        )
    try:
        pack = build_pack(common_ir)
    except Exception:
        return None, "저장된 Common IR 로 CandidatePack 을 만들지 못했다"
    if (pack.generator, pack.generator_version) != (generator, version):
        return None, (
            f"CandidatePack 생성기가 그 실행과 다르다 "
            f"(기록 {generator}/{version}, 현재 {pack.generator}/"
            f"{pack.generator_version})"
        )
    return pack, None


def _validate_profile_lineage(
    *,
    profile: Mapping[str, Any],
    common_ir: Mapping[str, Any],
    run_id: str,
    source_sha256: str,
) -> None:
    if common_ir.get("schema_version") != "common_ir_v1":
        raise AnalysisJobContractError("Common IR schema is unsupported")
    document = common_ir.get("document")
    if not isinstance(document, Mapping):
        raise AnalysisJobContractError("Common IR document identity is missing")
    document_id = document.get("document_id")
    provenance = document.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    if provenance.get("source_sha256") != source_sha256:
        raise AnalysisJobContractError("Common IR source hash does not match the claim")

    if profile.get("schema_version") != REQUEST_PROFILE_SCHEMA:
        raise AnalysisJobContractError("request profile schema is unsupported")
    expected_id = f"request:{run_id}"
    if profile.get("profile_id") != expected_id:
        raise AnalysisJobContractError("request profile identity does not match its analysis run")
    metadata = profile.get("processing_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if metadata.get("common_ir_document_id") != document_id:
        raise AnalysisJobContractError("request profile Common IR identity is inconsistent")
    if metadata.get("common_ir_source_sha256") != source_sha256:
        raise AnalysisJobContractError("request profile source hash is inconsistent")
