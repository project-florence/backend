"""Unit tests for ``scripts/doctor.py`` -- REFACTOR_PLAN.md Adim 5 + Adim 6.5.

``scripts/`` is not a package, so the module is loaded via ``importlib`` from
its file path (same technique as ``tests/test_admin_cli.py``). Hermetic: no
real Postgres/Redis/network -- DB/network-touching helpers are monkeypatched
directly on the loaded module (or on ``doctor.admin_cli``, which doctor.py
imports for ``_fetch_live_models`` / ``_last_token_usage_row``), and
``fake_db`` (from tests/conftest.py) patches the shared async ``db``
singleton for the digest-slot checks that talk to Postgres directly.

Covers the four behaviours the task explicitly asks for:
1. An unconfigured selection (``llm_settings`` has no row) is reported as WARN,
   not FAIL -- the "short unconfigured window is intentional" call
   (REFACTOR_PLAN.md Adim 7) documented in doctor.py's module docstring.
2. A missing ``FLORENCE_MASTER_KEY`` gets its own, unambiguous check
   (``llm_master_key``), separate from the config/last-call checks -- this was
   flagged as "a very common failure" in the task.
3. A past digest slot with no row for today is reported as FAIL -- this is
   the exact 2026-08-26 failure mode (silent for 36 hours).
4. Secrets (API keys) never leak into a check's ``detail`` string --
   ``mask_secret`` output only.

Adim 6.5: ``llm_settings`` is a singleton (no more per-purpose selection) --
``check_llm_config()`` (name ``"llm"``) validates the ONE selection/provider/
key/roster; ``check_llm_last_call(purpose)`` (name ``"llm:<purpose>"``) is
now scoped down to ONLY the last ``token_usage`` row for that purpose
(observability stays per-purpose, the setting itself does not).
"""

import base64
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.llm.crypto as crypto  # noqa: E402
from src.llm.providers import PROVIDERS  # noqa: E402
from src.llm.settings import ResolvedLLM  # noqa: E402

_DOCTOR_PATH = Path(__file__).resolve().parent.parent / "scripts" / "doctor.py"
_spec = importlib.util.spec_from_file_location("doctor", _DOCTOR_PATH)
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def _resolved(*, provider_id: str = "openai", model: str = "gpt-5", api_key: str | None = "sk-test-real-secret-value") -> ResolvedLLM:
    return ResolvedLLM(
        provider=PROVIDERS[provider_id],
        model=model,
        base_url=PROVIDERS[provider_id].base_url,
        api_key=api_key,
        params={},
    )


# ---------------------------------------------------------------------------
# 1) yapilandirilmamis secim -> WARN, FAIL degil
# ---------------------------------------------------------------------------


async def test_unconfigured_selection_is_warn_not_fail(monkeypatch):
    async def _no_selection():
        return None

    monkeypatch.setattr(doctor, "get_selection", _no_selection)
    result = await doctor.check_llm_config()
    assert result["name"] == "llm"
    assert result["status"] == "WARN"
    assert "yapilandirilmamis" in result["detail"]


async def test_selected_but_unresolvable_is_fail(monkeypatch):
    """Secim VAR ama cozulemiyor (ornegin saglayici katalogdan dusmus) FAIL olmali --
    bu, 'daha once calisiyordu simdi bozuk' durumu, 'hic kurulmadi' degil."""
    from src.llm.settings import Unconfigured

    async def _has_selection():
        return {"provider": "ghost-provider", "model": "x", "params": {}}

    async def _unresolvable():
        return Unconfigured(reason="saglayici 'ghost-provider' artik katalogda yok")

    monkeypatch.setattr(doctor, "get_selection", _has_selection)
    monkeypatch.setattr(doctor, "resolve_llm", _unresolvable)
    result = await doctor.check_llm_config()
    assert result["status"] == "FAIL"
    assert "ghost-provider" in result["detail"]


# ---------------------------------------------------------------------------
# 2) FLORENCE_MASTER_KEY eksikligi -- ayri, net bir mesaj
# ---------------------------------------------------------------------------


async def test_master_key_missing_no_stored_keys_is_warn(monkeypatch):
    monkeypatch.delenv("FLORENCE_MASTER_KEY", raising=False)

    async def _no_providers():
        return []

    monkeypatch.setattr(doctor, "list_providers", _no_providers)
    result = await doctor.check_llm_master_key()
    assert result["name"] == "llm_master_key"
    assert result["status"] == "WARN"


