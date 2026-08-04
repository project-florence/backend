"""Şirket sitelerinden yüksek çözünürlüklü ikon/logo indirir.

fetch_company_urls.py'nin ürettiği data/company_urls.json'daki URL'lere gidip
favicon/apple-touch-icon/manifest ikonlarını tarar, en büyüğünü seçer ve
{LOGO_SAVE_PATH}/{ticker}.png olarak kaydeder. 256'dan küçük ikonlar Pillow ile
256x256'ya büyütülür. Hiçbiri bulunamazsa logo.dev fallback'i denenir.

Kullanım:
  python scripts/download_icons.py [--urls data/company_urls.json] [--workers 3]
"""

import argparse
import asyncio
import io
import json
import os
import random
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import pykap
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SIZE_RE = re.compile(r"(\d+)x(\d+)")
TARGET_SIZE = 256
EXT_PREFERENCE = {"png": 10, "jpg": 8, "jpeg": 8, "webp": 5, "ico": 2, "svg": 0}
LOGO_DEV_TOKEN = os.getenv("LOGODEV_API_KEY")

PROJECT_ROOT = Path(__file__).parent.parent


def score_size(size: int | None) -> int:
    if size is None:
        return 20
    if size >= 512:
        return 100
    if size >= 256:
        return 90
    if size >= 192:
        return 80
    if size >= 128:
        return 70
    if size >= 64:
        return 50
    if size >= 32:
        return 30
    return 25


def candidate_score(url: str, size: int | None) -> int:
    ext = urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path else ""
    return score_size(size) + EXT_PREFERENCE.get(ext, 0)


def parse_size(raw: str | None) -> int | None:
    if not raw or raw.strip().lower() == "any":
        return None
    match = SIZE_RE.search(raw)
    if match:
        return max(int(match.group(1)), int(match.group(2)))
    return None


def collect_candidates(soup: BeautifulSoup, base_url: str) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    for link in soup.find_all("link"):
        rel = (link.get("rel") or [])
        rels = {r.lower() for r in rel}
        if not ({"icon", "shortcut icon"} & rels or "icon" in rels or "apple-touch-icon" in rels or "mask-icon" in rels):
            continue
        href = link.get("href")
        if not href:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        raw_sizes = link.get("sizes")
        size = parse_size(raw_sizes)
        if size is None and "apple-touch-icon" in rels and not raw_sizes:
            size = 180
        candidates.append({"url": url, "size": size, "href": href})

    manifest = soup.find("link", rel=lambda r: r and "manifest" in r)
    if manifest and manifest.get("href"):
        manifest_url = urljoin(base_url, manifest["href"])
        candidates.append({"url": manifest_url, "size": None, "manifest": True})

    return candidates


