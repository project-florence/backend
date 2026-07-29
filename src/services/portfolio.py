import uuid
import csv
import io
import math

from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone

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


def delete_portfolio(portfolio_id: str) -> bool:
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM portfolio WHERE portfolio_id = %s", (portfolio_id,))
            return cur.rowcount > 0
    except Exception:
        return False


def rename_portfolio(portfolio_id: str, new_name: str) -> bool:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return False
    portfolio.metadata.name = new_name
    return save_portfolio(portfolio)


def get_portfolio_by_name(user_id: int, name: str) -> Portfolio | None:
    portfolios = list_portfolios(user_id)
    for p in portfolios:
        if p.metadata.name.lower() == name.lower():
            return p
    return None


def duplicate_portfolio(portfolio_id: str, new_name: str) -> Portfolio | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None
    now = datetime.now(timezone.utc)
    metadata = Metadata(
        id=f"port-{uuid.uuid4()}",
        user_id=portfolio.metadata.user_id,
        name=new_name,
        initial_balance=portfolio.metadata.initial_balance,
        balance=portfolio.metadata.balance,
        created_at=now,
        updated_at=now,
    )
    new_portfolio = Portfolio(
        metadata=metadata,
        transactions=[tx.model_copy(deep=True) for tx in portfolio.transactions],
    )
    if not save_portfolio(new_portfolio):
        return None
    return new_portfolio


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


def get_transactions(
    portfolio_id: str,
    ticker: str | None = None,
    tx_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Transaction] | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    txs = portfolio.transactions
    if ticker:
        txs = [tx for tx in txs if tx.ticker.upper() == ticker.upper()]
    if tx_type:
        txs = [tx for tx in txs if tx.type.upper() == tx_type.upper()]
    if start:
        txs = [tx for tx in txs if tx.date >= start]
    if end:
        txs = [tx for tx in txs if tx.date <= end]

    return sorted(txs, key=lambda tx: tx.date)


def undo_last_transaction(portfolio_id: str) -> bool:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None or not portfolio.transactions:
        return False

    last_tx = portfolio.transactions.pop()
    if last_tx.type == "BUY":
        portfolio.metadata.balance += last_tx.quantity * last_tx.price
    elif last_tx.type == "SELL":
        portfolio.metadata.balance -= last_tx.quantity * last_tx.price

    return save_portfolio(portfolio)


def _recalculate_portfolio(portfolio: Portfolio) -> None:
    balance = portfolio.metadata.initial_balance
    for tx in portfolio.transactions:
        cost = tx.quantity * tx.price
        if tx.type == "BUY":
            balance -= cost
        elif tx.type == "SELL":
            balance += cost
    portfolio.metadata.balance = balance


def update_transaction(portfolio_id: str, tx_id: str, price: float | None = None, quantity: float | None = None) -> bool:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return False

    for tx in portfolio.transactions:
        if tx.id == tx_id:
            if price is not None:
                tx.price = price
            if quantity is not None:
                tx.quantity = quantity
            _recalculate_portfolio(portfolio)
            return save_portfolio(portfolio)

    return False


