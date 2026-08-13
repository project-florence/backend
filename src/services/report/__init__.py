from datetime import datetime, timezone
import logging
import os

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.core.config import get_config
from src.services.bist import is_valid_bist_ticker
from src.services.company import get_company_info
from src.services.economy import get_currency, get_gold_prices
from src.analysis.metrics import compute_all
from src.analysis.stock_vector import company_vector
from src.clients.search import news_search

logger = logging.getLogger(__name__)


def get_date() -> str:
    """Get the current date and time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


async def get_economic_data(ticker: str) -> dict:
    """Bir hisse senedi için şirket profilleri, finansal ölçümler, güncel fiyat,
    döviz kurları ve altın fiyatları gibi tüm bilgileri tek bir seferde getirir."""
    if not await is_valid_bist_ticker(ticker):
        return {"error": f"Invalid ticker: {ticker}"}

    try:
        profile = await get_company_info(ticker)
        if not profile:
            return {"error": f"No data found for {ticker}"}
        metrics = compute_all(profile)
        vector = company_vector(profile)
        economy = {}
        try:
            economy = {**(await get_currency()), **(await get_gold_prices())}
        except Exception:
            pass
        return {
            "ticker": ticker,
            "company_name": profile.get("name"),
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "market": profile.get("market", {}),
            "trading": profile.get("trading", {}),
            "valuation": profile.get("valuation", {}),
            "financials": profile.get("financials", {}),
            "metrics": metrics,
            "vector": vector,
            "economy": economy,
        }
    except Exception as e:
        return {"error": f"economic_data failed: {e}"}


async def search_news(query: str) -> list[dict]:
    """SearXNG kullanarak, bir hisse senedi kodu veya şirket için en son haberleri getirir. Her haber öğesi başlık, içerik (özet), URL ve kaynak motor bilgilerini içerir."""
    try:
        items = await news_search(query, limit=20)
    except Exception as e:
        return [{"error": f"News search failed: {e}"}]
    return [
        {"index": i + 1, "url": it.url, "title": it.title,
         "content": it.content, "source": getattr(it, "source_engine", "")}
        for i, it in enumerate(items)
    ]


async def content_fetch(urls: list[str]) -> list[dict]:
    """'search_news' kullanılarak bulunan haberlerden seçilen URL'lerin tam metin içeriğini getirir."""
    import asyncio
    from src.clients.scraping import get_text_from_url as _get_text_from_url

    MAX_URLS = 5
    results = []
    for url in urls[:MAX_URLS]:
        try:
            full_text = await asyncio.to_thread(_get_text_from_url, url)
        except Exception as e:
            full_text = f"[Icerik cekilemedi: {e}]"
        results.append({"url": url, "content": full_text})
    return results


def _mode_config(mode: str) -> tuple[int, str, str]:
    cfg = get_config()["article_analyzer"]
    if mode == "quick":
        return (
            cfg["quick_report_article_limit"],
            "kisa",
            "Bir kac paragraf (maksimum 500 kelime).",
        )
    return (
        cfg["deep_report_article_limit"],
        "detayli",
        "Kapsamli, uzun format (1500+ kelime). Birden cok bakis acisi, risk analizi, finansal degerlendirme.",
    )


def _build_system_prompt(ticker: str, mode: str, purpose: str | None = None) -> str:
    max_articles, mode_label, length_desc = _mode_config(mode)

    prompt = f"""Sen bir finans analistsin. Asagidaki araclari kullanarak "{ticker}" hakkinda kapsamli bir arastirma yap ve bir rapor hazirla.

## Kullanabilecegin araclar

1. **search_news(query)**: "{ticker}" ile ilgili haberleri getirir. Ihtiyacin kadar tekrar tekrar kullanabilirsin.
2. **content_fetch(urls)**: search_news ile buldugun haberlerden secilen URL'lerin tam metnini okur. Bazi URL'lerden icerik cekilemeyebilir, bu durumda **search_news'teki ozet (content) bilgisini kullan**.
3. **get_economic_data(ticker)**: Sirketin finansal verilerini, fiyatini, sektor bilgilerini, doviz/altin piyasasini getirir.

## Rapor modu: {mode_label}

Okuma siniri: En fazla **{max_articles} haber** degerlendirebilirsin (ozet veya tam metin).
Rapor uzunlugu: {length_desc}

## Calisma akisi

1. **search_news** ile en az 2-3 farkli arama yap (farkli terimler dene: ticker, sirket adi, sektor). Arama sonuclarindaki **content (ozet)** bilgisi genellikle yeterlidir.
2. Arama sonuclarini birlestir, en onemli haberleri **content_fetch** ile acip oku. Icerik cekilemezse, search_news'teki ozet bilgisini kullan.
3. **get_economic_data** ile finansal verileri kontrol et. Bu arac henuz kullanilamiyorsa, mevcut bilgilerle raporu olustur.
4. Tum bilgileri sentezle ve raporu olustur.

## Kurallar

- **Kesinlikle uydurma bilgi ekleme.** Sadece okudugun haberlerden ve get_economic_data'dan gelen bilgileri kullan. Eger bazi veriler eksikse (get_economic_data calismiyorsa, icerik cekilemiyorsa), **mevcut verilerle en iyi raporu olustur** ve eksik oldugunu belirt. Eksik bilgi nedeniyle raporu reddetme.
- Kullandigin her haber icin **sentiment** belirt (positive/neutral/negative), haberin URL'sini ve nedenini acikla.
- Raporu **markdown** formatinda yaz. Baslik, alt basliklar, maddeler ve vurgular kullan.
- Raporun bir **title** (baslik) olsun. "{ticker}" icin bir analiz basligi belirle.
- Finansal terimleri gerektigi yerde kullan ama karmasiklastirma. Basit yatirimcilar da anlasin."""

    if purpose:
        prompt += f"""

## Kullanıcının sorusu/amacı: {purpose}

Raporu bu amaca gore onceliklendir; kullanici bu sorunun/amacın yanitini raporun icinde bulabilmeli."""
    return prompt


