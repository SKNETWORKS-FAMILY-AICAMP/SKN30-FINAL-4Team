import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatMessageRequest


@pytest.mark.parametrize("content", [" ", "\t", "\n", " \t\n "])
def test_chat_message_rejects_whitespace_only_content(content: str) -> None:
    with pytest.raises(ValidationError):
        ChatMessageRequest(content=content)


def test_chat_message_keeps_nonblank_content() -> None:
    assert ChatMessageRequest(content="  사업 목적은 무엇인가요?  ").content == (
        "  사업 목적은 무엇인가요?  "
    )
