import re
import json
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from src.core.config import get_config

TURKISH_CHAR_MAP = str.maketrans({
    "Ü": "U", "ü": "u",
    "Ö": "O", "ö": "o",
    "İ": "I", "ı": "i",
    "Ş": "S", "ş": "s",
    "Ç": "C", "ç": "c",
    "Ğ": "G", "ğ": "g",
})

LEGAL_SUFFIXES = [
    "A.Ş.", "A.O.", "T.A.Ş.", "T.A.O.", "A.Ş", "A.O",
    "LTD. ŞTİ.", "LTD.ŞTİ.", "LTD ŞTİ",
    "SANAYİ VE TİCARET", "SANAYİİ VE TİCARET",
    "SANAYİ TİCARET", "SANAYİİ TİCARET",
    "SANAYİ VE DIŞ TİCARET",
    "HOLDİNG", "HOLDING",
]

MANUAL_OVERRIDES = {
    "THYAO": {"search_title": ["TÜRK HAVA", "TURKISH AIRLINES", "THY"], "search_gkg": ["TURKISH AIRLINES"], "filter_lang": {"THY": ["TURKISH"]}},
    "ASELS": {"search_title": ["ASELSAN"], "search_gkg": ["ASELSAN"]},
    "GARAN": {"search_title": ["GARANTI BBVA", "GARANTI BANKASI", "GARANTI BANK"], "search_gkg": ["GARANTI BANKASI", "GARANTI BBVA", "TURKIYE GARANTI BANKASI"]},
    "KCHOL": {"search_title": ["KOÇ HOLDİNG", "KOÇ HOLDING", "KOC HOLDING"], "search_gkg": ["KOC HOLDING"]},
    "SAHOL": {"search_title": ["SABANCI", "SABANCI HOLDİNG", "SABANCI HOLDING"], "search_gkg": ["SABANCI HOLDING"]},
    "TUPRS": {"search_title": ["TÜPRAŞ", "TUPRAS", "TURKISH PETROLEUM"], "search_gkg": ["TUPRAS"]},
    "PGSUS": {"search_title": ["PEGASUS", "PEGASUS HAVA", "PEGASUS AIRLINES"], "search_gkg": ["PEGASUS AIRLINES"]},
    "TCELL": {"search_title": ["TURKCELL"], "search_gkg": ["TURKCELL"]},
    "EREGL": {"search_title": ["EREĞLİ", "EREGLI", "ERDEMIR"], "search_gkg": ["EREGLI DEMIR", "ERDEMIR"]},
    "ISCTR": {"search_title": ["İŞ BANKASI", "IS BANKASI", "İŞ BANK", "IS BANK"], "search_gkg": ["IS BANKASI"]},
    "AKBNK": {"search_title": ["AKBANK"], "search_gkg": ["AKBANK"]},
    "YKBNK": {"search_title": ["YAPI KREDİ", "YAPI KREDI", "YAPI VE KREDİ"], "search_gkg": ["YAPI KREDI"]},
    "VAKBN": {"search_title": ["VAKIFBANK", "VAKIFLAR BANKASI"], "search_gkg": ["VAKIFLAR BANKASI"]},
    "HALKB": {"search_title": ["HALKBANK", "HALK BANKASI"], "search_gkg": ["HALK BANKASI"]},
    "TOASO": {"search_title": ["TOFAŞ", "TOFAS"], "search_gkg": ["TOFAS"]},
    "FROTO": {"search_title": ["FORD OTOSAN", "FORD OTOMOTIV"], "search_gkg": ["FORD OTOSAN"]},
    "SISE": {"search_title": ["ŞİŞE CAM", "SISE CAM", "ŞİŞECAM", "SISECAM"], "search_gkg": ["SISECAM"]},
    "ARCLK": {"search_title": ["ARÇELİK", "ARCELIK"], "search_gkg": ["ARCELIK"]},
    "BIMAS": {"search_title": ["BİM", "BIM BIRLESIK"], "search_gkg": ["BIM BIRLESIK"]},
    "MGROS": {"search_title": ["MİGROS", "MIGROS"], "search_gkg": ["MIGROS"]},
    "SASA": {"search_title": ["SASA POLYESTER", "SASA"], "search_gkg": ["SASA POLYESTER"]},
    "EKGYO": {"search_title": ["EMLAK KONUT", "EMLAK GAYRIMENKUL"], "search_gkg": ["EMLAK KONUT"]},
    "PETKM": {"search_title": ["PETKİM", "PETKIM"], "search_gkg": ["PETKIM"]},
    "TKFEN": {"search_title": ["TEKFEN", "TEKFEN HOLDİNG"], "search_gkg": ["TEKFEN HOLDING"]},
    "TTKOM": {"search_title": ["TÜRK TELEKOM", "TURK TELEKOM"], "search_gkg": ["TURK TELEKOM"]},
    "TAVHL": {"search_title": ["TAV HAVALİMANLARI", "TAV HAVALIMANLARI", "TAV AIRPORTS"], "search_gkg": ["TAV HAVALIMANLARI"]},
    "CCOLA": {"search_title": ["COCA COLA İÇECEK", "COCA COLA ICECEK", "COCA-COLA İÇECEK"], "search_gkg": ["COCA COLA ICECEK"]},
    "AEFES": {"search_title": ["ANADOLU EFES", "EFES", "EFES BIRA"], "search_gkg": ["ANADOLU EFES"]},
    "MAVI": {"search_title": ["MAVİ GİYİM", "MAVI GIYIM"], "search_gkg": ["MAVI GIYIM"]},
    "ULKER": {"search_title": ["ÜLKER", "ULKER", "ÜLKER BİSKÜVİ", "ULKER BISKUVI"], "search_gkg": ["ULKER"]},
    "DOAS": {"search_title": ["DOĞUŞ OTOMOTİV", "DOGUS OTOMOTIV"], "search_gkg": ["DOGUS OTOMOTIV"]},
    "KRDMD": {"search_title": ["KARDEMİR", "KARDEMIR", "KARABÜK DEMİR"], "search_gkg": ["KARDEMIR"]},
    "ZOREN": {"search_title": ["ZORLU ENERJİ", "ZORLU ENERJI", "ZORLU ENERGY"], "search_gkg": ["ZORLU ENERJI"]},
    "ODAS": {"search_title": ["ODAŞ ELEKTRİK", "ODAS ELEKTRIK"], "search_gkg": ["ODAS ELEKTRIK"]},
    "HEKTS": {"search_title": ["HEKTAŞ", "HEKTAS"], "search_gkg": ["HEKTAS"]},
    "KOZAL": {"search_title": ["KOZA ALTIN", "KOZA GOLD"], "search_gkg": ["KOZA ALTIN"]},
    "IPEKE": {"search_title": ["İPEK ENERJİ", "IPEK ENERJI"], "search_gkg": ["IPEK ENERJI"]},
    "OYAKC": {"search_title": ["OYAK ÇİMENTO", "OYAK CIMENTO"], "search_gkg": ["OYAK CIMENTO"]},
    "PAPIL": {"search_title": ["PAPİLON", "PAPILON SAVUNMA"], "search_gkg": ["PAPILON SAVUNMA"]},
    "SDTTR": {"search_title": ["SDT UZAY", "SDT SPACE"], "search_gkg": ["SDT UZAY"]},
    "OTKAR": {"search_title": ["OTOKAR"], "search_gkg": ["OTOKAR"]},
    "ASTOR": {"search_title": ["ASTOR ENERJİ", "ASTOR ENERJI"], "search_gkg": ["ASTOR ENERJI"]},
    "ALARK": {"search_title": ["ALARKO", "ALARKO HOLDİNG", "ALARKO HOLDING"], "search_gkg": ["ALARKO HOLDING"]},
    "ENJSA": {"search_title": ["ENERJİSA", "ENERJISA"], "search_gkg": ["ENERJISA"]},
    "ENKAI": {"search_title": ["ENKA", "ENKA İNŞAAT", "ENKA INSAAT"], "search_gkg": ["ENKA INSAAT"]},
    "GUBRF": {"search_title": ["GÜBRE FABRİKALARI", "GUBRE FABRIKALARI"], "search_gkg": ["GUBRE FABRIKALARI"]},
    "KMPUR": {"search_title": ["KİMTEKS", "KIMTEKS"], "search_gkg": ["KIMTEKS"]},
    "MIATK": {"search_title": ["MİA TEKNOLOJİ", "MIA TEKNOLOJI"], "search_gkg": ["MIA TEKNOLOJI"]},
    "NETAS": {"search_title": ["NETAŞ", "NETAS"], "search_gkg": ["NETAS"]},
    "REEDR": {"search_title": ["REEDER", "REEDER TEKNOLOJİ"], "search_gkg": ["REEDER TEKNOLOJI"]},
    "VESTL": {"search_title": ["VESTEL", "VESTEL ELEKTRONİK"], "search_gkg": ["VESTEL"]},
    "VESBE": {"search_title": ["VESTEL BEYAZ", "VESTEL BEYAZ EŞYA"], "search_gkg": ["VESTEL"]},
    "KARSN": {"search_title": ["KARSAN"], "search_gkg": ["KARSAN"]},
    "KONTR": {"search_title": ["KONTROLMATİK", "KONTROLMATIK"], "search_gkg": ["KONTROLMATIK"]},
    "KONYA": {"search_title": ["KONYA ÇİMENTO", "KONYA CIMENTO"], "search_gkg": ["KONYA CIMENTO"]},
    "LINK": {"search_title": ["LİNK BİLGİSAYAR", "LINK BILGISAYAR"], "search_gkg": ["LINK BILGISAYAR"]},
    "LOGO": {"search_title": ["LOGO YAZILIM"], "search_gkg": ["LOGO YAZILIM"]},
    "MACKO": {"search_title": ["MACKOLİK", "MACKOLIK"], "search_gkg": ["MACKOLIK"]},
    "MPARK": {"search_title": ["MLP SAĞLIK", "MLP SAGLIK", "MEDICAL PARK"], "search_gkg": ["MLP SAGLIK"]},
    "NTHOL": {"search_title": ["NET HOLDİNG", "NET HOLDING"], "search_gkg": ["NET HOLDING"]},
    "OBAMS": {"search_title": ["OBA MAKARNA", "OBA MAKARNACILIK"], "search_gkg": ["OBA MAKARNA"]},
    "PKART": {"search_title": ["PLASTİKKART", "PLASTIKKART"], "search_gkg": ["PLASTIKKART"]},
    "QUAGR": {"search_title": ["QUA GRANITE", "QUA GRANİT"], "search_gkg": ["QUA GRANITE"]},
    "SOKM": {"search_title": ["ŞOK MARKET", "SOK MARKET"], "search_gkg": ["SOK MARKET"]},
    "TABGD": {"search_title": ["TAB GIDA", "TAB FOOD"], "search_gkg": ["TAB GIDA"]},
    "TATEN": {"search_title": ["TATLIPINAR", "TATLIPINAR ENERJİ"], "search_gkg": ["TATLIPINAR ENERJI"]},
    "TMSN": {"search_title": ["TÜMOSAN", "TUMOSAN"], "search_gkg": ["TUMOSAN"]},
    "TRILC": {"search_title": ["TURK İLAÇ", "TURK ILAC"], "search_gkg": ["TURK ILAC"]},
    "TTRAK": {"search_title": ["TÜRK TRAKTÖR", "TURK TRAKTOR"], "search_gkg": ["TURK TRAKTOR"]},
    "ULUSE": {"search_title": ["ULUSOY ELEKTRİK", "ULUSOY ELEKTRIK"], "search_gkg": ["ULUSOY ELEKTRIK"]},
    "USAK": {"search_title": ["UŞAK SERAMİK", "USAK SERAMIK"], "search_gkg": ["USAK SERAMIK"]},
    "YATAS": {"search_title": ["YATAŞ", "YATAS"], "search_gkg": ["YATAS"]},
    "YEOTK": {"search_title": ["YEO TEKNOLOJİ", "YEO TEKNOLOJI"], "search_gkg": ["YEO TEKNOLOJI"]},
    "YYLGD": {"search_title": ["YAYLA AGRO", "YAYLA GIDA"], "search_gkg": ["YAYLA AGRO"]},
    "ISDMR": {"search_title": ["İSKENDERUN DEMİR", "ISKENDERUN DEMIR", "İSDEMİR", "ISDEMIR"], "search_gkg": ["ISKENDERUN DEMIR"]},
    "NUHCM": {"search_title": ["NUH ÇİMENTO", "NUH CIMENTO"], "search_gkg": ["NUH CIMENTO"]},
    "PRKAB": {"search_title": ["PRYSMİAN", "PRYSMIAN", "TÜRK PRYSMİAN"], "search_gkg": ["PRYSMIAN"]},
    "AKSEN": {"search_title": ["AKSA ENERJİ", "AKSA ENERJI"], "search_gkg": ["AKSA ENERJI"]},
    "AKSA": {"search_title": ["AKSA AKRİLİK", "AKSA AKRILIK"], "search_gkg": ["AKSA AKRILIK"]},
    "ALKIM": {"search_title": ["ALKİM", "ALKIM KİMYA"], "search_gkg": ["ALKIM KIMYA"]},
    "AYGAZ": {"search_title": ["AYGAZ"], "search_gkg": ["AYGAZ"]},
    "BRSAN": {"search_title": ["BORUSAN", "BORUSAN MANNESMANN"], "search_gkg": ["BORUSAN"]},
    "CLEBI": {"search_title": ["ÇELEBİ", "CELEBI"], "search_gkg": ["CELEBI"]},
    "CEMTS": {"search_title": ["ÇEMTAŞ", "CEMTAS", "ÇEMTAŞ ÇELİK"], "search_gkg": ["CEMTAS"]},
    "DEVA": {"search_title": ["DEVA HOLDİNG", "DEVA HOLDING"], "search_gkg": ["DEVA HOLDING"]},
    "EGEEN": {"search_title": ["EGE ENDÜSTRİ", "EGE ENDUSTRI"], "search_gkg": ["EGE ENDUSTRI"]},
    "EKOS": {"search_title": ["EKOS TEKNOLOJİ", "EKOS TEKNOLOJI"], "search_gkg": ["EKOS TEKNOLOJI"]},
    "GESAN": {"search_title": ["GİRİŞİM ELEKTRİK", "GIRISIM ELEKTRIK", "GESAN"], "search_gkg": ["GESAN"]},
    "GLYHO": {"search_title": ["GLOBAL YATIRIM HOLDİNG", "GLOBAL YATIRIM HOLDING"], "search_gkg": ["GLOBAL YATIRIM HOLDING"]},
    "GWIND": {"search_title": ["GALATA WIND", "GALATA ENERJİ"], "search_gkg": ["GALATA WIND"]},
    "INVES": {"search_title": ["INVESTCO", "INVESTCO HOLDİNG"], "search_gkg": ["INVESTCO HOLDING"]},
    "KARTN": {"search_title": ["KARTONSAN"], "search_gkg": ["KARTONSAN"]},
    "KLSER": {"search_title": ["KALESERAMİK", "KALESERAMIK", "KALE SERAMİK"], "search_gkg": ["KALESERAMIK"]},
    "KORDS": {"search_title": ["KORDSA", "KORDSA TEKNİK"], "search_gkg": ["KORDSA"]},
    "KOTON": {"search_title": ["KOTON", "KOTON MAĞAZACILIK"], "search_gkg": ["KOTON"]},
    "LILAK": {"search_title": ["LİLA KAĞIT", "LILA KAGIT"], "search_gkg": ["LILA KAGIT"]},
    "MOGAN": {"search_title": ["MOGAN ENERJİ", "MOGAN ENERJI"], "search_gkg": ["MOGAN ENERJI"]},
    "MTRKS": {"search_title": ["MATRİKS", "MATRIKS"], "search_gkg": ["MATRIKS"]},
    "ODINE": {"search_title": ["ODİNE", "ODINE SOLUTIONS"], "search_gkg": ["ODINE"]},
    "ORGE": {"search_title": ["ORGE ENERJİ", "ORGE ENERJI"], "search_gkg": ["ORGE ENERJI"]},
    "OZATD": {"search_title": ["ÖZATA DENİZCİLİK", "OZATA DENIZCILIK"], "search_gkg": ["OZATA DENIZCILIK"]},
    "OZKGY": {"search_title": ["ÖZAK GAYRİMENKUL", "OZAK GAYRIMENKUL"], "search_gkg": ["OZAK GAYRIMENKUL"]},
    "PEKGY": {"search_title": ["PEKER GAYRİMENKUL", "PEKER GAYRIMENKUL"], "search_gkg": ["PEKER GAYRIMENKUL"]},
    "PENTA": {"search_title": ["PENTA TEKNOLOJİ", "PENTA TEKNOLOJI"], "search_gkg": ["PENTA TEKNOLOJI"]},
    "PINSU": {"search_title": ["PINAR SU"], "search_gkg": ["PINAR SU"]},
    "PNSUT": {"search_title": ["PINAR SÜT", "PINAR SUT"], "search_gkg": ["PINAR SUT"]},
    "PETUN": {"search_title": ["PINAR ET", "PINAR ENTEGRE"], "search_gkg": ["PINAR ET"]},
    "POLHO": {"search_title": ["POLİSAN", "POLISAN", "POLİSAN HOLDİNG"], "search_gkg": ["POLISAN HOLDING"]},
    "RALYH": {"search_title": ["RAL YATIRIM", "RAL YATIRIM HOLDİNG"], "search_gkg": ["RAL YATIRIM HOLDING"]},
    "RTALB": {"search_title": ["RTA LABORATUVARLARI", "RTA LAB"], "search_gkg": ["RTA LAB"]},
    "SAFKR": {"search_title": ["SAFKAR"], "search_gkg": ["SAFKAR"]},
    "SELEC": {"search_title": ["SELÇUK ECZA", "SELCUK ECZA"], "search_gkg": ["SELCUK ECZA"]},
    "SKTAS": {"search_title": ["SÖKTAŞ", "SOKTAS"], "search_gkg": ["SOKTAS"]},
    "SMART": {"search_title": ["SMARTİKS", "SMARTIKS"], "search_gkg": ["SMARTIKS"]},
    "SMRTG": {"search_title": ["SMART GÜNEŞ", "SMART GUNES", "SMART ENERJİ"], "search_gkg": ["SMART GUNES"]},
    "SNGYO": {"search_title": ["SİNPAŞ GAYRİMENKUL", "SINPAS GAYRIMENKUL"], "search_gkg": ["SINPAS GAYRIMENKUL"]},
    "SUWEN": {"search_title": ["SUWEN TEKSTİL", "SUWEN"], "search_gkg": ["SUWEN"]},
    "TARKM": {"search_title": ["TARKİM", "TARKIM"], "search_gkg": ["TARKIM"]},
    "TEZOL": {"search_title": ["EUROPAP", "TEZOL KAĞIT"], "search_gkg": ["EUROPAP"]},
    "TRCAS": {"search_title": ["TURCAS", "TURCAS HOLDİNG"], "search_gkg": ["TURCAS HOLDING"]},
    "TRGYO": {"search_title": ["TORUNLAR GAYRİMENKUL", "TORUNLAR GAYRIMENKUL"], "search_gkg": ["TORUNLAR GAYRIMENKUL"]},
    "TUREX": {"search_title": ["TUREKS TURİZM", "TUREKS TURIZM"], "search_gkg": ["TUREKS TURIZM"]},
    "TURSG": {"search_title": ["TÜRKİYE SİGORTA", "TURKIYE SIGORTA"], "search_gkg": ["TURKIYE SIGORTA"]},
    "IZENR": {"search_title": ["İZDEMİR ENERJİ", "IZDEMIR ENERJI"], "search_gkg": ["IZDEMIR ENERJI"]},
    "ISMEN": {"search_title": ["İŞ YATIRIM", "IS YATIRIM"], "search_gkg": ["IS YATIRIM"]},
    "HTTBT": {"search_title": ["HİTİT BİLGİSAYAR", "HITIT BILGISAYAR"], "search_gkg": ["HITIT"]},
    "TSPOR": {"search_title": ["TRABZONSPOR"], "search_gkg": ["TRABZONSPOR"]},
    "BJKAS": {"search_title": ["BEŞİKTAŞ", "BESIKTAS"], "search_gkg": ["BESIKTAS"]},
    "GSRAY": {"search_title": ["GALATASARAY", "GALATASARAY SPORTİF"], "search_gkg": ["GALATASARAY"]},
    "FENER": {"search_title": ["FENERBAHÇE", "FENERBAHCE"], "search_gkg": ["FENERBAHCE"]},
}

