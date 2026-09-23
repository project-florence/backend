"""Unit tests for src/services/market.py -- alim-satim kapisi.

Tamamen hermetik: ``get_market_status``/``is_holiday``/``next_open_at``
sync ve yan etkisiz (herhangi bir DB/Redis erisimi yok). Sadece
``get_market_status_payload`` Redis kullanir (``fake_redis``).
"""

from datetime import date, datetime, timedelta

import pytest

import src.services.market as market_module

TZ = market_module.MARKET_TIMEZONE


def _ist(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Gunluk saat siniri: Pzt-Cum 10:00-18:10 Istanbul, dakika hassasiyetinde.
# 2026-08-24 kasitli secildi: sıradan bir Pazartesi, tatil degil.
# ---------------------------------------------------------------------------


def test_closed_one_minute_before_open():
    assert market_module.get_market_status(_ist(2026, 8, 24, 9, 59)) == "closed"


def test_open_exactly_at_open():
    assert market_module.get_market_status(_ist(2026, 8, 24, 10, 0)) == "open"


def test_open_one_minute_before_close():
    assert market_module.get_market_status(_ist(2026, 8, 24, 18, 9)) == "open"


def test_closed_exactly_at_close_boundary():
    # MARKET_CLOSE ust sinir HARIC (`< MARKET_CLOSE`) -- 18:10'da kapali.
    assert market_module.get_market_status(_ist(2026, 8, 24, 18, 10)) == "closed"


def test_closed_one_minute_after_close():
    assert market_module.get_market_status(_ist(2026, 8, 24, 18, 11)) == "closed"


def test_open_at_midday():
    assert market_module.get_market_status(_ist(2026, 8, 24, 13, 0)) == "open"


# ---------------------------------------------------------------------------
# Hafta sonu: gun ne olursa olsun kapali.
# ---------------------------------------------------------------------------


def test_saturday_closed_during_normal_hours():
    assert market_module.get_market_status(_ist(2026, 8, 22, 13, 0)) == "closed"


def test_sunday_closed_during_normal_hours():
    assert market_module.get_market_status(_ist(2026, 8, 23, 13, 0)) == "closed"


# ---------------------------------------------------------------------------
# TR_HOLIDAYS_2026: resmi tatil gunu, hafta ici ve saatler icinde olsa bile kapali.
# ---------------------------------------------------------------------------


def test_holiday_closed_during_normal_hours():
    # 2026-01-01 (Yilbasi) bir Persembe -- hafta ici ama tatil.
    assert date(2026, 1, 1).weekday() < 5
    assert market_module.is_holiday(date(2026, 1, 1)) == "Yılbaşı"
    assert market_module.get_market_status(_ist(2026, 1, 1, 12, 0)) == "closed"


def test_non_holiday_weekday_is_not_flagged():
    assert market_module.is_holiday(date(2026, 8, 24)) is None


def test_all_2026_holidays_are_closed_at_midday():
    for day in market_module.TR_HOLIDAYS_2026:
        status = market_module.get_market_status(
            datetime(day.year, day.month, day.day, 13, 0, tzinfo=TZ)
        )
        assert status == "closed", f"{day} resmi tatil ama piyasa 'acik' donuyor"


def test_holiday_calendar_is_hand_written_and_scoped_to_2026_only():
    """``TR_HOLIDAYS_2026`` elle yazilmis ve YALNIZCA 2026 icin tanimli
    (CLAUDE.md: "Yila ozel, her yil guncellenmeli"). Bu test o sinirlamayi
    GIZLEMEK yerine gorunur kilar: 2027 Yilbasi (2027-01-01, Persembe --
    hafta ici) bu takvimde YOK, bu yuzden piyasa YANLISLIKLA 'acik'
    hesaplanir. Bu MEVCUT (bilinen, kasitli olarak duzeltilmeyen) davranistir
    -- takvim her yil elle guncellenmezse sessizce tekrarlanacak bir hata
    sinifini belgeler.
    """
    next_new_year = date(2027, 1, 1)
    assert next_new_year.weekday() < 5  # hafta ici
    assert market_module.is_holiday(next_new_year) is None  # takvimde yok (BEKLENEN ACIK BULGU)
    assert market_module.get_market_status(_ist(2027, 1, 1, 12, 0)) == "open"  # YANLIS ama mevcut


# ---------------------------------------------------------------------------
# next_open_at
# ---------------------------------------------------------------------------


def test_next_open_at_returns_none_when_already_open():
    assert market_module.next_open_at(_ist(2026, 8, 24, 12, 0)) is None


def test_next_open_at_same_day_before_open():
    result = market_module.next_open_at(_ist(2026, 8, 24, 9, 0))
    assert result == _ist(2026, 8, 24, 10, 0)


def test_next_open_at_after_close_rolls_to_next_business_day():
    # 2026-08-24 Pazartesi 19:00 (kapanistan sonra) -> Sali 10:00.
    result = market_module.next_open_at(_ist(2026, 8, 24, 19, 0))
    assert result == _ist(2026, 8, 25, 10, 0)


def test_next_open_at_friday_evening_rolls_to_monday():
    # 2026-08-21 Cuma 19:00 -> hafta sonu atlanir, 2026-08-24 Pazartesi 10:00.
    friday = _ist(2026, 8, 21, 19, 0)
    assert friday.weekday() == 4
    result = market_module.next_open_at(friday)
    assert result == _ist(2026, 8, 24, 10, 0)


def test_next_open_at_skips_holiday():
    # 2025-12-31 Persembe 19:00 -> 2026-01-01 tatil (Yilbasi), 2026-01-02'ye atlar.
    thursday_before_holiday = _ist(2025, 12, 31, 19, 0)
    result = market_module.next_open_at(thursday_before_holiday)
    assert result == _ist(2026, 1, 2, 10, 0)


# ---------------------------------------------------------------------------
# last_trading_day: d'den KESINLIKLE onceki islem gunu (hafta sonu + tatil atlanir)
# ---------------------------------------------------------------------------


def test_last_trading_day_rolls_back_over_weekend():
    # 2026-09-21 Pazartesi -> Cuma 2026-09-18 (hafta sonu atlanir).
    assert market_module.last_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)
    # Pazar gununden de ayni Cuma'ya.
    assert market_module.last_trading_day(date(2026, 9, 20)) == date(2026, 9, 18)


