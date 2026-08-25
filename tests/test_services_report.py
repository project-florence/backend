"""Unit tests for src/services/report/__init__.py.

Hermetic: the pydantic-ai Agent is stubbed so no network/LLM call happens.
Focus is FIX 2 — the defensive post-processing step that strips any surviving
internal tool identifier from the generated title/report before persisting.
"""

from types import SimpleNamespace

from src.services import report as report_module


# ---------------------------------------------------------------------------
# _strip_tool_identifiers (pure function)
# ---------------------------------------------------------------------------


def test_strip_tool_identifiers_removes_bolded_tool_name():
    text = (
        "Not: Bu rapor yalnizca kamuya acik haber ve finansal verilerden derlenmistir; "
        "yatirim tavsiyesi degildir. **get_economic_data** tam veri saglamistir; "
        "haber kaynaklarinin tamamina erisilememis olup ozetler kullanilmistir."
    )
    cleaned = report_module._strip_tool_identifiers(text)
    assert "get_economic_data" not in cleaned
    # Yatirim tavsiyesi uyarisi (legitimate disclaimer) degismemeli.
    assert "yatirim tavsiyesi degildir" in cleaned


def test_strip_tool_identifiers_removes_all_registered_tool_names():
    text = " ".join(report_module._TOOL_NAMES)
    cleaned = report_module._strip_tool_identifiers(text)
    for name in report_module._TOOL_NAMES:
        assert name not in cleaned


def test_strip_tool_identifiers_noop_when_no_tool_names_present():
    text = "Bu rapor finansal verilere ve haber kaynaklarina dayanmaktadir."
    assert report_module._strip_tool_identifiers(text) == text


def test_strip_tool_identifiers_handles_empty_text():
    assert report_module._strip_tool_identifiers("") == ""
    assert report_module._strip_tool_identifiers(None) is None


# ---------------------------------------------------------------------------
# generate_report applies the post-processing to the persisted Report
# ---------------------------------------------------------------------------


class _FakeUsage:
    input_tokens = 100
    output_tokens = 900
    total_tokens = 1000


class _FakeResult:
    def __init__(self, output):
        self.output = output
        self.usage_value = _FakeUsage()

    @property
    def usage(self):
        return self.usage_value


async def test_generate_report_strips_tool_name_from_title_and_body(monkeypatch):
    leaked_report = (
        "Analiz tamamlandi. **get_economic_data** tam veri saglamistir; "
        "haber kaynaklarinin tamamina erisilememis olup ozetler kullanilmistir."
    )
    draft = SimpleNamespace(
        title="THYAO get_economic_data Analizi",
        about="THYAO",
        date="2026-08-24T12:00:00+00:00",
        report=leaked_report,
        sentiments=[],
    )

    class _FakeAgent:
        async def run(self, prompt):
            return _FakeResult(draft)

    monkeypatch.setattr(report_module, "_build_agent", lambda *a, **k: _FakeAgent())

    async def _no_log(*args, **kwargs):
        return None

    monkeypatch.setattr("src.services.token.log_token_usage", _no_log)

    result = await report_module.generate_report("THYAO", "quick", user_id=1)

    assert "get_economic_data" not in result.title
    assert "get_economic_data" not in result.report
    # Meru bilgi korunmali (sadece arac adi cikarilmali).
    assert "haber kaynaklarinin tamamina erisilememis" in result.report
