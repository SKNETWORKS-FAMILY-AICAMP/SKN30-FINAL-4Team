from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import router
from app.core.config import Settings
from app.db.session import create_database_engine


def create_app(database_url: str | None = None) -> FastAPI:
    provided_settings = (
        Settings(database_url=database_url) if database_url is not None else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = provided_settings or Settings()
        engine = create_database_engine(
            str(settings.database_url),
            settings.database_connect_timeout_seconds,
        )
        application.state.database_engine = engine
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title="SIMS Pre-review API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    return application


app = create_app()
