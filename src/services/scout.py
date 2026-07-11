def scout_best_tickers():
    return """{
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
        "rank": 1,
        "risk_level": "moderate",
        "expected_return_range": {"low": 12.5, "high": 34.0, "unit": "percent_annual"},
        "score_breakdown": {
          "volatility": 0.62, "growth": 0.81, "dividend_yield": 0.30,
          "valuation": 0.55, "liquidity": 0.90, "news_sentiment": 0.74
        },
        "match_reasons": [
          "Risk toleransınıza uygun volatilite aralığında",
          "Son 3 çeyrekte istikrarlı gelir büyümesi"
        ],
        "confidence": 0.82,
        "last_updated": "2026-07-11T09:30:00Z"
      }
    },
    {
      "ticker": "THYAO",
      "company_name": "Türk Hava Yolları A.O.",
      "sector": "transportation",
      "current_price": 312.10,
      "currency": "TRY",
      "affordable_lots": 160,
      "analysis": {
        "overall_score": 79.6,
        "rank": 2,
        "risk_level": "moderate_high",
        "expected_return_range": {"low": 8.0, "high": 41.0, "unit": "percent_annual"},
        "score_breakdown": {
          "volatility": 0.48, "growth": 0.70, "dividend_yield": 0.15,
          "valuation": 0.62, "liquidity": 0.95, "news_sentiment": 0.58
        },
        "match_reasons": [
          "Yüksek işlem hacmi ve likidite",
          "Sektörel büyüme beklentisi hedef getiriyle uyumlu"
        ],
        "confidence": 0.75,
        "last_updated": "2026-07-11T09:30:00Z"
      }
    }
  ],
  "meta": {
    "total_candidates_evaluated": 214,
    "total_after_hard_filter": 38,
    "returned_count": 2,
    "algorithm_version": "the algoritma"
  }
}"""
