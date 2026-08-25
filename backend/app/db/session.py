from sqlalchemy import Engine, create_engine, text


def create_database_engine(database_url: str, connect_timeout_seconds: int) -> Engine:
    return create_engine(
        database_url,
        connect_args={"connect_timeout": connect_timeout_seconds},
        pool_pre_ping=True,
    )


def check_database_ready(engine: Engine) -> None:
    with engine.connect() as connection:
        app_user_table = connection.scalar(
            text("SELECT to_regclass('sims.app_user')")
        )

    if app_user_table is None:
        raise RuntimeError("SIMS v2.1 schema is not installed")
