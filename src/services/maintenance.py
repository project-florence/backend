import logging

from fastapi import HTTPException

from src.core.redis import r

logger = logging.getLogger(__name__)

_REDIS_KEY = "maintenance:disabled"
_FEATURES = {
    "report_generate",
    "simulation",
    "news",
    "advisor",
}


async def is_disabled(feature: str) -> bool:
    return bool(await r.sismember(_REDIS_KEY, feature))


async def list_disabled() -> list[str]:
    members = await r.smembers(_REDIS_KEY)
    return list(members or [])


async def toggle(feature: str, action: str) -> dict:
    if feature not in _FEATURES:
        raise HTTPException(status_code=400, detail=f"Unknown feature: {feature}")
    if action == "disable":
        await r.sadd(_REDIS_KEY, feature)
        logger.info("Maintenance: %s disabled", feature)
        return {"feature": feature, "disabled": True}
    elif action == "enable":
        await r.srem(_REDIS_KEY, feature)
        logger.info("Maintenance: %s enabled", feature)
        return {"feature": feature, "disabled": False}
    else:
        raise HTTPException(status_code=400, detail="Action must be 'enable' or 'disable'")


def require_feature(feature: str):
    async def _check():
        if await is_disabled(feature):
            raise HTTPException(
                status_code=503,
                detail=f"{feature} is temporarily disabled for maintenance",
            )
        return True
    return _check
