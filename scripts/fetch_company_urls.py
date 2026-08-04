"""BIST şirketlerinin resmi web sitesi URL'lerini TradingView'den toplar.

Her ticker için https://www.tradingview.com/symbols/BIST-{TICKER}/ sayfasından
"Details" bölümündeki website URL'sini çıkarır ve data/company_urls.json olarak
kaydeder. Daha önce başarıyla çekilen ticker'ları atlar (resume destekli).

Kullanım:
  python scripts/fetch_company_urls.py [--out data/company_urls.json] [--workers 3]
"""

import argparse
import asyncio
import json
import random
import re
import time
from pathlib import Path

import httpx
import pykap

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
WEB_SITE_URL_RE = re.compile(r'"web_site_url":"([^"]*)"')

PROJECT_ROOT = Path(__file__).parent.parent


def extract_website_url(html: str) -> str | None:
    match = WEB_SITE_URL_RE.search(html)
    if match and match.group(1):
        return match.group(1)

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for div in soup.find_all("div", class_=lambda c: c and "label" in c):
        if div.get_text(strip=True) != "Website":
            continue
        node = div
        for _ in range(8):
            node = node.find_next()
            if node is not None and node.name == "a" and node.get("href", "").startswith("http"):
                return node["href"]
    return None


async def fetch_ticker_url(
    client: httpx.AsyncClient,
    ticker: str,
    retries: int = 3,
) -> str | None:
    url = f"https://www.tradingview.com/symbols/BIST-{ticker}/"
    for attempt in range(retries):
        try:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            website = extract_website_url(response.text)
            return website
        except (httpx.HTTPError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "company_urls.json")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--tickers", type=str, default=None, help="virgülle ayrılmış alt küme")
    args = parser.parse_args()

    tickers = pykap.bist_company_list()
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]

    result = {}
    if args.out.exists():
        result = json.loads(args.out.read_text())
        done = set(result)
    else:
        done = set()

    pending = [t for t in tickers if t not in done]
    print(f"toplam {len(tickers)} ticker, {len(pending)} tanesi çekilecek")

    semaphore = asyncio.Semaphore(args.workers)
    failed: list[str] = []

    async def worker(client: httpx.AsyncClient, ticker: str) -> None:
        async with semaphore:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            website = await fetch_ticker_url(client, ticker)
            if website:
                result[ticker] = website
                print(f"{ticker}: {website}")
            else:
                failed.append(ticker)
                print(f"{ticker}: URL bulunamadı")

    timeout = httpx.Timeout(30.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        await asyncio.gather(*(worker(client, t) for t in pending))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"\nçekildi: {len(result)} | başarısız: {len(failed)}")
    print(f"çıktı: {args.out}")
    if failed:
        print(f"URL bulunamayanlar: {', '.join(failed)}")


if __name__ == "__main__":
    asyncio.run(main())
