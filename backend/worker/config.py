"""워커가 쓰는 모델 제공자 접속 설정. 값은 전부 환경변수에서만 온다.

``.env`` 로딩은 프로세스 진입점(앱/CLI)의 책임이고 여기서는 하지 않는다.
비밀값에 기본값을 두지 않는다: 없으면 변수 이름을 밝히고 실패한다.
"""

from dataclasses import dataclass
import math
import os


class MissingConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise MissingConfigError(f"required environment variable is not set: {name}")
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise MissingConfigError(
            f"environment variable must be an integer: {name}"
        ) from None


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            raise MissingConfigError(
                f"environment variable must be a number: {name}"
            ) from None
    if not math.isfinite(value) or value <= 0:
        raise MissingConfigError(
            f"environment variable must be a finite positive number: {name}"
        )
    return value


@dataclass(frozen=True, slots=True)
class VllmConfig:
    base_url: str
    api_key: str
    llm_model: str
    embedding_model: str
    timeout_seconds: float = 60.0
    max_repairs: int = 1

    @classmethod
    def from_env(cls) -> "VllmConfig":
        return cls(
            base_url=_required("VLLM_BASE_URL").rstrip("/"),
            api_key=_required("VLLM_API_KEY"),
            llm_model=_required("VLLM_LLM_MODEL"),
            embedding_model=_required("VLLM_EMBEDDING_MODEL"),
            timeout_seconds=float(_int("VLLM_TIMEOUT_SECONDS", 60)),
            max_repairs=_int("VLLM_MAX_REPAIRS", 1),
        )


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    """OpenAI provider settings for the worker process only.

    ``llm_model_profiles`` stays explicit because pipeline stages can choose a
    profile name while the deployment controls the underlying model ID.  A
    simple deployment can use ``{"default": config.llm_model}``.
    """

    api_key: str
    llm_model: str
    embedding_model: str
    timeout_seconds: float = 60.0
    max_repairs: int = 1

    @classmethod
    def from_env(cls) -> "OpenAIConfig":
        return cls(
            api_key=_required("OPENAI_API_KEY"),
            llm_model=_required("OPENAI_LLM_MODEL"),
            embedding_model=_required("OPENAI_EMBEDDING_MODEL"),
            timeout_seconds=_float("OPENAI_TIMEOUT_SECONDS", 60.0),
            max_repairs=_int("OPENAI_MAX_REPAIRS", 1),
        )

    def llm_model_profiles(self, *names: str) -> dict[str, str]:
        """Map selected stage profile names to the configured model ID.

        This is a deliberate deployment helper, not a hardcoded model policy.
        Callers with several models can pass their own mapping directly to the
        adapter instead.
        """

        requested = names or ("default",)
        if any(not name.strip() for name in requested):
            raise ValueError("OpenAI model profile names must not be blank")
        return {name: self.llm_model for name in requested}
