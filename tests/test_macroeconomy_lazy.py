"""FRED lazy initialization — import must never require FRED_API_KEY.

Design spec 8.4: the import-time ``ValueError`` is gone; the application
boots without the key and ``_get_fred()`` stays ``None`` until a key is
actually configured.
"""

import pytest


def _neutralize_dotenv(monkeypatch):
    """Stop load_dotenv() from re-injecting the real .env FRED key.

    ``src/clients/macroeconomy.py:52`` calls ``load_dotenv()`` at import time,
    so importing the module (or re-exec'ing it in the cold-reimport test) would
    reload ``FRED_API_KEY`` from a local ``.env`` and clobber the env isolation.
    Patching the ``dotenv`` module attribute — not the macroeconomy module's
    local binding, which ``exec_module`` re-imports — keeps both code paths
    deterministic regardless of a local ``.env`` presence.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)


def test_module_imports_without_key_and_fred_stays_lazy(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    _neutralize_dotenv(monkeypatch)
    import src.clients.macroeconomy as macro

    macro._fred = None  # deterministic regardless of prior tests
    assert macro._get_fred() is None
    assert macro._fred is None  # no client constructed at import time
    assert callable(macro.get_macroeconomy_data)


def test_cold_reimport_without_key_is_harmless(monkeypatch):
    """Simulate a fresh process boot: module exec must not raise without the key."""
    import importlib.util

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    _neutralize_dotenv(monkeypatch)
    spec = importlib.util.find_spec("src.clients.macroeconomy")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not raise (no ValueError on import)
    assert module._get_fred() is None


def test_fred_initialized_only_when_key_present(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "unit-test-key")
    import src.clients.macroeconomy as macro

    macro._fred = None
    try:
        client = macro._get_fred()
        assert client is not None
    finally:
        macro._fred = None  # do not leak a configured client into other tests