def test_last_trading_day_skips_holiday():
    # 2026-01-02 Cuma -> 2026-01-01 tatil (Yilbasi) atlanir -> 2025-12-31 Carsamba.
    assert market_module.is_holiday(date(2026, 1, 1)) == "Yılbaşı"
    assert market_module.last_trading_day(date(2026, 1, 2)) == date(2025, 12, 31)


def test_last_trading_day_guard_raises_when_calendar_is_all_holidays(monkeypatch):
    """30 gunluk pencerenin tamami tatil sayilirsa sonsuz dongu yerine hata."""
    start = date(2026, 9, 23)
    forced = {(start - timedelta(days=i)): "forced" for i in range(0, 40)}
    monkeypatch.setattr(market_module, "TR_HOLIDAYS_2026", forced)
    with pytest.raises(ValueError):
        market_module.last_trading_day(start)


# ---------------------------------------------------------------------------
# expected_last_session_date: kapanis oncesi/sonrasi
# ---------------------------------------------------------------------------


def test_expected_last_session_date_before_close_on_normal_monday():
    # 2026-08-24 Pazartesi 15:00 -> seans suruyor, onceki Cuma tamamlanmis.
    assert market_module.expected_last_session_date(_ist(2026, 8, 24, 15, 0)) == date(2026, 8, 21)


def test_expected_last_session_date_at_or_after_close_returns_today():
    # Kapanis sinirinda (18:10) ve sonrasinda bugunun seansi tamamlanmis sayilir.
    assert market_module.expected_last_session_date(_ist(2026, 8, 24, 18, 10)) == date(2026, 8, 24)
    assert market_module.expected_last_session_date(_ist(2026, 8, 24, 18, 30)) == date(2026, 8, 24)


