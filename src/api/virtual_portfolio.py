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
async def create_portfolio(body: CreatePortfolioBody, user_id: int = Depends(get_current_user)):
    result = await svc.create_portfolio(user_id, body.name, body.initial_balance)
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to create portfolio")
    return result.model_dump()


@router.get("")
async def list_portfolios(user_id: int = Depends(get_current_user)):
    result = await svc.list_portfolios(user_id)
    return [p.model_dump() for p in result]


@router.get("/{portfolio_id}")
async def get_portfolio(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.load_portfolio(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result.model_dump()


@router.put("/{portfolio_id}")
async def rename_portfolio(portfolio_id: str, body: RenamePortfolioBody, user_id: int = Depends(get_current_user)):
    if not await svc.rename_portfolio(portfolio_id, user_id, body.name):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"message": "Portfolio renamed"}


@router.delete("/{portfolio_id}")
async def delete_portfolio(portfolio_id: str, user_id: int = Depends(get_current_user)):
    if not await svc.delete_portfolio(portfolio_id, user_id):
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return {"message": "Portfolio deleted"}


@router.post("/{portfolio_id}/duplicate")
async def duplicate_portfolio(portfolio_id: str, body: DuplicatePortfolioBody, user_id: int = Depends(get_current_user)):
    result = await svc.duplicate_portfolio(portfolio_id, user_id, body.name)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result.model_dump()


@router.get("/{portfolio_id}/transactions")
async def get_transactions(
    portfolio_id: str,
    ticker: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="tx_type"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    user_id: int = Depends(get_current_user),
):
    result = await svc.get_transactions(portfolio_id, user_id, ticker=ticker, tx_type=type, start=start, end=end)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return [tx.model_dump() for tx in result]


@router.post("/{portfolio_id}/transactions")
async def add_transaction(portfolio_id: str, body: AddTransactionBody, user_id: int = Depends(get_current_user)):
    if not await svc.add_transaction(portfolio_id, user_id, body.ticker, body.type, body.quantity):
        raise HTTPException(status_code=400, detail="Transaction failed")
    return {"message": "Transaction added"}


@router.put("/{portfolio_id}/transactions/{tx_id}")
async def update_transaction(portfolio_id: str, tx_id: str, body: UpdateTransactionBody, user_id: int = Depends(get_current_user)):
    if not await svc.update_transaction(portfolio_id, user_id, tx_id, price=body.price, quantity=body.quantity):
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"message": "Transaction updated"}


@router.delete("/{portfolio_id}/transactions/undo")
async def undo_last_transaction(portfolio_id: str, user_id: int = Depends(get_current_user)):
    if not await svc.undo_last_transaction(portfolio_id, user_id):
        raise HTTPException(status_code=400, detail="Nothing to undo")
    return {"message": "Last transaction undone"}


@router.get("/{portfolio_id}/valuation")
async def portfolio_valuation(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.get_portfolio_valuation(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/diversification")
async def portfolio_diversification(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.get_diversification(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/performers")
async def portfolio_performers(portfolio_id: str, top_n: int = Query(default=5, ge=1, le=20), user_id: int = Depends(get_current_user)):
    result = await svc.get_best_worst_performers(portfolio_id, user_id, top_n=top_n)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/history")
async def portfolio_history(portfolio_id: str, period: str = Query(default="1mo", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = await svc.get_portfolio_history(portfolio_id, user_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/returns")
async def portfolio_returns(portfolio_id: str, period: str = Query(default="1mo", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = await svc.get_returns(portfolio_id, user_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/risk")
async def portfolio_risk(portfolio_id: str, period: str = Query(default="1y", pattern="^(1w|1mo|3mo|6mo|1y|max)$"), user_id: int = Depends(get_current_user)):
    result = await svc.get_risk_metrics(portfolio_id, user_id, period=period)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/benchmark")
async def portfolio_benchmark(portfolio_id: str, ticker: str = Query(default="XU100"), user_id: int = Depends(get_current_user)):
    result = await svc.compare_with_benchmark(portfolio_id, user_id, benchmark_ticker=ticker)
    if result is None:
        return {}
    return result


@router.get("/{portfolio_id}/performance")
async def portfolio_performance(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.analyze_portfolio_performance(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/stats")
async def portfolio_stats(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.get_transaction_stats(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/snapshot")
async def portfolio_snapshot(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.get_portfolio_snapshot(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return result


@router.get("/{portfolio_id}/export/csv", response_class=PlainTextResponse)
async def portfolio_export_csv(portfolio_id: str, user_id: int = Depends(get_current_user)):
    result = await svc.export_portfolio_csv(portfolio_id, user_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return PlainTextResponse(result, media_type="text/csv")
