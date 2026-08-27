"""``llm_providers`` / ``llm_settings`` okuma-yazma + tek-ayar cozumleme.

REFACTOR_PLAN.md Adim 6.5: ``llm_settings`` TEK SATIRLIK (singleton) --
``purpose`` birincil anahtar olmaktan cikti, model birden fazla amacta
(digest, report) kullanildiginda birini guncelleyip digerini unutmak riski
yapisal olarak imkansiz hale geldi (bu, CUSTOM_MODEL/CUSTOM_URL
ayrismasinin -- 2026-08-26 arizasinin kok nedeni -- bir kat yukarida
yeniden uretilmesiydi). ``resolve_llm()`` iki tabloyu birlestirip "hangi
saglayici, hangi model, hangi base_url, hangi (cozulmus) API anahtari,
hangi ek parametreler" sorusunu AMAC ALMADAN yanitlar.

``purpose`` TAMAMEN KAYBOLMADI -- ayar ekseninden gozlemlenebilirlik
eksenine tasindi. ``token_usage.purpose`` hala digest/report cagrilarini
ayirt eder (bkz. ``src/llm/agents.py::log_llm_call``); yalniz ayarin
KENDISI artik amaca gore dallanamaz.

Onbellekleme tasarimi (bilincli karar, Adim 1'den korunuyor): Redis'te
YALNIZ secim bilgisi (saglayici id + model + params) TEK bir anahtar
altinda (``_CACHE_KEY``) tutulur, TTL <= 60s. Sifresi cozulmus API anahtari
HICBIR ZAMAN Redis'e yazilmaz -- saglayici satiri ve anahtar her
``resolve_llm`` cagrisinda DB'den taze okunup cozulur. Bu, AES-GCM'in kucuk
payload'da mikrosaniyeler surmesi sayesinde ucretsiz (crypto.py'deki not)
ve sirri DB+ana anahtar sinirinin disina hic cikarmiyor. Sonuc: bir
saglayici anahtari rotate edildiginde onbellek gecerliligini beklemeden
aninda etkili olur; yalniz "hangi saglayici/model secili" bilgisi 60s'e
kadar bayatlayabilir (yazma sonrasi dogrudan invalidasyon zaten bunu
sifira indirir).

Seçim yoksa/cozulmezse uygulama COKMEZ: ``resolve_llm`` ya ``ResolvedLLM``
ya da ``Unconfigured`` doner (nedeni aciklayan bir ``reason`` ile). Ajanlara
baglama (bu donus degerini gercekten kullanma) ``src/llm/agents.py::
build_agent``'in isi -- o katman ``purpose``'u sadece loglama etiketi
olarak ekler (``LLMPurposeUnconfigured(purpose, reason)``).
"""

import json
from dataclasses import dataclass
from typing import Any

from src.core.database import db
from src.core.redis import r
from src.llm import crypto
from src.llm.providers import PROVIDERS, ProviderSpec

# Bugun gozlemlenebilirlik icin (token_usage.purpose) kullanilan amaclar.
# Ayar ARTIK bu kumeye gore dallanmiyor (Adim 6.5) -- bu sadece etiketleme
# ve dogrulama (CLI'nin "purpose" pozisyonel argumanlari, structured-output
# reasoning kurali) icin kullanilan bir liste. "embedding" Adim 6.5'te
# kaldirildi: embedding bir LLM degil, src/clients/embedding.py'nin hicbir
# cagirani yoktu (dogrulandi) -- var olmayan bir tuketici icin yapilandirma
# yuzeyiydi.
PURPOSES: tuple[str, ...] = ("digest", "report")

# ``output_type`` pydantic modeli olan amaclar -- reasoning bunlarda kapali
# olmali (bkz. REFACTOR_PLAN.md 2.5 ve 0. bolumdeki 2026-08-26 ariza teshisi).
# Not: embedding kaldirildiktan sonra bu kume PURPOSES ile AYNI -- yani tek
# model ayari acildiginda reasoning her zaman en az bir tuketiciyi (aslinda
# ikisini de) etkiler. Bu kume yine de AYRI tutuluyor cunku anlami farkli
# (yapilandirilmis cikti kullanan amaclar) ve gelecekte structured-output
# OLMAYAN bir amac eklenirse (ornegin serbest metin ureten bir ozellik) bu
# ayrim otomatik olarak dogru davranir.
_STRUCTURED_OUTPUT_PURPOSES = frozenset({"digest", "report"})

