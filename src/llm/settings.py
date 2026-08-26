"""``llm_providers`` / ``llm_settings`` okuma-yazma + amac bazli cozumleme.

Iki tabloyu birlestirip her amac (``digest`` | ``report`` | ``embedding``) icin
tek bir cagriyla "hangi saglayici, hangi model, hangi base_url, hangi (cozulmus)
API anahtari, hangi ek parametreler" sorusunu yanitlar (``resolve_purpose``).

Onbellekleme tasarimi (bilincli karar): Redis'te YALNIZ secim bilgisi
(saglayici id + model + params) tutulur, TTL <= 60s. Sifresi cozulmus API
anahtari HICBIR ZAMAN Redis'e yazilmaz -- saglayici satiri ve anahtar her
``resolve_purpose`` cagrisinda DB'den taze okunup cozulur. Bu, AES-GCM'in
kucuk payload'da mikrosaniyeler surmesi sayesinde ucretsiz (crypto.py'deki
not) ve sirri DB+ana anahtar sinirinin disina hic cikarmiyor. Sonuc: bir
saglayici anahtari rotate edildiginde onbellek gecerliligini beklemeden
aninda etkili olur; yalniz "hangi saglayici/model secili" bilgisi 60s'e kadar
bayatlayabilir (yazma sonrasi dogrudan invalidasyon zaten bunu sifira indirir).

Seçim yoksa/cozulmezse uygulama COKMEZ: ``resolve_purpose`` ya ``ResolvedLLM``
ya da ``Unconfigured`` doner (nedeni aciklayan bir ``reason`` ile). Ajanlara
baglama (bu donus degerini gercekten kullanma) Adim 2'nin isi.
"""

import json
from dataclasses import dataclass
from typing import Any

from src.core.database import db
from src.core.redis import r
from src.llm import crypto
from src.llm.providers import PROVIDERS, ProviderSpec

# Bugun desteklenen amaclar. REFACTOR_PLAN.md 2.3: llm_settings.purpose bu
# kumeden biri olmali (DB'de CHECK constraint yok, dogrulama burada).
PURPOSES: tuple[str, ...] = ("digest", "report", "embedding")

# ``output_type`` pydantic modeli olan amaclar -- reasoning bunlarda kapali
# olmali (bkz. REFACTOR_PLAN.md 2.5 ve 0. bolumdeki 2026-08-26 ariza teshisi).
_STRUCTURED_OUTPUT_PURPOSES = frozenset({"digest", "report"})

_CACHE_TTL = 60  # saniye -- plan: "TTL <= 60s"
_CACHE_PREFIX = "llm:selection:"


def _cache_key(purpose: str) -> str:
    return f"{_CACHE_PREFIX}{purpose}"


def structured_output_forbids_reasoning(purpose: str) -> bool:
    """``output_type`` pydantic modeli olan amaclarda reasoning kapali olmali mi?

    Sebep sağlayıcı/model ekseninde degil: reasoning token'lari yapisal
    ciktinin (Digest, Report) semaya ayristirilmasini bozup hata firlatiyor.
    Bu, 2026-08-26 arizasindan AYRI ve ondan onceki bir sebeptir; o ariza
    model/base_url ayrismasindan cikti. ``"deepseek" not in
    model_name.lower()`` gibi model-adina-bakan sezgiler yerine bu acik kural
    kullanilmali. Bu fonksiyon yalniz kurali ifade eder; ajanlara (digest/
    report agent kurulumuna) baglanmasi Adim 2'nin isi.
    """
    return purpose in _STRUCTURED_OUTPUT_PURPOSES


def mask_secret(value: str | None) -> str:
    """CLI/log icin: gercek degeri hicbir zaman basmayan maskeli gosterim."""
    if not value:
        return "(anahtar yok)"
    if len(value) <= 4:
        return "…" + value
    return "…" + value[-4:]


@dataclass(frozen=True)
class ResolvedLLM:
    """Bir amac icin tam cozulmus, kullanima hazir LLM yapilandirmasi."""

    purpose: str
    provider: ProviderSpec
    model: str
    base_url: str
    api_key: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class Unconfigured:
    """Bir amac icin ya secim yok ya da secim cozulemiyor (anahtar/saglayici)."""

    purpose: str
    reason: str


ResolveResult = ResolvedLLM | Unconfigured


# --------------------------------------------------------------------------
# llm_providers: yazma
# --------------------------------------------------------------------------