TICKER_ALIASES = ["THYAO", "ISE", "SISE", "CCOLA", "HALKB", "TUPRS",
                   "VAKBN", "YKBNK", "ISCTR", "AKBNK", "GARAN", "KCHOL",
                   "SAHOL", "ASELS", "KRDMD", "TCELL", "BIMAS", "MGROS",
                   "PGSUS", "TOASO", "EKGYO", "ARCLK"]


def scrape_bist_companies(url: str) -> list[dict]:
    cfg = get_config()["generate_bist_mapping"]
    headers = {"User-Agent": cfg["user_agent"]}
    resp = requests.get(url, headers=headers, timeout=cfg["timeout"])
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    companies = []
    seen_tickers: set[str] = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        cells = rows[0].find_all("td")
        if len(cells) < 2:
            continue

        ticker_cell = cells[0].get_text(strip=True)
        name_cell = cells[1].get_text(strip=True)

        if "," in ticker_cell:
            tickers = [t.strip() for t in ticker_cell.split(",")]
            primary = tickers[0]
        else:
            primary = ticker_cell[:5] if len(ticker_cell) >= 5 else ticker_cell

        if not re.match(r'^[A-Z0-9]+$', primary) or len(primary) < 3:
            continue
        if primary in seen_tickers:
            continue
        seen_tickers.add(primary)

        companies.append({"ticker": primary, "name_tr": name_cell})

    return companies