_CACHE_TTL = 60  # saniye -- plan: "TTL <= 60s"
_CACHE_KEY = "llm:selection"  # TEK anahtar -- singleton ayarda amac yok


def structured_output_forbids_reasoning(purpose: str) -> bool:
    """``output_type`` bir pydantic modeli olan amaclarda reasoning kapali olmali mi?

    Sebep sağlayıcı/model ekseninde degil: reasoning token'lari yapisal
    ciktinin (Digest, Report) semaya ayristirilmasini bozup hata firlatiyor.
    Bu, 2026-08-26 arizasindan AYRI ve ondan onceki bir sebeptir; o ariza
    model/base_url ayrismasindan cikti. ``"deepseek" not in
    model_name.lower()`` gibi model-adina-bakan sezgiler yerine bu acik kural
    kullanilmali. Bu fonksiyon yalniz kurali ifade eder; ajanlara (digest/
    report agent kurulumuna) baglanmasi ``src/llm/agents.py``'nin isi.
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
    """Tam cozulmus, kullanima hazir TEK LLM yapilandirmasi (singleton).

    ``purpose`` ALANI YOK -- cozumleme artik amaca gore dallanmiyor
    (Adim 6.5). Bir cagrinin hangi amac icin yapildigini bilmek gereken
    yerler (``build_agent``, ``log_llm_call``) bunu KENDI parametreleri
    olarak, ayri tasir.
    """

    provider: ProviderSpec
    model: str
    base_url: str
    api_key: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class Unconfigured:
    """Secim yok ya da secim cozulemiyor (saglayici/anahtar/base_url)."""

    reason: str


ResolveResult = ResolvedLLM | Unconfigured


class LLMPurposeUnconfigured(RuntimeError):
    """Bir amac (digest/report) icin cagrilan ``build_agent`` LLM'i coz(em)edi.

    ``resolve_llm`` sessizce ``Unconfigured`` dondugu icin cokmez; bu
    istisna cagiran katmanlarin (``src/llm/agents.py::build_agent``) o
    durumu acik bir hataya cevirmek icin kullandigi ortak tip -- boylece
    cagiran taraf yakalayip anlamli bir HTTP/cron hatasi dondurebilir,
    sessizce varsayilana dusmek YOK (REFACTOR_PLAN.md 2.4). ``purpose``
    burada YALNIZ hangi cagrinin basarisiz oldugunu belirten bir etiket --
    ayarin kendisi ``purpose``'a gore degismiyor, tek bir ``reason`` her
    amac icin aynidir."""

    def __init__(self, purpose: str, reason: str) -> None:
        self.purpose = purpose
        self.reason = reason
        super().__init__(f"LLM not configured (requested for purpose={purpose!r}): {reason}")


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
    await _invalidate_selection_cache()


async def clear_provider_key(provider: str) -> None:
    """Saglayicinin sifreli anahtarini acikca kaldirir (satiri silmez)."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "UPDATE llm_providers SET api_key_encrypted = NULL, updated_at = NOW() WHERE provider = %s",
            (provider,),
        )
        await db.commit()
    await _invalidate_selection_cache()


