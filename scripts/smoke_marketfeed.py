"""Market feed smoke testi — DB/Redis GEREKMEZ (yalnizca canli ag erisimi).

Calistirma (FRED anahtari GEREKMEZ; placeholder istemiyoruz):
    cd backend && FRED_API_KEY='' .venv/bin/python scripts/smoke_marketfeed.py

Su uc noktayi dogrular:
1. RSS modulu gercek bir feed URL'sinden bos olmayan FeedItem listesi doner.
2. indices batch'i (yfinance) en az bir IndexQuote doner (tek tek toleransli).
3. Macro modulu FRED anahtari YOKKEN [] doner.

Onemli: bu script ``tests/`` altinda degil — mevcut pytest suiti yu nin
conftest ile yfinance/network stub'larini etkilemez, suite'i bozmaz.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clients.feeds import fetch_indices, fetch_macro_calendar, fetch_rss  # noqa: E402
from src.core.config import get_config  # noqa: E402


def _hr(name: str) -> str:
    return f"\n=== {name} ==="


async def main() -> int:
    failures = 0

    # 1) RSS
    cfg = get_config()["marketfeed"]
    sources = list(cfg["rss_urls"].items())
    print(_hr("RSS — canli dogrulama"))
    total_items = 0
    for source, url in sources:
        items = await fetch_rss(url, limit=5, source=source)
        total_items += len(items)
        status = "OK" if items else "EMPTY/FAIL"
        print(f"  [{status}] {source:18s} url={url} -> {len(items)} item")
        if items:
            it = items[0]
            print(f"      ornek: {it.title[:70]!r} | published={it.published}")
        if not items:
            failures += 1

    print(_hr("RSS — birlesik fetch (get_news_feed, Redis yokken)"))

    # 2) Indices
    print(_hr("Global endeksler (yfinance tek batch)"))
    quotes = await fetch_indices(cfg["index_symbols"], cfg["index_names"])
    print(f"  {len(quotes)}/{len(cfg['index_symbols'])} sembol dondu")
    for q in quotes:
        print(f"    {q.key:12s} {q.name:18s} value={q.value} change_pct={q.change_pct} as_of={q.as_of}")
    if not quotes:
        failures += 1
        print("  !! hicbir endeks donmedi (ag/yfinance engeli olabilir)")

    # 3) Macro — FRED anahtarsiz []
    print(_hr("Makro takvim (FRED_API_KEY yoksa []))"))
    key = os.getenv("FRED_API_KEY")
    events = await fetch_macro_calendar(days=45)
    if key:
        print(f"  !! FRED_API_KEY ortamda set ({key[:6]}...): bu smoke'u "
              "FRED_API_KEY='' ile calistirin — makro gercek cagri yapar.")
    print(f"  FRED_API_KEY set={bool(key)} -> {len(events)} macro event")
    for e in events:
        print(f"    {e.date} | {e.region} | {e.event} | impact={e.impact}")
    if key:
        # anahtar varsa gercek test anlamsiz; yine de [] oldugunu denetleme
        print("  (anahtar varken sonuc canli FRED'e bagli — dogrulama atlandi)")
    elif events:
        failures += 1
        print("  !! anahtar yokken [] bekleniyordu")

    print(_hr("SONUC"))
    if failures:
        print(f"  {failures} kontrol basarisiz")
        return 1
    print("  tum kontroller gecti")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
