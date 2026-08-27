"""Unit tests for src/llm/agents.py -- REFACTOR_PLAN.md Adim 3 (gozlemlenebilirlik).

Hermetic: no real Postgres/Redis/network. Covers the two additions this step
introduced on top of Adim 2's ``build_agent``:

1. ``_sanitize_error`` -- secrets (API key, Authorization header, URL
   credentials) never survive into the string written to ``token_usage``.
2. ``log_llm_call`` -- writes success/failure rows via
   ``src.services.token.log_token_usage`` and NEVER lets its own failure
   (e.g. DB down) propagate to the caller (the LLM call itself must not be
   dropped because logging failed).
"""

import pytest

import src.llm.agents as agents_module
from src.llm.agents import elapsed_ms, log_llm_call


# ---------------------------------------------------------------------------
# _sanitize_error: secret redaction
# ---------------------------------------------------------------------------


def test_sanitize_error_redacts_authorization_bearer_token():
    exc = RuntimeError("401: Authorization: Bearer sk-live-abcdef123456 rejected")
    text = agents_module._sanitize_error(exc)
    assert "sk-live-abcdef123456" not in text
    assert "RuntimeError" in text
    assert "401" in text


def test_sanitize_error_redacts_bare_bearer_token():
    exc = RuntimeError("upstream saw header value 'Bearer sk-proj-zzzzzzzzzzzz' and rejected it")
    text = agents_module._sanitize_error(exc)
    assert "sk-proj-zzzzzzzzzzzz" not in text
    assert "rejected it" in text


def test_sanitize_error_redacts_api_key_kv_pair():
    exc = ValueError("bad request body: api_key='sk-supersecretvalue0000' is invalid")
    text = agents_module._sanitize_error(exc)
    assert "sk-supersecretvalue0000" not in text
    assert "ValueError" in text


def test_sanitize_error_redacts_url_embedded_credentials():
    exc = RuntimeError("connection to https://user:hunter2@internal-proxy.example/v1 failed")
    text = agents_module._sanitize_error(exc)
    assert "hunter2" not in text
    assert "user:hunter2" not in text
    assert "internal-proxy.example" in text  # host kendisi sir degil, korunur


def test_sanitize_error_truncates_to_max_length():
    exc = RuntimeError("x" * 5000)
    text = agents_module._sanitize_error(exc)
    assert len(text) <= agents_module._MAX_ERROR_LEN


def test_sanitize_error_preserves_harmless_message():
    exc = TimeoutError("request timed out after 30s")
    text = agents_module._sanitize_error(exc)
    assert text == "TimeoutError: request timed out after 30s"


# ---------------------------------------------------------------------------
# log_llm_call: success/failure rows + never raises
# ---------------------------------------------------------------------------


async def test_log_llm_call_success_forwards_expected_fields(monkeypatch):
    calls = []

    async def _fake_log(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("src.services.token.log_token_usage", _fake_log)

    await log_llm_call(
        purpose="digest",
        model_name="deepseek-v4-flash-free",
        provider_id="opencode-zen",
        status="ok",
        duration_ms=123,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        user_id=7,
    )

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "deepseek-v4-flash-free"
    assert kwargs["purpose"] == "digest"
    assert kwargs["provider"] == "opencode-zen"
    assert kwargs["status"] == "ok"
    assert kwargs["duration_ms"] == 123
    assert kwargs["prompt_tokens"] == 10
    assert kwargs["completion_tokens"] == 20
    assert kwargs["total_tokens"] == 30
    assert kwargs["error"] is None
    assert kwargs["user_id"] == 7


async def test_log_llm_call_failure_sanitizes_exception_before_forwarding(monkeypatch):
    calls = []

    async def _fake_log(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("src.services.token.log_token_usage", _fake_log)

    secret_exc = RuntimeError("401 Authorization: Bearer sk-live-hidden999999")
    await log_llm_call(
        purpose="report",
        model_name="gpt-5",
        provider_id="openai",
        status="error",
        duration_ms=50,
        error=secret_exc,
    )

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["status"] == "error"
    assert kwargs["error"] is not None
    assert "sk-live-hidden999999" not in kwargs["error"]
    assert "RuntimeError" in kwargs["error"]
    # basarisiz cagrida token sayilari None kalmali
    assert kwargs["prompt_tokens"] is None
    assert kwargs["completion_tokens"] is None
    assert kwargs["total_tokens"] is None


async def test_log_llm_call_accepts_plain_string_error(monkeypatch):
    """error bir Exception degil, hazir bir string de olabilir (ornegin
    build_agent basarisiz oldugunda cagiran taraf kendi mesajini kurabilir)."""
    calls = []

    async def _fake_log(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("src.services.token.log_token_usage", _fake_log)

    await log_llm_call(
        purpose="digest",
        model_name="unknown",
        provider_id=None,
        status="error",
        duration_ms=1,
        error="LLMPurposeUnconfigured: bu amac icin kayitli bir secim yok",
    )

    assert calls[0]["error"] == "LLMPurposeUnconfigured: bu amac icin kayitli bir secim yok"


async def test_log_llm_call_never_raises_when_logging_itself_fails(monkeypatch):
    """Kesin kural (REFACTOR_PLAN.md Adim 3): loglama basarisiz olursa LLM
    cagrisinin kendisi bundan etkilenmemeli -- log_llm_call hicbir zaman
    disariya istisna sizdirmamali, sadece logger.warning ile gecmeli."""

    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.services.token.log_token_usage", _boom)

    # Firlatmadan donmeli:
    await log_llm_call(
        purpose="digest",
        model_name="m",
        provider_id="p",
        status="ok",
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# elapsed_ms: kucuk yardimci
# ---------------------------------------------------------------------------


def test_elapsed_ms_returns_nonnegative_int():
    import time

    start = time.monotonic()
    assert elapsed_ms(start) >= 0
    assert isinstance(elapsed_ms(start), int)
