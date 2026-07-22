from datetime import datetime, timezone
from pydantic import BaseModel
from src.core.config import get_config


class Report(BaseModel):
    title: str
    about: str
    date: str
    report: str
    sentiments: list[dict]
    token_usage: dict = {"prompt": 0, "completion": 0, "total": 0}


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


def _build_system_prompt(ticker: str, mode: str) -> str:
    max_articles, mode_label, length_desc = _mode_config(mode)

    return f"""Sen bir finans analistsin. Asagidaki araclari kullanarak "{ticker}" hakkinda kapsamli bir arastirma yap ve bir rapor hazirla.

## Kullanabilecegin araclar

1. **news_search(query)**: "{ticker}" ile ilgili haberleri getirir. Ihtiyacin kadar tekrar tekrar kullanabilirsin.
2. **content_fetch(indices)**: news_search ile buldugun haberlerden secilen index'lerin tam metnini okur. Bazi URL'lerden icerik cekilemeyebilir, bu durumda **news_search'teki ozet (content) bilgisini kullan**.
3. **economic_data(ticker)**: Sirketin finansal verilerini, fiyatini, sektor bilgilerini, doviz/altin piyasasini getirir.

## Rapor modu: {mode_label}

Okuma siniri: En fazla **{max_articles} haber** degerlendirebilirsin (ozet veya tam metin).
Rapor uzunlugu: {length_desc}

## Calisma akisi

1. **news_search** ile en az 2-3 farkli arama yap (farkli terimler dene: ticker, sirket adi, sektor). Arama sonuclarindaki **content (ozet)** bilgisi genellikle yeterlidir.
2. Arama sonuclarini birlestir, en onemli haberleri **content_fetch** ile acip oku. Icerik cekilemezse, news_search'teki ozet bilgisini kullan.
3. **economic_data** ile finansal verileri kontrol et. Bu arac henuz kullanilamiyorsa, mevcut bilgilerle raporu olustur.
4. Tum bilgileri sentezle ve **generate_report** tool'unu cagirarak raporu olustur.

## Kurallar

- **Kesinlikle uydurma bilgi ekleme.** Sadece okudugun haberlerden ve economic_data'dan gelen bilgileri kullan. Eger bazi veriler eksikse (economic_data calismiyorsa, icerik cekilemiyorsa), **mevcut verilerle en iyi raporu olustur** ve eksik oldugunu belirt. Eksik bilgi nedeniyle raporu reddetme.
- Kullandigin her haber icin **sentiment** belirt (positive/neutral/negative), haberin URL'sini ve nedenini acikla.
- Raporu **markdown** formatinda yaz. Baslik, alt basliklar, maddeler ve vurgular kullan.
- Raporun bir **title** (baslik) olsun. "{ticker}" icin bir analiz basligi belirle.
- Finansal terimleri gerektigi yerde kullan ama karmasiklastirma. Basit yatirimcilar da anlasin.
- Tum arastirma bitince **generate_report** cagir. Ondan once bu tool'u kullanma."""


def _generate_report(ticker: str, mode: str) -> Report | None:
    from src.services.report.tools import load_tool_definitions, run_tool_loop

    tools = load_tool_definitions()
    prompt = _build_system_prompt(ticker, mode)
    result = run_tool_loop(prompt, tools=tools, endpoint=f"{mode}_report")

    now = datetime.now(timezone.utc).isoformat()

    usage = result.get("usage", {"prompt": 0, "completion": 0, "total": 0})

    if result["type"] == "report":
        content = result["content"]
        return Report(
            title=content.get("title", f"{ticker} Analizi"),
            about=ticker,
            date=now,
            report=content.get("report", ""),
            sentiments=content.get("sentiments", []),
            token_usage=usage,
        )

    if result["type"] == "text":
        return Report(
            title=f"{ticker} Analizi",
            about=ticker,
            date=now,
            report=result["content"],
            sentiments=[],
            token_usage=usage,
        )

    if result["type"] == "error":
        raise RuntimeError(result.get("detail", "LLM error"))

    raise RuntimeError(f"Unknown result type: {result.get('type')}")


def generate_quick_report(ticker: str) -> Report | None:
    return _generate_report(ticker, "quick")


def generate_deep_report(ticker: str) -> Report | None:
    return _generate_report(ticker, "deep")


def get_report_by_id(report_id: int) -> Report | None:
    import json
    from src.core.database import db

    with db.cursor() as cur:
        cur.execute("""
            SELECT ticker, title, token_usage, content, sentiments, created_at
            FROM reports
            WHERE id = %s
        """, (report_id,))
        row = cur.fetchone()

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
