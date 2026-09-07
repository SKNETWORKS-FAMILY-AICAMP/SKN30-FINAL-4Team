"""워커가 쓰는 vLLM 접속 설정. 값은 전부 환경변수에서만 온다.

``.env`` 로딩은 프로세스 진입점(앱/CLI)의 책임이고 여기서는 하지 않는다.
비밀값에 기본값을 두지 않는다: 없으면 변수 이름을 밝히고 실패한다.
"""

from dataclasses import dataclass
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
