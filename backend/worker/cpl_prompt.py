"""CPL 의미 축 분류 프롬프트를 버전 파일에서 읽는다.

문구를 코드 상수로 두지 않는 이유는 배포 없이 바꾸기 위해서가 아니다. 오히려
그 반대다: 같은 버전 이름으로 내용이 바뀌면 같은 이름의 산출물이 서로 다른
분류를 담게 되어 재현성이 깨진다. 그래서 파일을 읽되 내용 해시를 함께 남기고,
문구를 고치면 ``v0.3`` 파일을 새로 만들어 버전도 같이 올린다.

경로는 저장소 안의 버전 파일이 기본이다. ``CPL_PURPOSE_AXIS_PROMPT_PATH`` 로
덮을 수 있지만, 없거나 비었거나 UTF-8 이 아니면 조용히 기본값으로 돌아가지
않고 즉시 실패한다. 프롬프트를 못 읽은 채 낸 분류는 버전을 신뢰할 수 없다.

옛 ``CPL_PROMPT_PATH`` 는 읽지 않는다. 이름이 CPL 전체를 가리키는데 실제로는
어떤 코드도 쓰지 않는 잔재이고, 이 축 프롬프트와 범위가 다르다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path


PURPOSE_AXIS_PROMPT_VERSION = "cpl-purpose-axis-v0.2"
PURPOSE_AXIS_PROMPT_ENV = "CPL_PURPOSE_AXIS_PROMPT_PATH"

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "prompts"
    / f"{PURPOSE_AXIS_PROMPT_VERSION}.txt"
)


class PromptUnavailableError(RuntimeError):
    """프롬프트를 읽지 못했다. 기본값으로 대체하지 않는다."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """문구와 그 출처. ``sha256`` 은 버전 이름이 가리키는 실제 내용이다."""

    version: str
    text: str
    sha256: str
    path: str


def load_purpose_axis_prompt() -> Prompt:
    """축 분류 프롬프트를 읽는다. 실패하면 예외를 던진다."""

    override = (os.environ.get(PURPOSE_AXIS_PROMPT_ENV) or "").strip()
    path = Path(override) if override else _DEFAULT_PATH
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PromptUnavailableError(
            f"축 프롬프트를 읽지 못했다: {path} ({type(error).__name__})"
        ) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PromptUnavailableError(f"축 프롬프트가 UTF-8 이 아니다: {path}") from error
    if not text.strip():
        raise PromptUnavailableError(f"축 프롬프트가 비어 있다: {path}")
    return Prompt(
        version=PURPOSE_AXIS_PROMPT_VERSION,
        text=text,
        sha256=sha256(raw).hexdigest(),
        path=str(path),
    )


__all__ = [
    "PURPOSE_AXIS_PROMPT_VERSION",
    "PURPOSE_AXIS_PROMPT_ENV",
    "Prompt",
    "PromptUnavailableError",
    "load_purpose_axis_prompt",
]
