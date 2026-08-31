"""SMTP implementation of the password-reset mail port."""

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

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        for address in (to_email, self.from_email):
            if not address or any(
                character in address
                for character in ("\r", "\n", "\x00")
            ):
                raise ValueError("Email address contains unsafe characters")

        message = EmailMessage()
        message["Subject"] = "SIMS 비밀번호 재설정"
        message["From"] = self.from_email
        message["To"] = to_email
        message.set_content(
            "비밀번호를 재설정하려면 아래 링크를 여세요.\n\n"
            f"{reset_url}\n\n"
            "이 링크는 발송 후 10분 동안만 유효합니다.\n"
            "본인이 요청하지 않았다면 이 메일을 무시하세요."
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
