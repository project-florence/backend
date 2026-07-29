import uuid

from pydantic import BaseModel, Field
from datetime import datetime, timezone

from services.ticker import is_valid_ticker, get_current_price, get_price_history
from src.core.database import db


class Asset(BaseModel):
    ticker: str
    amount: float
    weighted_price: float
    total_transactions: int


class Metadata(BaseModel):
    id: str
    user_id: int
    name: str
    initial_balance: float
    balance: float
    created_at: datetime
    updated_at: datetime


class Transaction(BaseModel):
    id: str
    ticker: str
    type: str
    quantity: float
    price: float
    date: datetime


class Portfolio(BaseModel):
    metadata: Metadata
    transactions: list[Transaction] = Field(default_factory=list)


def save_portfolio(portfolio: Portfolio) -> bool:
    portfolio.metadata.updated_at = datetime.now(timezone.utc)
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO portfolio (portfolio_id, user_id, portfolio)
                VALUES (%s, %s, %s)
                ON CONFLICT (portfolio_id)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    portfolio = EXCLUDED.portfolio;
                """,
                (
                    portfolio.metadata.id,
                    portfolio.metadata.user_id,
                    portfolio.model_dump_json(),
                ),
            )
        return True
    except Exception:
        return False


def load_portfolio(portfolio_id: str) -> Portfolio | None:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT portfolio FROM portfolio WHERE portfolio_id = %s;",
                (portfolio_id,)
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            return Portfolio.model_validate_json(row[0])
    except Exception:
        return None


def list_portfolios(user_id: int) -> list[Portfolio]:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT portfolio FROM portfolio WHERE user_id = %s;",
                (user_id,)
            )
            rows = cur.fetchall()
            if not rows:
                return []
            return [Portfolio.model_validate_json(row[0]) for row in rows]
    except Exception:
        return []


def create_portfolio(user_id: int, name: str, initial_balance: float) -> Portfolio | None:
    now = datetime.now(timezone.utc)
    metadata = Metadata(
        id=f"port-{uuid.uuid4()}",
        user_id=user_id,
        name=name,
        initial_balance=initial_balance,
        balance=initial_balance,
        created_at=now,
        updated_at=now,
    )
    portfolio = Portfolio(metadata=metadata, transactions=[])
    if not save_portfolio(portfolio):
        return None
    return portfolio


def calculate_assets(portfolio: Portfolio) -> dict[str, Asset]:
    assets: dict[str, Asset] = {}

    if not portfolio.transactions:
        return assets

    for tx in portfolio.transactions:
        if tx.ticker not in assets:
            assets[tx.ticker] = Asset(
                ticker=tx.ticker,
                amount=0.0,
                weighted_price=0.0,
                total_transactions=0,
            )

        asset = assets[tx.ticker]
        asset.total_transactions += 1

        if tx.type == "BUY":
            total_cost = (asset.amount * asset.weighted_price) + (tx.quantity * tx.price)
            asset.amount += tx.quantity
            asset.weighted_price = total_cost / asset.amount if asset.amount > 0 else 0.0

        elif tx.type == "SELL":
            asset.amount -= tx.quantity

    return {ticker: asset for ticker, asset in assets.items() if asset.amount > 0}


def _add_transaction(portfolio: Portfolio, ticker: str, _type: str, quantity: float, price: float) -> None:
    tx = Transaction(
        id=f"tx-{uuid.uuid4()}",
        ticker=ticker,
        type=_type,
        quantity=quantity,
        price=price,
        date=datetime.now(timezone.utc),
    )
    portfolio.transactions.append(tx)


def add_transaction(portfolio_id: str, ticker: str, _type: str, quantity: float) -> bool:
    if quantity <= 0:
        return False

    if not is_valid_ticker(ticker):
        return False

    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return False

    assets = calculate_assets(portfolio)

    price = get_current_price(ticker)
    if price is None:
        return False

    cost = price * quantity

    if _type == "BUY":
        if cost > portfolio.metadata.balance:
            return False
        portfolio.metadata.balance -= cost
        _add_transaction(portfolio, ticker, _type, quantity, price)

    elif _type == "SELL":
        if ticker not in assets or assets[ticker].amount < quantity:
            return False
        portfolio.metadata.balance += cost
        _add_transaction(portfolio, ticker, _type, quantity, price)

    else:
        return False

    return save_portfolio(portfolio)


def get_portfolio_valuation(portfolio_id: str) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    assets = calculate_assets(portfolio)

    holdings_value = 0.0
    asset_details = []

    for ticker, asset in assets.items():
        current_price = get_current_price(ticker)
        value = (current_price * asset.amount) if current_price is not None else None

        if value is not None:
            holdings_value += value

        asset_details.append({
            "ticker": ticker,
            "amount": asset.amount,
            "current_price": current_price,
            "total_value": value,
            "weighted_avg_cost": asset.weighted_price,
            "unrealized_pnl": value - (asset.weighted_price * asset.amount) if value is not None and asset.weighted_price > 0 else None,
        })

    total_value = portfolio.metadata.balance + holdings_value
    total_pnl = total_value - portfolio.metadata.initial_balance
    pnl_pct = (total_pnl / portfolio.metadata.initial_balance * 100) if portfolio.metadata.initial_balance > 0 else None

    return {
        "total_value": total_value,
        "cash_balance": portfolio.metadata.balance,
        "holdings_value": holdings_value,
        "total_pnl": total_pnl,
        "pnl_percentage": pnl_pct,
        "assets": asset_details,
    }


def analyze_portfolio_performance(portfolio_id: str) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    if not portfolio.transactions:
        return {
            "overall": None,
            "assets": [],
        }

    traded_tickers = set(tx.ticker for tx in portfolio.transactions)
    asset_analyses = []

    for ticker in traded_tickers:
        ticker_txs = [tx for tx in portfolio.transactions if tx.ticker == ticker]
        tx_dates = [tx.date for tx in ticker_txs]
        date_min = min(tx_dates)
        date_max = max(tx_dates)

        actual_pnl = sum(
            -tx.quantity * tx.price if tx.type == "BUY" else tx.quantity * tx.price
            for tx in ticker_txs
        )

        price_history = get_price_history(ticker, start=date_min, end=date_max)

        optimal_points = None
        efficiency_score = None
        optimal_pnl = None

        if price_history:
            prices = [p["price"] if "price" in p else p["close"] for p in price_history]
            if prices:
                min_price = min(prices)
                max_price = max(prices)

                min_row = next((p for p in price_history if (p.get("price") or p.get("close")) == min_price), None)
                max_row = next((p for p in price_history if (p.get("price") or p.get("close")) == max_price), None)

                best_buy_date = min_row.get("ts") if min_row else None
                best_sell_date = max_row.get("ts") if max_row else None

                optimal_points = {
                    "best_buy": {"date": best_buy_date, "price": min_price},
                    "best_sell": {"date": best_sell_date, "price": max_price},
                }

                total_buy_qty = sum(tx.quantity for tx in ticker_txs if tx.type == "BUY")
                total_sell_qty = sum(tx.quantity for tx in ticker_txs if tx.type == "SELL")

                optimal_pnl = (max_price * total_sell_qty) - (min_price * total_buy_qty)

                if optimal_pnl != 0:
                    efficiency_score = min(actual_pnl / optimal_pnl, 1.0) if actual_pnl >= 0 else 0.0

        asset_analyses.append({
            "ticker": ticker,
            "efficiency_score": efficiency_score,
            "actual_trades": [
                {
                    "date": tx.date.isoformat(),
                    "type": tx.type,
                    "price": tx.price,
                    "quantity": tx.quantity,
                }
                for tx in ticker_txs
            ],
            "optimal_points": optimal_points,
            "price_history": [
                {"ts": p["ts"], "close": p.get("price") or p.get("close")}
                for p in price_history
            ] if price_history else None,
            "actual_pnl": actual_pnl,
            "optimal_pnl": optimal_pnl,
        })

    scores = [a["efficiency_score"] for a in asset_analyses if a["efficiency_score"] is not None]
    total_actual = sum(a["actual_pnl"] for a in asset_analyses)
    total_optimal = sum(a["optimal_pnl"] for a in asset_analyses if a["optimal_pnl"] is not None)

    return {
        "overall": {
            "efficiency_score": sum(scores) / len(scores) if scores else None,
            "actual_pnl": total_actual,
            "optimal_pnl": total_optimal,
        },
        "assets": asset_analyses,
    }