def get_transaction_stats(portfolio_id: str) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    txs = portfolio.transactions
    if not txs:
        return {
            "total_transactions": 0,
            "total_buys": 0,
            "total_sells": 0,
            "total_buy_volume": 0.0,
            "total_sell_volume": 0.0,
            "avg_transaction_size": 0.0,
            "unique_tickers": 0,
        }

    buys = [tx for tx in txs if tx.type == "BUY"]
    sells = [tx for tx in txs if tx.type == "SELL"]

    buy_volume = sum(tx.quantity * tx.price for tx in buys)
    sell_volume = sum(tx.quantity * tx.price for tx in sells)

    return {
        "total_transactions": len(txs),
        "total_buys": len(buys),
        "total_sells": len(sells),
        "total_buy_volume": buy_volume,
        "total_sell_volume": sell_volume,
        "avg_transaction_size": (buy_volume + sell_volume) / len(txs) if txs else 0.0,
        "unique_tickers": len(set(tx.ticker for tx in txs)),
    }


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

        total_cost = asset.weighted_price * asset.amount
        unrealized_pnl = value - total_cost if value is not None and total_cost > 0 else None

        asset_details.append({
            "ticker": ticker,
            "amount": asset.amount,
            "current_price": current_price,
            "total_value": value,
            "total_cost": total_cost if total_cost > 0 else None,
            "weighted_avg_cost": asset.weighted_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": (unrealized_pnl / total_cost * 100) if unrealized_pnl is not None and total_cost > 0 else None,
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


def get_diversification(portfolio_id: str) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    assets = calculate_assets(portfolio)
    if not assets:
        return {"total_value": portfolio.metadata.balance, "assets": []}

    total_holdings_value = 0.0
    asset_details = []

    for ticker, asset in assets.items():
        current_price = get_current_price(ticker)
        value = (current_price * asset.amount) if current_price is not None else None
        if value is not None:
            total_holdings_value += value

        ticker_upper = ticker.upper()
        from services.economy import get_currency
        currency_data = get_currency()
        is_forex = isinstance(currency_data, dict) and ticker_upper in currency_data
        from services.ticker import PRECIOUS_METAL_KEYS
        is_metal = ticker.lower() in PRECIOUS_METAL_KEYS
        asset_type = "metal" if is_metal else "forex" if is_forex else "stock"

        asset_details.append({
            "ticker": ticker,
            "amount": asset.amount,
            "value": value,
            "type": asset_type,
        })

    total_value = portfolio.metadata.balance + total_holdings_value

    for a in asset_details:
        a["allocation_pct"] = (a["value"] / total_value * 100) if a["value"] is not None and total_value > 0 else 0.0

    by_type: dict[str, float] = {}
    for a in asset_details:
        if a["value"] is not None:
            by_type[a["type"]] = by_type.get(a["type"], 0.0) + a["value"]

    return {
        "total_value": total_value,
        "cash_balance": portfolio.metadata.balance,
        "cash_allocation_pct": (portfolio.metadata.balance / total_value * 100) if total_value > 0 else 0.0,
        "assets": asset_details,
        "allocation_by_type": by_type,
    }


def get_best_worst_performers(portfolio_id: str, top_n: int = 5) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    assets = calculate_assets(portfolio)
    if not assets:
        return {"best": [], "worst": []}

    performers = []
    for ticker, asset in assets.items():
        current_price = get_current_price(ticker)
        if current_price is not None and asset.weighted_price > 0:
            total_cost = asset.weighted_price * asset.amount
            current_value = current_price * asset.amount
            pnl = current_value - total_cost
            pnl_pct = (pnl / total_cost * 100)
            performers.append({
                "ticker": ticker,
                "amount": asset.amount,
                "pnl": pnl,
                "pnl_percentage": pnl_pct,
            })

    performers.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "best": performers[:top_n],
        "worst": performers[-top_n:][::-1] if performers else [],
    }


def _compute_portfolio_value_at(
    initial_balance: float,
    transactions: list[Transaction],
    date: datetime,
) -> tuple[float, float]:
    balance = initial_balance
    txs_up_to = [tx for tx in transactions if tx.date <= date]
    for tx in txs_up_to:
        cost = tx.quantity * tx.price
        if tx.type == "BUY":
            balance -= cost
        elif tx.type == "SELL":
            balance += cost

    temp = Portfolio(metadata=Metadata(
        id="", user_id=0, name="", initial_balance=initial_balance,
        balance=balance, created_at=date, updated_at=date,
    ), transactions=txs_up_to)
    assets = calculate_assets(temp)

    holdings_value = 0.0
    for ticker, asset in assets.items():
        hist = get_price_history(ticker, start=date - timedelta(days=5), end=date + timedelta(days=1))
        if hist:
            closest = min(hist, key=lambda p: abs(
                datetime.fromisoformat(p["ts"]).replace(tzinfo=timezone.utc) - date
            ))
            price = closest.get("price") or closest.get("close")
            if price is not None:
                holdings_value += price * asset.amount

    return balance, holdings_value


def get_portfolio_history(portfolio_id: str, period: str = "1mo") -> list[dict] | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    now = datetime.now(timezone.utc)
    period_map = {"1w": 7, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "max": None}
    days = period_map.get(period)
    if days is None:
        start = portfolio.metadata.created_at
    else:
        start = now - timedelta(days=days)

    dates = {portfolio.metadata.created_at, now}
    for tx in portfolio.transactions:
        if start <= tx.date <= now:
            dates.add(tx.date)

    dates = sorted(d for d in dates if d >= start)

    result = []
    for date in dates:
        cash, holdings = _compute_portfolio_value_at(
            portfolio.metadata.initial_balance, portfolio.transactions, date,
        )
        total = cash + holdings
        result.append({
            "ts": date.isoformat(),
            "total_value": total,
            "cash_balance": cash,
            "holdings_value": holdings,
        })

    return result


def get_returns(portfolio_id: str, period: str = "1mo") -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    history = get_portfolio_history(portfolio_id, period)
    if not history or len(history) < 1:
        return None

    start_value = history[0]["total_value"]
    end_value = history[-1]["total_value"]
    absolute_return = end_value - portfolio.metadata.initial_balance
    total_return_pct = ((end_value - portfolio.metadata.initial_balance) / portfolio.metadata.initial_balance * 100) if portfolio.metadata.initial_balance > 0 else None

    first_date = datetime.fromisoformat(history[0]["ts"]).replace(tzinfo=timezone.utc)
    last_date = datetime.fromisoformat(history[-1]["ts"]).replace(tzinfo=timezone.utc)
    years = (last_date - first_date).total_seconds() / (365.25 * 86400)

    cagr = ((end_value / start_value) ** (1 / years) - 1) * 100 if years > 0 and start_value > 0 else None

    return {
        "period": period,
        "start_value": start_value,
        "end_value": end_value,
        "absolute_return": absolute_return,
        "total_return_percentage": total_return_pct,
        "cagr_percentage": cagr,
    }


def _daily_returns(values: list[float]) -> list[float]:
    if len(values) < 2:
        return []
    return [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values)) if values[i - 1] > 0]