async def remove_provider(provider: str) -> None:
    """Saglayici satirini tamamen siler.

    Not: ``llm_settings.provider`` bu satira FK ile bagli; secim (varsa) hala
    bu saglayiciyi kullaniyorsa DB bu silmeyi reddeder (referans butunlugu).
    Once secimi (``set_selection``) baska bir saglayiciya tasimak/temizlemek
    gerekir.
    """
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("DELETE FROM llm_providers WHERE provider = %s", (provider,))
        await db.commit()
    await _invalidate_selection_cache()


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
    """resolve_llm icin ic kullanim: tuple satir (row_factory=None)."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT api_key_encrypted, base_url, enabled FROM llm_providers WHERE provider = %s",
            (provider,),
        )
        return await cur.fetchone()


# --------------------------------------------------------------------------
# llm_settings: yazma (singleton -- amac parametresi YOK)
# --------------------------------------------------------------------------


async def set_selection(
    provider: str,
    model: str,
    *,
    params: dict[str, Any] | None = None,
    updated_by: str | None = None,
) -> None:
    """TEK saglayici+model secimini kaydeder (digest VE report bunu kullanir).

    Saglayicinin katalogda var olup olmadigini dogrular; ama saglayicinin
    ``llm_providers``'ta bir satiri olup olmadigini (anahtar girilmis mi)
    KONTROL ETMEZ -- FK bunu zaten zorunlu kilar (once ``upsert_provider``
    cagirilmis olmali, en azindan anahtarsiz bir satir icin bile). Model
    adinin saglayicinin canli roster'inda olup olmadigi dogrulamasi
    ``scripts/admin_cli.py llm model set``'in isi, burada degil.
    """
    if provider not in PROVIDERS:
        raise ValueError(
            f"bilinmeyen saglayici: {provider!r} (katalogda yok: {sorted(PROVIDERS)})"
        )
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            """
            INSERT INTO llm_settings (id, provider, model, params, updated_at, updated_by)
            VALUES (TRUE, %s, %s, %s, NOW(), %s)
            ON CONFLICT (id) DO UPDATE SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                params = EXCLUDED.params,
                updated_at = NOW(),
                updated_by = EXCLUDED.updated_by
            """,
            (provider, model, json.dumps(params or {}), updated_by),
        )
        await db.commit()
    await _invalidate_selection_cache()


async def clear_selection() -> None:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("DELETE FROM llm_settings WHERE id")
        await db.commit()
    await _invalidate_selection_cache()


async def get_selection() -> dict | None:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT provider, model, params, updated_at, updated_by FROM llm_settings WHERE id"
        )
        return await cur.fetchone()


async def _invalidate_selection_cache() -> None:
    await r.delete(_CACHE_KEY)


# --------------------------------------------------------------------------
# Cozumleme
# --------------------------------------------------------------------------


async def _get_selection_cached() -> dict | None:
    """Secimi (saglayici id + model + params) Redis onbellekli okur.

    Donus: secim yoksa ``None``; varsa ``{"provider", "model", "params"}``.
    Onbellekte "secim yok" durumu da TTL boyunca tutulur (yoksa her cagri
    bos DB taramasi yapardi); bir yazma/silme sonrasi
    ``_invalidate_selection_cache`` bu onbellegi hemen gecersiz kilar.
    """
    cached = await r.get(_CACHE_KEY)
    if cached is not None:
        payload = json.loads(cached)
        return payload if payload.get("configured") else None

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT provider, model, params FROM llm_settings WHERE id")
        row = await cur.fetchone()

    if row is None:
        await r.set(_CACHE_KEY, json.dumps({"configured": False}), ex=_CACHE_TTL)
        return None

    provider_id, model, params = row
    payload = {
        "configured": True,
        "provider": provider_id,
        "model": model,
        "params": params or {},
    }
    await r.set(_CACHE_KEY, json.dumps(payload), ex=_CACHE_TTL)
    return payload


async def resolve_llm() -> ResolveResult:
    """TEK LLM yapilandirmasini cozer (amac almaz -- Adim 6.5).

    Cokmez: secim yoksa, saglayici katalogdan dusmusse, saglayici satiri
    yoksa/devre disiysa, base_url eksikse veya anahtar cozulemiyorsa
    ``Unconfigured(reason=...)`` doner.
    """
    selection = await _get_selection_cached()
    if selection is None:
        return Unconfigured(
            reason="henuz bir model secilmedi (admin_cli.py llm model set <saglayici>/<model>)"
        )

    provider_id: str = selection["provider"]
    model: str = selection["model"]
    params: dict[str, Any] = selection["params"]

    provider_spec = PROVIDERS.get(provider_id)
    if provider_spec is None:
        return Unconfigured(
            reason=f"saglayici {provider_id!r} artik katalogda yok (secim yapildiktan sonra kaldirilmis olabilir)",
        )

    provider_row = await _fetch_provider_row_raw(provider_id)
    if provider_row is None:
        return Unconfigured(
            reason=f"saglayici {provider_id!r} icin llm_providers'ta satir yok (once 'llm provider set' calistirilmali)",
        )
    api_key_encrypted, stored_base_url, enabled = provider_row
    if not enabled:
        return Unconfigured(reason=f"saglayici {provider_id!r} devre disi (enabled=false)")

    base_url = provider_spec.base_url or stored_base_url
    if not base_url:
        return Unconfigured(
            reason=f"saglayici {provider_id!r} icin base_url gerekli ama ne katalogda ne DB'de var",
        )

    api_key: str | None = None
    if api_key_encrypted is not None:
        try:
            api_key = crypto.decrypt(bytes(api_key_encrypted), aad=provider_id)
        except crypto.LLMCryptoError as exc:
            return Unconfigured(
                reason=f"saglayici {provider_id!r} icin API anahtari cozulemedi: {exc}",
            )

    return ResolvedLLM(
        provider=provider_spec,
        model=model,
        base_url=base_url,
        api_key=api_key,
        params=params,
    )
