"""Unit tests for the LLM foundation layer (src/llm/*) -- REFACTOR_PLAN.md
Adim 1 + Adim 6.5 (tek ayar/singleton, embedding kaldirildi).

Hermetic: no real Postgres/Redis/network. ``fake_db``/``fake_redis`` swap the
shared async singletons (see tests/conftest.py, tests/api_helpers.py).
Covers: AES-256-GCM round-trip + AAD isolation, master-key failure modes,
provider catalog invariants, ``resolve()`` spec parsing, and
``resolve_llm`` behaviour (singleton, amac almaz) when nothing is configured.
"""

import base64
import os

import pytest

import src.llm.crypto as crypto
import src.llm.providers as providers
import src.llm.settings as settings

# ---------------------------------------------------------------------------
# crypto: round-trip, AAD isolation, master-key failure modes
# ---------------------------------------------------------------------------


def _set_master_key(monkeypatch, raw: bytes | None = None) -> None:
    raw = raw if raw is not None else os.urandom(32)
    monkeypatch.setenv("FLORENCE_MASTER_KEY", base64.b64encode(raw).decode("ascii"))


def test_encrypt_decrypt_round_trip(monkeypatch):
    _set_master_key(monkeypatch)
    blob = crypto.encrypt("sk-super-secret-value", aad="openai")
    assert isinstance(blob, bytes)
    plaintext = crypto.decrypt(blob, aad="openai")
    assert plaintext == "sk-super-secret-value"


def test_aad_mismatch_fails_to_decrypt(monkeypatch):
    _set_master_key(monkeypatch)
    blob = crypto.encrypt("sk-provider-a-key", aad="provider-a")
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(blob, aad="provider-b")


def test_nonce_is_random_per_call(monkeypatch):
    _set_master_key(monkeypatch)
    blob1 = crypto.encrypt("same-plaintext", aad="openai")
    blob2 = crypto.encrypt("same-plaintext", aad="openai")
    assert blob1 != blob2  # farkli nonce -> farkli ciphertext
    assert crypto.decrypt(blob1, aad="openai") == "same-plaintext"
    assert crypto.decrypt(blob2, aad="openai") == "same-plaintext"


def test_missing_master_key_raises(monkeypatch):
    monkeypatch.delenv("FLORENCE_MASTER_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.encrypt("x", aad="openai")


def test_invalid_base64_master_key_raises(monkeypatch):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", "not-valid-base64!!!")
    with pytest.raises(crypto.MasterKeyInvalid):
        crypto.encrypt("x", aad="openai")


def test_wrong_length_master_key_raises(monkeypatch):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(crypto.MasterKeyInvalid):
        crypto.encrypt("x", aad="openai")


def test_truncated_ciphertext_fails_cleanly(monkeypatch):
    _set_master_key(monkeypatch)
    blob = crypto.encrypt("x", aad="openai")
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(blob[:5], aad="openai")


def test_generate_master_key_produces_valid_key(monkeypatch):
    key_b64 = crypto.generate_master_key()
    monkeypatch.setenv("FLORENCE_MASTER_KEY", key_b64)
    blob = crypto.encrypt("round-trips", aad="anthropic")
    assert crypto.decrypt(blob, aad="anthropic") == "round-trips"


# ---------------------------------------------------------------------------
# providers: catalog invariants
# ---------------------------------------------------------------------------


EXPECTED_PROVIDER_IDS = {
    "openai",
    "anthropic",
    "xai",
    "groq",
    "deepseek",
    "mistral",
    "openrouter",
    "opencode-zen",
    "opencode-go",
    "ollama-cloud",
    "ollama-local",
    "openai-compatible",
}


def test_catalog_has_all_expected_providers():
    assert set(providers.PROVIDERS) == EXPECTED_PROVIDER_IDS


@pytest.mark.parametrize("provider_id", sorted(EXPECTED_PROVIDER_IDS))
def test_catalog_entries_have_required_fields(provider_id):
    spec = providers.PROVIDERS[provider_id]
    assert spec.id == provider_id
    assert spec.api_style in ("openai-chat", "anthropic", "responses")
    assert isinstance(spec.reasoning_values, frozenset)
    assert isinstance(spec.supports_tools, bool)
    assert isinstance(spec.verified, bool)
    if provider_id == "openai-compatible":
        assert spec.base_url is None
    else:
        assert spec.base_url, f"{provider_id} disinda hepsi sabit bir base_url tasimali"


def test_no_provider_has_api_key_env_field():
    """Tasarim kurali: anahtarlar artik DB'de sifreli, api_key_env alani YOK."""
    for spec in providers.PROVIDERS.values():
        assert not hasattr(spec, "api_key_env")


def test_opencode_providers_marked_verified_with_no_auth_note():
    zen = providers.PROVIDERS["opencode-zen"]
    go = providers.PROVIDERS["opencode-go"]
    assert zen.base_url == "https://opencode.ai/zen/v1"
    assert zen.models_url == "https://opencode.ai/zen/v1/models"
    assert go.base_url == "https://opencode.ai/zen/go/v1"
    assert go.models_url == "https://opencode.ai/zen/go/v1/models"
    assert zen.verified is True
    assert go.verified is True


def test_deepseek_reasoning_param_is_none():
    """2026-08-26 arizasinin kok nedeni: deepseek'e reasoning_effort gonderilmesi.

    Katalog artik bu saglayici icin bilincli olarak reasoning_param=None
    tasir, boylece "model adina bakarak reasoning ac/kapa" sezgisi bir daha
    kurulamaz.
    """
    spec = providers.PROVIDERS["deepseek"]
    assert spec.reasoning_param is None
    assert spec.reasoning_values == frozenset()


# ---------------------------------------------------------------------------
# providers.resolve()
# ---------------------------------------------------------------------------


def test_resolve_valid_spec():
    resolved = providers.resolve("opencode-zen/deepseek-v4-flash-free")
    assert resolved.provider.id == "opencode-zen"
    assert resolved.model == "deepseek-v4-flash-free"


def test_resolve_model_with_embedded_slash():
    """OpenRouter model id'leri kendi iclerinde '/' icerebilir -- yalniz ilk
    ayrac saglayici/model sinirini belirlemeli."""
    resolved = providers.resolve("openrouter/anthropic/claude-3.5-sonnet")
    assert resolved.provider.id == "openrouter"
    assert resolved.model == "anthropic/claude-3.5-sonnet"


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "no-slash-here",
        "/missing-provider",
        "missing-model/",
        "totally-unknown-provider/some-model",
    ],
)
def test_resolve_invalid_spec_raises(spec):
    with pytest.raises(providers.InvalidModelSpec):
        providers.resolve(spec)


