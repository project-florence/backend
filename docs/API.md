# Project Florence API

Base URL: `http://localhost:7055`

---

## Health & Meta

### `GET /`

Root endpoint. Boş obje döndürür.

**Response:** `{}`

---

### `GET /health`

Sunucunun çalışıp çalışmadığını kontrol eder.

**Response:**
```json
{"status": "ok"}
```

---

## BIST / Companies

### `GET /api/v1/bist/companies`

BIST'teki tüm şirketlerin listesini döndürür. Veri `pykap` kütüphanesi aracılığıyla KAP'tan çekilir ve Redis'te cache'lenir (30 gün TTL).

**Response (array):**
```json
[
  {
    "ticker": "A1CAP",
    "name": "A1 CAPİTAL YATIRIM MENKUL DEĞERLER A.Ş.",
    "summary_page": "https://www.kap.org.tr/tr/sirket/A1CAP",
    "city": "İSTANBUL",
    "auditor": "PKF ADAY BAĞIMSIZ DENETİM A.Ş.",
    "company_id": "4028e4a1416e696301416f37201c5f2e"
  }
]
```

---

### `GET /api/v1/bist/tickers`

BIST'teki tüm hisse sembollerinin listesini döndürür.

**Response:**
```json
["A1CAP", "A1YEN", "ACP", "ACSEL", "ADEL", "THYAO", "ASELS", ...]
```

---

### `GET /api/v1/companies/search/{ticker}`

Bir hisse senedi hakkında KAP (Kamuyu Aydınlatma Platformu) üzerinden arama yapar.

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü (örn. `ASELS`) |

**Response:** KAP API cevabı (pykap üzerinden).

---

### `GET /api/v1/companies/info/{ticker}`

Bir hisse senedinin detaylı finansal bilgilerini döndürür. Veri yfinance (Yahoo Finance) üzerinden çekilir ve Redis'te cache'lenir (24 saat TTL).

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü (örn. `ASELS`) |

**Response:**
```json
{
  "symbol": "ASELS.IS",
  "name": "ASELSAN",
  "sector": "Industrials",
  "industry": "Aerospace & Defense",
  "currency": "TRY",
  "exchange": "IST",
  "market": {
    "currentPrice": 370.0,
    "marketCap": 1687199940608,
    "dayHigh": 375.25,
    "dayLow": 355.0,
    "regularMarketVolume": 54713081,
    "fiftyTwoWeekHigh": 450.0,
    "fiftyTwoWeekLow": 151.4
  },
  "valuation": {
    "trailingPE": 51.82,
    "forwardPE": 18.10,
    "priceToBook": 6.0,
    "dividendYield": 0.12,
    "payoutRatio": 0.03,
    "targetMeanPrice": 380.66,
    "targetHighPrice": 495.0,
    "targetLowPrice": 249.5
  },
  "financials": {
    "totalRevenue": 184925585408,
    "grossProfits": 59037466624,
    "netIncomeToCommon": 32511891456,
    "profitMargins": 0.18,
    "operatingMargins": 0.21,
    "revenueGrowth": 0.15,
    "earningsGrowth": 0.86,
    "returnOnEquity": 0.15,
    "ebitda": 48649863168
  },
  "balanceSheet": {
    "totalCash": 31005730816,
    "totalDebt": 53089816576,
    "debtToEquity": 18.78,
    "currentRatio": 1.54
  }
}
```

---

## Reports

### `GET /api/v1/generate/report/quick/{ticker}`

Hızlı haber raporu oluşturur. (Şu an placeholder — Lorem Ipsum döndürür.)

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |

---

### `GET /api/v1/generate/report/deep/{ticker}`

Derinlemesine haber raporu oluşturur. (Şu an placeholder — Lorem Ipsum döndürür.)

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |

---

## News

### `GET /api/v1/news/{ticker}`

Bir hisse senedi hakkındaki en son haberleri getirir. Veri GDELT (Global Database of Events, Language, and Tone) üzerinden BigQuery aracılığıyla çekilir. Redis'te cache'lenir (1 saat TTL).

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü (örn. `ASELS`) |
| `amount` | query | hayır (default: 10) | Dönecek haber sayısı |

