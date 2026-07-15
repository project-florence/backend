from fastapi import APIRouter
from src.api.bist import router as bist_router
from src.api.reports import router as reports_router
from src.api.simulations import router as simulations_router
from src.api.economy import router as economy_router
from src.api.ipo import router as ipo_router
from src.api.stats import router as stats_router
from src.api.auth import router as auth_router
from src.api.favorites import router as favorites_router
from src.api.fit import router as fit_router

router = APIRouter(prefix="/api/v1")
router.include_router(bist_router)
router.include_router(reports_router)
router.include_router(fit_router)
router.include_router(simulations_router)
router.include_router(economy_router)
router.include_router(ipo_router)
router.include_router(stats_router)
router.include_router(auth_router)
router.include_router(favorites_router)
