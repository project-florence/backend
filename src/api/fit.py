from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from src.analysis.stock_vector import build_query, rank_by_similarity

router = APIRouter()

VALID_HORIZON = {"short", "medium", "long"}
VALID_PROFITABILITY = {"low", "medium", "high"}
VALID_RISK_TOLERANCE = {"low", "medium", "high"}


class FitRequest(BaseModel):
    horizon: str = "long"
    profitability: str = "high"
    risk_tolerance: str = "medium"
    limit: int = 5

    @field_validator("horizon")
    @classmethod
    def validate_horizon(cls, v):
        if v not in VALID_HORIZON:
            raise ValueError(f"Must be one of: {', '.join(sorted(VALID_HORIZON))}")
        return v

    @field_validator("profitability")
    @classmethod
    def validate_profitability(cls, v):
        if v not in VALID_PROFITABILITY:
            raise ValueError(f"Must be one of: {', '.join(sorted(VALID_PROFITABILITY))}")
        return v

    @field_validator("risk_tolerance")
    @classmethod
    def validate_risk_tolerance(cls, v):
        if v not in VALID_RISK_TOLERANCE:
            raise ValueError(f"Must be one of: {', '.join(sorted(VALID_RISK_TOLERANCE))}")
        return v

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v):
        if v < 1 or v > 100:
            raise ValueError("Must be between 1 and 100")
        return v


@router.post("/stocks/fit")
def fit_stocks(body: FitRequest):
    try:
        query = build_query(body.horizon, body.profitability, body.risk_tolerance)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Invalid value: {e}")

    results = rank_by_similarity(query, n=body.limit)
    return {"query": query, "results": results}
