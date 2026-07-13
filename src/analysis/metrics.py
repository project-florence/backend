from typing import Any


def _g(d: dict[str, Any] | None, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


def peg_ratio(profile: dict) -> float | None:
    pe = _g(profile, "valuation", "trailingPE")
    g = _g(profile, "financials", "earningsGrowth")
    if pe and g:
        return round(pe / (g * 100), 2)
    return _g(profile, "valuation", "pegRatio")


def earnings_yield(profile: dict) -> float | None:
    pe = _g(profile, "valuation", "trailingPE")
    if pe:
        return round(1 / pe * 100, 2)
    return None


def fcf_yield(profile: dict) -> float | None:
    fcf = _g(profile, "financials", "freeCashflow")
    mcap = _g(profile, "market", "marketCap")
    if fcf and mcap:
        return round(fcf / mcap * 100, 2)
    return None


def price_to_sales(profile: dict) -> float | None:
    ps = _g(profile, "valuation", "priceToSalesTrailing12Months")
    if ps:
        return round(ps, 2)
    mcap = _g(profile, "market", "marketCap")
    rev = _g(profile, "financials", "totalRevenue")
    if mcap and rev:
        return round(mcap / rev, 2)
    return None


def ev_to_ebitda(profile: dict) -> float | None:
    ev_ebitda = _g(profile, "valuation", "enterpriseToEbitda")
    if ev_ebitda:
        return round(ev_ebitda, 2)
    ev = _g(profile, "valuation", "enterpriseValue")
    ebitda = _g(profile, "financials", "ebitda")
    if ev and ebitda:
        return round(ev / ebitda, 2)
    return None


def book_value_per_share(profile: dict) -> float | None:
    bv = _g(profile, "valuation", "bookValue")
    if bv:
        return round(bv, 2)
    shares = _g(profile, "trading", "sharesOutstanding")
    equity = _g(profile, "valuation", "enterpriseValue")
    if shares and equity:
        return round(equity / shares, 2)
    return None


def target_upside(profile: dict) -> float | None:
    price = _g(profile, "market", "currentPrice")
    target = _g(profile, "valuation", "targetMeanPrice")
    if price and target:
        return round((target - price) / price * 100, 2)
    return None


def volume_ratio(profile: dict) -> float | None:
    vol = _g(profile, "market", "regularMarketVolume")
    avg = _g(profile, "trading", "averageVolume")
    if vol and avg:
        return round(vol / avg, 2)
    return None


def dollar_volume(profile: dict) -> float | None:
    vol = _g(profile, "market", "regularMarketVolume")
    price = _g(profile, "market", "currentPrice")
    if vol and price:
        return round(vol * price, 2)
    return None


def fifty_two_week_position(profile: dict) -> float | None:
    price = _g(profile, "market", "currentPrice")
    high = _g(profile, "market", "fiftyTwoWeekHigh")
    low = _g(profile, "market", "fiftyTwoWeekLow")
    if price and high and low and (high - low) > 0:
        return round((price - low) / (high - low) * 100, 2)
    return None


def volatility_classification(profile: dict) -> str | None:
    beta = _g(profile, "trading", "beta")
    if beta is None:
        return None
    if beta < 0.5:
        return "dusuk"
    if beta < 1.0:
        return "orta"
    if beta < 1.5:
        return "yuksek"
    return "cok_yuksek"


def liquidity_classification(profile: dict) -> str | None:
    avg_vol = _g(profile, "trading", "averageVolume")
    price = _g(profile, "market", "currentPrice")
    if avg_vol is None:
        return None
    dv = avg_vol * (price or 1)
    if dv > 500_000_000:
        return "cok_yuksek"
    if dv > 100_000_000:
        return "yuksek"
    if dv > 20_000_000:
        return "orta"
    return "dusuk"


def cash_to_debt(profile: dict) -> float | None:
    cash = _g(profile, "balanceSheet", "totalCash")
    debt = _g(profile, "balanceSheet", "totalDebt")
    if cash is not None and debt and debt > 0:
        return round(cash / debt, 2)
    return None


def quick_ratio(profile: dict) -> float | None:
    qr = _g(profile, "balanceSheet", "quickRatio")
    if qr:
        return round(qr, 2)
    return _g(profile, "balanceSheet", "currentRatio")


def sma_distance(profile: dict, ma: str = "fiftyDayAverage") -> float | None:
    price = _g(profile, "market", "currentPrice")
    avg = _g(profile, "trading", ma)
    if price and avg:
        return round((price - avg) / avg * 100, 2)
    return None


ALL_METRICS = {
    "peg_ratio": peg_ratio,
    "earnings_yield": earnings_yield,
    "fcf_yield": fcf_yield,
    "price_to_sales": price_to_sales,
    "ev_to_ebitda": ev_to_ebitda,
    "book_value_per_share": book_value_per_share,
    "target_upside": target_upside,
    "volume_ratio": volume_ratio,
    "dollar_volume": dollar_volume,
    "fifty_two_week_position": fifty_two_week_position,
    "volatility_classification": volatility_classification,
    "liquidity_classification": liquidity_classification,
    "cash_to_debt": cash_to_debt,
    "quick_ratio": quick_ratio,
    "sma_50_distance": lambda p: sma_distance(p, "fiftyDayAverage"),
    "sma_200_distance": lambda p: sma_distance(p, "twoHundredDayAverage"),
}


def compute_all(profile: dict) -> dict[str, Any]:
    return {name: fn(profile) for name, fn in ALL_METRICS.items()}
