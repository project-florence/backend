import math
from typing import Any

from src.analysis.metrics import _g, earnings_yield, peg_ratio, price_to_sales, fcf_yield, cash_to_debt, quick_ratio


def clip(val: float | None, min_val: float, max_val: float) -> float:
    if val is None or math.isnan(val):
        return 0.5
    return float(max(min(val, max_val), min_val))


def company_vector(profile: dict) -> dict[str, float]:
    beta = _g(profile, "trading", "beta")

    avg_vol = _g(profile, "trading", "averageVolume")
    price = _g(profile, "market", "currentPrice") or 1.0
    dollar_vol = (avg_vol * price) if avg_vol else None

    ey = earnings_yield(profile)
    peg = peg_ratio(profile)
    ps = price_to_sales(profile)

    fcf_y = fcf_yield(profile)
    c2d = cash_to_debt(profile)
    qr = quick_ratio(profile)

    beta_norm = clip(beta, 0.0, 2.5) / 2.5

    if dollar_vol and dollar_vol > 100_000:
        log_dv = math.log10(dollar_vol)
        dv_norm = clip((log_dv - 5) / (9 - 5), 0.0, 1.0)
    else:
        dv_norm = 0.0
    illiquidity_norm = 1.0 - dv_norm

    risk_score = round(0.6 * beta_norm + 0.4 * illiquidity_norm, 2)

    ey_norm = clip(ey, 0.0, 20.0) / 20.0
    val_vade = 1.0 - ey_norm

    peg_norm = clip(peg, 0.0, 3.0) / 3.0
    ps_norm = clip(ps, 0.0, 15.0) / 15.0

    horizon_score = round(0.4 * val_vade + 0.3 * peg_norm + 0.3 * ps_norm, 2)

    fcf_norm = clip(fcf_y, 0.0, 15.0) / 15.0
    c2d_norm = clip(c2d, 0.0, 2.0) / 2.0
    qr_norm = clip((qr or 1.0) - 0.5, 0.0, 1.5) / 1.5

    profitability_score = round(0.5 * fcf_norm + 0.3 * c2d_norm + 0.2 * qr_norm, 2)

    return {
        "risk": risk_score,
        "horizon": horizon_score,
        "profitability": profitability_score,
    }


VECTOR_KEYS = ["risk", "horizon", "profitability"]


def _load_map(section: str) -> dict[str, float]:
    from src.core.config import get_config
    try:
        return dict(get_config()["stock_vector"][section])
    except Exception:
        return {}


RISK_TOLERANCE_MAP = _load_map("risk_tolerance_map")
HORIZON_MAP = _load_map("horizon_map")
PROFITABILITY_MAP = _load_map("profitability_map")


def vector_to_list(v: dict[str, float]) -> list[float]:
    return [v[k] for k in VECTOR_KEYS]


def vector_to_dict(v: list[float]) -> dict[str, float]:
    return dict(zip(VECTOR_KEYS, v))


def company_vector_as_list(profile: dict) -> list[float]:
    return vector_to_list(company_vector(profile))


def compute_vectors_for_top_as_dicts(n: int = 10) -> list[dict[str, Any]]:
    from src.services.stats import get_popular_tickers
    from src.services.company import get_company_info

    tickers = get_popular_tickers(n)
    result = []
    for ticker in tickers:
        profile = get_company_info(ticker)
        if profile:
            vec = company_vector(profile)
            result.append({"ticker": ticker, **vec})
    return result


def compute_vectors_for_top_as_lists(n: int = 10) -> list[dict[str, Any]]:
    from src.services.stats import get_popular_tickers
    from src.services.company import get_company_info

    tickers = get_popular_tickers(n)
    result = []
    for ticker in tickers:
        profile = get_company_info(ticker)
        if profile:
            vec = company_vector_as_list(profile)
            result.append({"ticker": ticker, "vector": vec})
    return result


def _vector_ttl() -> int:
    from src.core.config import get_config
    try:
        return get_config()["stock_vector"]["ttl"]
    except Exception:
        return 86400


def write_vectors_to_redis(
    vectors: list[dict[str, Any]], ttl: int | None = None
) -> None:
    if ttl is None:
        ttl = _vector_ttl()
    from src.core.redis import r
    import json

    pipe = r.pipeline()
    for item in vectors:
        ticker = item["ticker"]
        vec = {k: item[k] for k in VECTOR_KEYS if k in item}
        if not vec:
            vec = dict(zip(VECTOR_KEYS, item.get("vector", [])))
        key = f"stock_vector:{ticker}"
        pipe.set(key, json.dumps(vec), ex=ttl)
    pipe.execute()


def read_vectors_from_redis(
    tickers: list[str],
) -> dict[str, list[float] | None]:
    from src.core.redis import r
    import json

    result = {}
    for ticker in tickers:
        key = f"stock_vector:{ticker}"
        raw = r.get(key)
        if raw:
            vec = json.loads(raw)
            result[ticker] = vector_to_list(vec)
        else:
            result[ticker] = None
    return result


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return round(dot / (na * nb), 4)


def euclidean_distance(a: list[float], b: list[float]) -> float:
    return round(math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b))), 4)


def weighted_distance(
    stock_vec: list[float],
    horizon_target: float,
    profitability_target: float,
    risk_tolerance: float,
) -> float:
    w_risk = 1.0 - risk_tolerance
    dist = math.sqrt(
        w_risk * (stock_vec[0] - 0.0) ** 2
        + (stock_vec[1] - horizon_target) ** 2
        + (stock_vec[2] - profitability_target) ** 2
    )
    return round(dist, 4)


def rank_by_similarity(
    query: dict[str, float],
    n: int = 5,
    top_n: int = 50,
) -> list[dict[str, Any]]:
    from src.services.stats import get_popular_tickers

    horizon_target = query["horizon_target"]
    profitability_target = query["profitability_target"]
    risk_tolerance = query["risk_tolerance"]

    tickers = get_popular_tickers(top_n)
    redis_data = read_vectors_from_redis(tickers)

    missing = [t for t, v in redis_data.items() if v is None]
    if missing:
        from src.services.company import get_company_info

        to_write = []
        for ticker in missing:
            profile = get_company_info(ticker)
            if profile:
                vec_d = company_vector(profile)
                to_write.append({"ticker": ticker, **vec_d})
                redis_data[ticker] = vector_to_list(vec_d)
        if to_write:
            write_vectors_to_redis(to_write)

    scored = []
    for ticker, vec in redis_data.items():
        if vec is not None:
            dist = weighted_distance(
                vec, horizon_target, profitability_target, risk_tolerance
            )
            score = round(1 / (1 + dist), 4)
            scored.append({
                "ticker": ticker,
                "vector": vec,
                "score": score,
                "distance": dist,
            })

    scored.sort(key=lambda x: -x["score"])
    return scored[:n]


def build_query(
    horizon: str, profitability: str, risk_tolerance: str
) -> dict[str, float]:
    return {
        "horizon_target": HORIZON_MAP[horizon],
        "profitability_target": PROFITABILITY_MAP[profitability],
        "risk_tolerance": RISK_TOLERANCE_MAP[risk_tolerance],
    }