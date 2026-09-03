"""SMTP implementation of the temporary-password mail port."""

import smtplib
import ssl
from email.message import EmailMessage


class SmtpMailSender:
    _timeout_seconds = 10.0

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        from_email: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_email = from_email

    def send_temporary_password(self, to_email: str, temporary_password: str) -> None:
        for address in (to_email, self.from_email):
            if not address or any(
                character in address
                for character in ("\r", "\n", "\x00")
            ):
                raise ValueError("Email address contains unsafe characters")

        message = EmailMessage()
        message["Subject"] = "Pre-review 임시 비밀번호"
        message["From"] = self.from_email
        message["To"] = to_email
        # 이 메일이 나간 뒤에는 기존 비밀번호를 쓸 수 없다. "요청하지 않았으면
        # 무시하세요" 라고 안내하면 로그인이 안 되는 이유를 알 수 없게 된다.
        message.set_content(
            "임시 비밀번호를 발급했습니다. 아래 값으로 로그인해 주세요.\n\n"
            f"    {temporary_password}\n\n"
            "기존 비밀번호는 더 이상 사용할 수 없습니다.\n"
            "로그인한 뒤 비밀번호를 변경해 주세요.\n"
            "본인이 요청하지 않았다면 관리자에게 알려 주세요."
        )

        if self.username and self.password is None:
            raise ValueError("SMTP password is required with SMTP username")

        with smtplib.SMTP_SSL(
            self.host,
            self.port,
            timeout=self._timeout_seconds,
            context=ssl.create_default_context(),
        ) as smtp:
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)
