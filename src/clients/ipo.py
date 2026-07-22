import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from src.core.config import get_config

CATEGORY_IDS = {
    "ana-pazar": 116,
    "aktif": "116,117",
    "taslak": 194,
}


def _cfg():
    return get_config()["halkarz"]


def list_ipos(category_slug: str, after: str | None = None) -> list[dict]:
    cfg = _cfg()
    cat_id = CATEGORY_IDS.get(category_slug)
    if cat_id is None:
        raise ValueError(f"Unknown category: {category_slug}")

    if after is None:
        after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")

    page = 1
    results = []
    headers = {"User-Agent": cfg["user_agent"]}

    while True:
        params = {
            "categories": cat_id,
            "per_page": 100,
            "page": page,
            "after": after,
            "_fields": "id,date,modified,slug,title,link",
        }
        resp = requests.get(cfg["wp_api"], params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        posts = resp.json()

        if not posts:
            break

        for post in posts:
            results.append({
                "id": post["id"],
                "slug": post["slug"],
                "title": post["title"]["rendered"],
                "link": post["link"],
                "date": post["date"],
                "modified": post.get("modified"),
            })

        total_pages = resp.headers.get("X-WP-TotalPages")
        if total_pages and page >= int(total_pages):
            break
        page += 1

    return results


def _extract_info_tds(soup: BeautifulSoup) -> dict:
    info = {}
    tab = soup.find("div", class_="tab_item")
    if not tab:
        return info

    tds = tab.find_all("td")
    i = 0
    while i < len(tds) - 1:
        key_text = tds[i].get_text(strip=True).rstrip(" :")

        if not key_text or key_text.startswith("- "):
            break

        val_text = tds[i + 1].get_text(strip=True)
        info[key_text] = val_text
        i += 2

    return info


def _extract_sections(soup: BeautifulSoup) -> dict:
    sections = {}
    wrapper = soup.find("div", class_="wrapper")
    if not wrapper:
        return sections

    for h5 in wrapper.find_all("h5"):
        title = h5.get_text(strip=True)
        if not title:
            continue

        paragraphs = []
        for sibling in h5.find_next_siblings():
            if sibling.name == "h5":
                break
            if sibling.name in ("p", "li"):
                text = sibling.get_text(strip=True)
                if text:
                    paragraphs.append(text)

        if paragraphs:
            sections[title] = "\n".join(paragraphs)

    return sections


def _extract_company_info(soup: BeautifulSoup) -> dict:
    info = {}
    city_span = soup.find("span", class_="shc-city")
    if city_span:
        info["city"] = city_span.get_text(strip=True).replace("\u015eehir :", "").strip()

    founded_span = soup.find("span", class_="shc-founded")
    if founded_span:
        info["founded"] = founded_span.get_text(strip=True).replace("Kurulu\u015f Tarihi :", "").strip()

    desc_div = soup.find("div", class_="acc-body")
    if desc_div:
        desc_p = desc_div.find("p")
        if desc_p:
            info["description"] = desc_p.get_text(strip=True)

    return info


def get_ipo_detail(slug: str) -> dict | None:
    cfg = _cfg()
    url = f"{cfg['base_url']}/{slug}/"
    headers = {"User-Agent": cfg["user_agent"]}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    ticker = None
    company_name = None

    il_content = soup.find("div", class_="il-content")
    if il_content:
        full_text = il_content.get_text(strip=True)
        full_text = re.sub(r"^Yeni!", "", full_text).strip()
        match = re.match(r"([A-Z]{2,6})(?=[A-ZĞÜŞİÖÇ][a-zğüşıöç])", full_text)
        if match:
            ticker = match.group(1)
            company_name = full_text[match.end():].strip()
        else:
            parts = full_text.split(maxsplit=1)
            if len(parts) == 2 and parts[0].isupper() and len(parts[0]) <= 6:
                ticker = parts[0]
                company_name = parts[1]

    if not company_name:
        h1 = soup.find("h1", class_="il-halka-arz-sirket")
        if h1:
            company_name = h1.get_text(strip=True)

    info = _extract_info_tds(soup)
    sections = _extract_sections(soup)
    company = _extract_company_info(soup)

    last_modified = soup.find("div", class_="last-modified")
    updated_at = None
    if last_modified:
        updated_at = last_modified.get_text(strip=True).replace("Son Güncelleme:", "").strip()

    return {
        "slug": slug,
        "ticker": ticker,
        "company_name": company_name,
        "info": info,
        "sections": sections,
        "company": company,
        "updated_at": updated_at,
    }
