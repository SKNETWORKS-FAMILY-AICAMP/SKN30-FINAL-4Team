"""워커가 작업을 받기 전에 프롬프트를 한 번 읽어 본다는 것을 고정한다.

문서 단위 실패 처리(``PROMPT_UNAVAILABLE``)는 런타임 방어다. 그것만 두면 배포에서
경로를 잘못 잡았을 때 워커는 정상 기동하고 모든 문서가 축 없이 "완료" 로 끝난다.
화면에도 에러가 없어 트레이스를 열기 전에는 모른다. 설정 오류는 문서 하나의
문제가 아니라 배포 전체의 문제이므로 부팅에서 멈춘다.

내용은 검사하지 않는다. 특정 SHA 를 강제하면 환경별 override 가 막힌다. 실제 쓴
SHA 는 실행 결과에 기록하므로 재현성은 거기서 확보한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worker.cpl_prompt import (
    PURPOSE_AXIS_PROMPT_ENV,
    PURPOSE_AXIS_PROMPT_VERSION,
    RECHECK_PROMPT_VERSION,
    PromptUnavailableError,
    check_prompts_ready,
)


def test_the_repository_prompts_are_ready() -> None:
    prompts = check_prompts_ready()

    assert [row.version for row in prompts] == [
        PURPOSE_AXIS_PROMPT_VERSION,
        RECHECK_PROMPT_VERSION,
    ]
    # 읽은 내용의 해시를 낼 수 있어야 실행 기록이 버전 이름을 뒷받침한다.
    assert all(len(row.sha256) == 64 for row in prompts)
    assert all(row.text.strip() for row in prompts)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("empty.txt", b""),
        ("blank.txt", "   \n  ".encode()),
        ("utf16.txt", "축 분류".encode("utf-16")),
    ],
    ids=["빈 파일", "공백만", "UTF-8 아님"],
)
def test_an_unusable_prompt_file_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, data: bytes,
) -> None:
    path = tmp_path / name
    path.write_bytes(data)
    monkeypatch.setenv(PURPOSE_AXIS_PROMPT_ENV, str(path))

    with pytest.raises(PromptUnavailableError):
        check_prompts_ready()


def test_a_missing_path_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PURPOSE_AXIS_PROMPT_ENV, str(tmp_path / "없는파일.txt"))

    with pytest.raises(PromptUnavailableError):
        check_prompts_ready()


# 디렉터리를 가리키면 read_bytes 가 플랫폼마다 다르게 동작한다. 먼저 거른다.
def test_a_directory_is_not_a_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PURPOSE_AXIS_PROMPT_ENV, str(tmp_path))

    with pytest.raises(PromptUnavailableError, match="일반 파일"):
        check_prompts_ready()


# 내용을 특정 해시로 고정하지 않는다. 환경별로 다른 문구를 쓸 수 있어야 한다.
def test_a_different_but_usable_prompt_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "custom.txt"
    # 버전 선언은 있어야 한다. 이름이 같은 옛 파일이 조용히 끼는 것을 막는
    # 장치이지 내용을 고정하는 장치가 아니다.
    path.write_text(
        f"""PROMPT-VERSION: {PURPOSE_AXIS_PROMPT_VERSION}
이 배포만 쓰는 축 분류 문구""",
        encoding="utf-8",
    )
    monkeypatch.setenv(PURPOSE_AXIS_PROMPT_ENV, str(path))

    prompts = check_prompts_ready()

    assert prompts[0].path == str(path)
    assert prompts[0].sha256  # 실제 쓴 내용의 해시가 기록된다
