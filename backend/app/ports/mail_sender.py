from typing import Protocol


class MailSender(Protocol):
    def send_temporary_password(self, to_email: str, temporary_password: str) -> None:
        """Send a message containing the newly issued ``temporary_password``."""