def test_expected_last_session_date_on_holiday_falls_back_to_previous_day():
    # 2026-01-01 Persembe tatil -> 2025-12-31 Carsamba.
    assert market_module.expected_last_session_date(_ist(2026, 1, 1, 12, 0)) == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# get_market_status_payload: Redis onbellegi (fake_redis)
# ---------------------------------------------------------------------------


async def test_payload_cache_miss_computes_and_caches(fake_redis):
    payload = await market_module.get_market_status_payload(_ist(2026, 8, 24, 12, 0))
    assert payload["open"] is True
    assert payload["is_holiday"] is False
    assert payload["holiday_name"] is None
    cached_raw = await fake_redis.get(market_module._CACHE_KEY)
    assert cached_raw is not None


async def test_payload_cache_hit_returns_cached_value_unchanged(fake_redis, monkeypatch):
    import json

    stale_payload = {
        "open": False,
        "next_open_at": None,
        "timezone": "Europe/Istanbul",
        "is_holiday": True,
        "holiday_name": "STALE-CACHE-MARKER",
        "as_of": "2020-01-01T00:00:00+03:00",
    }
    await fake_redis.set(market_module._CACHE_KEY, json.dumps(stale_payload))

    # Gercek zamana bakilmaksizin onbellekteki (bayat) deger donmeli.
    payload = await market_module.get_market_status_payload(_ist(2026, 8, 24, 12, 0))
    assert payload["holiday_name"] == "STALE-CACHE-MARKER"


async def test_payload_holiday_fields_populated_on_holiday(fake_redis):
    payload = await market_module.get_market_status_payload(_ist(2026, 1, 1, 12, 0))
    assert payload["open"] is False
    assert payload["is_holiday"] is True
    assert payload["holiday_name"] == "Yılbaşı"


async def test_payload_tolerates_redis_get_failure(fake_redis, monkeypatch):
    """Redis proxy down ise (get patlarsa) endpoint yine de hesaplayip donmeli.

    ``fake_redis`` fixture'i ``redis_module.r.get``'i ``fr.get``'in BAGLI
    (bound) bir referansiyla degistirir (bkz. conftest.py) -- sadece
    ``fake_redis`` nesnesinin ozniteligini degistirmek bu baglantiyi
    etkilemez, dogrudan ``redis_module.r`` uzerinde yamalamak gerekir.
    """
    from src.core import redis as redis_module

    async def _boom(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_module.r, "get", _boom)
    payload = await market_module.get_market_status_payload(_ist(2026, 8, 24, 12, 0))
    assert payload["open"] is True


async def test_payload_tolerates_redis_set_failure(fake_redis, monkeypatch):
    """Onbellege yazma patlarsa bile hesaplanan payload yine donmeli (yutulur)."""
    from src.core import redis as redis_module

    async def _boom(key, value, ex=None, nx=False, xx=False):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_module.r, "set", _boom)
    payload = await market_module.get_market_status_payload(_ist(2026, 8, 24, 12, 0))
    assert payload["open"] is True


# ---------------------------------------------------------------------------
# next_open_at: 14 gunluk arama siniri
# ---------------------------------------------------------------------------


def test_next_open_at_returns_none_when_no_open_day_within_14_days(monkeypatch):
    """Onumuzdeki 14 gunun TAMAMI tatil sayilirsa (uc durum) None doner --
    fonksiyonun sonsuz donguye girmedigini ve acikca vazgectigini dogrular."""
    from datetime import timedelta as _td

    start = _ist(2026, 8, 24, 19, 0)  # kapanistan sonra, aramaya buradan baslar
    forced_holidays = {
        (start.date() + _td(days=i)): "forced-holiday" for i in range(0, 16)
    }
    monkeypatch.setattr(market_module, "TR_HOLIDAYS_2026", forced_holidays)

    assert market_module.next_open_at(start) is None