def ascii_normalize(text: str) -> str:
    return text.translate(TURKISH_CHAR_MAP)


def strip_legal_suffix(name: str) -> str:
    upper = name.upper()
    for suffix in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
        if upper.endswith(suffix.upper()):
            result = name[:-len(suffix)].strip()
            result = result.rstrip(".-, ")
            return result
    return name


def generate_search_terms(name_tr: str) -> tuple[list[str], list[str]]:
    core = strip_legal_suffix(name_tr)
    core_upper = core.upper()
    core_ascii = ascii_normalize(core_upper)

    words = core_upper.split()
    if len(words) >= 2:
        title_terms = [" ".join(words[:3]), " ".join(words[:2])]
    else:
        title_terms = [core_upper]

    title_terms = [t.upper() for t in title_terms]
    ascii_terms = [ascii_normalize(t) for t in title_terms]
    all_title_terms = list(set(title_terms + ascii_terms))

    return all_title_terms, [core_ascii]


def build_companies_dict(companies: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for comp in companies:
        ticker = comp["ticker"]
        name_tr = comp["name_tr"]

        if ticker in MANUAL_OVERRIDES:
            entry = MANUAL_OVERRIDES[ticker].copy()
            entry["name_tr"] = name_tr
            result[ticker] = entry
        else:
            search_title, search_gkg = generate_search_terms(name_tr)
            result[ticker] = {"name_tr": name_tr, "search_title": search_title, "search_gkg": search_gkg}
    return result


def generate_bist_mapping() -> dict[str, dict]:
    url = get_config()["generate_bist_mapping"]["scrape_url"]
    companies = scrape_bist_companies(url)
    return build_companies_dict(companies)


def save_bist_mapping(path: str | None = None) -> dict[str, dict]:
    mapping = generate_bist_mapping()
    if path is None:
        cfg = get_config()["generate_bist_mapping"]
        path = str(Path(__file__).parent.parent.parent / cfg["output_dir"] / cfg["output_filename"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    return mapping


def load_bist_mapping(path: str | None = None) -> dict[str, dict]:
    file_path = path or str(Path(__file__).parent.parent.parent / "data" / "bist_companies.json")
    with open(file_path, encoding="utf-8") as f:
        return json.load(f)
