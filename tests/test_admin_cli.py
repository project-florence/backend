"""Unit tests for ``scripts/admin_cli.py`` -- REFACTOR_PLAN.md Adim 4.

``scripts/`` is not a package, so the module is loaded via
``importlib.util`` from its file path (same technique would apply to any
other script under ``scripts/``). Hermetic: no real Postgres/Redis/network
-- ``fake_db``/``fake_redis`` swap the shared async singletons, ``respx``
mocks the shared httpx client, and DB/network-touching helpers inside
``admin_cli`` (``get_provider_row``, ``_fetch_live_models``,
``set_selection``, ...) are monkeypatched directly on the loaded module so
each test exercises exactly the validation logic it targets.

Covers:
- ``_check_admin_token``: the ADMIN_TOKEN gate (REFACTOR_PLAN.md Adim 4 "A" --
  this is the behaviour that REPLACES the old file's "warn and proceed").
- ``llm model set``'s five pre-write validations (Adim 6.5: singleton, no
  ``--purpose`` anywhere -- ``args.spec`` is a single ``"provider/model"``
  string parsed via ``src.llm.providers.resolve``), in particular that a
  model absent from the live roster is rejected unconditionally
  (2026-08-26's actual failure mode) and that ``--force`` only ever bypasses
  "roster unreachable", never "roster reachable but model absent".
- Secrets never appear in printed output (``llm provider set``).
- ``_confirm`` refuses a destructive action on a non-TTY without ``--yes``.
- ``llm provider rm`` turns a FK violation into a friendly message instead
  of a traceback.
- Small pure helpers: ``_parse_since``, ``_fetch_live_models`` (both known
  live response shapes: OpenAI-style ``data`` and Ollama-style ``models``).
"""

import base64
import importlib.util
import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.llm.crypto as crypto  # noqa: E402

_ADMIN_CLI_PATH = Path(__file__).resolve().parent.parent / "scripts" / "admin_cli.py"
_spec = importlib.util.spec_from_file_location("admin_cli", _ADMIN_CLI_PATH)
admin_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_cli)


class _Args:
    """Minimal stand-in for the argparse.Namespace admin_cli handlers expect."""

    def __init__(self, **kwargs):
        self.admin_token = None
        self.json = False
        self.yes = True
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# ADMIN_TOKEN gate -- REFACTOR_PLAN.md Adim 4 "A": tanimliysa eslesme
# ZORUNLU; tanimsizsa yikici komutlar reddedilir, salt-okunur komutlar calisir.
# ---------------------------------------------------------------------------