def get_risk_metrics(portfolio_id: str, period: str = "1y") -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    history = get_portfolio_history(portfolio_id, period)
    if not history or len(history) < 3:
        return {"volatility": None, "max_drawdown": None, "sharpe_ratio": None}

    values = [h["total_value"] for h in history]
    returns = _daily_returns(values)

    volatility = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5 * 100 if returns else None

    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    avg_return = sum(returns) / len(returns) if returns else 0
    risk_free = 0.0
    sharpe = ((avg_return - risk_free) / (volatility / 100)) if volatility and volatility > 0 else None

    return {
        "volatility": volatility,
        "max_drawdown": max_dd,
        "sharpe_ratio": sharpe,
    }


def compare_with_benchmark(portfolio_id: str, benchmark_ticker: str = "XU100") -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    history = get_portfolio_history(portfolio_id, "max")
    if not history or len(history) < 2:
        return None

    first_date = datetime.fromisoformat(history[0]["ts"]).replace(tzinfo=timezone.utc)
    last_date = datetime.fromisoformat(history[-1]["ts"]).replace(tzinfo=timezone.utc)

    bench_history = get_price_history(benchmark_ticker, start=first_date, end=last_date)
    if not bench_history:
        return None

    portfolio_start = history[0]["total_value"]
    portfolio_end = history[-1]["total_value"]
    portfolio_return_pct = ((portfolio_end - portfolio_start) / portfolio_start * 100) if portfolio_start > 0 else 0

    bench_prices = [p.get("price") or p.get("close") for p in bench_history if p.get("price") or p.get("close")]
    if len(bench_prices) < 2:
        return None

    bench_start = bench_prices[0]
    bench_end = bench_prices[-1]
    bench_return_pct = ((bench_end - bench_start) / bench_start * 100) if bench_start > 0 else 0

    return {
        "portfolio_return_pct": portfolio_return_pct,
        "benchmark_ticker": benchmark_ticker,
        "benchmark_return_pct": bench_return_pct,
        "difference_pct": portfolio_return_pct - bench_return_pct,
        "outperformed": portfolio_return_pct > bench_return_pct,
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


def get_portfolio_snapshot(portfolio_id: str) -> dict | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    valuation = get_portfolio_valuation(portfolio_id)
    diversification = get_diversification(portfolio_id)
    performers = get_best_worst_performers(portfolio_id)
    tx_stats = get_transaction_stats(portfolio_id)
    recent_txs = get_transactions(portfolio_id)
    recent_txs = recent_txs[-5:] if recent_txs and len(recent_txs) > 5 else recent_txs

    return {
        "portfolio": {
            "id": portfolio.metadata.id,
            "name": portfolio.metadata.name,
            "created_at": portfolio.metadata.created_at.isoformat(),
            "updated_at": portfolio.metadata.updated_at.isoformat(),
        },
        "valuation": valuation,
        "diversification": diversification,
        "performers": performers,
        "transaction_stats": tx_stats,
        "recent_transactions": [
            {"id": tx.id, "ticker": tx.ticker, "type": tx.type, "quantity": tx.quantity, "price": tx.price, "date": tx.date.isoformat()}
            for tx in (recent_txs or [])
        ],
    }


def export_portfolio_csv(portfolio_id: str) -> str | None:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return None

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "ticker", "type", "quantity", "price", "total"])

    for tx in sorted(portfolio.transactions, key=lambda t: t.date):
        writer.writerow([
            tx.date.isoformat(),
            tx.ticker,
            tx.type,
            tx.quantity,
            tx.price,
            tx.quantity * tx.price,
        ])

    return output.getvalue()


