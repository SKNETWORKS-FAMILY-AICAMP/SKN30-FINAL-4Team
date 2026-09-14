"""워커가 쓰는 모델 제공자 접속 설정. 값은 전부 환경변수에서만 온다.

``.env`` 로딩은 프로세스 진입점(앱/CLI)의 책임이고 여기서는 하지 않는다.
비밀값에 기본값을 두지 않는다: 없으면 변수 이름을 밝히고 실패한다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os


class MissingConfigError(RuntimeError):
    pass


def _required(name: str, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    value = values.get(name)
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


def _optional(name: str, env: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if env is None else env
    value = values.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    """OpenAI provider settings for the worker process only.

    ``llm_model_profiles`` stays explicit because pipeline stages can choose a
    profile name while the deployment controls the underlying model ID.  A
    simple deployment can use ``{"default": config.llm_model}``.
    """

    api_key: str
    llm_model: str
    request_profile_model: str | None = None
    cpl_model: str | None = None
    fit_model: str | None = None
    sim_model: str | None = None
    chat_model: str | None = None
    timeout_seconds: float = 120.0
    # 보완 호출은 검증이 깨졌을 때만 나간다. 1 회는 근거 span 선택이 한 번
    # 어긋나면 그대로 실행 전체가 실패한다는 뜻이라, 같은 문서가 어떤 날은
    # 되고 어떤 날은 안 된다. 정상 실행의 비용은 그대로다.
    max_repairs: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OpenAIConfig":
        return cls(
            api_key=_required("OPENAI_API_KEY", env),
            llm_model=_required("OPENAI_LLM_MODEL", env),
            request_profile_model=_optional("OPENAI_REQUEST_PROFILE_MODEL", env),
            cpl_model=_optional("OPENAI_CPL_MODEL", env),
            fit_model=_optional("OPENAI_FIT_MODEL", env),
            sim_model=_optional("OPENAI_SIM_MODEL", env),
            chat_model=_optional("OPENAI_CHAT_MODEL", env),
            timeout_seconds=_float_from_env("OPENAI_TIMEOUT_SECONDS", 120.0, env),
            max_repairs=_nonnegative_int_from_env(
                "OPENAI_MAX_REPAIRS", 2, env
            ),
        )

    def llm_model_profiles(self, *names: str) -> dict[str, str]:
        """Map selected stage profile names to an override or common fallback.

        ``OPENAI_LLM_MODEL`` remains the fallback for every stage.  The four
        named worker stages may each select a different deployment-owned model
        ID without changing callers that use the fallback.
        """

        requested = names or ("default",)
        if any(not name.strip() for name in requested):
            raise ValueError("OpenAI model profile names must not be blank")
        return {name: self.llm_model_for(name) for name in requested}

    def llm_model_for(self, name: str) -> str:
        """Return the resolved model ID for one named LLM stage."""

        if not name.strip():
            raise ValueError("OpenAI model profile names must not be blank")
        overrides = {
            "request_profile": self.request_profile_model,
            "cpl": self.cpl_model,
            "fit": self.fit_model,
            "sim": self.sim_model,
            "chat": self.chat_model,
        }
        return overrides.get(name) or self.llm_model


@dataclass(frozen=True, slots=True)
class OpenAIEmbeddingConfig:
    """OpenAI embedding settings, deliberately independent of the LLM provider.

    Retrieval embeddings are persisted with their model provenance.  Selecting
    a self-hosted chat-completions provider must therefore not implicitly
    replace this boundary or require an unrelated OpenAI LLM model setting.
    """

    api_key: str
    embedding_model: str
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "OpenAIEmbeddingConfig":
        return cls(
            api_key=_required("OPENAI_API_KEY", env),
            embedding_model=_required("OPENAI_EMBEDDING_MODEL", env),
            timeout_seconds=_float_from_env("OPENAI_TIMEOUT_SECONDS", 120.0, env),
        )


def _int_from_env(name: str, default: int, env: Mapping[str, str] | None) -> int:
    if env is None:
        return _int(name, default)
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise MissingConfigError(
            f"environment variable must be an integer: {name}"
        ) from None


def _nonnegative_int_from_env(
    name: str, default: int, env: Mapping[str, str] | None
) -> int:
    value = _int_from_env(name, default, env)
    if value < 0:
        raise MissingConfigError(
            f"environment variable must be non-negative: {name}"
        )
    return value


def _float_from_env(
    name: str, default: float, env: Mapping[str, str] | None
) -> float:
    if env is None:
        return _float(name, default)
    raw = env.get(name)
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
