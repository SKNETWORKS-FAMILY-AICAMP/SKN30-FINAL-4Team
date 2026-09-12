"""CPL 의미 축 분류 프롬프트를 버전 파일에서 읽는다.

문구를 코드 상수로 두지 않는 이유는 배포 없이 바꾸기 위해서가 아니다. 오히려
그 반대다: 같은 버전 이름으로 내용이 바뀌면 같은 이름의 산출물이 서로 다른
분류를 담게 되어 재현성이 깨진다. 그래서 파일을 읽되 내용 해시를 함께 남기고,
문구를 고치면 다음 번호 파일을 새로 만들어 버전도 같이 올린다.

경로는 저장소 안의 버전 파일이 기본이다. ``CPL_PURPOSE_AXIS_PROMPT_PATH`` 와
``CPL_RECHECK_PROMPT_PATH`` 로
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


PURPOSE_AXIS_PROMPT_VERSION = "cpl-purpose-axis-v0.3"
PURPOSE_AXIS_PROMPT_ENV = "CPL_PURPOSE_AXIS_PROMPT_PATH"
# 재검은 구역 원문에서 값과 축을 함께 받는다. 축만 붙이는 1차 분류와 입력이
# 달라 프롬프트도 따로 둔다. v0.2 부터 목적과 수행관계를 타입별 섹션으로 한 번에
# 묻는다 — 문서당 재검 호출을 하나로 유지하기 위해서다. v0.3 은 문구가 아니라
# 자기 버전 선언 줄만 더했다. 그래도 모델이 받는 문자열이 달라지므로 같은 이름을
# 유지하지 않는다. v0.4 는 기대효과 섹션을 더했다 — 목적·수행관계 절 문구는 그대로
# 옮겼지만 전체 입력이 달라지므로 버전을 올린다.
RECHECK_PROMPT_VERSION = "cpl-recheck-v0.4"
RECHECK_PROMPT_ENV = "CPL_RECHECK_PROMPT_PATH"
# v0.1 은 목적만 묻고 응답 스키마도 달랐다. 새 요청에 그 파일을 끼우면 모델이
# 수행관계 섹션을 통째로 못 본 채 조용히 절반만 답한다. fallback 으로 쓰지 않고,
# 옛 변수만 설정된 배포는 원인이 보이는 설정 오류로 멈춘다.
_RETIRED_RECHECK_PROMPT_ENV = "CPL_PURPOSE_RECHECK_PROMPT_PATH"

# 프롬프트 파일이 자기 버전을 선언하는 줄.
_VERSION_MARKER = "PROMPT-VERSION"


def _prompt_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "prompts"


class PromptUnavailableError(RuntimeError):
    """프롬프트를 읽지 못했다. 기본값으로 대체하지 않는다."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """문구와 그 출처. ``sha256`` 은 버전 이름이 가리키는 실제 내용이다."""

    version: str
    text: str
    sha256: str
    path: str


def _load(version: str, env_name: str) -> Prompt:
    override = (os.environ.get(env_name) or "").strip()
    path = Path(override) if override else _prompt_dir() / f"{version}.txt"
    # 디렉터리나 장치 파일을 가리키면 read_bytes 가 OSError 로 새거나 플랫폼마다
    # 다르게 동작한다. 먼저 일반 파일인지 본다.
    if path.exists() and not path.is_file():
        raise PromptUnavailableError(f"프롬프트 경로가 일반 파일이 아니다: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PromptUnavailableError(
            f"프롬프트를 읽지 못했다: {path} ({type(error).__name__})"
        ) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PromptUnavailableError(f"프롬프트가 UTF-8 이 아니다: {path}") from error
    if not text.strip():
        raise PromptUnavailableError(f"프롬프트가 비어 있다: {path}")
    # 파일이 스스로 어느 버전인지 말하게 한다. 환경변수 이름은 버전을 바꿔도
    # 그대로라, 외부에서 옛 파일을 가리키면 기록에는 새 버전이 남고 모델은 옛
    # 지시를 받는다. 그 상태는 트레이스만 봐서는 드러나지 않는다.
    declared = f"{_VERSION_MARKER}: {version}"
    if declared not in text:
        raise PromptUnavailableError(
            f"프롬프트가 {version} 이라고 선언하지 않았다: {path}. "
            f"첫 줄에 '{declared}' 가 있어야 한다"
        )
    return Prompt(
        version=version,
        text=text,
        sha256=sha256(raw).hexdigest(),
        path=str(path),
    )


def load_purpose_axis_prompt() -> Prompt:
    """축 분류 프롬프트를 읽는다. 실패하면 예외를 던진다."""

    return _load(PURPOSE_AXIS_PROMPT_VERSION, PURPOSE_AXIS_PROMPT_ENV)


def load_recheck_prompt() -> Prompt:
    """재검 프롬프트를 읽는다. 실패하면 예외를 던진다."""

    retired = (os.environ.get(_RETIRED_RECHECK_PROMPT_ENV) or "").strip()
    if retired and not (os.environ.get(RECHECK_PROMPT_ENV) or "").strip():
        raise PromptUnavailableError(
            f"{_RETIRED_RECHECK_PROMPT_ENV} 는 더 쓰지 않는다. "
            f"{RECHECK_PROMPT_ENV} 로 {RECHECK_PROMPT_VERSION} 경로를 지정한다"
        )
    return _load(RECHECK_PROMPT_VERSION, RECHECK_PROMPT_ENV)


def check_prompts_ready() -> list[Prompt]:
    """워커가 작업을 받기 전에 프롬프트를 한 번 읽어 본다.

    문서 단위 실패 처리(``PROMPT_UNAVAILABLE``)는 런타임 방어다. 그것만 두면
    배포에서 경로를 잘못 잡았을 때 워커는 정상 기동하고 모든 문서가 축 없이
    "완료" 로 끝난다. 화면에도 에러가 없어 트레이스를 열기 전에는 모른다.

    설정 오류는 문서 하나의 문제가 아니라 배포 전체의 문제이므로 부팅에서
    멈춘다. 내용은 검사하지 않는다 — 특정 SHA 를 강제하면 환경별 override 가
    막힌다. 실제 쓴 SHA 는 실행 결과에 기록하므로 재현성은 거기서 확보한다.
    """

    return [load_purpose_axis_prompt(), load_recheck_prompt()]


__all__ = [
    "PURPOSE_AXIS_PROMPT_VERSION",
    "PURPOSE_AXIS_PROMPT_ENV",
    "Prompt",
    "PromptUnavailableError",
    "RECHECK_PROMPT_VERSION",
    "RECHECK_PROMPT_ENV",
    "check_prompts_ready",
    "load_purpose_axis_prompt",
    "load_recheck_prompt",
]