async def upsert_provider(
    provider: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    enabled: bool = True,
) -> None:
    """Saglayici satirini olusturur/gunceller.

    ``api_key=None`` gecilirse mevcut (varsa) sifreli anahtar DOKUNULMADAN
    korunur -- bu, yalniz ``base_url`` veya ``enabled`` guncellemek icin
    anahtari yeniden girmeyi gerektirmez. Anahtari acikca kaldirmak icin
    ``clear_provider_key`` kullanilmali.
    """
    if provider not in PROVIDERS:
        raise ValueError(
            f"bilinmeyen saglayici: {provider!r} (katalogda yok: {sorted(PROVIDERS)})"
        )
    encrypted = crypto.encrypt(api_key, aad=provider) if api_key else None
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            """
            INSERT INTO llm_providers (provider, api_key_encrypted, base_url, enabled, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (provider) DO UPDATE SET
                api_key_encrypted = COALESCE(EXCLUDED.api_key_encrypted, llm_providers.api_key_encrypted),
                base_url = COALESCE(EXCLUDED.base_url, llm_providers.base_url),
                enabled = EXCLUDED.enabled,
                updated_at = NOW()
            """,
            (provider, encrypted, base_url, enabled),
        )
        await db.commit()
    await _invalidate_all_selections()


async def clear_provider_key(provider: str) -> None:
    """Saglayicinin sifreli anahtarini acikca kaldirir (satiri silmez)."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "UPDATE llm_providers SET api_key_encrypted = NULL, updated_at = NOW() WHERE provider = %s",
            (provider,),
        )
        await db.commit()
    await _invalidate_all_selections()


async def remove_provider(provider: str) -> None:
    """Saglayici satirini tamamen siler.

    Not: ``llm_settings.provider`` bu satiriya FK ile bagli; halen bu
    saglayiciyi kullanan bir amac varsa DB bu silmeyi reddeder (referans
    butunlugu). Once ilgili amaclarin secimini degistirmek/temizlemek gerekir.
    """
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("DELETE FROM llm_providers WHERE provider = %s", (provider,))
        await db.commit()
    await _invalidate_all_selections()


# --------------------------------------------------------------------------
# llm_providers: okuma (ham -- anahtar hala sifreli, CLI/introspection icin)
# --------------------------------------------------------------------------


async def get_provider_row(provider: str) -> dict | None:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT provider, api_key_encrypted, base_url, enabled, created_at, updated_at "
            "FROM llm_providers WHERE provider = %s",
            (provider,),
        )
        return await cur.fetchone()


async def list_providers() -> list[dict]:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT provider, api_key_encrypted, base_url, enabled, created_at, updated_at "
            "FROM llm_providers ORDER BY provider"
        )
        return await cur.fetchall()


async def _fetch_provider_row_raw(provider: str) -> tuple | None:
    """resolve_purpose icin ic kullanim: tuple satir (row_factory=None)."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT api_key_encrypted, base_url, enabled FROM llm_providers WHERE provider = %s",
            (provider,),
        )
        return await cur.fetchone()


# --------------------------------------------------------------------------
# llm_settings: yazma
# --------------------------------------------------------------------------


