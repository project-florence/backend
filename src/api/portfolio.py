from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from src.analysis.stock_vector import (
    read_vectors_from_redis, write_vectors_to_redis,
    average_vector, estimate_profile, company_vector,
    vector_to_list, euclidean_distance, VECTOR_KEYS,
)

router = APIRouter()


class PortfolioProfileRequest(BaseModel):
    tickers: list[str]
    limit: int = 5

    @field_validator("tickers")
    @classmethod
    def validate_tickers(cls, v):
        if not v:
            raise ValueError("tickers list cannot be empty")
        if len(v) > 50:
            raise ValueError("Maximum 50 tickers allowed")
        return [t.upper() for t in v]

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v):
        if v < 1 or v > 50:
            raise ValueError("limit must be between 1 and 50")
        return v


@router.post("/portfolio/profile")
def portfolio_profile(body: PortfolioProfileRequest):
    tickers = body.tickers
    redis_data = read_vectors_from_redis(tickers)

    missing = [t for t in tickers if redis_data.get(t) is None]
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

    vectors = []
    ticker_vectors = []
    for ticker in tickers:
        vec = redis_data.get(ticker)
        if vec is not None:
            vectors.append(vec)
            ticker_vectors.append({"ticker": ticker, "vector": vec})

    if not vectors:
        raise HTTPException(status_code=400, detail="No vector data available for given tickers")

    avg = average_vector(vectors)
    profile = estimate_profile(avg)

    from src.services.stats import get_popular_tickers
    candidates = get_popular_tickers(100)
    candidate_data = read_vectors_from_redis(candidates)
    candidate_missing = [t for t, v in candidate_data.items() if v is None]
    if candidate_missing:
        to_write = []
        for ticker in candidate_missing:
            p = get_company_info(ticker)
            if p:
                vd = company_vector(p)
                to_write.append({"ticker": ticker, **vd})
                candidate_data[ticker] = vector_to_list(vd)
        if to_write:
            write_vectors_to_redis(to_write)

    for t in tickers:
        candidate_data.pop(t, None)

    scored = []
    for ticker, vec in candidate_data.items():
        if vec is not None:
            dist = euclidean_distance(avg, vec)
            scored.append({
                "ticker": ticker,
                "vector": vec,
                "score": round(1 / (1 + dist), 4),
                "distance": dist,
            })
    scored.sort(key=lambda x: -x["score"])

    return {
        "avg_vector": avg,
        "estimated_profile": profile,
        "portfolio": ticker_vectors,
        "similar_stocks": scored[:body.limit],
    }
