from src.utils.mapping import load_bist_mapping, ascii_normalize
from src.services.bist import get_bist_companies_as_dict_from_redis

SEARCH_ALIASES: dict[str, list[str]] = {
    "VAKIFBANK": ["VAKBN"],
    "VAKIFLAR BANKASI": ["VAKBN"],
    "AKBANK": ["AKBNK"],
    "IS BANK": ["ISCTR"],
    "IS BANKASI": ["ISCTR"],
    "GARANTI BANK": ["GARAN"],
    "GARANTI BANKASI": ["GARAN"],
    "YAPI KREDI": ["YKBNK"],
    "HALK BANK": ["HALKB"],
    "HALK BANKASI": ["HALKB"],
    "SISE CAM": ["SISE"],
    "SISECAM": ["SISE"],
    "SISE VE CAM": ["SISE"],
    "EREGLI DEMIR": ["EREGL"],
    "ERDEMIR": ["EREGL"],
    "TURK HAVA": ["THYAO"],
    "TURKISH AIRLINES": ["THYAO"],
    "THY": ["THYAO"],
    "TURK TELEKOM": ["TTKOM"],
    "PETKIM": ["PETKM"],
    "PETROKIMYA": ["PETKM"],
    "AKSA AKRILIK": ["AKSA"],
    "AKSA ENERJI": ["AKSEN"],
    "KOC HOLDING": ["KCHOL"],
    "SABANCI HOLDING": ["SAHOL"],
    "TOFAS": ["TOASO"],
    "FORD OTOSAN": ["FROTO"],
    "ARCELIK": ["ARCLK"],
    "BIM MAGAZALAR": ["BIMAS"],
    "SOK MARKET": ["SOKM"],
    "MIGROS": ["MGROS"],
    "SASA POLYESTER": ["SASA"],
    "ASELSAN": ["ASELS"],
    "TURKCELL": ["TCELL"],
    "ENKA INSAAT": ["ENKAI"],
    "PETROL OFISI": ["PTOFS"],
    "TRAKYA CAM": ["TRC"],
    "EGE SERAMIK": ["EGSER"],
}


def _build_dataset() -> dict[str, dict]:
    mapping = load_bist_mapping()
    pykap_companies = {c["ticker"]: c for c in get_bist_companies_as_dict_from_redis()}

    dataset = {}
    all_tickers = set(list(pykap_companies.keys()) + list(mapping.keys()))

    for ticker in all_tickers:
        pykap_entry = pykap_companies.get(ticker)
        mapping_entry = mapping.get(ticker)

        name = (pykap_entry or mapping_entry or {}).get("name") or (mapping_entry or {}).get("name_tr", "")
        search_title = (mapping_entry or {}).get("search_title", [])
        city = (pykap_entry or {}).get("city", "")
        auditor = (pykap_entry or {}).get("auditor", "")
        company_id = (pykap_entry or {}).get("company_id", "")

        dataset[ticker] = {
            "ticker": ticker,
            "name": name,
            "search_title": search_title,
            "city": city,
            "auditor": auditor,
            "company_id": company_id,
        }

    return dataset


_dataset: dict[str, dict] | None = None


def _get_dataset() -> dict[str, dict]:
    global _dataset
    if _dataset is None:
        _dataset = _build_dataset()
    return _dataset


def _deduplicate(results: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    deduped = []
    for r in results:
        key = r["name"]
        idx = seen.get(key)
        if idx is not None:
            existing = deduped[idx]
            if r["score"] > existing["score"]:
                deduped[idx] = r
        else:
            seen[key] = len(deduped)
            deduped.append(r)
    return deduped


def search_companies(query: str, limit: int = 20) -> list[dict]:
    q = ascii_normalize(query.strip().upper())
    if not q:
        return []

    alias_matches = SEARCH_ALIASES.get(q, [])

    results = []

    for ticker, entry in _get_dataset().items():
        ticker_upper = ticker.upper()
        name_ascii = ascii_normalize(entry["name"].upper())

        score = 0

        if ticker_upper in alias_matches:
            score = 95
        elif q == ticker_upper:
            score = 100
        elif ticker_upper.startswith(q) or q.startswith(ticker_upper):
            if len(q) >= 2 and len(ticker_upper) >= 2:
                score = 80
        elif q in name_ascii:
            if name_ascii.startswith(q):
                score = 75
            else:
                score = 60
        elif any(ascii_normalize(t.upper()) == q for t in entry.get("search_title", [])):
            score = 70
        elif any(q in ascii_normalize(t.upper()) for t in entry.get("search_title", [])):
            if len(q) >= 4:
                score = 55
        elif any(ascii_normalize(w).startswith(q)
                 for w in name_ascii.split() if len(w) >= 3):
            score = 30
        elif any(q in ascii_normalize(w)
                 for w in name_ascii.split() if len(w) >= 3):
            score = 20
        elif len(q) >= 3 and q in ticker_upper:
            score = 70

        if score > 0:
            results.append({
                "ticker": ticker,
                "name": entry["name"],
                "score": score,
            })

    results.sort(key=lambda x: (-x["score"], x["ticker"]))
    results = _deduplicate(results)
    return results[:limit]