# ---------------------------------------------------------------------------
# settings: structured_output_forbids_reasoning
# ---------------------------------------------------------------------------


def test_structured_output_forbids_reasoning_for_digest_and_report():
    assert settings.structured_output_forbids_reasoning("digest") is True
    assert settings.structured_output_forbids_reasoning("report") is True
    # "embedding" artik gecerli bir amac degil (Adim 6.5.B) -- kume disinda
    # herhangi bir string icin fonksiyon False donmeli (kapali degil, ilgisiz).
    assert settings.structured_output_forbids_reasoning("some-unknown-purpose") is False


def test_purposes_no_longer_includes_embedding():
    """Adim 6.5.B: embedding bir LLM degil, src/clients/embedding.py'nin
    hicbir cagirani yoktu (dogrulandi) -- var olmayan bir tuketici icin
    yapilandirma yuzeyiydi, tamamen kaldirildi."""
    assert settings.PURPOSES == ("digest", "report")
    assert "embedding" not in settings.PURPOSES


def test_mask_secret():
    assert settings.mask_secret(None) == "(anahtar yok)"
    assert settings.mask_secret("") == "(anahtar yok)"
    assert settings.mask_secret("sk-abcd1234") == "…1234"


# ---------------------------------------------------------------------------
# settings: DB/Redis-backed write + resolve flows (fake_db / fake_redis)
# Adim 6.5: llm_settings singleton -- set_selection/get_selection/
# resolve_llm ARTIK amac parametresi almiyor (tek ayar, digest+report
# paylasir).
# ---------------------------------------------------------------------------


async def test_set_selection_rejects_unknown_provider(fake_db, fake_redis):
    with pytest.raises(ValueError):
        await settings.set_selection("not-a-provider", "some-model")


async def test_upsert_provider_rejects_unknown_provider(fake_db, fake_redis):
    with pytest.raises(ValueError):
        await settings.upsert_provider("not-a-provider", api_key="x")


async def test_upsert_provider_encrypts_key_before_storing(fake_db, fake_redis, monkeypatch):
    _set_master_key(monkeypatch)
    await settings.upsert_provider("openai", api_key="sk-real-secret", base_url=None)
    inserts = [q for q in fake_db.queries if "INSERT INTO llm_providers" in q[0]]
    assert len(inserts) == 1
    stored_blob = inserts[0][1][1]
    assert stored_blob is not None
    assert b"sk-real-secret" not in stored_blob
    assert crypto.decrypt(stored_blob, aad="openai") == "sk-real-secret"