def test_admin_token_undefined_allows_read_only(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    args = _Args(admin_token=None)
    assert admin_cli._check_admin_token(args, destructive=False) is None


def test_admin_token_undefined_rejects_destructive(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    args = _Args(admin_token=None)
    assert admin_cli._check_admin_token(args, destructive=True) == 1


def test_admin_token_defined_requires_match_even_for_read_only(monkeypatch):
    """Eski davranistan FARK: token tanimliysa salt-okunur komutlar da eslesme ister."""
    monkeypatch.setenv("ADMIN_TOKEN", "secret123")
    args = _Args(admin_token=None)
    assert admin_cli._check_admin_token(args, destructive=False) == 1


def test_admin_token_defined_correct_value_allows_destructive(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret123")
    args = _Args(admin_token="secret123")
    assert admin_cli._check_admin_token(args, destructive=True) is None


def test_admin_token_defined_wrong_value_rejects(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret123")
    args = _Args(admin_token="wrong")
    assert admin_cli._check_admin_token(args, destructive=True) == 1


def test_admin_token_comparison_is_constant_time(monkeypatch):
    """``hmac.compare_digest`` kullanildigini dogrudan dogrulamak zor (yan
    kanal), ama en azindan cagrildigini teyit ediyoruz -- naif ``==``
    kullanan bir regresyona karsi."""
    calls = []
    real_compare = admin_cli.hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(admin_cli.hmac, "compare_digest", _spy)
    monkeypatch.setenv("ADMIN_TOKEN", "secret123")
    args = _Args(admin_token="secret123")
    admin_cli._check_admin_token(args, destructive=False)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# _confirm: TTY yoksa --yes sart
# ---------------------------------------------------------------------------


def test_confirm_returns_true_immediately_when_yes_flag_set():
    assert admin_cli._confirm("prompt", yes=True) is True


def test_confirm_refuses_on_non_tty_without_yes(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert admin_cli._confirm("prompt", yes=False) is False
    assert "onay" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


def test_parse_since_hours():
    from datetime import datetime

    before = datetime.now(timezone.utc)
    result = admin_cli._parse_since("24h")
    after = datetime.now(timezone.utc)
    assert before - timedelta(hours=24, seconds=1) <= result <= after - timedelta(hours=24) + timedelta(seconds=1)


def test_parse_since_days():
    result = admin_cli._parse_since("7d")
    from datetime import datetime

    delta = datetime.now(timezone.utc) - result
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_parse_since_iso_date():
    result = admin_cli._parse_since("2026-08-20")
    assert result.year == 2026
    assert result.month == 8
    assert result.day == 20


def test_parse_since_invalid_raises():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        admin_cli._parse_since("not-a-date")


# ---------------------------------------------------------------------------
# _fetch_live_models: iki bilinen roster sekli
# ---------------------------------------------------------------------------


def _seed_master_key(monkeypatch) -> None:
    """llm_model_set dogrulama 2'si crypto.decrypt'i DOGRUDAN cagiriyor; testin
    gercek bir ana anahtara ve gercek sifreli veriye ihtiyaci var."""
    monkeypatch.setenv(
        "FLORENCE_MASTER_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )


async def test_fetch_live_models_openai_style():
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").mock(
            return_value=Response(200, json={"data": [{"id": "a-free"}, {"id": "b"}]})
        )
        ids = await admin_cli._fetch_live_models("opencode-zen", "https://opencode.ai/zen/v1/models", None)
    assert ids == ["a-free", "b"]


async def test_fetch_live_models_ollama_style():
    with respx.mock:
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=Response(200, json={"models": [{"name": "llama3:latest"}]})
        )
        ids = await admin_cli._fetch_live_models("ollama-local", "http://localhost:11434/api/tags", None)
    assert ids == ["llama3:latest"]


async def test_fetch_live_models_returns_none_on_network_error():
    with respx.mock:
        respx.get("https://api.openai.com/v1/models").mock(side_effect=httpx.ConnectError("boom"))
        ids = await admin_cli._fetch_live_models("openai", "https://api.openai.com/v1/models", "sk-x")
    assert ids is None


async def test_fetch_live_models_returns_none_on_http_error_status():
    with respx.mock:
        respx.get("https://api.openai.com/v1/models").mock(return_value=Response(401, json={"error": "bad key"}))
        ids = await admin_cli._fetch_live_models("openai", "https://api.openai.com/v1/models", "sk-bad")
    assert ids is None


async def test_fetch_live_models_sends_bearer_header_for_openai_style():
    with respx.mock:
        route = respx.get("https://api.openai.com/v1/models").mock(
            return_value=Response(200, json={"data": [{"id": "gpt-5"}]})
        )
        await admin_cli._fetch_live_models("openai", "https://api.openai.com/v1/models", "sk-real")
    assert route.calls.last.request.headers["Authorization"] == "Bearer sk-real"


async def test_fetch_live_models_sends_anthropic_headers():
    with respx.mock:
        route = respx.get("https://api.anthropic.com/v1/models").mock(
            return_value=Response(200, json={"data": [{"id": "claude-x"}]})
        )
        await admin_cli._fetch_live_models("anthropic", "https://api.anthropic.com/v1/models", "sk-ant")
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "sk-ant"
    assert "Authorization" not in req.headers


# ---------------------------------------------------------------------------
# llm model set: bes dogrulama (Adim 6.5: singleton, args.spec = "provider/model",
# --purpose YOK -- set_selection artik (provider, model) alir, purpose almaz)
# ---------------------------------------------------------------------------


def _provider_row(provider_id: str, *, has_key: bool, master_key_env: str | None = None) -> dict:
    encrypted = None
    if has_key:
        assert master_key_env is not None
        encrypted = crypto.encrypt("sk-test-key", aad=provider_id)
    return {
        "provider": provider_id,
        "api_key_encrypted": encrypted,
        "base_url": None,
        "enabled": True,
        "created_at": None,
        "updated_at": None,
    }


async def test_llm_model_set_rejects_unknown_provider(capsys):
    args = _Args(spec="not-a-real-provider/x", reasoning=None, timeout=None, force=False)
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert "bilinmeyen saglayici" in capsys.readouterr().out


async def test_llm_model_set_rejects_spec_without_slash(capsys):
    args = _Args(spec="no-slash-here", reasoning=None, timeout=None, force=False)
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert "HATA" in capsys.readouterr().out


async def test_llm_model_set_keyless_provider_without_row_is_rejected(monkeypatch, capsys):
    """Adim 4'te bulunan gercek bir bosluk: anahtarsiz saglayicilar bile
    llm_providers'ta bir SATIR gerektirir (llm_settings.provider FK'si) --
    ``llm provider set <id>`` hic calistirilmadiysa reddedilmeli."""

    async def _no_row(provider_id):
        return None

    monkeypatch.setattr(admin_cli, "get_provider_row", _no_row)
    args = _Args(spec="opencode-zen/deepseek-v4-flash-free", reasoning=None, timeout=None, force=False)
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    out = capsys.readouterr().out
    assert "llm provider set opencode-zen" in out


async def test_llm_model_set_rejects_missing_key_for_key_requiring_provider(monkeypatch, capsys):
    async def _no_key_row(provider_id):
        return {"provider": provider_id, "api_key_encrypted": None, "base_url": None, "enabled": True}

    monkeypatch.setattr(admin_cli, "get_provider_row", _no_key_row)
    args = _Args(spec="openai/gpt-5", reasoning=None, timeout=None, force=False)
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert "kayitli bir API anahtari yok" in capsys.readouterr().out


async def test_llm_model_set_rejects_model_not_in_live_roster(monkeypatch, capsys):
    """2026-08-26 arizasinin tam olarak yakalanmasi gereken durum."""

    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster(provider_id, models_url, api_key):
        return ["deepseek-v4-flash-free", "some-other-model"]

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)

    args = _Args(
        spec="opencode-zen/ox-alpha-free",
        reasoning=None, timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    out = capsys.readouterr().out
    assert "canli roster'inda YOK" in out


async def test_llm_model_set_force_does_not_bypass_model_absent_from_reachable_roster(monkeypatch, capsys):
    """--force yalniz 'roster'a erisilemedi' durumunu atlar; roster
    erisilebilir ve model orada YOKSA --force bile yazmaya izin vermemeli."""

    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster(provider_id, models_url, api_key):
        return ["some-other-model"]

    set_selection_calls = []

    async def _fake_set_selection(*a, **kw):
        set_selection_calls.append((a, kw))

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)
    monkeypatch.setattr(admin_cli, "set_selection", _fake_set_selection)

    args = _Args(
        spec="opencode-zen/ox-alpha-free",
        reasoning=None, timeout=None, force=True,  # --force verildi
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert not set_selection_calls  # yazmaya HIC gidilmedi


async def test_llm_model_set_force_bypasses_unreachable_roster(monkeypatch, capsys):
    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster_unreachable(provider_id, models_url, api_key):
        return None  # ag hatasi

    set_selection_calls = []

    async def _fake_set_selection(provider, model, *, params=None, updated_by=None):
        set_selection_calls.append((provider, model, params))

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster_unreachable)
    monkeypatch.setattr(admin_cli, "set_selection", _fake_set_selection)

    args = _Args(
        spec="opencode-zen/deepseek-v4-flash-free",
        reasoning=None, timeout=None, force=True,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 0
    assert len(set_selection_calls) == 1


async def test_llm_model_set_without_force_rejects_unreachable_roster(monkeypatch, capsys):
    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster_unreachable(provider_id, models_url, api_key):
        return None

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster_unreachable)

    args = _Args(
        spec="opencode-zen/deepseek-v4-flash-free",
        reasoning=None, timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 1


async def test_llm_model_set_rejects_reasoning_for_provider_without_reasoning_param(monkeypatch, capsys):
    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster(provider_id, models_url, api_key):
        return ["deepseek-v4-flash-free"]

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)

    # opencode-zen: reasoning_param is None
    args = _Args(
        spec="opencode-zen/deepseek-v4-flash-free",
        reasoning="medium", timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert "reasoning_param'i yok" in capsys.readouterr().out


async def test_llm_model_set_rejects_reasoning_value_outside_accepted_set(monkeypatch, capsys, monkeypatch_master_key):
    async def _key_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": crypto.encrypt("sk-x", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _roster(provider_id, models_url, api_key):
        return ["gpt-5"]

    monkeypatch.setattr(admin_cli, "get_provider_row", _key_row)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)

    # openai reasoning_values = {"minimal", "low", "medium", "high"} -- "ultra" gecersiz.
    args = _Args(
        spec="openai/gpt-5",
        reasoning="ultra", timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 1
    assert "gecerli degil" in capsys.readouterr().out


async def test_llm_model_set_warns_but_allows_reasoning_override(
    monkeypatch, capsys, monkeypatch_master_key
):
    """5. dogrulama: uyar, ENGELLEME (admin override kazanir). Adim 6.5: tek
    ayar digest+report tarafindan PAYLASILDIGI icin -- ve ikisi de
    yapilandirilmis cikti kullandigi icin -- bu uyari artik amac
    parametresi almadan, kosulsuz basiliyor."""

    async def _key_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": crypto.encrypt("sk-x", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _roster(provider_id, models_url, api_key):
        return ["gpt-5"]

    set_selection_calls = []

    async def _fake_set_selection(provider, model, *, params=None, updated_by=None):
        set_selection_calls.append((provider, model, params))

    monkeypatch.setattr(admin_cli, "get_provider_row", _key_row)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)
    monkeypatch.setattr(admin_cli, "set_selection", _fake_set_selection)

    args = _Args(
        spec="openai/gpt-5",
        reasoning="medium", timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 0
    out = capsys.readouterr().out
    assert "yapilandirilmis cikti kullaniyor" in out  # uyari basildi
    assert len(set_selection_calls) == 1  # ama yazma ENGELLENMEDI
    assert set_selection_calls[0][2] == {"reasoning": "medium"}


async def test_llm_model_set_writes_selection_on_full_success(monkeypatch):
    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster(provider_id, models_url, api_key):
        return ["deepseek-v4-flash-free"]

    set_selection_calls = []

    async def _fake_set_selection(provider, model, *, params=None, updated_by=None):
        set_selection_calls.append((provider, model, params))

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)
    monkeypatch.setattr(admin_cli, "set_selection", _fake_set_selection)

    args = _Args(
        spec="opencode-zen/deepseek-v4-flash-free",
        reasoning=None, timeout=30.0, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 0
    assert set_selection_calls == [("opencode-zen", "deepseek-v4-flash-free", {"timeout": 30.0})]


async def test_llm_model_set_model_id_with_embedded_slash_parses_correctly(monkeypatch):
    """OpenRouter model id'leri kendi iclerinde '/' icerebilir -- src.llm.providers.resolve
    yalniz ILK ayraci saglayici/model sinirini belirlemek icin kullanir."""
    _seed_master_key(monkeypatch)

    async def _keyed_row(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": admin_cli.crypto.encrypt("sk-test-key", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    async def _decrypted(provider_id):
        return "sk-test-key"

    async def _roster(provider_id, models_url, api_key):
        return ["anthropic/claude-3.5-sonnet"]

    set_selection_calls = []

    async def _fake_set_selection(provider, model, *, params=None, updated_by=None):
        set_selection_calls.append((provider, model, params))

    monkeypatch.setattr(admin_cli, "get_provider_row", _keyed_row)
    monkeypatch.setattr(admin_cli, "_resolve_decrypted_key", _decrypted)
    monkeypatch.setattr(admin_cli, "_fetch_live_models", _roster)
    monkeypatch.setattr(admin_cli, "set_selection", _fake_set_selection)

    args = _Args(
        spec="openrouter/anthropic/claude-3.5-sonnet",
        reasoning=None, timeout=None, force=False,
    )
    result = await admin_cli.llm_model_set(args)
    assert result == 0
    assert set_selection_calls == [("openrouter", "anthropic/claude-3.5-sonnet", {})]


# ---------------------------------------------------------------------------
# Secretler cikti icinde asla gorunmez
# ---------------------------------------------------------------------------


@pytest.fixture
def monkeypatch_master_key(monkeypatch):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


async def test_llm_provider_set_never_prints_raw_secret(monkeypatch, capsys, monkeypatch_master_key):
    upsert_calls = []

    async def _fake_upsert(provider, *, api_key=None, base_url=None, enabled=True):
        upsert_calls.append((provider, api_key, base_url, enabled))

    async def _row_after_write(provider_id):
        return {
            "provider": provider_id,
            "api_key_encrypted": crypto.encrypt("sk-super-secret-raw-value", aad=provider_id),
            "base_url": None,
            "enabled": True,
        }

    monkeypatch.setattr(admin_cli, "upsert_provider", _fake_upsert)
    monkeypatch.setattr(admin_cli, "get_provider_row", _row_after_write)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "sk-super-secret-raw-value\n")

    args = _Args(provider="openai", base_url=None)
    result = await admin_cli.llm_provider_set(args)

    assert result == 0
    assert upsert_calls == [("openai", "sk-super-secret-raw-value", None, True)]
    out = capsys.readouterr().out
    assert "sk-super-secret-raw-value" not in out
    assert "…" in out  # maskeli kuyruk basildi


def test_read_secret_optional_reads_piped_line(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "sk-piped-value\n")
    assert admin_cli._read_secret_optional("prompt: ") == "sk-piped-value"


def test_read_secret_optional_empty_line_means_skip(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "\n")
    assert admin_cli._read_secret_optional("prompt: ") is None


# ---------------------------------------------------------------------------
# llm provider rm: FK ihlali dostane mesaja donusur
# ---------------------------------------------------------------------------


async def test_llm_provider_rm_reports_fk_violation_as_friendly_message(monkeypatch, capsys):
    import psycopg

    async def _boom(provider_id):
        raise psycopg.errors.ForeignKeyViolation("still referenced")

    monkeypatch.setattr(admin_cli, "remove_provider", _boom)
    args = _Args(provider="opencode-zen")
    result = await admin_cli.llm_provider_rm(args)
    assert result == 1
    out = capsys.readouterr().out
    assert "TEK model ayarinin" in out
    assert "Traceback" not in out


async def test_llm_provider_rm_succeeds_when_unreferenced(monkeypatch, capsys):
    async def _ok(provider_id):
        return None

    monkeypatch.setattr(admin_cli, "remove_provider", _ok)
    args = _Args(provider="opencode-zen")
    result = await admin_cli.llm_provider_rm(args)
    assert result == 0
    assert "silindi" in capsys.readouterr().out
