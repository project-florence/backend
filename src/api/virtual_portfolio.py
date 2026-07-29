from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from datetime import datetime

from src.api.deps import get_current_user
from src.services import portfolio as svc

router = APIRouter(prefix="/portfolios")


class CreatePortfolioBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    initial_balance: float = Field(gt=0)


class RenamePortfolioBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class DuplicatePortfolioBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class AddTransactionBody(BaseModel):
    ticker: str = Field(min_length=1)
    type: str = Field(pattern="^(BUY|SELL)$")
    quantity: float = Field(gt=0)


class UpdateTransactionBody(BaseModel):
    price: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)


@router.post("")
def create_portfolio(body: CreatePortfolioBody, user_id: int = Depends(get_current_user)):
    result = svc.create_portfolio(user_id, body.name, body.initial_balance)
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to create portfolio")
    return result.model_dump()


@router.get("")
def list_portfolios(user_id: int = Depends(get_current_user)):
    result = svc.list_portfolios(user_id)
    return [p.model_dump() for p in result]


@router.get("/{portfolio_id}")
def get_portfolio(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.load_portfolio(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result.model_dump()


@router.put("/{portfolio_id}")
def rename_portfolio(portfolio_id: str, body: RenamePortfolioBody, user_id: int = Depends(get_current_user)):
    if not svc.rename_portfolio(portfolio_id, body.name):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"message": "Portfolio renamed"}


@router.delete("/{portfolio_id}")
def delete_portfolio(portfolio_id: str, user_id: int = Depends(get_current_user)):
    if not svc.delete_portfolio(portfolio_id):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"message": "Portfolio deleted"}


@router.post("/{portfolio_id}/duplicate")
def duplicate_portfolio(portfolio_id: str, body: DuplicatePortfolioBody, user_id: int = Depends(get_current_user)):
    result = svc.duplicate_portfolio(portfolio_id, body.name)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result.model_dump()


@router.get("/{portfolio_id}/transactions")
def get_transactions(
    portfolio_id: str,
    ticker: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="tx_type"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    user_id: int = Depends(get_current_user),
):
    result = svc.get_transactions(portfolio_id, ticker=ticker, tx_type=type, start=start, end=end)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return [tx.model_dump() for tx in result]


@router.post("/{portfolio_id}/transactions")
def add_transaction(portfolio_id: str, body: AddTransactionBody, user_id: int = Depends(get_current_user)):
    if not svc.add_transaction(portfolio_id, body.ticker, body.type, body.quantity):
        raise HTTPException(status_code=400, detail="Transaction failed")
    return {"message": "Transaction added"}


@router.put("/{portfolio_id}/transactions/{tx_id}")
def update_transaction(portfolio_id: str, tx_id: str, body: UpdateTransactionBody, user_id: int = Depends(get_current_user)):
    if not svc.update_transaction(portfolio_id, tx_id, price=body.price, quantity=body.quantity):
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"message": "Transaction updated"}


@router.delete("/{portfolio_id}/transactions/undo")
def undo_last_transaction(portfolio_id: str, user_id: int = Depends(get_current_user)):
    if not svc.undo_last_transaction(portfolio_id):
        raise HTTPException(status_code=400, detail="Nothing to undo")
    return {"message": "Last transaction undone"}


@router.get("/{portfolio_id}/valuation")
def portfolio_valuation(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.get_portfolio_valuation(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/diversification")
def portfolio_diversification(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.get_diversification(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/performers")
def portfolio_performers(portfolio_id: str, top_n: int = Query(default=5, ge=1, le=20), user_id: int = Depends(get_current_user)):
    result = svc.get_best_worst_performers(portfolio_id, top_n=top_n)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/history")
def portfolio_history(portfolio_id: str, period: str = Query(default="1mo", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = svc.get_portfolio_history(portfolio_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/returns")
def portfolio_returns(portfolio_id: str, period: str = Query(default="1mo", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = svc.get_returns(portfolio_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/risk")
def portfolio_risk(portfolio_id: str, period: str = Query(default="1y", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = svc.get_risk_metrics(portfolio_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/benchmark")
def portfolio_benchmark(portfolio_id: str, ticker: str = Query(default="XU100"), user_id: int = Depends(get_current_user)):
    result = svc.compare_with_benchmark(portfolio_id, benchmark_ticker=ticker)
    if result is None:
        return {}
    return result


@router.get("/{portfolio_id}/performance")
def portfolio_performance(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.analyze_portfolio_performance(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/stats")
def portfolio_stats(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.get_transaction_stats(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/snapshot")
def portfolio_snapshot(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.get_portfolio_snapshot(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/export/csv", response_class=PlainTextResponse)
def portfolio_export_csv(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = svc.export_portfolio_csv(portfolio_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return PlainTextResponse(result, media_type="text/csv")