async def test_upsert_provider_without_key_stores_null(fake_db, fake_redis):
    await settings.upsert_provider("opencode-zen", api_key=None, base_url=None)
    inserts = [q for q in fake_db.queries if "INSERT INTO llm_providers" in q[0]]
    assert inserts[0][1][1] is None


async def test_resolve_llm_unconfigured_when_no_selection(fake_db, fake_redis):
    fake_db.queue_fetchone(None)  # llm_settings SELECT -> hic satir yok
    result = await settings.resolve_llm()
    assert isinstance(result, settings.Unconfigured)


async def test_resolve_llm_unconfigured_when_provider_row_missing(fake_db, fake_redis):
    # 1) llm_settings SELECT -> secim var (openai/gpt-5)
    # 2) llm_providers SELECT -> satir yok
    fake_db.queue_fetchone(("openai", "gpt-5", {}), None)
    result = await settings.resolve_llm()
    assert isinstance(result, settings.Unconfigured)
    assert "llm_providers" in result.reason or "satir yok" in result.reason


async def test_resolve_llm_unconfigured_when_disabled(fake_db, fake_redis):
    fake_db.queue_fetchone(("openai", "gpt-5", {}), (None, "https://api.openai.com/v1", False))
    result = await settings.resolve_llm()
    assert isinstance(result, settings.Unconfigured)
    assert "devre disi" in result.reason


async def test_resolve_llm_unconfigured_when_key_undecryptable(fake_db, fake_redis, monkeypatch):
    _set_master_key(monkeypatch)
    bogus_blob = b"\x00" * 40  # gecerli uzunlukta ama gecersiz tag -> decrypt basarisiz
    fake_db.queue_fetchone(("openai", "gpt-5", {}), (bogus_blob, None, True))
    result = await settings.resolve_llm()
    assert isinstance(result, settings.Unconfigured)
    assert "cozulemedi" in result.reason


async def test_resolve_llm_success(fake_db, fake_redis, monkeypatch):
    _set_master_key(monkeypatch)
    encrypted = crypto.encrypt("sk-live-key", aad="openai")
    fake_db.queue_fetchone(
        ("openai", "gpt-5", {"temperature": 0.2}),
        (encrypted, None, True),
    )
    result = await settings.resolve_llm()
    assert isinstance(result, settings.ResolvedLLM)
    assert result.provider.id == "openai"
    assert result.model == "gpt-5"
    assert result.base_url == "https://api.openai.com/v1"
    assert result.api_key == "sk-live-key"
    assert result.params == {"temperature": 0.2}
    assert not hasattr(result, "purpose")  # Adim 6.5: ResolvedLLM'de purpose alani YOK


async def test_resolve_llm_openai_compatible_uses_stored_base_url(fake_db, fake_redis):
    fake_db.queue_fetchone(
        ("openai-compatible", "local-model", {}),
        (None, "https://my-custom-gateway.example.com/v1", True),
    )
    result = await settings.resolve_llm()
    assert isinstance(result, settings.ResolvedLLM)
    assert result.base_url == "https://my-custom-gateway.example.com/v1"
    assert result.api_key is None


async def test_resolve_llm_uses_redis_cache_on_second_call(fake_db, fake_redis, monkeypatch):
    """Ikinci cagri llm_settings'e tekrar SELECT atmamali (Redis onbellek isabeti)."""
    _set_master_key(monkeypatch)
    encrypted = crypto.encrypt("sk-live-key", aad="openai")
    fake_db.queue_fetchone(
        ("openai", "gpt-5", {}),
        (encrypted, None, True),
    )
    first = await settings.resolve_llm()
    assert isinstance(first, settings.ResolvedLLM)
    settings_selects_after_first = len(
        [q for q in fake_db.queries if "FROM llm_settings" in q[0]]
    )

    # Provider anahtarini tekrar decrypt edebilmesi icin ikinci saglayici
    # satirini kuyruga koy -- fakat llm_settings SELECT'i onbellekten
    # gelmeli, kuyruga ikinci bir secim satiri KONULMADI.
    fake_db.queue_fetchone((encrypted, None, True))
    second = await settings.resolve_llm()
    assert isinstance(second, settings.ResolvedLLM)
    settings_selects_after_second = len(
        [q for q in fake_db.queries if "FROM llm_settings" in q[0]]
    )
    assert settings_selects_after_second == settings_selects_after_first


async def test_set_selection_invalidates_cache(fake_db, fake_redis):
    fake_redis.store["llm:selection"] = '{"configured": false}'
    await settings.set_selection("openai", "gpt-5")
    assert "llm:selection" not in fake_redis.store
