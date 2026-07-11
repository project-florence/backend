# mock for now, as compromise
def get_upcoming_ipos():
    return """
    [{
  "symbol": "SARAE",
  "companyName": "Sa-Ra Enerji İnşaat Tic. ve San. A.Ş.",
  "companyDescription": "Lorem ipsum...",

  "offering": {
    "offeringDate": {
      "start": "2026-07-08",
      "end": "2026-07-10"
    },
    "price": 70.0,
    "currency": "TRY",

    "distributionMethod": "equal",

    "broker": "Tera Yatırım Menkul Değerler A.Ş.",

    "market": {
      "exchange": "BIST",
      "segment": "Yıldız Pazar"
    },

    "totalShares": 89000000,
    "publicFloat": 20.02,
    "offeringSize": 6230000000,

    "firstTradingDate": null,

    "priceStabilization": false,
    "discountRate": 20,

    "lockup": {
      "issuerMonths": 12,
      "shareholdersMonths": 12
    }
  },

  "shareStructure": {
    "capitalIncreaseShares": 44500000,
    "existingShareholderSale": [
      {
        "shareholder": "Şadi Türk",
        "shares": 35600000
      },
      {
        "shareholder": "Hilkat Mor",
        "shares": 8900000
      }
    ]
  },

  "allocation": [
    {
      "group": "Domestic Individual Investors",
      "shares": 33820000,
      "percentage": 38
    },
    {
      "group": "Company Employees",
      "shares": 1780000,
      "percentage": 2
    },
    {
      "group": "High Application Investors",
      "shares": 8900000,
      "percentage": 10
    },
    {
      "group": "Domestic Institutional Investors",
      "shares": 22250000,
      "percentage": 25
    },
    {
      "group": "Foreign Institutional Investors",
      "shares": 22250000,
      "percentage": 25
    }
  ],

  "useOfFunds": [
    {
      "purpose": "Investment Expenditures",
      "percentage": 15
    },
    {
      "purpose": "Working Capital",
      "percentage": 55
    },
    {
      "purpose": "Financial Debt Repayment",
      "percentage": 30
    }
  ],

  "subscription": {
    "method": "Fixed Price Book Building",
    "brokerageMethod": "Best Effort"
  },

  "estimatedAllocation": [
    {
      "participants": 150000,
      "estimatedShares": 225,
      "estimatedCost": 15750
    },
    {
      "participants": 250000,
      "estimatedShares": 135,
      "estimatedCost": 9450
    },
    {
      "participants": 350000,
      "estimatedShares": 96,
      "estimatedCost": 6720
    },
    {
      "participants": 500000,
      "estimatedShares": 67,
      "estimatedCost": 4690
    },
    {
      "participants": 700000,
      "estimatedShares": 48,
      "estimatedCost": 3360
    },
    {
      "participants": 1100000,
      "estimatedShares": 31,
      "estimatedCost": 2170
    },
    {
      "participants": 1600000,
      "estimatedShares": 21,
      "estimatedCost": 1470
    },
    {
      "participants": 2200000,
      "estimatedShares": 16,
      "estimatedCost": 1120
    }
  ],

  "financials": [
    {
      "period": "2026-Q1",
      "revenue": 3300000000,
      "grossProfit": 904900000
    },
    {
      "period": "2025",
      "revenue": 7000000000,
      "grossProfit": 1900000000
    },
    {
      "period": "2024",
      "revenue": 14300000000,
      "grossProfit": 3400000000
    }
  ],

  "references": {
    "prospectus": "https://...",
    "spkBulletin": "2026/43"
  },

  "metadata": {
    "lastUpdated": "2026-07-11T18:35:00Z",
    "source": "ArzHalk",
    "status": "upcoming"
  }
}]
    """