import pytest

from app.core.config import Settings


def settings(**values):
    return Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", jwt_secret="x" * 32, **values)


def test_blank_mail_settings_disable_mail():
    value = settings(smtp_host="", smtp_password="")
    assert value.smtp_host is None
    assert value.smtp_password is None


def test_password_reset_url_requires_trusted_frontend_origin():
    assert settings(password_reset_url="https://front.example/reset").password_reset_url == (
        "https://front.example/reset"
    )
    assert settings(password_reset_url="http://localhost:3000/reset").password_reset_url == (
        "http://localhost:3000/reset"
    )
    for value in (
        "http://front.example/reset",
        "https://front.example/reset?token=bad",
        "https://front.example/reset#token=bad",
        "https://user:pass@front.example/reset",
    ):
        with pytest.raises(ValueError):
            settings(password_reset_url=value)
