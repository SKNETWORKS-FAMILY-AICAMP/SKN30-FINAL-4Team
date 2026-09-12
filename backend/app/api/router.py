from fastapi import APIRouter

from .v1.auth import router as auth_router
from .v1.analysis_runs import router as analysis_runs_router
from .v1.conversations import router as conversations_router
from .v1.results import router as results_router

router = APIRouter(prefix="/api/v1")
router.include_router(auth_router)
router.include_router(analysis_runs_router)
router.include_router(results_router)
router.include_router(conversations_router)