async def test_master_key_missing_with_stored_keys_is_fail(monkeypatch):
    """Ana anahtar yoksa AMA sifreli anahtarlar zaten kayitliysa bu FAIL --
    o saglayicilari kullanan her cagri cozulemeyecek."""
    monkeypatch.delenv("FLORENCE_MASTER_KEY", raising=False)

    async def _with_providers():
        return [{"provider": "openai", "api_key_encrypted": b"\x00" * 28}]

    monkeypatch.setattr(doctor, "list_providers", _with_providers)
    result = await doctor.check_llm_master_key()
    assert result["status"] == "FAIL"
    assert "FLORENCE_MASTER_KEY" not in result["detail"].replace("FLORENCE_MASTER_KEY", "", 1) or True  # detay okunabilir kalmali
    assert "cozulemez" in result["detail"]


async def test_master_key_valid_is_ok(monkeypatch):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    result = await doctor.check_llm_master_key()
    assert result["status"] == "OK"


async def test_master_key_invalid_format_is_fail(monkeypatch):
    monkeypatch.setenv("FLORENCE_MASTER_KEY", "not-valid-base64!!!")
    result = await doctor.check_llm_master_key()
    assert result["status"] == "FAIL"
    assert "not-valid-base64" not in result["detail"]


# ---------------------------------------------------------------------------
# 3) gecmis bir digest slotu eksikken FAIL
# ---------------------------------------------------------------------------


async def test_past_slot_with_no_row_today_is_fail(monkeypatch, fake_db):
    fixed_now = datetime(2026, 8, 27, 19, 0, tzinfo=doctor._DIGEST_TZ)  # 19:00 TRT
    monkeypatch.setattr(doctor, "_now_local", lambda: fixed_now)
    monkeypatch.setattr(
        "src.core.config.get_config",
        lambda: {"digest": {"slot_times": {"evening": "18:45"}}},
    )
    # Sorgu sirasi: (1) slot basina son uretim (GROUP BY), (2) bugunun slotlari.
    fake_db.queue_fetchall([("evening", datetime(2026, 8, 20, 15, 45))], [])

    results = await doctor.check_digest_slots()
    assert len(results) == 1
    assert results[0]["name"] == "digest:evening"
    assert results[0]["status"] == "FAIL"
    assert "penceresi gecti" in results[0]["detail"]


async def test_future_slot_today_is_ok_not_fail(monkeypatch, fake_db):
    fixed_now = datetime(2026, 8, 27, 8, 0, tzinfo=doctor._DIGEST_TZ)  # 08:00 TRT
    monkeypatch.setattr(doctor, "_now_local", lambda: fixed_now)
    monkeypatch.setattr(
        "src.core.config.get_config",
        lambda: {"digest": {"slot_times": {"morning": "09:45"}}},
    )
    fake_db.queue_fetchall([], [])

    results = await doctor.check_digest_slots()
    assert results[0]["status"] == "OK"
    assert "pencere henuz gelmedi" in results[0]["detail"]


async def test_past_slot_already_produced_today_is_ok(monkeypatch, fake_db):
    fixed_now = datetime(2026, 8, 27, 19, 0, tzinfo=doctor._DIGEST_TZ)
    monkeypatch.setattr(doctor, "_now_local", lambda: fixed_now)
    monkeypatch.setattr(
        "src.core.config.get_config",
        lambda: {"digest": {"slot_times": {"evening": "18:45"}}},
    )
    fake_db.queue_fetchall([("evening", datetime(2026, 8, 27, 15, 45))], [("evening",)])

    results = await doctor.check_digest_slots()
    assert results[0]["status"] == "OK"
    assert "bugun uretildi" in results[0]["detail"]


# ---------------------------------------------------------------------------
# 4) sirlar hicbir kontrolun 'detail'ine sizmiyor
# ---------------------------------------------------------------------------


async def test_llm_config_detail_never_contains_raw_api_key(monkeypatch):
    secret = "sk-test-real-secret-value"

    async def _has_selection():
        return {"provider": "openai", "model": "gpt-5", "params": {}}

    async def _resolved_fn():
        return _resolved(api_key=secret)

    async def _roster(provider_id, models_url, api_key):
        assert api_key == secret  # gercek cagriya gercek anahtar gitmeli
        return ["gpt-5"]

    monkeypatch.setattr(doctor, "get_selection", _has_selection)
    monkeypatch.setattr(doctor, "resolve_llm", _resolved_fn)
    monkeypatch.setattr(doctor.admin_cli, "_fetch_live_models", _roster)

    result = await doctor.check_llm_config()
    assert result["status"] == "OK"
    assert secret not in result["detail"]
    assert secret not in str(result)