class SentimentItem(BaseModel):
    sentiment: str = Field(description="positive, neutral veya negative")
    url: str = Field(description="Haberin kaynağı/URL'i")
    reasoning: str = Field(description="Bu sentiment'ın gerekçesi")


class ReportDraft(BaseModel):
    title: str = Field(description="Raporun başlığı")
    about: str = Field(description="Hisse senedi kodu / şirket adı")
    date: str
    report: str = Field(description="Raporun metni")
    sentiments: list[dict] = Field(description="Raporda kullanılan kaynakların analizleri")


class Report(BaseModel):
    title: str = Field(description="Raporun başlığı")
    about: str = Field(description="Hisse senedi kodu / şirket adı")
    date: str
    report: str = Field(description="Raporun metni")
    sentiments: list[dict] = Field(description="Raporda kullanılan kaynakların analizleri")
    token_usage: dict = {"prompt": 0, "completion": 0, "total": 0}


def _build_agent(ticker: str, mode: str, purpose: str | None = None) -> Agent:
    cfg = get_config()["llm_client"]
    model_id = os.getenv("CUSTOM_MODEL") or cfg.get("custom_model", "gemma")
    base_url = os.getenv("CUSTOM_URL") or cfg.get("custom_url")
    api_key = os.getenv("CUSTOM_API_KEY") or "dummy-api-key"

    provider = OpenAIProvider(base_url=base_url, api_key=api_key)
    model = OpenAIChatModel(model_id, provider=provider)

    return Agent(
        model=model,
        system_prompt=_build_system_prompt(ticker, mode, purpose),
        output_type=ReportDraft,
        tools=[get_date, search_news, content_fetch, get_economic_data],
    )


async def generate_report(ticker: str, mode: str, user_id: int | None = None, purpose: str | None = None) -> Report:
    report_agent = _build_agent(ticker, mode, purpose=purpose)
    result = await report_agent.run(
        f"'{ticker}' hissesi icin {mode} analiz raporunu olustur."
    )

    draft: ReportDraft = result.output
    usage_data = result.usage

    prompt_tokens = usage_data.input_tokens or 0
    completion_tokens = usage_data.output_tokens or 0
    total_tokens = usage_data.total_tokens or 0

    # Token kullanimini token_usage tablosuna kaydet (admin get_token_summary
    # ucu bunu okur). Loglama hatasi raporu basarisiz kilmasin.
    try:
        from src.services.token import log_token_usage

        model_id = os.getenv("CUSTOM_MODEL") or get_config()["llm_client"].get("custom_model", "gemma")
        await log_token_usage(
            model=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            endpoint="report",
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("log_token_usage failed: %s", e)

    return Report(
        title=draft.title,
        about=draft.about,
        date=draft.date,
        report=draft.report,
        sentiments=draft.sentiments,
        token_usage={
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
        },
    )


async def get_report_by_id(report_id: int, user_id: int | None = None) -> Report | None:
    import json
    from src.core.database import db

    async with db.cursor(row_factory=None) as cur:
        if user_id is not None:
            await cur.execute("""
                SELECT ticker, title, token_usage, content, sentiments, created_at
                FROM reports
                WHERE id = %s AND user_id = %s
            """, (report_id, user_id))
        else:
            await cur.execute("""
                SELECT ticker, title, token_usage, content, sentiments, created_at
                FROM reports
                WHERE id = %s
            """, (report_id,))
        row = await cur.fetchone()

    if not row:
        return None

    token_usage = row[2]
    if isinstance(token_usage, str):
        token_usage = json.loads(token_usage) if token_usage else {"prompt": 0, "completion": 0, "total": 0}

    sentiments = row[4]
    if isinstance(sentiments, str):
        sentiments = json.loads(sentiments) if sentiments else []
    elif sentiments is None:
        sentiments = []

    return Report(
        title=row[1] or f"{row[0]} Analizi",
        about=row[0],
        date=row[5].isoformat(),
        report=row[3] or "",
        sentiments=sentiments,
        token_usage=token_usage,
    )


def report_to_str(report: Report) -> str:
    try:
        dt = datetime.fromisoformat(report.date)
        pretty_date = dt.strftime("%d %B %Y, %H:%M")
    except (ValueError, TypeError):
        pretty_date = report.date

    sentiment_lines = "\n".join(
        f"  - [{s.get('sentiment', '?')}] {s.get('url', '')} — {s.get('reasoning', '')}"
        for s in (report.sentiments or [])
    )

    return f"""# {report.title}
## {report.about} — {pretty_date}

{report.report}

### Sources
{sentiment_lines if sentiment_lines else "  No sources recorded."}
(Financial and economic data from the relevant date have not been included.)
"""
