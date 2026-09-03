from typing import Protocol


class MailSender(Protocol):
    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        """Send a password-reset message containing ``reset_url``."""
