import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.auth import router as auth_router
from app.api.v1.analyze import router as analyze_router
from app.api.v1.cases import router as cases_router
from app.api.v1.chat import router as chat_router
from app.api.v1.report import router as report_router
from app.api.v1.password_reset import router as password_reset_router
from app.db.session import check_database_ready


logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(auth_router)
router.include_router(password_reset_router)
router.include_router(cases_router)
router.include_router(analyze_router)
router.include_router(report_router)
router.include_router(chat_router)


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    try:
        check_database_ready(request.app.state.database_engine)
    except Exception as error:
        logger.warning("Database readiness check failed: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    return JSONResponse(content={"status": "ready"})
