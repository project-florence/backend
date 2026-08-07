from fastapi import APIRouter, HTTPException, Query

from src.legal.about import ABOUT_TR, ABOUT_EN
from src.legal.contact import CONTACT
from src.legal.cookie_policy import COOKIE_POLICY_TR, COOKIE_POLICY_EN
from src.legal.disclaimer import DISCLAIMER_TR, DISCLAIMER_EN
from src.legal.privacy_policy import PRIVACY_POLICY_TR, PRIVACY_POLICY_EN
from src.legal.terms import TERMS_TR, TERMS_EN
from src.version import VERSION

router = APIRouter()

LAST_UPDATED = "2026-07-22"

ABOUT = {"tr": ABOUT_TR, "en": ABOUT_EN}

POLICIES = {
    "terms": {"tr": TERMS_TR, "en": TERMS_EN},
    "privacy_policy": {"tr": PRIVACY_POLICY_TR, "en": PRIVACY_POLICY_EN},
    "cookie_policy": {"tr": COOKIE_POLICY_TR, "en": COOKIE_POLICY_EN},
    "disclaimer": {"tr": DISCLAIMER_TR, "en": DISCLAIMER_EN},
}


@router.get("/legal")
async def get_legal(policy: str = Query(..., description="Policy name: terms, privacy_policy, cookie_policy, disclaimer"),
                    lang: str = Query("tr", description="Language: tr or en")):
    if policy not in POLICIES:
        raise HTTPException(status_code=404, detail=f"Unknown policy: {policy}")
    if lang not in ("tr", "en"):
        raise HTTPException(status_code=400, detail="Language must be 'tr' or 'en'")

    return {
        "policy": policy,
        "lang": lang,
        "last_updated": LAST_UPDATED,
        "content": POLICIES[policy][lang],
    }


@router.get("/legal/all")
async def get_all_legal(lang: str = Query("tr", description="Language: tr or en")):
    if lang not in ("tr", "en"):
        raise HTTPException(status_code=400, detail="Language must be 'tr' or 'en'")

    return {
        "last_updated": LAST_UPDATED,
        "lang": lang,
        "policies": {name: texts[lang] for name, texts in POLICIES.items()},
    }


@router.get("/about")
async def get_about(lang: str = Query("tr", description="Language: tr or en")):
    if lang not in ("tr", "en"):
        raise HTTPException(status_code=400, detail="Language must be 'tr' or 'en'")

    return {
        "lang": lang,
        "content": ABOUT[lang],
    }


@router.get("/contact")
async def get_contact():
    return CONTACT


@router.get("/version")
async def get_version():
    return {"version": VERSION}