async def set_selection(
    purpose: str,
    provider: str,
    model: str,
    *,
    params: dict[str, Any] | None = None,
    updated_by: str | None = None,
) -> None:
    """Bir amac icin saglayici+model secimini kaydeder.

    Saglayicinin katalogda var olup olmadigini dogrular; ama saglayicinin
    ``llm_providers``'ta bir satiri olup olmadigini (anahtar girilmis mi)
    KONTROL ETMEZ -- FK bunu zaten zorunlu kilar (once ``upsert_provider``
    cagirilmis olmali, en azindan anahtarsiz bir satir icin bile). Model
    adinin saglayicinin canli roster'inda olup olmadigi dogrulamasi Adim 4'un
    (``admin_cli.py llm set``) isi, burada degil.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"bilinmeyen amac: {purpose!r} (beklenen: {PURPOSES})")
    if provider not in PROVIDERS:
        raise ValueError(
            f"bilinmeyen saglayici: {provider!r} (katalogda yok: {sorted(PROVIDERS)})"
        )
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            """
            INSERT INTO llm_settings (purpose, provider, model, params, updated_at, updated_by)
            VALUES (%s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (purpose) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                params = EXCLUDED.params,
                updated_at = NOW(),
                updated_by = EXCLUDED.updated_by
            """,
            (purpose, provider, model, json.dumps(params or {}), updated_by),
        )
        await db.commit()
    await r.delete(_cache_key(purpose))


async def clear_selection(purpose: str) -> None:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("DELETE FROM llm_settings WHERE purpose = %s", (purpose,))
        await db.commit()
    await r.delete(_cache_key(purpose))


async def get_selection(purpose: str) -> dict | None:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT purpose, provider, model, params, updated_at, updated_by "
            "FROM llm_settings WHERE purpose = %s",
            (purpose,),
        )
        return await cur.fetchone()


async def _invalidate_all_selections() -> None:
    """Bir saglayici degisince hangi amac(lar) etkilendigi bilinmez -- hepsini
    temizlemek yanlis-pozitif (bayat) onbellek riskini sifirlar."""
    await r.delete(*[_cache_key(p) for p in PURPOSES])


# --------------------------------------------------------------------------
# Cozumleme
# --------------------------------------------------------------------------


async def _get_selection_cached(purpose: str) -> dict | None:
    """Secimi (saglayici id + model + params) Redis onbellekli okur.

    Donus: secim yoksa ``None``; varsa ``{"provider", "model", "params"}``.
    Onbellekte "secim yok" durumu da TTL boyunca tutulur (yoksa her cagri
    bos DB taramasi yapardi); bir yazma/silme sonrasi ``_invalidate_all_
    selections`` / ``clear_selection`` bu onbellegi hemen gecersiz kilar.
    """
    cache_key = _cache_key(purpose)
    cached = await r.get(cache_key)
    if cached is not None:
        payload = json.loads(cached)
        return payload if payload.get("configured") else None

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT provider, model, params FROM llm_settings WHERE purpose = %s",
            (purpose,),
        )
        row = await cur.fetchone()

    if row is None:
        await r.set(cache_key, json.dumps({"configured": False}), ex=_CACHE_TTL)
        return None

    provider_id, model, params = row
    payload = {
        "configured": True,
        "provider": provider_id,
        "model": model,
        "params": params or {},
    }
    await r.set(cache_key, json.dumps(payload), ex=_CACHE_TTL)
    return payload


async def resolve_purpose(purpose: str) -> ResolveResult:
    """Bir amac icin tam LLM yapilandirmasini cozer.

    Cokmez: secim yoksa, saglayici katalogdan dusmusse, saglayici satiri
    yoksa/devre disiysa, base_url eksikse veya anahtar cozulemiyorsa
    ``Unconfigured(reason=...)`` doner. Yalniz ``purpose`` gecersizse (bu
    kumenin disindaysa) ``ValueError`` firlatir -- bu bir programlama hatasi,
    calisma-zamani "yapilandirilmamis" durumu degil.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"bilinmeyen amac: {purpose!r} (beklenen: {PURPOSES})")

    selection = await _get_selection_cached(purpose)
    if selection is None:
        return Unconfigured(purpose=purpose, reason="bu amac icin kayitli bir secim yok")

    provider_id: str = selection["provider"]
    model: str = selection["model"]
    params: dict[str, Any] = selection["params"]

    provider_spec = PROVIDERS.get(provider_id)
    if provider_spec is None:
        return Unconfigured(
            purpose=purpose,
            reason=f"saglayici {provider_id!r} artik katalogda yok (secim yapildiktan sonra kaldirilmis olabilir)",
        )

    provider_row = await _fetch_provider_row_raw(provider_id)
    if provider_row is None:
        return Unconfigured(
            purpose=purpose,
            reason=f"saglayici {provider_id!r} icin llm_providers'ta satir yok (once 'llm provider set' calistirilmali)",
        )
    api_key_encrypted, stored_base_url, enabled = provider_row
    if not enabled:
        return Unconfigured(purpose=purpose, reason=f"saglayici {provider_id!r} devre disi (enabled=false)")

    base_url = provider_spec.base_url or stored_base_url
    if not base_url:
        return Unconfigured(
            purpose=purpose,
            reason=f"saglayici {provider_id!r} icin base_url gerekli ama ne katalogda ne DB'de var",
        )

    api_key: str | None = None
    if api_key_encrypted is not None:
        try:
            api_key = crypto.decrypt(bytes(api_key_encrypted), aad=provider_id)
        except crypto.LLMCryptoError as exc:
            return Unconfigured(
                purpose=purpose,
                reason=f"saglayici {provider_id!r} icin API anahtari cozulemedi: {exc}",
            )

    return ResolvedLLM(
        purpose=purpose,
        provider=provider_spec,
        model=model,
        base_url=base_url,
        api_key=api_key,
        params=params,
    )