def import_transactions_csv(portfolio_id: str, csv_content: str) -> dict:
    portfolio = load_portfolio(portfolio_id)
    if portfolio is None:
        return {"success": False, "message": "Portfolio not found", "imported": 0, "failed": 0}

    reader = csv.DictReader(io.StringIO(csv_content))
    imported = 0
    failed = 0
    errors = []

    for row in reader:
        try:
            ticker = row.get("ticker", "").strip().upper()
            if not ticker or not is_valid_ticker(ticker):
                failed += 1
                errors.append(f"Invalid ticker: {ticker}")
                continue

            tx_type = row.get("type", "").strip().upper()
            if tx_type not in ("BUY", "SELL"):
                failed += 1
                errors.append(f"Invalid type: {tx_type}")
                continue

            quantity = float(row.get("quantity", 0))
            if quantity <= 0:
                failed += 1
                errors.append(f"Invalid quantity: {quantity}")
                continue

            price = float(row.get("price", 0))
            if price <= 0:
                failed += 1
                errors.append(f"Invalid price: {price}")
                continue

            try:
                tx_date = datetime.fromisoformat(row.get("date", ""))
            except (ValueError, TypeError):
                tx_date = datetime.now(timezone.utc)

            tx = Transaction(
                id=f"tx-{uuid.uuid4()}",
                ticker=ticker,
                type=tx_type,
                quantity=quantity,
                price=price,
                date=tx_date,
            )
            portfolio.transactions.append(tx)
            imported += 1

        except (ValueError, KeyError) as e:
            failed += 1
            errors.append(str(e))

    if imported > 0:
        _recalculate_portfolio(portfolio)
        save_portfolio(portfolio)

    return {
        "success": failed == 0,
        "message": f"Imported {imported}, failed {failed}",
        "imported": imported,
        "failed": failed,
        "errors": errors[:10],
    }
