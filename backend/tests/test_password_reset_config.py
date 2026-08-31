import pytest
from pydantic import ValidationError

from app.core.config import Settings


def settings(**values):
    return Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", jwt_secret="x" * 32, **values)


@pytest.mark.parametrize("url", ["https://front.example/reset", "http://localhost:3000/reset", "http://127.0.0.1:3000/reset"])
def test_reset_url_accepts_trusted_frontend(url):
    assert settings(password_reset_url=url).password_reset_url == url


@pytest.mark.parametrize("url", ["http://front.example/reset", "https://user:pass@front.example/reset", "https://front.example/reset?token=x", "https://front.example/reset#x", "//front.example/reset", "https://front.example/\nreset", "https://front.example:bad/reset"])
def test_reset_url_rejects_unsafe_targets(url):
    with pytest.raises(ValidationError):
        settings(password_reset_url=url)


def test_blank_mail_settings_disable_mail():
    value = settings(smtp_host="", smtp_password="", password_reset_url="")
    assert value.smtp_host is None
    assert value.smtp_password is None
    assert value.password_reset_url is None
