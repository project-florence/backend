import logging
from fastapi import HTTPException, Depends
from src.core.redis import r

logger = logging.getLogger(__name__)

_REDIS_KEY = "maintenance:disabled"
_FEATURES = {
    "report_generate",
    "simulation",
    "news",
    "advisor",
}


def is_disabled(feature: str) -> bool:
    return r.sismember(_REDIS_KEY, feature)


def list_disabled() -> list[str]:
    return list(r.smembers(_REDIS_KEY))


def toggle(feature: str, action: str) -> dict:
    if feature not in _FEATURES:
        raise HTTPException(status_code=400, detail=f"Unknown feature: {feature}")
    if action == "disable":
        r.sadd(_REDIS_KEY, feature)
        logger.info("Maintenance: %s disabled", feature)
        return {"feature": feature, "disabled": True}
    elif action == "enable":
        r.srem(_REDIS_KEY, feature)
        logger.info("Maintenance: %s enabled", feature)
        return {"feature": feature, "disabled": False}
    else:
        raise HTTPException(status_code=400, detail="Action must be 'enable' or 'disable'")


def require_feature(feature: str):
    def _check():
        if is_disabled(feature):
            raise HTTPException(
                status_code=503,
                detail=f"{feature} is temporarily disabled for maintenance",
            )
        return True
    return Depends(_check)
