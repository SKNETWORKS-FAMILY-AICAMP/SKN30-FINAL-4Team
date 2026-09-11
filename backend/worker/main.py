"""Production composition and CLI entry point for the polling worker.

This module is deliberately the only place that turns deployment environment
variables into concrete worker adapters. The pipeline itself is kept free of
FastAPI, browser credentials, Redis/RQ, and Edge Function callback concerns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import shutil
import socket
import sys
from uuid import uuid4

from dotenv import load_dotenv

from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.adapters.ml_subprocess import (
    Model1SubprocessMlModel,
    Model2SubprocessMlModel,
    Model3SubprocessMlModel,
    model1_command,
    model2_command,
    model3_command,
)
from worker.analysis_job import (
    AnalysisJobHandler,
    CoreAnalysisEngine,
    VendoredRequestProfileProducer,
)
from worker.config import MissingConfigError, OpenAIConfig
from worker.cpl_prompt import check_prompts_ready
from worker.postgres_analysis_store import PostgresAnalysisStore
from worker.postgres_repository import PostgresJobRepository
from worker.runtime import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_IDLE_POLL_SECONDS,
    DEFAULT_LEASE_SECONDS,
    JobHandler,
    JobRepository,
    WorkerRuntime,
)
from worker.profiles import DEFAULT_PARSE_TIMEOUT_SECONDS
from worker.supabase_storage import SupabaseWorkerStorage
from worker.contracts.ml_result import MlModelId
from worker.ml_reference import (
    MlModel,
    missing_artifact_model,
    missing_runtime_model,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_ML_ROOT = Path(__file__).resolve().parents[2] / "ml"
DEFAULT_ML_TIMEOUT_SECONDS = 180.0


class WorkerConfigurationError(RuntimeError):
    """A missing or invalid deployment setting, never containing its value."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Non-secret worker process settings safe to log during operations."""

    database_url: str
    supabase_url: str
    service_role_key: str
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS
    top_k: int = 5
    storage_timeout_seconds: float = 30.0
    database_connect_timeout_seconds: int = 10
    parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS
    ml_root: Path = DEFAULT_ML_ROOT
    model1_serving_dir: Path | None = None
    ml_python_executable: str | None = None
    ml_timeout_seconds: float = DEFAULT_ML_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkerSettings":
        values = os.environ if env is None else env
        database_url = _first_required(values, "DATABASE_URL", "SUPABASE_DB_URL")
        service_role_key = _first_required(
            values, "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"
        )
        heartbeat_seconds = _positive_float(
            values,
            "PREREVIEW_WORKER_HEARTBEAT_SECONDS",
            DEFAULT_HEARTBEAT_SECONDS,
        )
        lease_seconds = _positive_int(
            values, "PREREVIEW_WORKER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS
        )
        if not 30 <= lease_seconds <= 3600:
            raise WorkerConfigurationError(
                "PREREVIEW_WORKER_LEASE_SECONDS must be between 30 and 3600"
            )
        if heartbeat_seconds >= lease_seconds:
            raise WorkerConfigurationError(
                "PREREVIEW_WORKER_HEARTBEAT_SECONDS must be shorter than "
                "PREREVIEW_WORKER_LEASE_SECONDS"
            )
        top_k = _positive_int(values, "PREREVIEW_WORKER_TOP_K", 5)
        if top_k > 100:
            raise WorkerConfigurationError("PREREVIEW_WORKER_TOP_K must be at most 100")
        ml_root = _optional_path(values, "PREREVIEW_ML_ROOT") or DEFAULT_ML_ROOT
        model1_serving_dir = _optional_path(values, "PREREVIEW_MODEL1_SERVING_DIR")
        ml_python_executable = _optional_string(values, "PREREVIEW_ML_PYTHON_EXECUTABLE")
        return cls(
            database_url=database_url,
            supabase_url=_required(values, "SUPABASE_URL").rstrip("/"),
            service_role_key=service_role_key,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
            idle_poll_seconds=_positive_float(
                values,
                "PREREVIEW_WORKER_IDLE_POLL_SECONDS",
                DEFAULT_IDLE_POLL_SECONDS,
            ),
            top_k=top_k,
            storage_timeout_seconds=_positive_float(
                values, "PREREVIEW_WORKER_STORAGE_TIMEOUT_SECONDS", 30.0
            ),
            database_connect_timeout_seconds=_positive_int(
                values, "PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS", 10
            ),
            parse_timeout_seconds=_positive_float(
                values,
                "PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS",
                DEFAULT_PARSE_TIMEOUT_SECONDS,
            ),
            ml_root=ml_root,
            model1_serving_dir=model1_serving_dir,
            ml_python_executable=ml_python_executable,
            ml_timeout_seconds=_positive_float(
                values,
                "PREREVIEW_ML_TIMEOUT_SECONDS",
                DEFAULT_ML_TIMEOUT_SECONDS,
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerComposition:
    """The assembled trusted boundaries for one worker process."""

    repository: JobRepository
    handler: JobHandler
    settings: WorkerSettings


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise WorkerConfigurationError(f"required environment variable is not set: {name}")
    return value


def _first_required(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    raise WorkerConfigurationError(
        "required environment variable is not set: " + " or ".join(names)
    )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise WorkerConfigurationError(f"environment variable must be an integer: {name}") from None
    if value <= 0:
        raise WorkerConfigurationError(f"environment variable must be positive: {name}")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise WorkerConfigurationError(f"environment variable must be a number: {name}") from None
    if not math.isfinite(value) or value <= 0:
        raise WorkerConfigurationError(f"environment variable must be finite and positive: {name}")
    return value


def _optional_string(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = _optional_string(env, name)
    return None if value is None else Path(value).expanduser()


def _first_missing(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if not path.is_file():
            return path
    return None


def _model1_root(configured: Path | None) -> Path | None:
    """Accept either a mounted ``model1`` directory or its serving parent."""

    if configured is None:
        return None
    if (configured / "inference.py").is_file():
        return configured
    nested = configured / "model1"
    if (nested / "inference.py").is_file():
        return nested
    return configured


def _ml_executable(settings: WorkerSettings) -> tuple[str | None, str | None]:
    """Return the child interpreter, or a safe unavailable detail."""

    executable = settings.ml_python_executable or sys.executable
    if settings.ml_python_executable is not None:
        candidate = Path(executable)
        if not candidate.is_file() and shutil.which(executable) is None:
            return None, executable
    return executable, None


def _build_ml_models(settings: WorkerSettings) -> dict[MlModelId, MlModel]:
    """Compose all three isolated ML ports without making ML mandatory.

    The worker remains bootable when a mounted source tree, artifact, or ML
    interpreter is absent.  In those cases an ``UnavailableModel`` preserves
    the reason for the per-model result instead of failing worker startup.
    """

    model_ids = (
        MlModelId.MODEL_1_SUPPORT_TYPE,
        MlModelId.MODEL_2_AMOUNT,
        MlModelId.MODEL_3_ANOMALY,
    )
    executable, runtime_detail = _ml_executable(settings)
    if runtime_detail is not None:
        return {
            model_id: missing_runtime_model(model_id, runtime_detail)
            for model_id in model_ids
        }

    assert executable is not None
    environment = {"PREREVIEW_ML_ROOT": str(settings.ml_root)}
    if settings.model1_serving_dir is not None:
        environment["PREREVIEW_MODEL1_SERVING_DIR"] = str(settings.model1_serving_dir)

    models: dict[MlModelId, MlModel] = {}
    model1_id = MlModelId.MODEL_1_SUPPORT_TYPE
    model1_root = _model1_root(settings.model1_serving_dir)
    if model1_root is None:
        models[model1_id] = missing_artifact_model(
            model1_id, "PREREVIEW_MODEL1_SERVING_DIR"
        )
    else:
        missing = _first_missing(
            (
                model1_root / "inference.py",
                model1_root / "model" / "model.safetensors",
                model1_root / "label_mapping.json",
                settings.ml_root / "pipelines" / "model1" / "dl07_m1_apply.py",
                settings.ml_root / "pipelines" / "model1" / "dl07_m1_apply.py",
            )
        )
        if missing is None and not (model1_root / "tokenizer").is_dir():
            missing = model1_root / "tokenizer"
        if missing is not None:
            models[model1_id] = missing_artifact_model(model1_id, missing)
        else:
            models[model1_id] = Model1SubprocessMlModel(
                model1_command(python_executable=executable),
                timeout_seconds=settings.ml_timeout_seconds,
                environment=environment,
                artifact_version="model1-external-serving-v1",
            )

    model2_id = MlModelId.MODEL_2_AMOUNT
    model2_root = settings.ml_root / "serving" / "model2"
    model2_missing = _first_missing(
        (
            model2_root / "predict.py",
            model2_root / "feature_builder.py",
            model2_root / "preprocessing.py",
            model2_root / "proximity.py",
            model2_root / "router.py",
            model2_root / "masking.py",
            model2_root / "cohort_reference.parquet",
            settings.ml_root / "serving" / "shared" / "preconsultation_adapter.py",
            settings.ml_root
            / "serving"
            / "shared"
            / "preconsultation_adapter.py",
            settings.ml_root / "models" / "model2_canonical" / "model2_p3_bundle.joblib",
        )
    )
    if model2_missing is not None:
        models[model2_id] = missing_artifact_model(model2_id, model2_missing)
    else:
        models[model2_id] = Model2SubprocessMlModel(
            model2_command(python_executable=executable),
            timeout_seconds=settings.ml_timeout_seconds,
            environment=environment,
            artifact_version="model2-p3-v1",
        )

    model3_id = MlModelId.MODEL_3_ANOMALY
    model3_root = settings.ml_root / "serving" / "model3"
    model3_missing = _first_missing(
        (
            model3_root / "score.py",
            model3_root / "inference.py",
            model3_root / "design_features_v3.parquet",
            settings.ml_root / "serving" / "shared" / "preconsultation_adapter.py",
        )
    )
    if model3_missing is not None:
        models[model3_id] = missing_artifact_model(model3_id, model3_missing)
    else:
        models[model3_id] = Model3SubprocessMlModel(
            model3_command(python_executable=executable),
            timeout_seconds=settings.ml_timeout_seconds,
            environment=environment,
            artifact_version="model3-design-v3",
        )

    return models


def build_worker() -> WorkerComposition:
    """Build the PostgreSQL-polling HWP/HWPX analysis worker without I/O."""

    settings = WorkerSettings.from_env()
    openai = OpenAIConfig.from_env()
    # 설정이 잘못됐으면 작업을 받기 전에 멈춘다. 문서마다 축을 조용히 비우는
    # 것보다 기동에 실패하는 편이 낫다.
    check_prompts_ready()
    llm = OpenAILLMClient(
        api_key=openai.api_key,
        model_profiles=openai.llm_model_profiles(
            "request_profile", "cpl", "fit", "sim"
        ),
        timeout_seconds=openai.timeout_seconds,
    )
    embedding = OpenAIEmbeddingClient(
        api_key=openai.api_key,
        model_name=openai.embedding_model,
        timeout_seconds=openai.timeout_seconds,
    )
    handler = AnalysisJobHandler(
        storage=SupabaseWorkerStorage(
            supabase_url=settings.supabase_url,
            service_role_key=settings.service_role_key,
            timeout_seconds=settings.storage_timeout_seconds,
        ),
        store=PostgresAnalysisStore(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        producer=VendoredRequestProfileProducer(
            llm,
            model_profile="request_profile",
            model_id=openai.llm_model,
            max_repairs=openai.max_repairs,
            parse_timeout_seconds=settings.parse_timeout_seconds,
        ),
        embedding_client=embedding,
        analysis_engine=CoreAnalysisEngine(
            llm,
            cpl_model_profile="cpl",
            fit_model_profile="fit",
            sim_model_profile="sim",
            max_repairs=openai.max_repairs,
            ml_models=_build_ml_models(settings),
        ),
        top_k=settings.top_k,
    )
    return WorkerComposition(
        repository=PostgresJobRepository(
            settings.database_url,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        ),
        handler=handler,
        settings=settings,
    )


def make_worker_id() -> str:
    """Return a process-unique, operator-readable worker identity."""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


def run_worker(
    repository: JobRepository,
    handler: JobHandler,
    *,
    worker_id: str | None = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    idle_poll_seconds: float = DEFAULT_IDLE_POLL_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
    """Run one synchronous worker process until SIGINT or SIGTERM."""

    runtime = WorkerRuntime(
        repository,
        handler,
        worker_id=worker_id or make_worker_id(),
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
        idle_poll_seconds=idle_poll_seconds,
        logger=logger,
    )
    with runtime.install_signal_handlers():
        runtime.run_forever()


def main() -> int:
    """Run the worker without ever printing deployment secrets."""

    load_dotenv(override=False)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    try:
        composition = build_worker()
    except (WorkerConfigurationError, MissingConfigError) as error:
        LOGGER.error("worker configuration is invalid: %s", error)
        return 2

    LOGGER.info(
        "starting PostgreSQL polling worker heartbeat_seconds=%s lease_seconds=%s "
        "idle_poll_seconds=%s top_k=%s",
        composition.settings.heartbeat_seconds,
        composition.settings.lease_seconds,
        composition.settings.idle_poll_seconds,
        composition.settings.top_k,
    )
    run_worker(
        composition.repository,
        composition.handler,
        heartbeat_seconds=composition.settings.heartbeat_seconds,
        lease_seconds=composition.settings.lease_seconds,
        idle_poll_seconds=composition.settings.idle_poll_seconds,
        logger=LOGGER,
    )
    return 0


__all__ = [
    "WorkerComposition",
    "WorkerConfigurationError",
    "WorkerSettings",
    "build_worker",
    "main",
    "make_worker_id",
    "run_worker",
]


if __name__ == "__main__":  # pragma: no cover - exercised by container command
    sys.exit(main())