**Response (array):**
```json
[
  {
    "url": "https://haber7.com/ekonomi/haber/...",
    "title": "ASELSAN borsa performansıyla dünyanın önde gelen savunma şirketlerini solladı",
    "lang": "TURKISH",
    "date": "2026-07-11T13:47:08+00:00"
  }
]
```

---

## Simulations

### `GET /api/v1/simulations/probability/{ticker}`

Monte Carlo simülasyonu ile bir hissenin belirli bir hedef fiyata ulaşma olasılığını tahmin eder. (Şu an mock veri döndürür.)

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |
| `time` | query | evet | Zaman aralığı (ör. `1mo`, `3mo`) |
| `target` | query | evet | Hedef fiyat |

**Response:**
```json
{
  "percent": 23,
  "ticker": "ASELS",
  "time": "3mo",
  "target": "400"
}
```

---

### `GET /api/v1/simulations/confidence-interval/{ticker}`

Bir hisse için güven aralığı tahmini döndürür. (Şu an mock veri döndürür.)

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |
| `time` | query | evet | Zaman aralığı |
| `bound` | query | evet | Güven aralığı yüzdesi (ör. 95) |

**Response:**
```json
{
  "min": 85,
  "max": 140,
  "currency": "TRY",
  "ticker": "ASELS",
  "time": "3mo",
  "bound": 95
}
```

---

## Economy

### `GET /api/v1/economy/gold-prices`

Güncel gram/çeyrek/yarım/tam altın fiyatlarını döndürür. Veri CollectAPI üzerinden çekilir, Redis'te cache'lenir (10 dk TTL).

**Response:** CollectAPI cevabı (`result` alanı).

---

### `GET /api/v1/economy/silver-prices`

Güncel gümüş fiyatlarını döndürür. Veri CollectAPI üzerinden çekilir, Redis'te cache'lenir (10 dk TTL).

---

### `GET /api/v1/economy/symbols`

Desteklenen döviz sembollerinin listesini döndürür.

---

### `GET /api/v1/economy/currency`

Tüm döviz kurlarını (USD, EUR, GBP, vs.) döndürür.

**Response (örnek):**
```json
[
  {
    "code": "USD",
    "name": "US Dolar",
    "rate": 37.85
  },
  {
    "code": "EUR",
    "name": "Euro",
    "rate": 41.23
  }
]
```

---

## Macro Economy

### `GET /api/v1/macroeconomy/all`

Temel makroekonomik göstergeleri döndürür (faiz, enflasyon, işsizlik, cari açık, GSYİH). (Henüz implement edilmedi.)

**Response:**
```json
{"henuz implement edilmedi. key value seklinde faiz, gdp, issizlik, cari acik, enflasyon vb. gibi verileri dondurur."}
```

---

## IPO

### `GET /api/v1/ipos/upcoming`

Yaklaşan halka arzları döndürür.

**Response:** IPO listesi (JSON).

---

## Scout / Stock Picker

### `GET /api/v1/scout/best-tickers`

Yatırımcının bütçe, risk toleransı ve zaman aralığına göre en uygun hisseleri önerir. (Mock veri.)

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `investment_budget` | query | evet | Yatırım bütçesi (TL) |
| `investment_horizon` | query | evet | Vade: `short_term`, `medium_term`, `long_term` |
| `risk_tolerance` | query | evet | Risk: `low`, `moderate`, `high` |

**Response:**
```json
{
  "query": {
    "investment_budget": 50000,
    "investment_horizon": "long_term",
    "risk_tolerance": "moderate"
  },
  "results": [
    {
      "ticker": "ASELS",
      "company_name": "Aselsan Elektronik Sanayi ve Ticaret A.Ş.",
      "sector": "defense_electronics",
      "current_price": 78.45,
      "currency": "TRY",
      "affordable_lots": 637,
      "analysis": {
        "overall_score": 87.3,
        "risk_level": "moderate",
        "expected_return_range": { "low": 12.5, "high": 34.0, "unit": "percent_annual" },
        "score_breakdown": {
          "volatility": 0.62, "growth": 0.81, "dividend_yield": 0.30,
          "valuation": 0.55, "liquidity": 0.90, "news_sentiment": 0.74
        },
        "confidence": 0.82
      }
    }
  ],
  "meta": {
    "total_candidates_evaluated": 214,
    "algorithm_version": "the algoritma"
  }
}
```