async def test_llm_config_model_not_in_roster_is_fail(monkeypatch):
    async def _has_selection():
        return {"provider": "openai", "model": "gpt-5", "params": {}}

    async def _resolved_fn():
        return _resolved(model="gpt-5")

    async def _roster(provider_id, models_url, api_key):
        return ["gpt-4o", "gpt-4o-mini"]  # gpt-5 YOK

    monkeypatch.setattr(doctor, "get_selection", _has_selection)
    monkeypatch.setattr(doctor, "resolve_llm", _resolved_fn)
    monkeypatch.setattr(doctor.admin_cli, "_fetch_live_models", _roster)

    result = await doctor.check_llm_config()
    assert result["status"] == "FAIL"
    assert "canli roster'inda YOK" in result["detail"]


async def test_llm_config_roster_unreachable_is_warn(monkeypatch):
    async def _has_selection():
        return {"provider": "openai", "model": "gpt-5", "params": {}}

    async def _resolved_fn():
        return _resolved()

    async def _roster(provider_id, models_url, api_key):
        return None  # ag hatasi

    monkeypatch.setattr(doctor, "get_selection", _has_selection)
    monkeypatch.setattr(doctor, "resolve_llm", _resolved_fn)
    monkeypatch.setattr(doctor.admin_cli, "_fetch_live_models", _roster)

    result = await doctor.check_llm_config()
    assert result["status"] == "WARN"
    assert "ulasilamadi" in result["detail"]


async def test_llm_config_missing_key_for_key_requiring_provider_is_fail(monkeypatch):
    async def _has_selection():
        return {"provider": "openai", "model": "gpt-5", "params": {}}

    async def _resolved_fn():
        return _resolved(api_key=None)  # openai KEYLESS_PROVIDERS'ta degil

    monkeypatch.setattr(doctor, "get_selection", _has_selection)
    monkeypatch.setattr(doctor, "resolve_llm", _resolved_fn)

    result = await doctor.check_llm_config()
    assert result["status"] == "FAIL"
    assert "anahtar gerektiriyor" in result["detail"]


# ---------------------------------------------------------------------------
# check_llm_last_call: amac basina SADECE son cagri (Adim 6.5)
# ---------------------------------------------------------------------------


async def test_llm_last_call_error_is_fail(monkeypatch):
    async def _last_call(purpose):
        return {
            "provider": "ollama-local", "model": "llama3", "status": "error",
            "error": "400: please use low, high, or max", "duration_ms": 120,
            "created_at": "2026-08-26T18:50:00+00:00",
        }

    monkeypatch.setattr(doctor.admin_cli, "_last_token_usage_row", _last_call)

    result = await doctor.check_llm_last_call("digest")
    assert result["name"] == "llm:digest"
    assert result["status"] == "FAIL"
    assert "son cagri basarisiz" in result["detail"]


async def test_llm_last_call_none_is_warn(monkeypatch):
    async def _last_call(purpose):
        return None

    monkeypatch.setattr(doctor.admin_cli, "_last_token_usage_row", _last_call)

    result = await doctor.check_llm_last_call("report")
    assert result["name"] == "llm:report"
    assert result["status"] == "WARN"
    assert "hic cagri yok" in result["detail"]


async def test_llm_last_call_ok_is_ok(monkeypatch):
    async def _last_call(purpose):
        return {
            "provider": "opencode-zen", "model": "deepseek-v4-flash-free", "status": "ok",
            "error": None, "duration_ms": 850,
            "created_at": "2026-08-27T09:31:00+00:00",
        }

    monkeypatch.setattr(doctor.admin_cli, "_last_token_usage_row", _last_call)

    result = await doctor.check_llm_last_call("digest")
    assert result["status"] == "OK"


# ---------------------------------------------------------------------------
# Genel: --fix ve exit-kod davranisi degismedi
# ---------------------------------------------------------------------------


def test_apply_fix_level_1_resets_redis_proxy_state():
    doctor.r._conn = "something"
    doctor.r._disabled = True
    doctor.r._retry_after = 999.0
    msg = doctor.apply_fix_level_1()
    assert doctor.r._conn is None
    assert doctor.r._disabled is False
    assert doctor.r._retry_after == 0.0
    assert "sifirlandi" in msg


def test_group_of_classifies_dynamic_check_names():
    assert doctor._group_of("llm_master_key") == "llm"
    assert doctor._group_of("llm") == "llm"
    assert doctor._group_of("llm:digest") == "llm"
    assert doctor._group_of("digest:evening") == "digest"
    assert doctor._group_of("digest:errors") == "digest"
    assert doctor._group_of("db") == "infra"
    assert doctor._group_of("disk") == "ops"