def pick_largest_png(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    if image.format == "ICO":
        frames = []
        try:
            for i in range(getattr(image, "n_frames", 1)):
                image.seek(i)
                frames.append((image.size, image.copy()))
            if frames:
                image = max(frames, key=lambda f: f[0][0] * f[0][1])[1]
        except EOFError:
            pass
    image.load()
    return image


def to_png_bytes(image: Image.Image) -> bytes:
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    if max(image.size) < TARGET_SIZE:
        ratio = TARGET_SIZE / max(image.size)
        new_size = (max(TARGET_SIZE, round(image.width * ratio)), max(TARGET_SIZE, round(image.height * ratio)))
        image = image.resize(new_size, Image.LANCZOS)
    elif max(image.size) != TARGET_SIZE and min(image.size) >= TARGET_SIZE and max(image.size) > TARGET_SIZE * 1.2:
        ratio = TARGET_SIZE / max(image.size)
        image = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def download_icon(client: httpx.AsyncClient, ticker: str, url: str, save_path: Path) -> tuple[str, int] | None:
    homepage = url
    try:
        response = await client.get(homepage, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        response.raise_for_status()
    except Exception:
        parsed = urlparse(url)
        if parsed.scheme == "https":
            try:
                response = await client.get(
                    "http://" + parsed.netloc + parsed.path,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                )
                response.raise_for_status()
            except Exception:
                return None
        else:
            return None

    soup = BeautifulSoup(response.text, "lxml")
    base_url = str(response.url) or homepage
    candidates = collect_candidates(soup, base_url)

    manifest_urls = [c for c in candidates if c.get("manifest")]
    candidates = [c for c in candidates if not c.get("manifest")]
    for manifest_candidate in manifest_urls:
        try:
            manifest_response = await client.get(manifest_candidate["url"], headers={"User-Agent": USER_AGENT})
            manifest_response.raise_for_status()
            manifest = manifest_response.json()
            for icon in manifest.get("icons", []):
                if not icon.get("src"):
                    continue
                icon_url = urljoin(manifest_candidate["url"], icon["src"])
                candidates.append({"url": icon_url, "size": parse_size(icon.get("sizes"))})
        except (httpx.HTTPError, ValueError, KeyError):
            pass

    candidates.sort(key=lambda c: candidate_score(c["url"], c["size"]), reverse=True)
    for candidate in candidates:
        try:
            icon_response = await client.get(candidate["url"], headers={"User-Agent": USER_AGENT})
            icon_response.raise_for_status()
            image = pick_largest_png(icon_response.content)
            data = to_png_bytes(image)
        except Exception:
            continue
        save_path.write_bytes(data)
        return candidate["url"], max(image.size)

    favicon_url = urljoin(base_url, "/favicon.ico")
    try:
        favicon_response = await client.get(favicon_url, headers={"User-Agent": USER_AGENT})
        favicon_response.raise_for_status()
        image = pick_largest_png(favicon_response.content)
        data = to_png_bytes(image)
        save_path.write_bytes(data)
        return favicon_url, max(image.size)
    except Exception:
        return None


async def download_logodev_logo(client: httpx.AsyncClient, ticker: str, save_path: Path) -> bool:
    if not LOGO_DEV_TOKEN:
        return False
    try:
        response = await client.get(
            f"https://img.logo.dev/ticker/{ticker}.IS",
            params={"token": LOGO_DEV_TOKEN, "format": "png", "size": 256},
        )
        response.raise_for_status()
        save_path.write_bytes(response.content)
        return True
    except httpx.HTTPError:
        return False


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", type=Path, default=PROJECT_ROOT / "data" / "company_urls.json")
    parser.add_argument("--save-path", type=Path, default=Path(os.getenv("LOGO_SAVE_PATH", "/var/lib/florence/logos")))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--tickers", type=str, default=None, help="virgülle ayrılmış alt küme")
    parser.add_argument("--no-fallback", action="store_true", help="logo.dev fallback'ini kapat")
    args = parser.parse_args()

    if not args.urls.exists():
        print(f"hata: {args.urls} bulunamadı, önce fetch_company_urls.py çalıştırın")
        return

    urls = json.loads(args.urls.read_text())
    if args.tickers:
        subset = {t.strip().upper() for t in args.tickers.split(",")}
        urls = {t: u for t, u in urls.items() if t in subset}

    args.save_path.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(args.workers)
    stats = {"high": 0, "medium": 0, "small": 0, "fallback": 0, "failed": 0}

    async def save_ticker(client: httpx.AsyncClient, ticker: str, target: Path) -> None:
        source = await download_icon(client, ticker, urls.get(ticker, ""), target)
        if source:
            source_url, size = source
            if size >= 192:
                stats["high"] += 1
            elif size >= 64:
                stats["medium"] += 1
            else:
                stats["small"] += 1
            print(f"{ticker}: {source_url} ({size}x{size})")
        elif not args.no_fallback and await download_logodev_logo(client, ticker, target):
            stats["fallback"] += 1
            print(f"{ticker}: logo.dev fallback")
        else:
            stats["failed"] += 1
            print(f"{ticker}: indirilemedi")

    async def worker(client: httpx.AsyncClient, ticker: str) -> None:
        async with semaphore:
            await asyncio.sleep(random.uniform(0.2, 0.8))
            target = args.save_path / f"{ticker}.png"
            try:
                if ticker in urls:
                    await save_ticker(client, ticker, target)
                elif not args.no_fallback and await download_logodev_logo(client, ticker, target):
                    stats["fallback"] += 1
                    print(f"{ticker}: logo.dev fallback")
                else:
                    stats["failed"] += 1
                    print(f"{ticker}: URL yok, indirilemedi")
            except Exception as exc:
                stats["failed"] += 1
                print(f"{ticker}: hata: {exc}")

    timeout = httpx.Timeout(25.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        all_tickers = list(urls) if args.tickers else sorted({*urls, *pykap.bist_company_list()})
        await asyncio.gather(*(worker(client, t) for t in all_tickers))

    print("\nistatistik:", stats)


if __name__ == "__main__":
    asyncio.run(main())