---

## Price History

### `GET /api/v1/price/history/{ticker}`

Bir hisse senedinin geçmiş fiyat verilerini döndürür. Candlestick chart (mum grafiği) çizmek için gerekli `ts`, `open`, `high`, `low`, `close`, `volume` alanlarını içerir. Veri yfinance'tan çekilir ve PostgreSQL'de persist edilir. Her sorgu için yfinance'a gerek yoktur — eksik data varsa otomatik olarak sadece eksik kısım çekilir.

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü (örn. `ASELS`) |
| `period` | query | hayır (default: `1mo`) | `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max` |
| `interval` | query | hayır (default: `1d`) | `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `1h`, `4h`, `1d`, `5d`, `1wk`, `1mo`, `3mo` |

**Not:** yfinance rate limit engeli için her çağrı arasında minimum 5 saniye zorunlu bekleme vardır. Bu yüzden ilk sorgu biraz uzun sürebilir. Sonraki sorgular PostgreSQL'den anında gelir.

**Response:**
```json
[
  {
    "ts": "2026-07-06T21:00:00+00:00",
    "open": 402.75,
    "high": 410.0,
    "low": 382.75,
    "close": 383.0,
    "volume": 65463431
  },
  {
    "ts": "2026-07-07T21:00:00+00:00",
    "open": 383.0,
    "high": 385.0,
    "low": 370.0,
    "close": 375.0,
    "volume": 42134567
  }
]
```

---

## Auth (Mock)

### `POST /api/v1/auth/register`

Kullanıcı kaydı. (Şu an mock — ileride JWT tabanlı olacak.)

**Request body:** Yok (şu an).

**Response:**
```json
{"message": "Kayit olundu! Burada daha sonradan JWT alisverisi olacak."}
```

---

### `POST /api/v1/auth/login`

Kullanıcı girişi. (Şu an mock — ileride JWT token dönecek.)

**Response:**
```json
{"message": "Giris yapildi! Burada daha sonradan JWT alisverisi olacak."}
```

---

## Favorites (Mock)

### `POST /api/v1/favorites/{ticker}`

Bir hisse senedini favorilere ekler.

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |

**Response:**
```json
{"message": "ASELS favorilere eklendi!"}
```

---

### `DELETE /api/v1/favorites/{ticker}`

Bir hisse senedini favorilerden çıkarır.

| Param | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `ticker` | path | evet | Hisse sembolü |

**Response:**
```json
{"message": "ASELS favorilerden cikarildi!"}
```

---

### `GET /api/v1/favorites`

Favori hisse senetlerinin listesini döndürür.

**Response:**
```json
{
  "favorites": ["ASELS", "THYAO", "GARAN", "KCHOL", "SISE"]
}
```

---

## Genel Notlar

- **Ticker validation:** Hisse sembolü alan tüm endpoint'ler (`{ticker}`), geçersiz bir sembol girildiğinde `404 Invalid BIST ticker` hatası döndürür.
- **Cache mekanizması:**
  - Company info → Redis (24 saat TTL)
  - News → Redis (1 saat TTL)
  - Price history → PostgreSQL (kalıcı, sadece eksik data çekilir)
  - Economy → Redis (10 dk TTL)
  - BIST tickers → Redis (30 gün TTL)
- **Rate limit:** Price history endpoint'i yfinance çağrıları arasında 5 saniye zorunlu bekleme uygular.
- **Encoding:** Veriler UTF-8'dir. GDELT'ten gelen Türkçe karakter bozulmaları otomatik onarılır (mojibake fix).
