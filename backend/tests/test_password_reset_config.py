from app.core.config import Settings


def settings(**values):
    return Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", jwt_secret="x" * 32, **values)


def test_blank_mail_settings_disable_mail():
    value = settings(smtp_host="", smtp_password="")
    assert value.smtp_host is None
    assert value.smtp_password is None
